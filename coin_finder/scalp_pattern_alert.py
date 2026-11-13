"""Polls system 3's scalp signals (scalp_pattern_signal.py) on a loop and
sends a Telegram alert only when a symbol is both (a) confirmed by either
signal path AND (b) price is actually AT the entry zone right now ("ready
to trade") -- not on every poll while the same confirmed-but-still-WAIT
signal is standing, and not the moment a pattern first confirms if price
hasn't reached the entry zone yet. A confirmed signal that's still WAIT
gets silently tracked, not pushed -- added 2026-11-06 at the user's
request so every push is immediately actionable, not "direction known, but
check back later."

The alert itself carries full entry/TP/SL/liquidation detail at both
ALERT_LEVERAGES (30x and 50x) via build_scalp_alert_message() -- "ready to
trade" on arrival, not just a pointer to run /fut_ afterward.

Default scan set is scalp_pattern_signal.py's own PRIORITY_SYMBOLS + top
DEFAULT_TOP_N by volume (BTC/ETH/SOL/XRP/BNB always, plus whatever else
ranks in the top 20 by 24h volume) -- same universe system 2 polls.

Shorter poll interval than system 2 (default 180s vs 300s) -- this system
resolves within 1-4h, not up to a day, so catching the moment price enters
the entry zone promptly matters more here even though the underlying
candles are now 1h/4h (moved off 15m 2026-11-06).

Also logs each alerted (i.e. ready-to-trade) signal to scalp_outcomes.py's
DB and resolves matured ones every poll, so accuracy can actually be
checked later instead of trading on an unvalidated signal indefinitely.

Set SCALP_PATTERN_POLL_SECONDS to change cadence. Set RUN_ONCE=1 to run
through one poll and exit (useful for testing).
"""

import json
import os
import time

from scalp_outcomes import record_signal, resolve_matured
from scalp_pattern_signal import (
    CONFIDENCE_LABELS,
    build_scalp_alert_message,
    scalp_entry_for_symbol,
    scan_scalp_signals,
    _tp_roi_for_lean,
)
from telegram_alert import load_env, send_telegram_message

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_JSON = os.path.join(HERE, "scalp_pattern_alert_state.json")
POLL_INTERVAL_SECONDS = int(os.environ.get("SCALP_PATTERN_POLL_SECONDS", 180))


def load_state():
    if not os.path.exists(STATE_JSON):
        return {}
    with open(STATE_JSON) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_JSON, "w") as f:
        json.dump(state, f)


def poll_once():
    env = load_env()
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("No TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID configured -- skipping")
        return
    # same .env convention webhook_server.py uses for /fut_'s sizing info
    account_equity = float(env["LEVERAGE_ACCOUNT_EQUITY_USDT"]) if env.get("LEVERAGE_ACCOUNT_EQUITY_USDT") else None
    risk_pct = float(env.get("LEVERAGE_RISK_PCT", "1")) / 100

    previous = load_state()
    signals = scan_scalp_signals()

    new_state = {}
    for symbol, signal in signals.items():
        is_long, method = signal["is_long"], signal["method"]
        direction = "LONG" if is_long else "SHORT"
        entry = scalp_entry_for_symbol(symbol, is_long)
        ready = entry is not None and entry["at_entry_now"]
        new_state[symbol] = {"direction": direction, "ready": ready}

        # old state file predates the ready flag -- a leftover plain
        # string value there is just treated as unknown (safe: alerts
        # fresh once ready, same as any other reset case below)
        prev = previous.get(symbol)
        prev = prev if isinstance(prev, dict) else {}
        # only alert (and log) when newly ready -- a confirmed signal
        # that's still WAIT is tracked silently; disappearing and later
        # reappearing ready (even same direction) alerts again, since
        # that's a genuinely new occurrence, same convention as the old
        # direction-only dedup.
        if ready and (prev.get("direction") != direction or not prev.get("ready")):
            message = build_scalp_alert_message(
                symbol, signal, account_equity=account_equity, risk_pct=risk_pct,
            )
            send_telegram_message(message, token, chat_id, parse_mode="HTML")
            confidence = CONFIDENCE_LABELS.get(_tp_roi_for_lean(signal))
            record_signal(symbol, direction, signal["close_price"], method, confidence=confidence)
            print(f"Alerted (ready to trade): {direction} {symbol} ({method}, {confidence})")

    save_state(new_state)

    try:
        resolved = resolve_matured()
        if resolved:
            print(f"Resolved {resolved} matured signal/horizon pair(s)")
    except Exception as e:
        print(f"resolve_matured failed: {e} -- continuing")


if __name__ == "__main__":
    poll_once()
    if os.environ.get("RUN_ONCE"):
        raise SystemExit(0)
    while True:
        time.sleep(POLL_INTERVAL_SECONDS)
        try:
            poll_once()
        except Exception as e:
            print(f"Poll failed: {e} -- continuing")
