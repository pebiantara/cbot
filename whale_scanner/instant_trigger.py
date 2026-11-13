"""Shared "big trade -> instant single-coin analysis" trigger, used by both
binance_trades.py (websocket) and webhook_server.py (on-chain webhooks). Each
process keeps its own in-memory debounce state -- they're separate containers,
so a coin could in theory double-trigger across both within the debounce
window, but that's an acceptable rare edge case for a personal project.
"""

import asyncio
import os
import sys
import time

TRIGGER_USD_THRESHOLD = 500_000  # instant single-coin analysis + alert above this
TRIGGER_DEBOUNCE_SECONDS = 15 * 60
_last_triggered = {}  # symbol -> last trigger time


def maybe_trigger_instant_analysis(event):
    """event: common whale-event dict (token_symbol, side, usd_value, chain).
    Fire-and-forget: schedules an instant re-analysis for a big-enough trade,
    skipping this symbol if it already triggered within the debounce window.
    Runs analyze_and_alert in a thread so its blocking HTTP/sqlite calls don't
    stall the caller's event loop."""
    if event["usd_value"] < TRIGGER_USD_THRESHOLD:
        return
    symbol = event["token_symbol"]
    now = time.time()
    if now - _last_triggered.get(symbol, 0) < TRIGGER_DEBOUNCE_SECONDS:
        return
    _last_triggered[symbol] = now

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "coin_finder"))
    from analyze_one import analyze_and_alert  # noqa: E402 -- imported lazily, avoids a slow startup

    trigger = {
        "symbol": symbol, "side": event["side"],
        "usd_value": event["usd_value"], "chain": event["chain"],
    }

    async def run():
        try:
            await asyncio.get_event_loop().run_in_executor(None, analyze_and_alert, trigger)
        except Exception as e:
            print(f"Instant analysis failed for {symbol}: {e}")

    asyncio.create_task(run())
