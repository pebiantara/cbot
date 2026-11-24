"""Support/resistance proximity alert -- scans the tracked universe's 4h
candles every POLL_INTERVAL_SECONDS and fires a Telegram heads-up when a
coin's current price comes within SR_ALERT_PROXIMITY_PCT of a support or
resistance level, reusing support_resistance.py's exact level logic
(breakout_patterns.next_key_level + volume_profile.volume_profile) -- same
building blocks System 3's own TP capping and the on-demand /sr_<symbol>
command already use, not a new detector. next_key_level always returns a
resistance strictly above price / support strictly below price (or None,
"clear air"), so proximity is unambiguous.

Diagnostic only: no SL/TP, no entry, no trade direction -- a coin sitting
at support isn't automatically a buy, nor is a coin at resistance
automatically a sell. This just flags "worth a look right now."

Dedup uses hysteresis, not a flat cooldown: once alerted, a side (support/
resistance) won't re-alert until price moves back out past
SR_ALERT_REARM_MULTIPLIER x the proximity threshold, then re-approaches.
This tracks the real "is price currently sitting at this level" state
instead of rate-limiting by time, which would either spam every poll
while price lingers near the level, or go silent on a genuinely new
second approach within the cooldown window.

Universe comes from quick_scalp_signal.py's own symbol resolution (the
same coins already confirmed tradable on futures) -- NOT filtered by
QUICK_SCALP_EXCLUDED_SYMBOLS, since SR is informational for any coin
(including BTC) unlike the fade engine's own entry logic, which excludes
BTC for strategy-specific reasons.

Set RUN_ONCE=1 to run a single poll and exit.
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from big_mover_alert import RSI_INTERVAL, overbought_oversold_status
from quick_scalp_signal import QUICK_SCALP_TOP_N
from scalp_pattern_signal import _resolve_symbols
from support_resistance import support_resistance_for_interval
from telegram_alert import load_env, send_telegram_message

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_JSON = os.path.join(HERE, "support_resistance_alert_state.json")

POLL_INTERVAL_SECONDS = int(os.environ.get("SR_ALERT_POLL_SECONDS", 300))
PROXIMITY_PCT = float(os.environ.get("SR_ALERT_PROXIMITY_PCT", 0.5))
REARM_MULTIPLIER = float(os.environ.get("SR_ALERT_REARM_MULTIPLIER", 2.0))
SCAN_WORKER_COUNT = 8  # same balance as quick_scalp_signal.py's own scan loop
INTERVAL = "4h"
LOOKBACK = 60  # matches support_resistance.py's own SR_INTERVALS entry for 4h


def load_state():
    if not os.path.exists(STATE_JSON):
        return {}
    with open(STATE_JSON) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_JSON, "w") as f:
        json.dump(state, f)


def check_symbol(symbol, sym_state):
    """Returns a list of {"side", "level", "close", "distance_pct"} for
    every side (support/resistance) that just transitioned into the "near"
    zone this poll. sym_state is mutated in place (per-symbol dict with
    "support"/"resistance" bool near-flags)."""
    try:
        result = support_resistance_for_interval(symbol, INTERVAL, LOOKBACK)
    except Exception as e:
        print(f"SR scan failed for {symbol}: {e} -- continuing")
        return []
    if result is None:
        return []

    close = result["close"]
    hits = []
    for side, level in (("support", result["support"]), ("resistance", result["resistance"])):
        if level is None:
            continue
        distance_pct = abs(close - level) / level * 100
        near = sym_state.get(side, False)
        if not near and distance_pct <= PROXIMITY_PCT:
            hits.append({"side": side, "level": level, "close": close, "distance_pct": distance_pct})
            sym_state[side] = True
        elif near and distance_pct > PROXIMITY_PCT * REARM_MULTIPLIER:
            sym_state[side] = False
    return hits


def poll_once(state):
    env = load_env()
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("No TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID configured -- skipping")
        return

    symbols = _resolve_symbols(QUICK_SCALP_TOP_N)
    with ThreadPoolExecutor(max_workers=SCAN_WORKER_COUNT) as pool:
        futures = {
            pool.submit(check_symbol, symbol, state.setdefault(symbol, {})): symbol
            for symbol in symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                hits = future.result()
            except Exception as e:
                print(f"SR check failed for {symbol}: {e} -- continuing")
                continue
            for hit in hits:
                emoji = "\U0001f7e2" if hit["side"] == "support" else "\U0001f534"
                short_symbol = symbol[:-4].lower() if symbol.endswith("USDT") else symbol.lower()

                # Added 2026-11-23 at the user's request -- RSI/overbought-
                # oversold context alongside the level touch itself. Same
                # reasoning as the other RSI additions this session: a
                # resistance touch while ALREADY overbought is a much
                # stronger "likely to reject" read than a plain proximity
                # ping with no positioning context at all, same for
                # support+oversold on the other side.
                rsi_status = overbought_oversold_status(symbol)
                rsi_line = (
                    f"RSI({RSI_INTERVAL})={rsi_status['rsi']:.1f} -- {rsi_status['label']}  "
                    f"({rsi_status['ema20_extension_pct']:+.1f}% vs EMA20)\n"
                ) if rsi_status is not None else ""

                send_telegram_message(
                    f"{emoji} {symbol} near 4h {hit['side']}: price {hit['close']:g} "
                    f"vs level {hit['level']:g} ({hit['distance_pct']:.2f}% away).\n"
                    f"{rsi_line}"
                    f"Diagnostic only -- not a trade direction. Run /sr_{short_symbol} for full detail.",
                    token, chat_id,
                )
                print(f"SR alert: {symbol} near {hit['side']} ({hit['distance_pct']:.2f}%)")

    save_state(state)


if __name__ == "__main__":
    state = load_state()
    poll_once(state)
    if os.environ.get("RUN_ONCE"):
        raise SystemExit(0)
    while True:
        time.sleep(POLL_INTERVAL_SECONDS)
        try:
            poll_once(state)
        except Exception as e:
            print(f"Poll failed: {e} -- continuing")
