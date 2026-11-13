"""Binance Futures liquidation websocket listener -- !forceOrder@arr is a
public, free, no-API-key stream covering liquidations across ALL USDT-M
futures symbols (confirmed live 2026-11-04; Coinglass's API, the usual
source for aggregated liquidation data, has no free tier and starts at
$29-35/mo, so this is the free alternative for Binance's own liquidations
specifically -- it won't cover other exchanges).

Unlike binance_trades.py's per-symbol aggTrade subscription, this is a
single global stream (no symbol list needed) -- Binance pushes at most one
(the largest) liquidation per symbol per 1000ms, so this is inherently a
low-volume stream, not a firehose.

Saved to liquidation_store.py's SQLite DB. No confirmation gate is wired
into futures_pattern_signal.py (system 2) off this yet -- there's no
historical data to validate against until this has run for a while.

Auto-prunes anything older than RETENTION_SECONDS on a timer -- the only
consumer (futures_context.liquidation_context) looks back 1h, so there's no
reason to let this grow unbounded on disk.
"""

import asyncio
import json
import time

import websockets

from liquidation_store import get_connection, prune_old, save_liquidation

STREAM_URL = "wss://fstream.binance.com/market/ws/!forceOrder@arr"  # Binance added the /market/ segment post-CM-migration; the old /ws/ path connects but silently delivers nothing -- found 2026-11-10 (see memory, this listener had been live ~7h and 0 rows ever)
RETENTION_SECONDS = 2 * 24 * 3600  # 2 days -- generous slack over the 1h lookback it's actually used for
PRUNE_INTERVAL_SECONDS = 3600


def parse_force_order(data):
    """Binance forceOrder payload -> liquidation_store.py's event format."""
    order = data["o"]
    price = float(order["ap"]) if float(order.get("ap", 0)) else float(order["p"])
    qty = float(order["q"])
    return {
        "time": order["T"] / 1000,
        "symbol": order["s"],
        "side": order["S"],  # SELL = long liquidated, BUY = short liquidated
        "price": price,
        "quantity": qty,
        "usd_value": price * qty,
    }


async def prune_periodically(conn):
    while True:
        await asyncio.sleep(PRUNE_INTERVAL_SECONDS)
        deleted = prune_old(time.time() - RETENTION_SECONDS, conn=conn)
        if deleted:
            print(f"Pruned {deleted} liquidation(s) older than {RETENTION_SECONDS / 3600:.0f}h")


async def watch(conn=None):
    """Connect to the global liquidation stream and save every event. Reconnects on drop."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        pruner = asyncio.create_task(prune_periodically(conn))
        try:
            while True:
                try:
                    async with websockets.connect(STREAM_URL, ping_interval=20) as ws:
                        print("Connected: watching all-market futures liquidations")
                        async for raw in ws:
                            msg = json.loads(raw)
                            if "o" not in msg:
                                continue
                            event = parse_force_order(msg)
                            save_liquidation(event, conn=conn)
                            print(f"{event['symbol']:<12} {event['side']:<4} ${event['usd_value']:>12,.0f}")
                except websockets.ConnectionClosed:
                    print("Connection closed, reconnecting...")
        finally:
            pruner.cancel()
    finally:
        if owns_conn:
            conn.close()


if __name__ == "__main__":
    asyncio.run(watch())
