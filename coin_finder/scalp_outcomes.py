"""SQLite log of every System 3 (scalp_pattern_signal.py) signal, resolved
later against real Binance FUTURES price action -- same approach as
pattern_outcomes.py (system 2), separate DB/table since system 3 is its own
independent engine with a much shorter resolution horizon (resolves within
4h, vs system 2's 1d).

Logging happens from scalp_pattern_alert.py's "is this a NEW signal" check,
not from every manual /fut_<symbol>_<leverage> call -- one row per actual
occurrence, same cadence as the Telegram alert.

Resolution fetches a small 15m futures klines window live per prediction,
same reasoning as pattern_outcomes.py: signals are infrequent enough (both
paths are deliberately strict) that this is cheap and avoids a local
rolling-cache window ever rolling past an unresolved prediction.
"""

import json
import os
import sqlite3
import time
import urllib.request

DB_PATH = os.path.join(os.path.dirname(__file__), "scalp_predictions.db")
FUTURES_API = "https://fapi.binance.com"

# Matches the spec this system was built to: direction for the next ~1h,
# resolving within 4h at the latest -- 30m/1h/2h/4h covers that window with
# finer granularity than system 2's hours-to-a-day horizons.
HORIZONS = {"30m": 1800, "1h": 3600, "2h": 2 * 3600, "4h": 4 * 3600}
CANDLE_TOLERANCE_SECONDS = 15 * 60 + 15 * 60  # two 15m candle-widths, mirrors pattern_outcomes.py's ratio
RISE_THRESHOLD_PCT = 1.0  # smaller than system 2's 2.0% -- a quick 15m-based trade targets a smaller, faster move

SCHEMA = """
CREATE TABLE IF NOT EXISTS scalp_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    snapshot_time REAL NOT NULL,
    close_price REAL NOT NULL,
    direction TEXT NOT NULL,
    method TEXT NOT NULL,
    UNIQUE(symbol, snapshot_time)
);
CREATE TABLE IF NOT EXISTS scalp_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER NOT NULL REFERENCES scalp_predictions(id),
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
    # confidence tier (e.g. "confident"/"super confident", see
    # scalp_pattern_signal.CONFIDENCE_LABELS) wasn't captured at launch --
    # added 2026-11-10 so the confidence-tiered TP (added 2026-11-09) can
    # actually be validated against outcomes later. NULL on rows logged
    # before this column existed.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(scalp_predictions)")}
    if "confidence" not in existing_cols:
        conn.execute("ALTER TABLE scalp_predictions ADD COLUMN confidence TEXT")
        conn.commit()
    return conn


def record_signal(symbol, direction, close_price, method, confidence=None, snapshot_time=None, conn=None):
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    conn.execute(
        """INSERT OR IGNORE INTO scalp_predictions
           (symbol, snapshot_time, close_price, direction, method, confidence)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (symbol, snapshot_time or time.time(), close_price, direction, method, confidence),
    )
    conn.commit()
    if owns_conn:
        conn.close()


def recent_alert_count(symbol, direction, window_seconds, before_time=None, conn=None):
    """Count of PRIOR alerts already logged for this symbol+direction
    within the trailing window ending at before_time (default now) --
    deliberately excludes "now" itself (strict <), since this is called to
    decide a warning BEFORE the current occurrence is recorded. Backs the
    repeat-alert exhaustion warning in scalp_pattern_signal.py (added
    2026-11-10)."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    before_time = before_time if before_time is not None else time.time()
    row = conn.execute(
        """SELECT COUNT(*) FROM scalp_predictions
           WHERE symbol = ? AND direction = ? AND snapshot_time >= ? AND snapshot_time < ?""",
        (symbol, direction, before_time - window_seconds, before_time),
    ).fetchone()
    if owns_conn:
        conn.close()
    return row[0]


def _closest_futures_candle(symbol, target_time, tolerance_seconds=CANDLE_TOLERANCE_SECONDS):
    start_ms = int((target_time - tolerance_seconds) * 1000)
    end_ms = int((target_time + tolerance_seconds) * 1000)
    url = (f"{FUTURES_API}/fapi/v1/klines?symbol={symbol}&interval=15m"
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
    """Direction-aware -- a LONG call hits on a rise, a SHORT call hits on a
    fall. Only the longest horizon (4h) requires a minimum-sized move;
    shorter horizons count any directionally-correct move as a hit."""
    signed_return = return_pct if direction == "LONG" else -return_pct
    if horizon == "4h":
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
               FROM scalp_predictions p
               LEFT JOIN scalp_outcomes o ON o.prediction_id = p.id AND o.horizon = ?
               WHERE p.snapshot_time <= ? AND o.id IS NULL""",
            (horizon, cutoff),
        ).fetchall()

        for pred_id, symbol, snapshot_time, close_price, direction in rows:
            candle = _closest_futures_candle(symbol, snapshot_time + horizon_seconds)
            if candle is None:
                continue  # not resolvable yet/anymore -- try again next run
            return_pct = (candle["close"] / close_price - 1) * 100
            conn.execute(
                """INSERT OR IGNORE INTO scalp_outcomes
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
            """SELECT o.return_pct, o.hit FROM scalp_outcomes o
               JOIN scalp_predictions p ON p.id = o.prediction_id
               WHERE o.horizon = ?""",
            (horizon,),
        ).fetchall()
        if not rows:
            print(f"{horizon}: no resolved signals yet")
            continue
        hit_rate = sum(h for _, h in rows) / len(rows)
        avg_return = sum(r for r, _ in rows) / len(rows)
        print(f"{horizon}: n={len(rows)} hit_rate={hit_rate:.1%} avg_return={avg_return:+.2f}%")

    print("\nBy confidence tier (NULL = logged before this column existed):")
    for horizon in HORIZONS:
        rows = conn.execute(
            """SELECT p.confidence, o.return_pct, o.hit FROM scalp_outcomes o
               JOIN scalp_predictions p ON p.id = o.prediction_id
               WHERE o.horizon = ?""",
            (horizon,),
        ).fetchall()
        by_conf = {}
        for confidence, ret, hit in rows:
            by_conf.setdefault(confidence, []).append((ret, hit))
        for confidence, vals in by_conf.items():
            n = len(vals)
            hit_rate = sum(h for _, h in vals) / n
            avg_return = sum(r for r, _ in vals) / n
            print(f"  {horizon} / {confidence}: n={n} hit_rate={hit_rate:.1%} avg_return={avg_return:+.2f}%")
    conn.close()
