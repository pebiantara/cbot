"""SQLite log of every combine.py snapshot, later joined against the real
1h/4h/1d candles once that much time has actually passed -- lets you check each
morning "did the GREEN/YELLOW/RED calls from earlier runs turn out to be right?"
instead of only trusting ml_model.py's train/test split on historical data.
"""

import csv
import os
import sqlite3
import time

DB_PATH = os.path.join(os.path.dirname(__file__), "predictions.db")
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# horizon name -> seconds ahead. 2-hour checkpoints through the day (so a
# morning run gets checked roughly every 2h) plus a final 1d mark. All horizons
# resolve against the 1h candle file (fine-grained and always closed), NOT the
# 4h/1d files -- those are anchored to calendar boundaries (UTC midnight for
# 1d), so the most recent row in a freshly-refreshed 4h/1d file is often
# still-forming and its close ~= the live price, which silently produced ~0%
# "returns" for every coin before this was caught.
HORIZONS = {
    "2h": 2 * 3600, "4h": 4 * 3600, "6h": 6 * 3600, "8h": 8 * 3600,
    "10h": 10 * 3600, "12h": 12 * 3600, "1d": 24 * 3600,
}
CANDLE_FILE_SUFFIX = "1h"
# must be >= half the 1h candle period (1800s) or a target landing between two
# candles can be farther from its nearest neighbor than the tolerance allows;
# +1800s buffer for cron-run drift.
CANDLE_TOLERANCE_SECONDS = 1800 + 1800
RISE_THRESHOLD_PCT = 2.0  # matches ml_model.py's LABEL_RISE_PCT, used only for the 1d "hit" flag

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    snapshot_time REAL NOT NULL,
    close_price REAL NOT NULL,
    technical_score REAL NOT NULL,
    ml_score REAL NOT NULL,
    whale_score REAL NOT NULL,
    final_score REAL NOT NULL,
    label TEXT NOT NULL,
    direction TEXT,
    entry_low REAL,
    entry_high REAL,
    stop_loss REAL,
    take_profit REAL,
    UNIQUE(symbol, snapshot_time)
);
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER NOT NULL REFERENCES predictions(id),
    horizon TEXT NOT NULL,
    outcome_time REAL NOT NULL,
    outcome_close REAL NOT NULL,
    return_pct REAL NOT NULL,
    hit INTEGER NOT NULL,
    UNIQUE(prediction_id, horizon)
);
"""

# added after the table already existed in some running predictions.db files --
# CREATE TABLE IF NOT EXISTS won't retrofit new columns onto an existing table.
_MIGRATION_COLUMNS = [
    ("direction", "TEXT"), ("entry_low", "REAL"), ("entry_high", "REAL"),
    ("stop_loss", "REAL"), ("take_profit", "REAL"),
]


def get_connection(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    for column, col_type in _MIGRATION_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {column} {col_type}")
        except sqlite3.OperationalError:
            pass  # column already exists
    return conn


def record_snapshot(rows, conn=None, snapshot_time=None):
    """rows: combine.py's per-coin dicts (must include "close"). One row per coin per run."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    snapshot_time = snapshot_time or time.time()
    for row in rows:
        entry = row.get("entry") or {}
        conn.execute(
            """INSERT OR IGNORE INTO predictions
               (symbol, snapshot_time, close_price, technical_score, ml_score,
                whale_score, final_score, label, direction, entry_low,
                entry_high, stop_loss, take_profit)
               VALUES (:symbol, :snapshot_time, :close_price, :technical_score,
                       :ml_score, :whale_score, :final_score, :label, :direction,
                       :entry_low, :entry_high, :stop_loss, :take_profit)""",
            {
                "symbol": row["symbol"],
                "snapshot_time": snapshot_time,
                "close_price": row["close"],
                "technical_score": row["technical_score"],
                "ml_score": row["ml_score"],
                "whale_score": row["whale_score"],
                "final_score": row["final_score"],
                "label": row["label"],
                "direction": row.get("direction"),
                "entry_low": entry.get("entry_low"),
                "entry_high": entry.get("entry_high"),
                "stop_loss": entry.get("stop_loss"),
                "take_profit": entry.get("take_profit"),
            },
        )
    conn.commit()
    if owns_conn:
        conn.close()


def _load_candles(symbol, timeframe_suffix):
    path = os.path.join(DATA_DIR, f"{symbol}_{timeframe_suffix}.csv")
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return [
            {"open_time": int(row["open_time"]) / 1000, "close": float(row["close"])}
            for row in csv.DictReader(f)
        ]


def _closest_candle(candles, target_time, tolerance_seconds):
    if not candles:
        return None
    best = min(candles, key=lambda c: abs(c["open_time"] - target_time))
    if abs(best["open_time"] - target_time) > tolerance_seconds:
        return None
    return best


def _hit(horizon, return_pct):
    # 1h/4h: did price simply move up at all; 1d: matches ml_model's training
    # target (rise >= RISE_THRESHOLD_PCT), so ml_score accuracy can be checked.
    if horizon == "1d":
        return 1 if return_pct >= RISE_THRESHOLD_PCT else 0
    return 1 if return_pct > 0 else 0


def resolve_matured(conn=None):
    """Fill in outcomes for every (prediction, horizon) pair whose time has come
    and that the current candle cache still covers. Returns count resolved."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()

    now = time.time()
    resolved = 0
    candle_cache = {}

    for horizon, horizon_seconds in HORIZONS.items():
        cutoff = now - horizon_seconds
        rows = conn.execute(
            """SELECT p.id, p.symbol, p.snapshot_time, p.close_price
               FROM predictions p
               LEFT JOIN outcomes o ON o.prediction_id = p.id AND o.horizon = ?
               WHERE p.snapshot_time <= ? AND o.id IS NULL""",
            (horizon, cutoff),
        ).fetchall()

        for pred_id, symbol, snapshot_time, close_price in rows:
            if symbol not in candle_cache:
                candle_cache[symbol] = _load_candles(symbol, CANDLE_FILE_SUFFIX)
            candle = _closest_candle(
                candle_cache[symbol], snapshot_time + horizon_seconds, CANDLE_TOLERANCE_SECONDS
            )
            if candle is None:
                continue  # candle cache has rolled past this window -- unresolvable now

            return_pct = (candle["close"] / close_price - 1) * 100
            conn.execute(
                """INSERT OR IGNORE INTO outcomes
                   (prediction_id, horizon, outcome_time, outcome_close, return_pct, hit)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (pred_id, horizon, candle["open_time"], candle["close"],
                 return_pct, _hit(horizon, return_pct)),
            )
            resolved += 1

    conn.commit()
    if owns_conn:
        conn.close()
    return resolved
