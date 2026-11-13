"""Solana whale wallet tracker via the Helius Wallet API (token transfers)."""

import csv
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # whale_scanner/
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "coin_finder"))
from store import get_connection, market_side, save_whale_event  # noqa: E402
from prices import token_usd_value  # noqa: E402
from token_map import to_binance_symbol  # noqa: E402

WATCHLIST_CSV = os.path.join(HERE, "watchlist.csv")
ENV_PATH = os.path.join(os.path.dirname(HERE), ".env")

TRANSFERS_URL = "https://api.helius.xyz/v1/wallet/{address}/transfers"
CHAIN = "solana"
MAX_PAGES = 5  # cap on first run so an active wallet's full history isn't pulled at once


def load_env(path=ENV_PATH):
    env = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def load_watchlist(path=WATCHLIST_CSV):
    """watchlist.csv columns: chain,address,label,source"""
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return [row for row in csv.DictReader(f) if row["chain"] == CHAIN]


def fetch_transfers_page(address, api_key, cursor=None):
    params = {"api-key": api_key, "limit": 100}
    if cursor:
        params["cursor"] = cursor
    resp = requests.get(TRANSFERS_URL.format(address=address), params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def parse_transfer(wallet_row, tx):
    """Helius wallet-transfers row -> common whale event format."""
    raw_side = "BUY" if tx["direction"] == "in" else "SELL"
    side = market_side(raw_side, wallet_row)
    # falls back to the raw on-chain symbol/truncated mint when unmapped -- those
    # stay watch-only and never join combine.py's Binance-symbol-keyed lookup
    binance_symbol = to_binance_symbol(CHAIN, tx["mint"])
    usd_value = token_usd_value(CHAIN, tx["mint"], tx["amount"])
    return {
        "time": float(tx["timestamp"]),
        "chain": CHAIN,
        "wallet": wallet_row["address"],
        "wallet_label": wallet_row.get("label"),
        "token_symbol": binance_symbol or tx.get("symbol") or tx["mint"][:6],
        "token_address": tx["mint"],
        "side": side,
        "amount": tx["amount"],
        "usd_value": usd_value if usd_value is not None else 0.0,
        "tx_hash": tx["signature"],
    }


def scan(conn=None, api_key=None):
    """Fetch token transfers for every Solana watchlist wallet, save new ones."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    api_key = api_key or load_env().get("HELIUS_API_KEY")
    if not api_key:
        raise RuntimeError("HELIUS_API_KEY not set — add it to .env in the project root")

    saved = 0
    for wallet_row in load_watchlist():
        cursor = None
        for _ in range(MAX_PAGES):
            page = fetch_transfers_page(wallet_row["address"], api_key, cursor)
            for tx in page.get("data", []):
                save_whale_event(parse_transfer(wallet_row, tx), conn=conn)
                saved += 1
            pagination = page.get("pagination", {})
            if not pagination.get("hasMore"):
                break
            cursor = pagination.get("nextCursor")
        time.sleep(0.25)

    if owns_conn:
        conn.close()
    return saved


if __name__ == "__main__":
    try:
        count = scan()
        print(f"solana: scanned watchlist, {count} transfers saved (duplicates ignored)")
    except RuntimeError as e:
        print(f"solana: skipped — {e}")
