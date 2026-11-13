"""SQLite storage for Binance Futures liquidation events (from the public
!forceOrder@arr websocket stream -- see futures_liquidation_listener.py).

side='SELL' means a LONG position was force-liquidated (forced market sell);
side='BUY' means a SHORT was force-liquidated (forced market buy). Same
SELL/BUY convention as whale_scanner/store.py's whale_events, for the same
reason: it's the side of the resulting market order, not the side of the
position being closed.
"""

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "futures_liquidations.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS liquidations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time REAL NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    quantity REAL NOT NULL,
    usd_value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_liquidations_symbol_time ON liquidations(symbol, time);
"""


def get_connection(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def save_liquidation(event, conn=None):
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    conn.execute(
        """INSERT INTO liquidations (time, symbol, side, price, quantity, usd_value)
           VALUES (:time, :symbol, :side, :price, :quantity, :usd_value)""",
        event,
    )
    conn.commit()
    if owns_conn:
        conn.close()


def net_liquidation_usd(symbol, since, conn=None):
    """(sell_usd, buy_usd) -- total $ of LONG liquidations (SELL) and SHORT
    liquidations (BUY) for this symbol since `since` (unix timestamp)."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    row = conn.execute(
        """SELECT
               SUM(CASE WHEN side = 'SELL' THEN usd_value ELSE 0 END),
               SUM(CASE WHEN side = 'BUY' THEN usd_value ELSE 0 END)
           FROM liquidations WHERE symbol = ? AND time >= ?""",
        (symbol, since),
    ).fetchone()
    if owns_conn:
        conn.close()
    return row[0] or 0.0, row[1] or 0.0


def prune_old(before, conn=None):
    """Delete liquidations older than `before` (unix timestamp) -- keeps the
    DB small on disk since net_liquidation_usd() only ever looks back 1h
    (LIQUIDATION_LOOKBACK_SECONDS in futures_context.py); nothing needs
    data older than a day or two for debugging slack. SQLite reuses freed
    pages for new inserts without a VACUUM, so file size stays bounded
    around the retention window's worth of data after the first prune."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    deleted = conn.execute("DELETE FROM liquidations WHERE time < ?", (before,)).rowcount
    conn.commit()
    if owns_conn:
        conn.close()
    return deleted


if __name__ == "__main__":
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM liquidations").fetchone()[0]
    print(f"{DB_PATH}: {count} liquidation events stored")
    for row in conn.execute(
        "SELECT time, symbol, side, usd_value FROM liquidations ORDER BY time DESC LIMIT 10"
    ):
        print(row)
    conn.close()
