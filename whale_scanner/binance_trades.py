"""Binance aggTrade websocket listener — saves trades >= USD_THRESHOLD as whale
events, and triggers an instant single-coin re-analysis + Telegram alert for
trades >= instant_trigger.TRIGGER_USD_THRESHOLD (debounced per symbol so a
flurry of big trades on the same coin doesn't spam alerts).
"""

import asyncio
import json
import os
import sys

import websockets

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "coin_finder"))

from instant_trigger import maybe_trigger_instant_analysis  # noqa: E402
from store import get_connection, save_whale_event  # noqa: E402
from universe import get_universe  # noqa: E402

STREAM_URL = "wss://stream.binance.com:9443/stream"
USD_THRESHOLD = 250_000  # saved to whale_events.db above this


def build_stream_url(symbols):
    streams = "/".join(f"{s.lower()}@aggTrade" for s in symbols)
    return f"{STREAM_URL}?streams={streams}"


def parse_agg_trade(data):
    """Binance aggTrade payload -> common whale event format."""
    price = float(data["p"])
    qty = float(data["q"])
    return {
        "time": data["T"] / 1000,
        "chain": "binance",
        "wallet": None,
        "wallet_label": None,
        "token_symbol": data["s"],
        "token_address": None,
        # m=True: buyer is the maker, so the taker (aggressor) sold.
        "side": "SELL" if data["m"] else "BUY",
        "amount": qty,
        "usd_value": price * qty,
        "tx_hash": str(data["a"]),
    }


async def watch(symbols, usd_threshold=USD_THRESHOLD, conn=None):
    """Connect to Binance aggTrade stream(s) and save trades >= usd_threshold. Reconnects on drop."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    url = build_stream_url(symbols)
    try:
        while True:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    print(f"Connected: watching {len(symbols)} symbols for trades >= ${usd_threshold:,.0f}")
                    async for raw in ws:
                        msg = json.loads(raw)
                        if "data" not in msg:
                            continue
                        event = parse_agg_trade(msg["data"])
                        if event["usd_value"] >= usd_threshold:
                            save_whale_event(event, conn=conn)
                            print(f"{event['token_symbol']:<10} {event['side']:<4} "
                                  f"${event['usd_value']:>12,.0f}  tx={event['tx_hash']}")
                            maybe_trigger_instant_analysis(event)
            except websockets.ConnectionClosed:
                print("Connection closed, reconnecting...")
    finally:
        if owns_conn:
            conn.close()


if __name__ == "__main__":
    universe = get_universe()
    symbols = [c["symbol"] for c in universe]
    asyncio.run(watch(symbols))
