"""BTC-specific crash/pump alert -- added 2026-11-17 after a same-day BTC
drop from ~65xxx to ~63xxx (-3% in under an hour) went unflagged: BTC is
deliberately excluded from quick_scalp_signal.py's tradeable universe (it
backtests as a losing instrument for the fade strategy, see
QUICK_SCALP_EXCLUDED_SYMBOLS), so nothing in the live system was watching
BTC's own price action at all. A BTC-wide move still matters even though
nothing trades it directly -- it tends to drag the whole alt universe with
it, so this is a standalone heads-up, not a trade signal.

Two independent tiers so both a fast flash-crash and a slower grind (like
the incident this was built for) get caught without needing separate
scripts:
  - fast: 2% move within 15 minutes
  - slow: 3% move within 60 minutes

Same velocity-window technique as scalp_early_warning.py (unconfirmed,
pure price-speed detection), generalized to multiple windows over one
shared in-memory price history. Price history is in-memory only and
rebuilds after a restart (fine -- a real move still has the full tier
window to be caught again). Per-tier+direction cooldown (persisted to
BTC_CRASH_STATE_JSON) is the tier's own window length, so a restart mid-
move doesn't immediately re-fire, and a sustained move gets a fresh alert
once each window has had time to confirm it's still happening.

Set RUN_ONCE=1 to run a single poll and exit.
"""

import json
import os
import time

from entry_price import fetch_live_futures_price, format_price
from telegram_alert import load_env, send_telegram_message

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_JSON = os.path.join(HERE, "btc_crash_alert_state.json")

SYMBOL = "BTCUSDT"
POLL_INTERVAL_SECONDS = int(os.environ.get("BTC_CRASH_POLL_SECONDS", 20))

TIERS = [
    {"name": "fast", "window_seconds": 15 * 60, "threshold_pct": 0.02},
    {"name": "slow", "window_seconds": 60 * 60, "threshold_pct": 0.03},
]
_MAX_WINDOW_SECONDS = max(t["window_seconds"] for t in TIERS)

# in-memory only -- rebuilds after a restart, same tradeoff as scalp_early_warning.py
_price_history = []  # [(ts, price), ...], oldest first


def load_state():
    if not os.path.exists(STATE_JSON):
        return {}
    with open(STATE_JSON) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_JSON, "w") as f:
        json.dump(state, f)


def _reference_price(buf, now_ts, window_seconds):
    """Price from ~window_seconds ago, or None if history doesn't go back far enough yet."""
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


def check_tiers(now_ts, price):
    """Returns a list of {"tier", "direction", "pct", "reference_price"} for every
    tier currently tripped (a fast and slow tier can both fire at once)."""
    _price_history.append((now_ts, price))
    cutoff = now_ts - _MAX_WINDOW_SECONDS
    while len(_price_history) > 1 and _price_history[0][0] < cutoff - POLL_INTERVAL_SECONDS:
        _price_history.pop(0)

    hits = []
    for tier in TIERS:
        reference = _reference_price(_price_history, now_ts, tier["window_seconds"])
        if reference is None:
            continue
        pct = (price - reference) / reference
        if abs(pct) < tier["threshold_pct"]:
            continue
        hits.append({
            "tier": tier["name"],
            "direction": "pump" if pct > 0 else "crash",
            "pct": pct,
            "reference_price": reference,
        })
    return hits


def poll_once(state):
    env = load_env()
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("No TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID configured -- skipping")
        return

    now = time.time()
    price = fetch_live_futures_price(SYMBOL)
    if price is None:
        print("BTC price fetch failed -- skipping this poll")
        return

    for hit in check_tiers(now, price):
        key = f"{hit['tier']}:{hit['direction']}"
        window_seconds = next(t["window_seconds"] for t in TIERS if t["name"] == hit["tier"])
        if now - state.get(key, 0) < window_seconds:
            continue  # cooldown -- already alerted this tier+direction recently

        emoji = "\U0001f7e2" if hit["direction"] == "pump" else "\U0001f534"
        window_min = window_seconds // 60
        send_telegram_message(
            f"{emoji} BTC {hit['direction'].upper()}: {hit['pct']:+.2%} in ~{window_min}min "
            f"({format_price(hit['reference_price'])} → {format_price(price)}).\n"
            f"BTC isn't traded directly by the fade engine, but a move this size "
            f"tends to drag the whole alt universe with it -- heads-up only, not a trade signal.",
            token, chat_id,
        )
        state[key] = now
        print(f"BTC {hit['direction']} alert ({hit['tier']}): {hit['pct']:+.2%}")

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
