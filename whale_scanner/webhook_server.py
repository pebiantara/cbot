"""Receives real-time whale alerts via webhook instead of polling.

- POST /webhooks/helius  -- Helius "enhanced" webhook (Solana), array of parsed txns.
- POST /webhooks/alchemy -- Alchemy "Address Activity" webhook (BSC/Base/Ethereum/Arbitrum).
- POST /telegram/webhook -- Telegram bot commands (/help lists all of them).

Only transfers touching a wallet in watchlist.csv are saved. Run with:
    uvicorn webhook_server:app --host 0.0.0.0 --port 8090
then expose it with a Cloudflare Tunnel and register the public URL as the
webhook target in the Helius / Alchemy dashboard (register_webhooks.py /
watch_tunnel.py do this automatically).
"""

import asyncio
import csv
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

from fastapi import FastAPI, Request

from chains.evm import parse_transfer as parse_evm_transfer
from chains.solana import parse_transfer as parse_solana_transfer
from instant_trigger import maybe_trigger_instant_analysis
from store import get_connection, save_whale_event

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_CSV = os.path.join(HERE, "watchlist.csv")
DASHBOARD_URL_JSON = os.path.join(HERE, "dashboard_url.json")

sys.path.insert(0, os.path.join(os.path.dirname(HERE), "coin_finder"))
from entry_price import (  # noqa: E402
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, fetch_live_price, format_price, refresh_with_live_price,
)
from candlestick_patterns import PATTERN_CHECK_INTERVALS, build_pattern_check_message  # noqa: E402
from support_resistance import build_support_resistance_message  # noqa: E402
from futures_pattern_signal import build_pattern_digest_message  # noqa: E402
from leverage_risk import MAX_PORTFOLIO_ALLOCATION_PCT, build_leverage_digest_message  # noqa: E402
from morning_report import get_morning_snapshot_time  # noqa: E402
from news_outcomes import HORIZONS as NEWS_HORIZONS, build_report as build_news_report, resolve_matured as resolve_matured_news  # noqa: E402
from portfolio_ledger import (  # noqa: E402
    list_open_positions, record_close, record_open, total_committed_margin,
)
from quick_scalp_signal import QUICK_SCALP_ALLOWED_LEVERAGES, build_quick_scalp_message  # noqa: E402
from pattern_watch import DEFAULT_PATTERN_WATCH_INTERVAL, start_pattern_watch, stop_pattern_watch  # noqa: E402
from quick_scalp_watch import start_watch, stop_watch  # noqa: E402
from scalp_pattern_signal import DEFAULT_TOP_N, build_scalp_digest_message  # noqa: E402
from outcomes import get_connection as get_predictions_connection  # noqa: E402
from telegram_alert import (  # noqa: E402
    LABEL_EMOJI, build_digest_message, load_env, load_green_list, send_telegram_message,
)
from universe import get_universe  # noqa: E402

ACCURACY_REPORT_JSON = os.path.join(os.path.dirname(HERE), "coin_finder", "accuracy_report.json")

# Alchemy's Address Activity webhook identifies chains by this network string
ALCHEMY_NETWORKS = {
    "ETH_MAINNET": "ethereum",
    "ARB_MAINNET": "arbitrum",
    "BNB_MAINNET": "bsc",
    "BASE_MAINNET": "base",
}

app = FastAPI()


def load_watchlist_by_chain():
    """{chain: {lowercase_address: watchlist_row}}"""
    by_chain = {}
    if not os.path.exists(WATCHLIST_CSV):
        return by_chain
    with open(WATCHLIST_CSV, newline="") as f:
        for row in csv.DictReader(f):
            by_chain.setdefault(row["chain"], {})[row["address"].lower()] = row
    return by_chain


@app.post("/webhooks/helius")
async def helius_webhook(request: Request):
    payload = await request.json()
    watchlist = load_watchlist_by_chain().get("solana", {})
    conn = get_connection()
    saved = 0
    try:
        for txn in payload:
            for transfer in txn.get("tokenTransfers", []):
                for account_field in ("fromUserAccount", "toUserAccount"):
                    address = transfer.get(account_field)
                    wallet_row = watchlist.get((address or "").lower())
                    if not wallet_row:
                        continue
                    tx = {
                        "signature": txn.get("signature"),
                        "timestamp": txn.get("timestamp"),
                        "direction": "in" if account_field == "toUserAccount" else "out",
                        "mint": transfer.get("mint"),
                        "symbol": None,
                        "amount": transfer.get("tokenAmount"),
                    }
                    whale_event = parse_solana_transfer(wallet_row, tx)
                    save_whale_event(whale_event, conn=conn)
                    maybe_trigger_instant_analysis(whale_event)
                    saved += 1
    finally:
        conn.close()
    return {"saved": saved}


@app.post("/webhooks/alchemy")
async def alchemy_webhook(request: Request):
    payload = await request.json()
    event = payload.get("event", {})
    chain = ALCHEMY_NETWORKS.get(event.get("network"))
    if chain is None:
        return {"saved": 0, "error": f"unknown network {event.get('network')!r}"}

    watchlist = load_watchlist_by_chain().get(chain, {})
    conn = get_connection()
    saved = 0
    try:
        for activity in event.get("activity", []):
            if activity.get("category") != "erc20":
                continue
            for side_address in (activity.get("fromAddress"), activity.get("toAddress")):
                wallet_row = watchlist.get((side_address or "").lower())
                if not wallet_row:
                    continue
                raw_contract = activity["rawContract"]
                # the webhook carries no per-activity timestamp, only the
                # delivery time (createdAt) -- close enough since this is realtime
                created_at = payload.get("createdAt")
                if created_at:
                    received_epoch = datetime.datetime.fromisoformat(
                        created_at.replace("Z", "+00:00")
                    ).timestamp()
                else:
                    received_epoch = time.time()
                tx = {
                    "to": activity["toAddress"],
                    "from": activity["fromAddress"],
                    "value": str(int(raw_contract["rawValue"], 16)),
                    "tokenDecimal": str(raw_contract["decimals"]),
                    "tokenSymbol": activity["asset"],
                    "contractAddress": raw_contract["address"],
                    "timeStamp": str(int(received_epoch)),
                    "hash": activity["hash"],
                }
                whale_event = parse_evm_transfer(chain, wallet_row, tx)
                save_whale_event(whale_event, conn=conn)
                maybe_trigger_instant_analysis(whale_event)
                saved += 1
    finally:
        conn.close()
    return {"saved": saved}


def get_dashboard_url():
    if not os.path.exists(DASHBOARD_URL_JSON):
        return None
    with open(DASHBOARD_URL_JSON) as f:
        return json.load(f).get("url")


def handle_status_accuracy():
    dashboard_url = get_dashboard_url()
    lines = [f"\U0001f4ca Dashboard: {dashboard_url}" if dashboard_url
             else "\U0001f4ca Dashboard link not ready yet (tunnel still starting)"]

    if not os.path.exists(ACCURACY_REPORT_JSON):
        lines.append("\nNo accuracy_report.json yet -- the pipeline hasn't run evaluate_accuracy.py.")
        return "\n".join(lines)

    with open(ACCURACY_REPORT_JSON) as f:
        report = json.load(f)
    lines.append("")
    for horizon, data in report.get("by_horizon", {}).items():
        if data["resolved"] == 0:
            lines.append(f"{horizon}: no matured predictions yet")
            continue
        parts = [f"{label} n={s['count']} hit={s['hit_rate']:.0%}"
                  for label, s in data["by_label"].items()]
        lines.append(f"{horizon}: " + "  ".join(parts))
    return "\n".join(lines)


def handle_news_accuracy():
    """news_alert.py has no predicted direction (no sentiment scoring), so
    this reports MAGNITUDE only -- avg/avg-abs return after a coin got
    news-flagged, not a hit rate. No baseline/control comparison yet (see
    news_outcomes.py's module docstring) -- a nonzero number here doesn't
    by itself prove news causes outsized moves."""
    try:
        resolve_matured_news()
    except Exception as e:
        print(f"news_outcomes resolve_matured failed: {e} -- continuing")

    report = build_news_report()
    lines = ["\U0001f4f0 News-alert follow-on price moves (magnitude only, "
             "no predicted direction -- NOT a hit rate, no baseline comparison yet):"]
    for horizon in NEWS_HORIZONS:
        stats = report.get(horizon, {"n": 0})
        if stats["n"] == 0:
            lines.append(f"{horizon}: no resolved alerts yet")
            continue
        lines.append(f"{horizon}: n={stats['n']} avg_return={stats['avg_return_pct']:+.2f}% "
                      f"avg_abs_return={stats['avg_abs_return_pct']:.2f}%")
    return "\n".join(lines)


def handle_list():
    """Today's morning (first) run's actionable LONG/SHORT coins only --
    matches the hourly digest's format, skips YELLOW (no clear trade)."""
    conn = get_predictions_connection()
    snapshot_time = get_morning_snapshot_time(conn)
    if snapshot_time is None:
        conn.close()
        return "No predictions logged yet today -- the pipeline hasn't run combine.py."

    rows = conn.execute(
        """SELECT symbol, label, direction, close_price, entry_low, entry_high, stop_loss, take_profit
           FROM predictions WHERE snapshot_time = ? AND label IN ('GREEN', 'RED')
           ORDER BY final_score DESC""",
        (snapshot_time,),
    ).fetchall()
    conn.close()

    dt = datetime.datetime.fromtimestamp(snapshot_time, datetime.timezone.utc)
    lines = [f"\U0001f4cb Morning picks ({dt:%Y-%m-%d %H:%M} UTC)"]
    if not rows:
        lines.append("\nNo clear LONG or SHORT signals this morning (all YELLOW/mixed).")
        return "\n".join(lines)

    for symbol, label, direction, close_price, entry_low, entry_high, stop_loss, take_profit in rows:
        emoji = "\U0001f7e2" if label == "GREEN" else "\U0001f534"
        lines.append(f"\n{emoji} {direction or label} {symbol}  (price then: {format_price(close_price)})")
        if entry_low is None:
            lines.append("(logged before entry-price tracking was added -- no levels recorded)")
            continue
        at_entry = (close_price <= entry_high) if label == "GREEN" else (close_price >= entry_low)
        if at_entry:
            lines.append(f"Entry: {format_price(entry_low)} - {format_price(entry_high)}  (at entry zone)")
            lines.append(f"TP: {format_price(take_profit)}  SL: {format_price(stop_loss)}")
        else:
            lines.append(f"Pullback entry: {format_price(entry_low)} - {format_price(entry_high)} (may not get hit)")
            lines.append(f"  -> TP {format_price(take_profit)}  SL {format_price(stop_loss)}")
            sign = 1 if label == "GREEN" else -1
            lines.append(f"If bought at {format_price(close_price)} instead:")
            lines.append(f"  -> TP {format_price(close_price * (1 + sign * TAKE_PROFIT_PCT))}  "
                          f"SL {format_price(close_price * (1 - sign * STOP_LOSS_PCT))}")
    return "\n".join(lines)


BINANCE_SPOT_API = "https://api.binance.com"
# Max allowed per aggTrades call -- the most recent COINS_TRADE_LIMIT trades
# for that symbol, NOT a fixed time window. A busy pair (BTC) might only
# span a few seconds of real time; a quiet one might span hours -- the
# span is shown in the output so that's visible, not hidden.
COINS_TRADE_LIMIT = 1000
COINS_DISPLAY_LIMIT = 20


def _biggest_recent_trade(symbol, limit=COINS_TRADE_LIMIT):
    """Live from Binance's own aggTrades endpoint, no local storage involved
    at all -- fetches the most recent `limit` trades for one symbol right
    now and returns the single biggest by usd_value, or None on any
    failure/empty response."""
    url = f"{BINANCE_SPOT_API}/api/v3/aggTrades?symbol={symbol}&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            raw = json.loads(resp.read())
    except Exception:
        return None
    if not raw:
        return None

    biggest = None
    for t in raw:
        usd_value = float(t["p"]) * float(t["q"])
        if biggest is None or usd_value > biggest["usd_value"]:
            biggest = {"usd_value": usd_value, "side": "SELL" if t["m"] else "BUY"}
    biggest["trade_count"] = len(raw)
    biggest["span_seconds"] = (raw[-1]["T"] - raw[0]["T"]) / 1000
    return biggest


def handle_coins():
    """Live snapshot straight from Binance, no stored/accumulated history
    involved -- for every coin in the current volume-filtered universe,
    fetches the most recent COINS_TRADE_LIMIT trades (one aggTrades call
    per symbol, at request time) and finds the single biggest by
    usd_value. Sorted descending -- "biggest transaction right now", not a
    technical/ml/whale composite score (that's /list) and not whale_events
    history (that's gone from this command entirely as of 2026-11-09, at
    the user's request -- it only reflected whatever our own listener
    happened to catch since it started, not a true live picture)."""
    symbols = [c["symbol"] for c in get_universe()]

    results = []
    for symbol in symbols:
        biggest = _biggest_recent_trade(symbol)
        if biggest is not None:
            results.append((symbol, biggest))

    if not results:
        return "Couldn't fetch live trade data from Binance right now -- try again shortly."

    results.sort(key=lambda r: r[1]["usd_value"], reverse=True)

    lines = [f"\U0001f40b Binance biggest recent trade, live (scanned {len(results)} coins)"]
    for symbol, b in results[:COINS_DISPLAY_LIMIT]:
        emoji = "\U0001f7e2" if b["side"] == "BUY" else "\U0001f534"
        span = f"{b['span_seconds'] / 60:.0f}min" if b["span_seconds"] >= 60 else f"{b['span_seconds']:.0f}s"
        lines.append(f"{emoji} {symbol:<10} ${b['usd_value']:,.0f}  ({b['trade_count']} trades / last {span})")
    return "\n".join(lines)


def handle_calc(price, leverage, pct):
    """/calc_<price>_<leverage>_<pct> -- ROI%->price-distance calculator,
    added 2026-11-13 after repeated manual "0.4787 at 50x, need to reduce
    30% ROI, what price is that" questions during live trading. price_move_pct
    = roi_pct / leverage (same convention as quick_scalp_levels.py/
    scalp_levels.py). Shows both directions since which one is "profit" vs
    "loss" depends on LONG/SHORT, not on the number alone."""
    distance_pct = pct / leverage
    up_price = price * (1 + distance_pct / 100)
    down_price = price * (1 - distance_pct / 100)
    return (
        f"\U0001f4d0 Price calculator\n"
        f"Entry: {price:.6g}  Leverage: {leverage}x  ROI move: {pct:g}%\n"
        f"(price distance: {distance_pct:.3f}%)\n\n"
        f"⬆️ Up:   {up_price:.6g}  (LONG profit / SHORT loss)\n"
        f"⬇️ Down: {down_price:.6g}  (SHORT profit / LONG loss)"
    )


def handle_open(symbol, margin_usd):
    """/open_<symbol>_<margin> -- self-reported: tells the bot you opened a
    leveraged position so position_sizing()'s portfolio cap (see
    leverage_risk.py's MAX_PORTFOLIO_ALLOCATION_PCT) can account for it on
    the next suggested size. None of the three signal systems are connected
    to a Binance API key -- this is the only way they know what's open."""
    record_open(symbol, margin_usd)
    total = total_committed_margin()
    return f"✅ Logged {symbol}: ${margin_usd:,.2f} margin. Total committed: ${total:,.2f}."


def handle_close(symbol):
    existed = record_close(symbol)
    total = total_committed_margin()
    if not existed:
        return f"{symbol} wasn't logged as open -- nothing to close. Total committed: ${total:,.2f}."
    return f"✅ Closed {symbol}. Total committed: ${total:,.2f}."


def handle_positions():
    positions = list_open_positions()
    total = total_committed_margin()
    env = load_env()
    account_equity = float(env["LEVERAGE_ACCOUNT_EQUITY_USDT"]) if env.get("LEVERAGE_ACCOUNT_EQUITY_USDT") else None

    lines = ["\U0001f4bc Self-reported open positions (not read from Binance -- see /open, /close)"]
    if not positions:
        lines.append("\nNothing logged as open.")
    else:
        for symbol, p in sorted(positions.items()):
            age_hours = (time.time() - p["opened_at"]) / 3600
            lines.append(f"{symbol}: ${p['margin_usd']:,.2f} margin  (opened {age_hours:.1f}h ago)")
        lines.append(f"\nTotal committed: ${total:,.2f}")

    if account_equity:
        cap = account_equity * MAX_PORTFOLIO_ALLOCATION_PCT
        lines.append(f"Portfolio cap: ${cap:,.2f} ({MAX_PORTFOLIO_ALLOCATION_PCT:.0%} of ${account_equity:,.2f} equity)  "
                      f"-- ${max(cap - total, 0):,.2f} available")
    else:
        lines.append("\nSet LEVERAGE_ACCOUNT_EQUITY_USDT in .env to see cap usage as a percentage.")
    return "\n".join(lines)


COIN_FINDER_DIR = os.path.join(os.path.dirname(HERE), "coin_finder")


def _apply_live_price_refresh(payload):
    """Refresh current_price/distance/TP-SL for each actionable coin against a
    live Binance price -- the EMA-based entry zone itself barely moves within
    an hour, but the price you'd actually act at does."""
    for coin in payload["coins"]:
        if coin["label"] not in ("GREEN", "RED") or coin["entry"] is None:
            continue
        live_price = fetch_live_price(coin["symbol"])
        if live_price is not None:
            coin["entry"] = refresh_with_live_price(coin["entry"], live_price, coin["label"] == "GREEN")
    return payload


def handle_now():
    """Same trade-signals format as the hourly digest, but with a fresh price
    fetched live for each actionable coin. Does NOT refresh the coin list/
    labels themselves -- those still come from the last hourly combine.py run.
    Used as a fast fallback; the real /now command path re-runs the fresh
    pipeline instead (see refresh_universe_and_scores)."""
    if not os.path.exists(os.path.join(COIN_FINDER_DIR, "green_list.json")):
        return "No green_list.json yet -- the pipeline hasn't run combine.py."
    return build_digest_message(_apply_live_price_refresh(load_green_list()))


def refresh_green_list_payload():
    """Re-scans the Binance universe and recomputes technical scores (fresh
    candles) + whale scores (from already-collected whale_events.db, no new
    network calls) + the combined green_list.json -- i.e. a genuinely fresh
    coin list and labels, not just a live price patched onto stale ones.
    Skips re-scanning on-chain whale wallets and retraining the ML model
    (both slow/network-heavy and not needed for "did the coin list/price
    change" freshness) -- that's still the hourly pipeline's job. Does
    re-score ml_score against the freshly-downloaded candles using the
    already-trained model (cheap inference, no retrain) so final_score
    isn't silently a third stale between hourly retrains."""
    subprocess.run([sys.executable, "technical.py"], cwd=COIN_FINDER_DIR, check=True, timeout=180)
    subprocess.run([sys.executable, "ml_model.py", "--predict-only"], cwd=COIN_FINDER_DIR, check=True, timeout=60)
    subprocess.run([sys.executable, "score.py"], cwd=HERE, check=True, timeout=30)
    subprocess.run([sys.executable, "combine.py"], cwd=COIN_FINDER_DIR, check=True, timeout=30)
    return _apply_live_price_refresh(load_green_list())


def refresh_universe_and_scores():
    return build_digest_message(refresh_green_list_payload())


def refresh_universe_and_scores_leverage(leverage, account_equity=None, risk_pct=0.01):
    """Same fresh pipeline as refresh_universe_and_scores(), but formatted as
    leverage-derived SL/TP/liquidation/position-size instead of the flat spot
    3%/4% -- see leverage_risk.py for why those don't carry over to leverage."""
    return build_leverage_digest_message(refresh_green_list_payload(), leverage,
                                          account_equity=account_equity, risk_pct=risk_pct)


async def _run_now_refresh_and_reply(chat_id, bot_token):
    try:
        message = await asyncio.get_event_loop().run_in_executor(None, refresh_universe_and_scores)
    except Exception as e:
        message = f"Refresh failed: {e}"
    send_telegram_message(message, bot_token, chat_id)


async def _run_coins_and_reply(chat_id, bot_token):
    """handle_coins() now does ~20 live Binance HTTP calls (~15s total) --
    run in an executor like the other slow commands so it doesn't block the
    whole async server (including Telegram's own webhook delivery and
    /health) for that whole window, same reasoning as _run_now_refresh_and_reply."""
    try:
        message = await asyncio.get_event_loop().run_in_executor(None, handle_coins)
    except Exception as e:
        message = f"/coins failed: {e}"
    send_telegram_message(message, bot_token, chat_id)


async def _run_now_leverage_refresh_and_reply(chat_id, bot_token, leverage):
    env = load_env()
    account_equity = float(env["LEVERAGE_ACCOUNT_EQUITY_USDT"]) if env.get("LEVERAGE_ACCOUNT_EQUITY_USDT") else None
    risk_pct = float(env.get("LEVERAGE_RISK_PCT", "1")) / 100
    try:
        message = await asyncio.get_event_loop().run_in_executor(
            None, refresh_universe_and_scores_leverage, leverage, account_equity, risk_pct,
        )
    except Exception as e:
        message = f"Refresh failed: {e}"
    send_telegram_message(message, bot_token, chat_id)


async def _run_futures_pattern_refresh_and_reply(chat_id, bot_token, leverage):
    """System 2 -- candlestick-pattern-only futures signal, independent of
    the trend/whale/ml pipeline above. See coin_finder/futures_pattern_signal.py."""
    env = load_env()
    account_equity = float(env["LEVERAGE_ACCOUNT_EQUITY_USDT"]) if env.get("LEVERAGE_ACCOUNT_EQUITY_USDT") else None
    risk_pct = float(env.get("LEVERAGE_RISK_PCT", "1")) / 100
    try:
        message = await asyncio.get_event_loop().run_in_executor(
            None, build_pattern_digest_message, leverage, account_equity, risk_pct,
        )
    except Exception as e:
        message = f"Pattern scan failed: {e}"
    send_telegram_message(message, bot_token, chat_id)


DEFAULT_FUTURES_SYMBOL_LEVERAGE = 20  # matches futures_pattern_signal.py's own CLI default


async def _run_futures_pattern_symbol_refresh_and_reply(chat_id, bot_token, symbol):
    """Single-symbol version of /futures_<leverage> -- skips the universe
    scan, just checks this one symbol's latest 1h/4h/1d futures candle."""
    env = load_env()
    account_equity = float(env["LEVERAGE_ACCOUNT_EQUITY_USDT"]) if env.get("LEVERAGE_ACCOUNT_EQUITY_USDT") else None
    risk_pct = float(env.get("LEVERAGE_RISK_PCT", "1")) / 100
    try:
        message = await asyncio.get_event_loop().run_in_executor(
            None, build_pattern_digest_message, DEFAULT_FUTURES_SYMBOL_LEVERAGE,
            account_equity, risk_pct, [symbol],
        )
    except Exception as e:
        message = f"Pattern scan failed: {e}"
    send_telegram_message(message, bot_token, chat_id)


async def _run_pattern_check_and_reply(chat_id, bot_token, symbol, interval):
    """/pattern_<symbol>_<interval> -- pure candlestick-pattern diagnostic,
    no SL/TP/leverage, independent of every trading system above. See
    candlestick_patterns.py's build_pattern_check_message()."""
    try:
        message = await asyncio.get_event_loop().run_in_executor(
            None, build_pattern_check_message, symbol, interval,
        )
    except Exception as e:
        message = f"Pattern check failed: {e}"
    send_telegram_message(message, bot_token, chat_id, parse_mode="HTML")


async def _run_support_resistance_and_reply(chat_id, bot_token, symbol):
    """/sr_<symbol> -- support/resistance on 1h and 4h candles, pure
    diagnostic, independent of every trading system above. See
    support_resistance.py's build_support_resistance_message()."""
    try:
        message = await asyncio.get_event_loop().run_in_executor(
            None, build_support_resistance_message, symbol,
        )
    except Exception as e:
        message = f"Support/resistance check failed: {e}"
    send_telegram_message(message, bot_token, chat_id, parse_mode="HTML")


async def _run_quick_scalp_and_reply(chat_id, bot_token, symbol, leverage):
    """Quick scalp -- 15m fast momentum signal, one symbol + leverage per
    call, e.g. /scalp_btc_50. See coin_finder/quick_scalp_signal.py.
    Deliberately separate from system 3 (/fut_) -- own tighter SL/TP,
    own looser/faster trigger, zero outcome history."""
    env = load_env()
    account_equity = float(env["LEVERAGE_ACCOUNT_EQUITY_USDT"]) if env.get("LEVERAGE_ACCOUNT_EQUITY_USDT") else None
    risk_pct = float(env.get("LEVERAGE_RISK_PCT", "1")) / 100
    try:
        message = await asyncio.get_event_loop().run_in_executor(
            None, build_quick_scalp_message, symbol, leverage, account_equity, risk_pct,
        )
    except Exception as e:
        message = f"Quick scalp scan failed: {e}"
    send_telegram_message(message, bot_token, chat_id, parse_mode="HTML")


async def _run_scalp_signal_refresh_and_reply(chat_id, bot_token, symbol, leverage):
    """System 3 -- 1h/4h quick-trade signal (reversal or breakout path), one
    symbol + leverage per call, e.g. /fut_btc_50. See
    coin_finder/scalp_pattern_signal.py. Independent of systems 1 and 2."""
    env = load_env()
    account_equity = float(env["LEVERAGE_ACCOUNT_EQUITY_USDT"]) if env.get("LEVERAGE_ACCOUNT_EQUITY_USDT") else None
    risk_pct = float(env.get("LEVERAGE_RISK_PCT", "1")) / 100
    try:
        message = await asyncio.get_event_loop().run_in_executor(
            None, build_scalp_digest_message, leverage, account_equity, risk_pct, [symbol],
        )
    except Exception as e:
        message = f"Scalp signal scan failed: {e}"
    send_telegram_message(message, bot_token, chat_id, parse_mode="HTML")


async def _run_scalp_signal_all_refresh_and_reply(chat_id, bot_token, leverage):
    """Full-universe version of /fut_<symbol>_<leverage> -- e.g. /fut_all_20
    scans every tracked symbol (DEFAULT_TOP_N) for a confirmed system 3
    signal instead of just one, falling back to the single best lean across
    all of them if nothing's fully confirmed yet (same fallback behavior
    build_scalp_digest_message already has for a multi-symbol scan)."""
    env = load_env()
    account_equity = float(env["LEVERAGE_ACCOUNT_EQUITY_USDT"]) if env.get("LEVERAGE_ACCOUNT_EQUITY_USDT") else None
    risk_pct = float(env.get("LEVERAGE_RISK_PCT", "1")) / 100
    try:
        message = await asyncio.get_event_loop().run_in_executor(
            None, build_scalp_digest_message, leverage, account_equity, risk_pct,
        )
    except Exception as e:
        message = f"Scalp signal scan failed: {e}"
    send_telegram_message(message, bot_token, chat_id, parse_mode="HTML")


NOW_LEVERAGE_RE = re.compile(r"^/now_(\d{1,4})x$")
# /fut_all_20 -- system 3 full-universe scan at a given leverage, as opposed
# to FUT_SYMBOL_LEVERAGE_RE's single-symbol check. Must be matched first --
# "all" would otherwise also match FUT_SYMBOL_LEVERAGE_RE's symbol group.
FUT_ALL_RE = re.compile(r"^/fut_all_(\d{1,4})$")
# /fut_btc_50 -- system 3's single combined symbol+leverage command, as
# opposed to system 2's two-command /futures_<leverage> + /futures_<symbol>.
FUT_SYMBOL_LEVERAGE_RE = re.compile(r"^/fut_([a-zA-Z]{1,15})_(\d{1,4})$")
# /scalp_btc_50 -- quick scalp (coin_finder/quick_scalp_signal.py), a
# separate fast/frequent engine from /fut_ (system 3).
SCALP_SYMBOL_LEVERAGE_RE = re.compile(r"^/scalp_([a-zA-Z]{1,15})_(\d{1,4})$")
FUTURES_PATTERN_RE = re.compile(r"^/futures_(\d{1,4})$")
# letters-only suffix -> single-symbol check (e.g. /futures_btc), as opposed
# to FUTURES_PATTERN_RE's digits-only suffix (full-universe scan at that
# leverage) -- the two never collide since a symbol can't be pure digits.
FUTURES_SYMBOL_RE = re.compile(r"^/futures_([a-zA-Z]{1,15})$")
# /open_btc_500 -- self-reported open position (symbol + margin in USD, see
# portfolio_ledger.py). Decimal margin allowed (e.g. /open_btc_250.5).
OPEN_POSITION_RE = re.compile(r"^/open_([a-zA-Z]{1,15})_(\d+(?:\.\d+)?)$")
CLOSE_POSITION_RE = re.compile(r"^/close_([a-zA-Z]{1,15})$")
# /calc_<price>_<leverage>_<roi_pct> e.g. /calc_0.4787_50_30
PATTERN_RE = re.compile(r"^/pattern_([a-zA-Z]{1,15})_(" + "|".join(PATTERN_CHECK_INTERVALS) + r")$")
SR_RE = re.compile(r"^/sr_([a-zA-Z]{1,15})$")
CALC_RE = re.compile(r"^/calc_(\d+(?:\.\d+)?)_(\d{1,4})_(\d+(?:\.\d+)?)$")
WATCH_RE = re.compile(r"^/watch_([a-zA-Z]{1,15})$")
UNWATCH_RE = re.compile(r"^/unwatch_([a-zA-Z]{1,15})$")
WATCH_PATTERN_RE = re.compile(r"^/watch_pattern_([a-zA-Z]{1,15})(?:_(" + "|".join(PATTERN_CHECK_INTERVALS) + r"))?$")
UNWATCH_PATTERN_RE = re.compile(r"^/unwatch_pattern_([a-zA-Z]{1,15})$")

def handle_help():
    """Plain-text command reference -- kept in one place so a new command
    added above should get a line added here too, same convention as
    TELEGRAM_COMMANDS itself."""
    return "\n".join([
        "Green Coin finder:",
        "/now -- rescan + recompute GREEN/YELLOW/RED for the whole universe",
        "/now_<leverage>x -- same, plus SL/TP/liquidation at that leverage (e.g. /now_20x)",
        "/coins -- live Binance trade scan per coin (~15s)",
        "/list -- today's actionable LONG/SHORT coins from the morning run",
        "",
        "Quick scalp (fade engine, live-traded system):",
        "/scalp_<symbol>_<leverage> -- 15m fast momentum signal (leverage 50 or 100, e.g. /scalp_btc_50)",
        "",
        "System 3 (/fut_, on-demand only -- not currently background-traded):",
        "/fut_<symbol>_<leverage> -- 1h/4h quick-trade signal, reversal or breakout (e.g. /fut_eth_50)",
        "/fut_all_<leverage> -- same, across the whole tracked universe",
        "",
        "System 2 (/futures_, on-demand only -- not currently background-traded):",
        "/futures_<leverage> -- full-universe reversal-pattern scan at that leverage",
        "/futures_<symbol> -- single-symbol reversal-pattern check (e.g. /futures_sol)",
        "",
        "Candlestick pattern + support/resistance (on demand):",
        "/pattern_<symbol>_<interval> -- candlestick pattern check "
        "(interval: 1m/3m/5m/15m/30m/1h/2h/4h/6h/8h/12h/1d, e.g. /pattern_btc_1h)",
        "/sr_<symbol> -- 1h/4h support/resistance levels (e.g. /sr_btc)",
        "",
        "Alerts -- watch/unwatch a symbol:",
        "/watch_<symbol> / /unwatch_<symbol> -- price-level watch (e.g. /watch_btc)",
        "/watch_pattern_<symbol>[_<interval>] / /unwatch_pattern_<symbol> -- "
        "candlestick pattern watch (default interval 30m, e.g. /watch_pattern_eth_1h)",
        "",
        "Position tracking + math:",
        "/open_<symbol>_<margin_usd> -- log a self-reported open position (e.g. /open_btc_500)",
        "/close_<symbol> -- close a logged position",
        "/positions -- list open logged positions",
        "/calc_<price>_<leverage>_<roi_pct> -- SL/TP price for a given ROI target (e.g. /calc_0.4787_50_30)",
        "",
        "Status:",
        "/status_accuracy -- dashboard link + prediction accuracy by horizon",
        "/news_accuracy -- avg price move after a coin got news-flagged (magnitude only, no hit rate)",
        "/help -- this list",
    ])


TELEGRAM_COMMANDS = {
    "/status_accuracy": handle_status_accuracy,
    "/news_accuracy": handle_news_accuracy,
    "/list": handle_list,
    "/positions": handle_positions,
    "/help": handle_help,
}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    message = update.get("message", {})
    text = (message.get("text") or "").strip()
    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        return {"ok": True}

    bot_token = load_env().get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        return {"ok": True}

    if text == "/now":
        send_telegram_message("⏳ Rescanning the universe + recomputing scores (under a minute)...",
                               bot_token, chat_id)
        asyncio.create_task(_run_now_refresh_and_reply(chat_id, bot_token))
        return {"ok": True}

    if text == "/coins":
        send_telegram_message("⏳ Scanning live Binance trades per coin (~15s)...", bot_token, chat_id)
        asyncio.create_task(_run_coins_and_reply(chat_id, bot_token))
        return {"ok": True}

    leverage_match = NOW_LEVERAGE_RE.match(text)
    if leverage_match:
        leverage = int(leverage_match.group(1))
        send_telegram_message(f"⏳ Rescanning + computing {leverage}x SL/TP/liquidation levels (under a minute)...",
                               bot_token, chat_id)
        asyncio.create_task(_run_now_leverage_refresh_and_reply(chat_id, bot_token, leverage))
        return {"ok": True}

    futures_pattern_match = FUTURES_PATTERN_RE.match(text)
    if futures_pattern_match:
        leverage = int(futures_pattern_match.group(1))
        send_telegram_message(
            f"⏳ Scanning futures candles for reversal patterns at {leverage}x (pattern-only, "
            "no trend/whale/ml -- separate from /now)...",
            bot_token, chat_id,
        )
        asyncio.create_task(_run_futures_pattern_refresh_and_reply(chat_id, bot_token, leverage))
        return {"ok": True}

    futures_symbol_match = FUTURES_SYMBOL_RE.match(text)
    if futures_symbol_match:
        symbol = f"{futures_symbol_match.group(1).upper()}USDT"
        send_telegram_message(
            f"⏳ Checking {symbol} futures 1h/4h/1d candles for a reversal pattern...",
            bot_token, chat_id,
        )
        asyncio.create_task(_run_futures_pattern_symbol_refresh_and_reply(chat_id, bot_token, symbol))
        return {"ok": True}

    fut_all_match = FUT_ALL_RE.match(text)
    if fut_all_match:
        leverage = int(fut_all_match.group(1))
        send_telegram_message(
            f"⏳ Checking all {DEFAULT_TOP_N} tracked symbols' 1h/4h candles for a quick-trade "
            f"signal at {leverage}x (reversal or breakout, separate from /now and /futures_...)...",
            bot_token, chat_id,
        )
        asyncio.create_task(_run_scalp_signal_all_refresh_and_reply(chat_id, bot_token, leverage))
        return {"ok": True}

    fut_match = FUT_SYMBOL_LEVERAGE_RE.match(text)
    if fut_match:
        symbol = f"{fut_match.group(1).upper()}USDT"
        leverage = int(fut_match.group(2))
        send_telegram_message(
            f"⏳ Checking {symbol} 1h/4h candles for a quick-trade signal at {leverage}x "
            "(reversal or breakout, separate from /now and /futures_...)...",
            bot_token, chat_id,
        )
        asyncio.create_task(_run_scalp_signal_refresh_and_reply(chat_id, bot_token, symbol, leverage))
        return {"ok": True}

    scalp_match = SCALP_SYMBOL_LEVERAGE_RE.match(text)
    if scalp_match:
        symbol = f"{scalp_match.group(1).upper()}USDT"
        leverage = int(scalp_match.group(2))
        if leverage not in QUICK_SCALP_ALLOWED_LEVERAGES:
            allowed = "/".join(f"{lv}x" for lv in QUICK_SCALP_ALLOWED_LEVERAGES)
            send_telegram_message(
                f"⚠️ /scalp_ only supports {allowed} -- got {leverage}x.",
                bot_token, chat_id,
            )
            return {"ok": True}
        send_telegram_message(
            f"⏳ Checking {symbol} 15m candles for a fast momentum signal at {leverage}x "
            "(quick scalp -- separate from /fut_)...",
            bot_token, chat_id,
        )
        asyncio.create_task(_run_quick_scalp_and_reply(chat_id, bot_token, symbol, leverage))
        return {"ok": True}

    open_match = OPEN_POSITION_RE.match(text)
    if open_match:
        symbol = f"{open_match.group(1).upper()}USDT"
        margin_usd = float(open_match.group(2))
        send_telegram_message(handle_open(symbol, margin_usd), bot_token, chat_id)
        return {"ok": True}

    close_match = CLOSE_POSITION_RE.match(text)
    if close_match:
        symbol = f"{close_match.group(1).upper()}USDT"
        send_telegram_message(handle_close(symbol), bot_token, chat_id)
        return {"ok": True}

    pattern_match = PATTERN_RE.match(text)
    if pattern_match:
        symbol = f"{pattern_match.group(1).upper()}USDT"
        interval = pattern_match.group(2)
        send_telegram_message(
            f"⏳ Checking {symbol} {interval} candles for a candlestick pattern...",
            bot_token, chat_id,
        )
        asyncio.create_task(_run_pattern_check_and_reply(chat_id, bot_token, symbol, interval))
        return {"ok": True}

    sr_match = SR_RE.match(text)
    if sr_match:
        symbol = f"{sr_match.group(1).upper()}USDT"
        send_telegram_message(
            f"⏳ Checking {symbol} 1h/4h candles for support/resistance...",
            bot_token, chat_id,
        )
        asyncio.create_task(_run_support_resistance_and_reply(chat_id, bot_token, symbol))
        return {"ok": True}

    calc_match = CALC_RE.match(text)
    if calc_match:
        price = float(calc_match.group(1))
        leverage = int(calc_match.group(2))
        pct = float(calc_match.group(3))
        send_telegram_message(handle_calc(price, leverage, pct), bot_token, chat_id)
        return {"ok": True}

    watch_match = WATCH_RE.match(text)
    if watch_match:
        symbol = f"{watch_match.group(1).upper()}USDT"
        send_telegram_message(start_watch(symbol), bot_token, chat_id)
        return {"ok": True}

    unwatch_match = UNWATCH_RE.match(text)
    if unwatch_match:
        symbol = f"{unwatch_match.group(1).upper()}USDT"
        send_telegram_message(stop_watch(symbol), bot_token, chat_id)
        return {"ok": True}

    watch_pattern_match = WATCH_PATTERN_RE.match(text)
    if watch_pattern_match:
        symbol = f"{watch_pattern_match.group(1).upper()}USDT"
        interval = watch_pattern_match.group(2) or DEFAULT_PATTERN_WATCH_INTERVAL
        send_telegram_message(start_pattern_watch(symbol, interval), bot_token, chat_id)
        return {"ok": True}

    unwatch_pattern_match = UNWATCH_PATTERN_RE.match(text)
    if unwatch_pattern_match:
        symbol = f"{unwatch_pattern_match.group(1).upper()}USDT"
        send_telegram_message(stop_pattern_watch(symbol), bot_token, chat_id)
        return {"ok": True}

    handler = TELEGRAM_COMMANDS.get(text)
    if handler:
        send_telegram_message(handler(), bot_token, chat_id)
    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok"}
