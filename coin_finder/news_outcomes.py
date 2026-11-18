"""SQLite log of every news_alert.py Telegram alert, resolved later against
real Binance FUTURES price action -- same shadow-logging/resolve approach as
quick_scalp_outcomes.py, but WITHOUT a hit/miss column: a news headline has
no predicted direction (news_alert.py deliberately skips sentiment scoring,
see its own docstring), so there's nothing to score a "hit" against. This
only measures MAGNITUDE -- did the coin move more than usual in the hours
after getting flagged -- not whether news alerts correctly call direction.

Also, unlike the quick-scalp trackers (intended hold under 1h), news
effects can play out slower, so horizons here are wider: 1h/4h/24h.

Logging happens from news_alert.py's poll loop, right when a Telegram
alert fires for a matched coin. Resolution fetches a small futures klines
window live per prediction, same reasoning as quick_scalp_outcomes.py.

IMPORTANT CAVEAT (see /news_accuracy's report): this has no baseline/
control comparison against the coin's normal volatility over the same
horizon. A nonzero avg_abs_return here does not by itself mean news
causes outsized moves -- it could just be the coin's ordinary noise.
Treat this as a first pass to build the dataset, not a verdict.
"""

import os
import sqlite3
import time

from http_pool import SESSION

DB_PATH = os.path.join(os.path.dirname(__file__), "news_predictions.db")
FUTURES_API = "https://fapi.binance.com"

HORIZONS = {"1h": 3600, "4h": 14400, "24h": 86400}
CANDLE_TOLERANCE_SECONDS = 15 * 60 + 15 * 60  # mirrors quick_scalp_outcomes.py's ratio

SCHEMA = """
CREATE TABLE IF NOT EXISTS news_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    snapshot_time REAL NOT NULL,
    close_price REAL NOT NULL,
    headline TEXT NOT NULL,
    UNIQUE(symbol, snapshot_time)
);
CREATE TABLE IF NOT EXISTS news_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER NOT NULL REFERENCES news_predictions(id),
    horizon TEXT NOT NULL,
    outcome_time REAL NOT NULL,
    outcome_close REAL NOT NULL,
    return_pct REAL NOT NULL,
    UNIQUE(prediction_id, horizon)
);
"""


def get_connection(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def record_signal(symbol, close_price, headline, snapshot_time=None, conn=None):
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    conn.execute(
        """INSERT OR IGNORE INTO news_predictions
           (symbol, snapshot_time, close_price, headline)
           VALUES (?, ?, ?, ?)""",
        (symbol, snapshot_time or time.time(), close_price, headline),
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


def resolve_matured(conn=None):
    """Fill in outcomes for every (signal, horizon) pair whose time has
    come. Returns count resolved."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()

    now = time.time()
    resolved = 0
    for horizon, horizon_seconds in HORIZONS.items():
        cutoff = now - horizon_seconds
        rows = conn.execute(
            """SELECT p.id, p.symbol, p.snapshot_time, p.close_price
               FROM news_predictions p
               LEFT JOIN news_outcomes o ON o.prediction_id = p.id AND o.horizon = ?
               WHERE p.snapshot_time <= ? AND o.id IS NULL""",
            (horizon, cutoff),
        ).fetchall()

        for pred_id, symbol, snapshot_time, close_price in rows:
            candle = _closest_futures_candle(symbol, snapshot_time + horizon_seconds)
            if candle is None:
                continue  # not resolvable yet/anymore -- try again next run
            return_pct = (candle["close"] / close_price - 1) * 100
            conn.execute(
                """INSERT OR IGNORE INTO news_outcomes
                   (prediction_id, horizon, outcome_time, outcome_close, return_pct)
                   VALUES (?, ?, ?, ?, ?)""",
                (pred_id, horizon, candle["open_time"], candle["close"], return_pct),
            )
            resolved += 1

    conn.commit()
    if owns_conn:
        conn.close()
    return resolved


def build_report(conn=None):
    """{horizon: {"n", "avg_return_pct", "avg_abs_return_pct"}} -- see
    module docstring's caveat about the lack of a baseline comparison."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()

    report = {}
    for horizon in HORIZONS:
        rows = conn.execute(
            "SELECT return_pct FROM news_outcomes WHERE horizon = ?", (horizon,),
        ).fetchall()
        if not rows:
            report[horizon] = {"n": 0}
            continue
        returns = [r for (r,) in rows]
        report[horizon] = {
            "n": len(returns),
            "avg_return_pct": sum(returns) / len(returns),
            "avg_abs_return_pct": sum(abs(r) for r in returns) / len(returns),
        }

    if owns_conn:
        conn.close()
    return report


if __name__ == "__main__":
    conn = get_connection()
    resolved = resolve_matured(conn=conn)
    print(f"Resolved {resolved} newly matured signal/horizon pairs this run.\n")

    for horizon, stats in build_report(conn=conn).items():
        if stats["n"] == 0:
            print(f"{horizon}: no resolved signals yet")
            continue
        print(f"{horizon}: n={stats['n']} avg_return={stats['avg_return_pct']:+.2f}% "
              f"avg_abs_return={stats['avg_abs_return_pct']:.2f}%")
    conn.close()
