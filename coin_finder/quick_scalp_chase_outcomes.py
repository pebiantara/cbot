"""SQLite log of every quick-scalp CHASE alert (quick_scalp_signal.py's
detect_quick_signal_chase), resolved later against real Binance FUTURES
price action -- separate DB from quick_scalp_outcomes.py (the FADE
engine) on purpose: chase and fade are opposite-direction mechanisms
with different validated conditions (see detect_quick_signal_chase's
docstring), so mixing their outcomes into one table would muddy hit-rate
tracking for both. Otherwise an exact structural copy of
quick_scalp_outcomes.py -- same horizons, same resolution logic, same
direction-aware hit rule (chase's "direction" is just whichever way the
trade went, LONG or SHORT, same as fade's logged direction).
"""

import json
import os
import sqlite3
import time

from http_pool import SESSION

DB_PATH = os.path.join(os.path.dirname(__file__), "quick_scalp_chase_predictions.db")
FUTURES_API = "https://fapi.binance.com"

HORIZONS = {"15m": 900, "30m": 1800, "1h": 3600}
CANDLE_TOLERANCE_SECONDS = 15 * 60 + 15 * 60
RISE_THRESHOLD_PCT = 0.3

SCHEMA = """
CREATE TABLE IF NOT EXISTS quick_scalp_chase_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    snapshot_time REAL NOT NULL,
    close_price REAL NOT NULL,
    direction TEXT NOT NULL,
    UNIQUE(symbol, snapshot_time)
);
CREATE TABLE IF NOT EXISTS quick_scalp_chase_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER NOT NULL REFERENCES quick_scalp_chase_predictions(id),
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


def record_signal(symbol, direction, close_price, snapshot_time=None, conn=None):
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    cur = conn.execute(
        """INSERT OR IGNORE INTO quick_scalp_chase_predictions
           (symbol, snapshot_time, close_price, direction)
           VALUES (?, ?, ?, ?)""",
        (symbol, snapshot_time or time.time(), close_price, direction),
    )
    conn.commit()
    prediction_id = cur.lastrowid
    if owns_conn:
        conn.close()
    return prediction_id


def _closest_futures_candle(symbol, target_time, tolerance_seconds=CANDLE_TOLERANCE_SECONDS):
    start_ms = int((target_time - tolerance_seconds) * 1000)
    end_ms = int((target_time + tolerance_seconds) * 1000)
    url = (f"{FUTURES_API}/fapi/v1/klines?symbol={symbol}&interval=15m"
           f"&startTime={start_ms}&endTime={end_ms}&limit=10")
    try:
        resp = SESSION.get(url, timeout=10)
        resp.raise_for_status()
        raw = resp.json()
    except Exception:
        return None
    if not raw:
        return None
    candles = [{"open_time": k[0] / 1000, "close": float(k[4])} for k in raw]
    best = min(candles, key=lambda c: abs(c["open_time"] - target_time))
    if abs(best["open_time"] - target_time) > tolerance_seconds:
        return None
    return best


def _hit(direction, return_pct):
    signed_return = return_pct if direction == "LONG" else -return_pct
    return 1 if signed_return >= RISE_THRESHOLD_PCT else 0


def resolve_matured(conn=None):
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()

    now = time.time()
    resolved = 0
    for horizon, horizon_seconds in HORIZONS.items():
        cutoff = now - horizon_seconds
        rows = conn.execute(
            """SELECT p.id, p.symbol, p.snapshot_time, p.close_price, p.direction
               FROM quick_scalp_chase_predictions p
               LEFT JOIN quick_scalp_chase_outcomes o ON o.prediction_id = p.id AND o.horizon = ?
               WHERE p.snapshot_time <= ? AND o.id IS NULL""",
            (horizon, cutoff),
        ).fetchall()

        for pred_id, symbol, snapshot_time, close_price, direction in rows:
            candle = _closest_futures_candle(symbol, snapshot_time + horizon_seconds)
            if candle is None:
                continue
            return_pct = (candle["close"] / close_price - 1) * 100
            conn.execute(
                """INSERT OR IGNORE INTO quick_scalp_chase_outcomes
                   (prediction_id, horizon, outcome_time, outcome_close, return_pct, hit)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (pred_id, horizon, candle["open_time"], candle["close"],
                 return_pct, _hit(direction, return_pct)),
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
            """SELECT o.return_pct, o.hit FROM quick_scalp_chase_outcomes o
               JOIN quick_scalp_chase_predictions p ON p.id = o.prediction_id
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
