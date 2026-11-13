"""SQLite log of every System 2 (futures_pattern_signal.py) signal, resolved
later against real Binance FUTURES price action -- mirrors outcomes.py's
approach for system 1, but a separate DB/table since system 2 is a fully
independent engine (see futures_pattern_signal.py's module docstring).

Logging happens from futures_pattern_alert.py's "is this a NEW signal" check,
not from every manual /futures_<x> call -- exactly one row per actual
occurrence, same cadence as the Telegram alert itself.

Unlike outcomes.py, resolution fetches a small futures klines window live
per prediction instead of maintaining a local rolling candle cache -- system
2 signals are rare enough (a handful a day at most) that this is cheap, and
it avoids the "cache rolled past the window" unresolvable case entirely.
"""

import json
import os
import sqlite3
import time
import urllib.request

DB_PATH = os.path.join(os.path.dirname(__file__), "pattern_predictions.db")
FUTURES_API = "https://fapi.binance.com"

HORIZONS = {
    "2h": 2 * 3600, "4h": 4 * 3600, "6h": 6 * 3600, "8h": 8 * 3600,
    "10h": 10 * 3600, "12h": 12 * 3600, "1d": 24 * 3600,
}
CANDLE_TOLERANCE_SECONDS = 1800 + 1800
RISE_THRESHOLD_PCT = 2.0  # same as outcomes.py's 1d "hit" bar

SCHEMA = """
CREATE TABLE IF NOT EXISTS pattern_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    snapshot_time REAL NOT NULL,
    close_price REAL NOT NULL,
    direction TEXT NOT NULL,
    interval TEXT NOT NULL,
    UNIQUE(symbol, snapshot_time)
);
CREATE TABLE IF NOT EXISTS pattern_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER NOT NULL REFERENCES pattern_predictions(id),
    horizon TEXT NOT NULL,
    outcome_time REAL NOT NULL,
    outcome_close REAL NOT NULL,
    return_pct REAL NOT NULL,
    hit INTEGER NOT NULL,
    UNIQUE(prediction_id, horizon)
);
"""


def get_connection(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def record_signal(symbol, direction, close_price, interval, snapshot_time=None, conn=None):
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    conn.execute(
        """INSERT OR IGNORE INTO pattern_predictions
           (symbol, snapshot_time, close_price, direction, interval)
           VALUES (?, ?, ?, ?, ?)""",
        (symbol, snapshot_time or time.time(), close_price, direction, interval),
    )
    conn.commit()
    if owns_conn:
        conn.close()


def _closest_futures_candle(symbol, target_time, tolerance_seconds=CANDLE_TOLERANCE_SECONDS):
    start_ms = int((target_time - tolerance_seconds) * 1000)
    end_ms = int((target_time + tolerance_seconds) * 1000)
    url = (f"{FUTURES_API}/fapi/v1/klines?symbol={symbol}&interval=1h"
           f"&startTime={start_ms}&endTime={end_ms}&limit=10")
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            raw = json.loads(resp.read())
    except Exception:
        return None
    if not raw:
        return None
    candles = [{"open_time": k[0] / 1000, "close": float(k[4])} for k in raw]
    best = min(candles, key=lambda c: abs(c["open_time"] - target_time))
    if abs(best["open_time"] - target_time) > tolerance_seconds:
        return None
    return best


def _hit(horizon, direction, return_pct):
    """Direction-aware unlike outcomes.py's system 1 version (which only
    ever checks "did price rise", since GREEN/RED there are both judged
    against the same rise-based ml_model training target) -- a LONG call
    hits on a rise, a SHORT call hits on a fall."""
    signed_return = return_pct if direction == "LONG" else -return_pct
    if horizon == "1d":
        return 1 if signed_return >= RISE_THRESHOLD_PCT else 0
    return 1 if signed_return > 0 else 0


def resolve_matured(conn=None):
    """Fill in outcomes for every (signal, horizon) pair whose time has come.
    Returns count resolved."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()

    now = time.time()
    resolved = 0
    for horizon, horizon_seconds in HORIZONS.items():
        cutoff = now - horizon_seconds
        rows = conn.execute(
            """SELECT p.id, p.symbol, p.snapshot_time, p.close_price, p.direction
               FROM pattern_predictions p
               LEFT JOIN pattern_outcomes o ON o.prediction_id = p.id AND o.horizon = ?
               WHERE p.snapshot_time <= ? AND o.id IS NULL""",
            (horizon, cutoff),
        ).fetchall()

        for pred_id, symbol, snapshot_time, close_price, direction in rows:
            candle = _closest_futures_candle(symbol, snapshot_time + horizon_seconds)
            if candle is None:
                continue  # not resolvable yet/anymore -- try again next run
            return_pct = (candle["close"] / close_price - 1) * 100
            conn.execute(
                """INSERT OR IGNORE INTO pattern_outcomes
                   (prediction_id, horizon, outcome_time, outcome_close, return_pct, hit)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (pred_id, horizon, candle["open_time"], candle["close"],
                 return_pct, _hit(horizon, direction, return_pct)),
            )
            resolved += 1

    conn.commit()
    if owns_conn:
        conn.close()
    return resolved


if __name__ == "__main__":
    conn = get_connection()
    resolved = resolve_matured(conn=conn)
    print(f"Resolved {resolved} newly matured signal/horizon pairs this run.\n")

    for horizon in HORIZONS:
        rows = conn.execute(
            """SELECT o.return_pct, o.hit FROM pattern_outcomes o
               JOIN pattern_predictions p ON p.id = o.prediction_id
               WHERE o.horizon = ?""",
            (horizon,),
        ).fetchall()
        if not rows:
            print(f"{horizon}: no resolved signals yet")
            continue
        hit_rate = sum(h for _, h in rows) / len(rows)
        avg_return = sum(r for r, _ in rows) / len(rows)
        print(f"{horizon}: n={len(rows)} hit_rate={hit_rate:.1%} avg_return={avg_return:+.2f}%")
    conn.close()
