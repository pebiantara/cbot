"""Binance aggTrade websocket listener -- watches BOTH spot and futures
(added 2026-11-19; the original version only watched spot, which turned
out to miss most of the real activity -- a same-day check on MMTUSDT
found futures carrying ~3x spot's notional volume).

Alerting went through two earlier designs the same day before landing
here, both rejected by the user after seeing real output: first a fixed
$250k single-trade threshold (never fires on a smaller-cap coin like MMT
-- its biggest SINGLE trade during an actively-pumping 2h window was only
~$20k), then a per-symbol RELATIVE multiplier (10x, then 20x, a coin's
own trailing average trade size) -- still "too small and annoying": with
~80 tracked symbols, individual-trade triggers fired roughly every few
seconds across the universe, including on objectively tiny prints like a
$1,974 HYPE buy that just happened to be 20x an even tinier average.

Final design: alert on AGGREGATE notional, not individual trades. Every
trade (any size) feeds a rolling AGGREGATION_WINDOW_SECONDS (5min) window
per (market, symbol); when that window's TOTAL crosses that symbol's
threshold, alert fires. Two-tier flat threshold (the user's own explicit
choice, after a flat $500k-for-everyone was floated and immediately
adjusted once live): $1M for MAJOR_SYMBOLS (BTC/ETH -- high enough
liquidity that $500k/5min is closer to routine than notable), $500k for
everything else. Hysteresis (ALERT_REARM_FRACTION), not a flat cooldown
-- won't re-alert while sustained above the bar, re-arms once the window
drops back below half its threshold.

DB logging (whale_events.db) is a SEPARATE, much lower, flat floor
(DB_LOG_MIN_USD) -- keeps a useful historical record of individually
sizable trades without reviving the per-trade alert noise above. Also
still calls instant_trigger.py's own full-reanalysis path per individual
trade, unchanged, gated by its own separate much-higher threshold.
"""

import asyncio
import json
import os
import sys
import time
from collections import deque

import requests
import websockets

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "coin_finder"))

from instant_trigger import maybe_trigger_instant_analysis  # noqa: E402
from store import get_connection, save_whale_event  # noqa: E402
from telegram_alert import load_env, send_telegram_message  # noqa: E402
from universe import get_universe  # noqa: E402

SPOT_STREAM_URL = "wss://stream.binance.com:9443/stream"
FUTURES_STREAM_URL = "wss://fstream.binance.com/stream"
FUTURES_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"

AGGREGATION_WINDOW_SECONDS = 5 * 60
MAJOR_SYMBOLS = {"BTCUSDT", "ETHUSDT"}
ALERT_THRESHOLD_USD_MAJOR = float(os.environ.get("BIG_TRADE_ALERT_USD_MAJOR", 1_000_000))
ALERT_THRESHOLD_USD_DEFAULT = float(os.environ.get("BIG_TRADE_ALERT_USD", 500_000))
ALERT_REARM_FRACTION = 0.5  # must drop back below this fraction of the threshold before re-arming
DB_LOG_MIN_USD = float(os.environ.get("BINANCE_TRADES_DB_LOG_MIN_USD", 5_000))


def _threshold_for(symbol):
    return ALERT_THRESHOLD_USD_MAJOR if symbol in MAJOR_SYMBOLS else ALERT_THRESHOLD_USD_DEFAULT

# (market, symbol) -> deque[(ts, usd_value, side)], trimmed to AGGREGATION_WINDOW_SECONDS
_window = {}
_elevated = {}  # (market, symbol) -> bool, hysteresis state


def get_futures_symbols():
    """All Binance Futures (USDT-M) symbols currently TRADING -- many spot-
    universe symbols aren't listed on futures at all, and subscribing a
    websocket stream to a symbol futures doesn't recognize would corrupt
    the whole combined-stream connection, not just skip that one symbol
    (unlike a one-off REST call, which can just catch+fallback per
    symbol, see get_futures_klines's docstring)."""
    resp = requests.get(FUTURES_EXCHANGE_INFO_URL, timeout=15)
    resp.raise_for_status()
    info = resp.json()
    return {s["symbol"] for s in info["symbols"] if s["status"] == "TRADING"}


def build_stream_url(base_url, symbols):
    streams = "/".join(f"{s.lower()}@aggTrade" for s in symbols)
    return f"{base_url}?streams={streams}"


def parse_agg_trade(data, market):
    """Binance aggTrade payload -> common whale event format."""
    price = float(data["p"])
    qty = float(data["q"])
    return {
        "time": data["T"] / 1000,
        "chain": f"binance_{market}",
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


def _check_aggregate(market, symbol, now):
    """Feeds the rolling window (every trade, any size) and returns
    {"total", "buy", "sell"} the moment the window's total crosses this
    symbol's threshold (_threshold_for) from below (hysteresis -- see
    module docstring), else None."""
    key = (market, symbol)
    buf = _window[key]
    cutoff = now - AGGREGATION_WINDOW_SECONDS
    while buf and buf[0][0] < cutoff:
        buf.popleft()

    total = sum(v for _, v, _ in buf)
    threshold = _threshold_for(symbol)
    was_elevated = _elevated.get(key, False)

    if not was_elevated and total >= threshold:
        _elevated[key] = True
        buy = sum(v for _, v, s in buf if s == "BUY")
        return {"total": total, "buy": buy, "sell": total - buy}

    if was_elevated and total < threshold * ALERT_REARM_FRACTION:
        _elevated[key] = False
    return None


def _send_aggregate_alert(market, symbol, hit, token, chat_id):
    emoji = "\U0001f7e2" if hit["buy"] >= hit["sell"] else "\U0001f534"
    lean = "buy-heavy" if hit["buy"] >= hit["sell"] else "sell-heavy"
    send_telegram_message(
        f"{emoji} {symbol} ({market}): ${hit['total']:,.0f} in trades over the last "
        f"{AGGREGATION_WINDOW_SECONDS // 60}min ({lean} -- buy ${hit['buy']:,.0f} / sell ${hit['sell']:,.0f}).\n"
        f"Diagnostic only -- not a trade signal.",
        token, chat_id,
    )


async def watch(symbols, market, base_url, conn=None):
    """Connect to one Binance aggTrade stream (spot or futures), feed every
    trade into its symbol's rolling aggregate window, and alert when that
    window crosses the symbol's threshold. Reconnects on drop."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    env = load_env()
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    url = build_stream_url(base_url, symbols)
    try:
        while True:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    print(f"Connected ({market}): watching {len(symbols)} symbols, "
                          f"alert = ${ALERT_THRESHOLD_USD_DEFAULT:,.0f}/{AGGREGATION_WINDOW_SECONDS // 60}min "
                          f"(${ALERT_THRESHOLD_USD_MAJOR:,.0f} for {', '.join(sorted(MAJOR_SYMBOLS))})")
                    async for raw in ws:
                        msg = json.loads(raw)
                        if "data" not in msg:
                            continue
                        event = parse_agg_trade(msg["data"], market)
                        symbol = event["token_symbol"]

                        if event["usd_value"] >= DB_LOG_MIN_USD:
                            save_whale_event(event, conn=conn)
                            print(f"[{market}] {symbol:<10} {event['side']:<4} "
                                  f"${event['usd_value']:>12,.0f}  tx={event['tx_hash']}")

                        key = (market, symbol)
                        _window.setdefault(key, deque()).append((event["time"], event["usd_value"], event["side"]))
                        hit = _check_aggregate(market, symbol, event["time"])
                        if hit:
                            print(f"[{market}] {symbol} AGGREGATE ALERT: ${hit['total']:,.0f} "
                                  f"in {AGGREGATION_WINDOW_SECONDS // 60}min")
                            if token and chat_id:
                                _send_aggregate_alert(market, symbol, hit, token, chat_id)

                        maybe_trigger_instant_analysis(event)
            except websockets.ConnectionClosed:
                print(f"[{market}] Connection closed, reconnecting...")
    finally:
        if owns_conn:
            conn.close()


async def watch_all(spot_symbols, futures_symbols):
    conn = get_connection()
    try:
        await asyncio.gather(
            watch(spot_symbols, "spot", SPOT_STREAM_URL, conn=conn),
            watch(futures_symbols, "futures", FUTURES_STREAM_URL, conn=conn),
        )
    finally:
        conn.close()


if __name__ == "__main__":
    universe_symbols = [c["symbol"] for c in get_universe()]
    futures_tradable = get_futures_symbols()
    futures_symbols = [s for s in universe_symbols if s in futures_tradable]
    print(f"Universe: {len(universe_symbols)} symbols, {len(futures_symbols)} also on futures")
    asyncio.run(watch_all(universe_symbols, futures_symbols))
