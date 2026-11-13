"""Polls system 2's pattern signals (futures_pattern_signal.py) on a loop and
sends a Telegram alert only when a symbol's signal is NEW -- just started
firing, or flipped direction -- not on every poll while the same candle is
still the latest one. Mirrors telegram_alert.py's newly_green()/
save_labels() pattern used for system 1 (green_list_prev.json), but keyed
on symbol->direction instead of symbol->label.

Scans the default set (PRIORITY_SYMBOLS + top DEFAULT_TOP_N by volume, see
futures_pattern_signal.py) -- i.e. BTC/ETH/SOL/XRP/BNB always, plus whatever
else ranks in the top 20 by 24h volume.

Also logs each new signal to pattern_outcomes.py's DB and resolves matured
ones every poll -- outcome-tracking for system 2, same idea as system 1's
predictions.db/outcomes.py, so accuracy can actually be checked later
instead of trading on an unvalidated signal indefinitely.

Set FUTURES_PATTERN_POLL_SECONDS to change cadence (default 300 = 5 min).
Set RUN_ONCE=1 to run through one poll and exit (useful for testing).
"""

import json
import os
import time

from futures_pattern_signal import scan_pattern_signals
from pattern_outcomes import record_signal, resolve_matured
from telegram_alert import load_env, send_telegram_message

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_JSON = os.path.join(HERE, "futures_pattern_alert_state.json")
POLL_INTERVAL_SECONDS = int(os.environ.get("FUTURES_PATTERN_POLL_SECONDS", 300))
# Telegram pushes muted (set in docker-compose.yml) once system 3 became
# the one actually used to trade off of -- scanning/logging to
# pattern_outcomes.py keeps running regardless, so its accuracy can still
# be checked later even while the alerts themselves are off.
ALERTS_ENABLED = os.environ.get("FUTURES_PATTERN_ALERTS_ENABLED", "1") != "0"


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

    previous = load_state()
    signals = scan_pattern_signals()

    new_state = {}
    for symbol, (is_long, interval, close_price) in signals.items():
        direction = "LONG" if is_long else "SHORT"
        new_state[symbol] = direction
        # only alert (and log) on an actual change -- a signal that's been
        # sitting there since the last poll (same candle still latest)
        # doesn't re-alert; disappearing and later reappearing (even same
        # direction) does, since that's a genuinely new occurrence.
        if previous.get(symbol) != direction:
            if ALERTS_ENABLED:
                emoji = "\U0001f7e2" if is_long else "\U0001f534"
                short_symbol = symbol[:-4].lower() if symbol.endswith("USDT") else symbol.lower()
                send_telegram_message(
                    f"{emoji} System 2 pattern alert: {direction} {symbol}\n"
                    f"Run /futures_{short_symbol} for full entry/TP/SL/liquidation detail.",
                    token, chat_id,
                )
            record_signal(symbol, direction, close_price, interval)
            print(f"{'Alerted' if ALERTS_ENABLED else 'Logged (alerts muted)'}: {direction} {symbol}")

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
