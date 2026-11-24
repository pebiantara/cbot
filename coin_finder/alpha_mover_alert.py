"""Binance Alpha-coin mover alert -- added 2026-11-24 at the user's
request to find volatile Binance Alpha coins specifically. Mirrors
big_mover_alert.py's tier/volume-confirm/RSI/structural-target logic
exactly (same TIERS, same thresholds, same message shape), reusing its
stateless per-symbol functions directly -- but scans
universe.get_alpha_futures_universe() (Binance's own "Alpha"-tagged
futures listings) instead of the shared _resolve_symbols() top-50 pool.

Why a separate module instead of just widening big_mover_alert's own
universe: a real gap check found 29 liquid (>=$5M volume) Alpha-tagged
coins completely missing from the shared top-50 -- crowded out by majors
(BTC/ETH/BNB/SOL) with huge volume but far less volatility. Guaranteeing
all 49+ current Alpha coins into that shared pool would roughly double
the symbol count every live service scans (the live-traded fade engine,
support_resistance_alert, reversal_watch_alert all share that same
pool) -- real added load across everything for a check that's actually
independent. This scanner touches NONE of that shared state -- own
price-history/state JSON files, own poll loop -- so it adds zero load to
anything else.

Diagnostic only -- no SL/TP, no entry, no trade direction, same
convention as big_mover_alert.py.

Set RUN_ONCE=1 to run a single poll and exit.
"""

import json
import os
import time

from big_mover_alert import (
    RSI_INTERVAL,
    overbought_oversold_status,
    structural_target,
    volume_confirmed,
)
from entry_price import fetch_live_futures_prices
from telegram_alert import load_env, send_telegram_message
from universe import get_alpha_futures_universe

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_JSON = os.path.join(HERE, "alpha_mover_alert_state.json")
PRICE_HISTORY_JSON = os.path.join(HERE, "alpha_mover_price_history.json")

POLL_INTERVAL_SECONDS = int(os.environ.get("ALPHA_MOVER_POLL_SECONDS", 60))

# Same tiers/thresholds as big_mover_alert.py's own TIERS -- this is the
# same alert, just pointed at a different universe slice, not a
# different sensitivity.
TIERS = [
    {"name": "fast", "window_seconds": 15 * 60, "threshold_pct": 0.10, "pump_only": True},
    {"name": "medium", "window_seconds": 2 * 60 * 60, "threshold_pct": 0.20},
    {"name": "slow", "window_seconds": 6 * 60 * 60, "threshold_pct": 0.35},
]
_MAX_WINDOW_SECONDS = max(t["window_seconds"] for t in TIERS)

_price_history = {}  # symbol -> [(ts, price), ...], oldest first


def load_price_history():
    if not os.path.exists(PRICE_HISTORY_JSON):
        return
    with open(PRICE_HISTORY_JSON) as f:
        raw = json.load(f)
    now = time.time()
    cutoff = now - _MAX_WINDOW_SECONDS
    for symbol, buf in raw.items():
        trimmed = [(ts, price) for ts, price in buf if ts >= cutoff]
        if trimmed:
            _price_history[symbol] = trimmed


def save_price_history():
    with open(PRICE_HISTORY_JSON, "w") as f:
        json.dump(_price_history, f)


def load_state():
    """{key: {"ts", "rsi"}} -- same shape as big_mover_alert.load_state(),
    duplicated here (not imported) because this module's state file is
    deliberately separate from big_mover_alert's own, to avoid two
    different containers racing on the same JSON file."""
    if not os.path.exists(STATE_JSON):
        return {}
    with open(STATE_JSON) as f:
        raw = json.load(f)
    return {
        key: (value if isinstance(value, dict) else {"ts": value, "rsi": None})
        for key, value in raw.items()
    }


def save_state(state):
    with open(STATE_JSON, "w") as f:
        json.dump(state, f)


def _reference_price(buf, now_ts, window_seconds):
    target = now_ts - window_seconds
    if not buf or buf[0][0] > target + POLL_INTERVAL_SECONDS:
        return None
    reference = buf[0][1]
    for ts, price in buf:
        if ts <= target + POLL_INTERVAL_SECONDS:
            reference = price
        else:
            break
    return reference


def check_tiers(symbol, now_ts, price):
    buf = _price_history.setdefault(symbol, [])
    buf.append((now_ts, price))
    cutoff = now_ts - _MAX_WINDOW_SECONDS
    while len(buf) > 1 and buf[0][0] < cutoff - POLL_INTERVAL_SECONDS:
        buf.pop(0)

    hits = []
    for tier in TIERS:
        reference = _reference_price(buf, now_ts, tier["window_seconds"])
        if reference is None:
            continue
        pct = (price - reference) / reference
        if abs(pct) < tier["threshold_pct"]:
            continue
        if tier.get("pump_only") and pct <= 0:
            continue
        hits.append({
            "tier": tier["name"], "direction": "pump" if pct > 0 else "dump",
            "pct": pct, "reference_price": reference,
        })
    return hits


def poll_once(state):
    env = load_env()
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("No TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID configured -- skipping")
        return

    symbols = [c["symbol"] for c in get_alpha_futures_universe()]
    prices = fetch_live_futures_prices()  # one request covers the whole exchange
    now = time.time()

    for symbol in symbols:
        price = prices.get(symbol)
        if price is None:
            continue
        hits = check_tiers(symbol, now, price)
        for hit in hits:
            key = f"{symbol}:{hit['tier']}:{hit['direction']}"
            window_seconds = next(t["window_seconds"] for t in TIERS if t["name"] == hit["tier"])
            if now - state.get(key, {}).get("ts", 0) < window_seconds:
                continue  # cooldown -- already alerted this tier+direction recently

            vc = volume_confirmed(symbol, window_seconds=window_seconds)
            if not vc["confirmed"]:
                print(f"[alpha mover] {symbol} {hit['direction']} ({hit['tier']}, {hit['pct']:+.1%}) "
                      f"-- no volume confirmation (ratio={vc['ratio']}), skipping")
                continue
            ratio = vc["ratio"]

            emoji = "\U0001f7e2" if hit["direction"] == "pump" else "\U0001f534"
            window_label = f"{window_seconds // 60}min" if window_seconds < 3600 else f"{window_seconds // 3600}h"
            short_symbol = symbol[:-4].lower() if symbol.endswith("USDT") else symbol.lower()

            total_usd = vc["buy_usd"] + vc["sell_usd"]
            buy_pct = vc["buy_usd"] / total_usd if total_usd > 0 else 0.5
            aggressor = "buyers" if buy_pct >= 0.5 else "sellers"
            extreme_label, extreme_price = ("high", vc["high"]) if hit["direction"] == "pump" else ("low", vc["low"])

            rsi_status = overbought_oversold_status(symbol)
            rsi_line = (
                f"RSI({RSI_INTERVAL})={rsi_status['rsi']:.1f} -- {rsi_status['label']}  "
                f"({rsi_status['ema20_extension_pct']:+.1f}% vs EMA20)\n"
            ) if rsi_status is not None else ""

            try:
                target_line = structural_target(symbol, hit["direction"], price, vc["high"] - vc["low"]) + "\n"
            except Exception:
                target_line = ""

            send_telegram_message(
                f"{emoji} ⚡ ALPHA MOVE: {symbol} {hit['direction']}ing -- {hit['pct']:+.1%} in ~{window_label} "
                f"({hit['reference_price']:g} → {price:g}), volume {ratio:.1f}x trailing average.\n"
                f"Aggression: buy=${vc['buy_usd']:,.0f}  sell=${vc['sell_usd']:,.0f}  "
                f"({aggressor} in control, {max(buy_pct, 1 - buy_pct):.0%})\n"
                f"Realized {extreme_label} with that volume so far: {extreme_price:g}\n"
                f"{rsi_line}"
                f"{target_line}"
                f"Diagnostic only -- not a trade signal. Run /pattern_{short_symbol}_15m or /sr_{short_symbol} "
                f"for more context.",
                token, chat_id,
            )
            state[key] = {"ts": now, "rsi": rsi_status["rsi"] if rsi_status is not None else None}
            print(f"[alpha mover] Alerted: {symbol} {hit['direction']} ({hit['tier']}, {hit['pct']:+.1%}, "
                  f"vol={ratio:.1f}x)")

    save_state(state)
    save_price_history()


if __name__ == "__main__":
    state = load_state()
    load_price_history()
    poll_once(state)
    if os.environ.get("RUN_ONCE"):
        raise SystemExit(0)
    while True:
        time.sleep(POLL_INTERVAL_SECONDS)
        try:
            poll_once(state)
        except Exception as e:
            print(f"Poll failed: {e} -- continuing")
