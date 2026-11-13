"""SQLite storage for whale events (common format across all chains + Binance)."""

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "whale_events.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS whale_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time REAL NOT NULL,
    chain TEXT NOT NULL,
    wallet TEXT,
    wallet_label TEXT,
    token_symbol TEXT NOT NULL,
    token_address TEXT,
    side TEXT NOT NULL,
    amount REAL NOT NULL,
    usd_value REAL NOT NULL,
    tx_hash TEXT NOT NULL,
    UNIQUE(chain, tx_hash)
);
"""


def market_side(raw_side, wallet_row):
    """Flip BUY/SELL for exchange wallets: a deposit INTO an exchange hot wallet
    is sell pressure on the coin, a withdrawal OUT is accumulation -- the opposite
    of what the raw wallet-direction side says for an ordinary whale wallet."""
    is_exchange = (wallet_row.get("is_exchange") or "").strip().lower() == "true"
    if not is_exchange:
        return raw_side
    return "SELL" if raw_side == "BUY" else "BUY"


def get_connection(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.execute(SCHEMA)
    return conn


def save_whale_event(event, conn=None):
    """Insert one whale event. Duplicates (same chain + tx_hash) are ignored."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    conn.execute(
        """INSERT OR IGNORE INTO whale_events
           (time, chain, wallet, wallet_label, token_symbol, token_address,
            side, amount, usd_value, tx_hash)
           VALUES (:time, :chain, :wallet, :wallet_label, :token_symbol, :token_address,
                   :side, :amount, :usd_value, :tx_hash)""",
        event,
    )
    conn.commit()
    if owns_conn:
        conn.close()


if __name__ == "__main__":
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM whale_events").fetchone()[0]
    print(f"{DB_PATH}: {count} whale events stored")
    for row in conn.execute(
        "SELECT time, chain, token_symbol, side, usd_value FROM whale_events "
        "ORDER BY time DESC LIMIT 10"
    ):
        print(row)
    conn.close()
