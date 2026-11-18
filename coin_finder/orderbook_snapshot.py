"""Level-2 order-book shadow-logging for quick-scalp, added 2026-11-13.

Binance doesn't retain historical L2 order-book snapshots, so this can't be
backtested retroactively the normal way -- the depth endpoint only ever
shows the CURRENT book. This module logs a snapshot of bid/ask imbalance
every time a real signal fires, tagged with the eventual outcome once
quick_scalp_outcomes.py resolves it, so correlation can be checked once
enough real signals accumulate. SHADOW ONLY -- never gates or changes a
live signal, same convention as the paused PRO MODE's Stage B logging.
"""

import os
import sqlite3
import time

from http_pool import SESSION

FUTURES_API = "https://fapi.binance.com"
HERE = os.path.dirname(os.path.abspath(__file__))
ORDERBOOK_DB = os.path.join(HERE, "quick_scalp_orderbook.db")
IMBALANCE_PCT_RANGE = 0.5  # +/- 0.5% around mid price, same ballpark as a scalp's own SL/TP distances


def fetch_depth(symbol, limit=100):
    url = f"{FUTURES_API}/fapi/v1/depth?symbol={symbol}&limit={limit}"
    resp = SESSION.get(url, timeout=8)
    resp.raise_for_status()
    return resp.json()


def orderbook_imbalance(depth, pct_range=IMBALANCE_PCT_RANGE):
    """(imbalance, bid_vol, ask_vol) within +/-pct_range% of mid price.
    imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol), positive =
    more resting buy-side depth nearby, negative = more resting sell-side
    depth. None if either side has no book at all."""
    bids = depth.get("bids") or []
    asks = depth.get("asks") or []
    if not bids or not asks:
        return None, 0.0, 0.0
    best_bid, best_ask = float(bids[0][0]), float(asks[0][0])
    mid = (best_bid + best_ask) / 2
    if mid <= 0:
        return None, 0.0, 0.0
    low = mid * (1 - pct_range / 100)
    high = mid * (1 + pct_range / 100)
    bid_vol = sum(float(qty) for price, qty in bids if float(price) >= low)
    ask_vol = sum(float(qty) for price, qty in asks if float(price) <= high)
    total = bid_vol + ask_vol
    if total <= 0:
        return None, bid_vol, ask_vol
    return (bid_vol - ask_vol) / total, bid_vol, ask_vol


def get_connection(path=ORDERBOOK_DB):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS quick_scalp_orderbook_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_id INTEGER,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            snapshot_time REAL NOT NULL,
            imbalance REAL,
            bid_vol REAL,
            ask_vol REAL
        )"""
    )
    conn.commit()
    return conn


def log_snapshot(symbol, direction, prediction_id=None, conn=None):
    """Fetches the CURRENT order book (best-effort, no retry -- a miss here
    just means one fewer data point, not worth failing the real alert
    over) and logs it. Returns the imbalance value logged, or None on any
    failure (never raises -- callers shouldn't need their own try/except
    for this)."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        depth = fetch_depth(symbol)
        imbalance, bid_vol, ask_vol = orderbook_imbalance(depth)
    except Exception:
        return None
    conn.execute(
        """INSERT INTO quick_scalp_orderbook_snapshots
           (prediction_id, symbol, direction, snapshot_time, imbalance, bid_vol, ask_vol)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (prediction_id, symbol, direction, time.time(), imbalance, bid_vol, ask_vol),
    )
    conn.commit()
    if owns_conn:
        conn.close()
    return imbalance


if __name__ == "__main__":
    import sys
    sym = sys.argv[1].upper() + "USDT" if len(sys.argv) > 1 else "BTCUSDT"
    depth = fetch_depth(sym)
    imbalance, bid_vol, ask_vol = orderbook_imbalance(depth)
    print(f"{sym}: imbalance={imbalance:+.3f}  bid_vol={bid_vol:.4f}  ask_vol={ask_vol:.4f}"
          if imbalance is not None else f"{sym}: no imbalance computed")
