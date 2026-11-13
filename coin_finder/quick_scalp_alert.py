"""Polls quick_scalp_signal.py's momentum signals on a loop and sends a
Telegram alert only when a symbol is both (a) momentum-confirmed AND (b)
price is actually AT the entry zone right now ("ready to trade") -- same
ready-gating as scalp_pattern_alert.py (/fut_), added there 2026-11-06 at
the user's request so every push is immediately actionable.

The alert itself carries full entry/TP/SL/liquidation detail at
ALERT_LEVERAGE (50x only -- 30x dropped 2026-11-10 at the user's request,
see quick_scalp_signal.py) via build_quick_scalp_alert_message() --
"ready to trade" on arrival, not just a pointer to run /scalp_ afterward.

Shorter poll interval than system 3's /fut_ alert (default 60s vs 180s) --
this system is meant to be fast/frequent (rolling order-flow trigger,
intended hold well under 1h), so catching the moment price enters the
entry zone matters even more here.

Also logs each alerted (i.e. ready-to-trade) signal to
quick_scalp_outcomes.py's DB and resolves matured ones every poll, so
accuracy can actually be checked later -- this system launched 2026-11-09
with completely unvalidated SL/TP numbers (tighter, first-guess, see
quick_scalp_signal.py's module docstring), more so than any other system
here had at its own launch.

PRO MODE Stage B shadow-logging (added 2026-11-11, see
quick_scalp_signal.py's PRO MODE section and
/Users/pebiantara/.claude/plans/cozy-waddling-peacock.md): every poll
also runs scan_quick_scalp_signals_pro() and logs any confirmed candidate
to quick_scalp_pro_outcomes.py's SEPARATE db -- SHADOW ONLY, no Telegram
alert is ever sent for a PRO MODE signal. This accumulates real forward-
looking samples under live conditions (Stage A's retroactive forward-walk
backtest can't fully substitute for this) before PRO MODE is trusted
enough to alert on for real.

Set QUICK_SCALP_POLL_SECONDS to change cadence. Set RUN_ONCE=1 to run
through one poll and exit (useful for testing).
"""

import json
import os
import time

from orderbook_snapshot import log_snapshot as log_orderbook_snapshot
from quick_scalp_outcomes import record_signal, resolve_matured
from quick_scalp_pro_outcomes import record_signal as record_signal_pro, resolve_matured as resolve_matured_pro
from quick_scalp_signal import (
    build_quick_scalp_alert_message,
    quick_scalp_entry_for_symbol,
    scan_quick_scalp_signals,
    scan_quick_scalp_signals_pro,
)
from quick_scalp_watch import check_watches
from telegram_alert import load_env, send_telegram_message

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_JSON = os.path.join(HERE, "quick_scalp_alert_state.json")
PRO_STATE_JSON = os.path.join(HERE, "quick_scalp_pro_shadow_state.json")
POLL_INTERVAL_SECONDS = int(os.environ.get("QUICK_SCALP_POLL_SECONDS", 60))


def load_state(path=STATE_JSON):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def save_state(state, path=STATE_JSON):
    with open(path, "w") as f:
        json.dump(state, f)


def poll_once_pro_shadow():
    """Stage B: compute PRO MODE signals and log any newly-confirmed one
    to quick_scalp_pro_outcomes.py -- SHADOW ONLY, never sends a Telegram
    message. Same "only log on a transition" dedup pattern as the real
    alert's poll_once(), just keyed off its own state file so it can't
    collide with/skip the real engine's dedup."""
    previous = load_state(PRO_STATE_JSON)
    signals = scan_quick_scalp_signals_pro()

    new_state = {}
    for symbol, signal in signals.items():
        direction = "LONG" if signal["is_long"] else "SHORT"
        new_state[symbol] = {"direction": direction, "score": signal["score"]}

        prev = previous.get(symbol)
        prev = prev if isinstance(prev, dict) else {}
        if prev.get("direction") != direction:
            record_signal_pro(symbol, direction, signal["close_price"], signal["score"], signal["components"])
            print(f"[PRO shadow] logged (not alerted): {direction} {symbol} score={signal['score']}")

    save_state(new_state, PRO_STATE_JSON)

    try:
        resolved = resolve_matured_pro()
        if resolved:
            print(f"[PRO shadow] resolved {resolved} matured signal/horizon pair(s)")
    except Exception as e:
        print(f"[PRO shadow] resolve_matured failed: {e} -- continuing")


def poll_once():
    env = load_env()
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("No TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID configured -- skipping")
        return
    account_equity = float(env["LEVERAGE_ACCOUNT_EQUITY_USDT"]) if env.get("LEVERAGE_ACCOUNT_EQUITY_USDT") else None
    risk_pct = float(env.get("LEVERAGE_RISK_PCT", "1")) / 100

    previous = load_state()
    signals = scan_quick_scalp_signals()

    new_state = {}
    for symbol, signal in signals.items():
        is_long = signal["is_long"]
        direction = "LONG" if is_long else "SHORT"
        entry = quick_scalp_entry_for_symbol(symbol, is_long)
        ready = entry is not None and entry["at_entry_now"]
        new_state[symbol] = {"direction": direction, "ready": ready}

        prev = previous.get(symbol)
        prev = prev if isinstance(prev, dict) else {}
        if ready and (prev.get("direction") != direction or not prev.get("ready")):
            message = build_quick_scalp_alert_message(
                symbol, signal, account_equity=account_equity, risk_pct=risk_pct,
            )
            send_telegram_message(message, token, chat_id, parse_mode="HTML")
            prediction_id = record_signal(symbol, direction, signal["close_price"])
            try:
                log_orderbook_snapshot(symbol, direction, prediction_id=prediction_id)
            except Exception as e:
                print(f"[orderbook] snapshot failed for {symbol}: {e} -- continuing")
            print(f"Alerted (ready to trade): {direction} {symbol}")

    save_state(new_state)

    try:
        resolved = resolve_matured()
        if resolved:
            print(f"Resolved {resolved} matured signal/horizon pair(s)")
    except Exception as e:
        print(f"resolve_matured failed: {e} -- continuing")


def poll_once_watch():
    """/watch_<symbol>'s poll -- checks every active pre-entry watch
    (see quick_scalp_watch.py) and sends a Telegram message only for a
    verdict change or expiry, never every poll."""
    env = load_env()
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    for symbol, message in check_watches():
        send_telegram_message(message, token, chat_id)
        print(f"[watch] {symbol}: {message}")


def poll_once_all():
    """Real alert poll + PRO MODE shadow poll, isolated from each other --
    a Telegram-config issue (poll_once()'s early return) or a PRO MODE bug
    should never take the other one down.

    PRO MODE PAUSED 2026-11-11 (gated behind QUICK_SCALP_PRO_SHADOW_ENABLED,
    default off): Stage A's 30-day forward-walk backtest (n=4023, the
    biggest/most stable sample run) showed the current structure-first
    design does NOT beat the live flat fade engine (24.6% hit rate,
    slightly negative avg return, vs the live engine's validated ~35-40%)
    -- the smaller 3-day/7-day samples' promising score>=8 tier mostly
    evaporated at scale. User chose to pause and refocus on the live
    engine (frequency + small edge) rather than keep iterating PRO MODE
    blind. All PRO MODE code/data is left in place for a later revisit,
    not deleted -- see cbot_quick_scalp_signal.md memory and
    /Users/pebiantara/.claude/plans/cozy-waddling-peacock.md."""
    try:
        poll_once()
    except Exception as e:
        print(f"Poll failed: {e} -- continuing")
    try:
        poll_once_watch()
    except Exception as e:
        print(f"[watch] poll failed: {e} -- continuing")
    if os.environ.get("QUICK_SCALP_PRO_SHADOW_ENABLED") == "1":
        try:
            poll_once_pro_shadow()
        except Exception as e:
            print(f"[PRO shadow] poll failed: {e} -- continuing")


if __name__ == "__main__":
    poll_once_all()
    if os.environ.get("RUN_ONCE"):
        raise SystemExit(0)
    while True:
        time.sleep(POLL_INTERVAL_SECONDS)
        poll_once_all()  # isolates/logs its own failures, never raises
