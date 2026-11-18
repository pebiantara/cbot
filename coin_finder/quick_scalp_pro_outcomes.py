"""SQLite log of PRO MODE (quick_scalp_signal.detect_quick_signal_pro)
candidate signals, for Stage B shadow-logging (see
/Users/pebiantara/.claude/plans/cozy-waddling-peacock.md and
cbot_quick_scalp_signal.md memory for full context).

Deliberately a SEPARATE sibling of quick_scalp_outcomes.py/DB rather than
an extended schema on it -- PRO MODE is still unvalidated and not live-
alerting anyone; keeping it in its own DB means it can be deleted/reset
freely without touching the real (already-validated) fade engine's
outcome history. Same resolution approach as quick_scalp_outcomes.py
(fetch a real futures candle at each horizon), plus `score` so results
can be sliced by confidence tier the same way Stage A's backtest did.

Logging happens from quick_scalp_alert.py's poll loop, SHADOW ONLY --
no Telegram alert is ever sent for a PRO MODE signal as of this writing.
"""

import json
import os
import sqlite3
import time

from http_pool import SESSION

DB_PATH = os.path.join(os.path.dirname(__file__), "quick_scalp_pro_predictions.db")
FUTURES_API = "https://fapi.binance.com"

HORIZONS = {"15m": 900, "30m": 1800, "1h": 3600}
CANDLE_TOLERANCE_SECONDS = 15 * 60 + 15 * 60
RISE_THRESHOLD_PCT = 0.3  # same bar as quick_scalp_outcomes.py, for direct comparability

SCHEMA = """
CREATE TABLE IF NOT EXISTS quick_scalp_pro_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    snapshot_time REAL NOT NULL,
    close_price REAL NOT NULL,
    direction TEXT NOT NULL,
    score INTEGER NOT NULL,
    components TEXT NOT NULL,
    UNIQUE(symbol, snapshot_time)
);
CREATE TABLE IF NOT EXISTS quick_scalp_pro_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER NOT NULL REFERENCES quick_scalp_pro_predictions(id),
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


def record_signal(symbol, direction, close_price, score, components, snapshot_time=None, conn=None):
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    conn.execute(
        """INSERT OR IGNORE INTO quick_scalp_pro_predictions
           (symbol, snapshot_time, close_price, direction, score, components)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (symbol, snapshot_time or time.time(), close_price, direction, score, json.dumps(components)),
    )
    conn.commit()
    if owns_conn:
        conn.close()


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
               FROM quick_scalp_pro_predictions p
               LEFT JOIN quick_scalp_pro_outcomes o ON o.prediction_id = p.id AND o.horizon = ?
               WHERE p.snapshot_time <= ? AND o.id IS NULL""",
            (horizon, cutoff),
        ).fetchall()

        for pred_id, symbol, snapshot_time, close_price, direction in rows:
            candle = _closest_futures_candle(symbol, snapshot_time + horizon_seconds)
            if candle is None:
                continue
            return_pct = (candle["close"] / close_price - 1) * 100
            conn.execute(
                """INSERT OR IGNORE INTO quick_scalp_pro_outcomes
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
    print(f"Resolved {resolved} newly matured PRO MODE signal/horizon pairs this run.\n")

    for horizon in HORIZONS:
        rows = conn.execute(
            """SELECT o.return_pct, o.hit, p.score FROM quick_scalp_pro_outcomes o
               JOIN quick_scalp_pro_predictions p ON p.id = o.prediction_id
               WHERE o.horizon = ?""",
            (horizon,),
        ).fetchall()
        if not rows:
            print(f"{horizon}: no resolved signals yet")
            continue
        hit_rate = sum(h for _, h, _ in rows) / len(rows)
        avg_return = sum(r for r, _, _ in rows) / len(rows)
        print(f"{horizon}: n={len(rows)} hit_rate={hit_rate:.1%} avg_return={avg_return:+.2f}%")
        by_score = {}
        for _, h, s in rows:
            by_score.setdefault(s, []).append(h)
        for s in sorted(by_score):
            hits = by_score[s]
            print(f"    score={s}: n={len(hits)} hit_rate={sum(hits) / len(hits):.1%}")
    conn.close()
