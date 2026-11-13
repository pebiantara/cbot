"""Early-warning layer for system 3 (scalp_pattern_signal.py / scalp_pattern_alert.py).

The confirmed scalp signal only fires after a full 15m candle shape forms
AND passes volume/funding/OI/mid-trend confirmation, polled every ~180s --
by the time that alert lands, a fast pump/dump has often already made most
of its move. This module trades confirmation quality for speed: it polls
live futures price every EARLY_WARNING_POLL_SECONDS (far tighter than
system 3's loop) and fires an UNCONFIRMED heads-up the moment price has
moved VELOCITY_THRESHOLD_PCT within the last VELOCITY_WINDOW_SECONDS.

Deliberately noisier than the confirmed signal -- it skips every quality
check (pattern shape, volume, funding, OI, mid-trend) in exchange for
catching the move while it's still early enough to matter for entry price.
Treat every alert from this file as "something fast is happening," not as
a trade signal on its own -- the confirmed system 3 alert (if one follows)
is still what carries the actual entry/TP/SL.

Trigger requires the velocity move to agree in sign with how the current
15m candle has moved from its own open (not just a raw N-second blip) --
this is a cheap filter against noise that's already round-tripped back
through the candle open by the time it's evaluated. It can also suppress a
real move that started right at a candle boundary before this filter has
a meaningful candle_pct to compare against; accepted trade-off for v1.

Dedup: at most one alert per symbol per direction per 15m candle boundary
(persisted to EARLY_WARNING_STATE_JSON so a restart mid-candle doesn't
immediately re-fire). Price history itself is in-memory only and rebuilds
after a restart, which naturally rate-limits re-alerting since
VELOCITY_WINDOW_SECONDS of data has to accumulate again first.

Set RUN_ONCE=1 to run a single poll and exit (useful for testing -- won't
actually trigger anything since there's no price history yet).
"""

import json
import os
import time

from entry_price import fetch_live_futures_price, fetch_live_futures_prices, format_price
from history import get_futures_klines
from scalp_pattern_signal import DEFAULT_TOP_N, PRIORITY_SYMBOLS, _resolve_symbols
from telegram_alert import load_env, send_telegram_message

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_JSON = os.path.join(HERE, "scalp_early_warning_state.json")

EARLY_WARNING_POLL_SECONDS = int(os.environ.get("EARLY_WARNING_POLL_SECONDS", 15))
VELOCITY_WINDOW_SECONDS = 360
VELOCITY_THRESHOLD_PCT = 0.015
CANDLE_SECONDS = 15 * 60

# in-memory only -- rebuilds after a restart, which is fine (see module docstring)
_price_history = {}  # symbol -> [(ts, price), ...], oldest first
_candle_open_cache = {}  # symbol -> (boundary_ts, open_price)


def load_state():
    if not os.path.exists(STATE_JSON):
        return {}
    with open(STATE_JSON) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_JSON, "w") as f:
        json.dump(state, f)


def _candle_open(symbol, now_ts):
    boundary = now_ts - (now_ts % CANDLE_SECONDS)
    cached = _candle_open_cache.get(symbol)
    if cached is not None and cached[0] == boundary:
        return boundary, cached[1]
    try:
        candles = get_futures_klines(symbol, "15m", limit=1)
        open_price = candles[-1]["open"]
    except Exception:
        return boundary, None
    _candle_open_cache[symbol] = (boundary, open_price)
    return boundary, open_price


def _check_symbol(symbol, now_ts, price):
    """Returns {"direction", "velocity_pct", "candle_pct", "price"} if the
    velocity trigger fires and agrees with the candle's own direction, else
    None."""
    buf = _price_history.setdefault(symbol, [])
    buf.append((now_ts, price))
    cutoff = now_ts - VELOCITY_WINDOW_SECONDS
    while len(buf) > 1 and buf[0][0] < cutoff - EARLY_WARNING_POLL_SECONDS:
        buf.pop(0)

    oldest_ts, oldest_price = buf[0]
    if oldest_ts > now_ts - (VELOCITY_WINDOW_SECONDS - EARLY_WARNING_POLL_SECONDS):
        return None  # not enough history yet to judge the window

    velocity_pct = (price - oldest_price) / oldest_price
    if abs(velocity_pct) < VELOCITY_THRESHOLD_PCT:
        return None

    _, open_price = _candle_open(symbol, now_ts)
    if not open_price:
        return None
    candle_pct = (price - open_price) / open_price
    if (velocity_pct > 0) != (candle_pct > 0):
        return None  # fast move already fighting/round-tripped the candle's own direction

    return {
        "direction": "pump" if velocity_pct > 0 else "dump",
        "velocity_pct": velocity_pct,
        "candle_pct": candle_pct,
        "price": price,
    }


def poll_once(state, symbols):
    env = load_env()
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("No TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID configured -- skipping")
        return

    now = time.time()
    boundary = now - (now % CANDLE_SECONDS)
    prices = fetch_live_futures_prices()  # one request covers every symbol
    for symbol in PRIORITY_SYMBOLS:
        # dedicated per-symbol call for the 5 priority coins -- freshest
        # price for the ones that matter most, independent of the batch
        # call succeeding/failing
        individual = fetch_live_futures_price(symbol)
        if individual is not None:
            prices[symbol] = individual
    for symbol in symbols:
        price = prices.get(symbol)
        if price is None:
            continue
        hit = _check_symbol(symbol, now, price)
        if hit is None:
            continue

        key = f"{symbol}:{hit['direction']}"
        if state.get(key) == boundary:
            continue  # already alerted this symbol+direction for this candle

        emoji = "\U0001f7e2⚡" if hit["direction"] == "pump" else "\U0001f534⚡"
        short_symbol = symbol[:-4].lower() if symbol.endswith("USDT") else symbol.lower()
        send_telegram_message(
            f"{emoji} EARLY WARNING (unconfirmed): {symbol} {hit['direction']}ing fast -- "
            f"{hit['velocity_pct']:+.2%} in ~{VELOCITY_WINDOW_SECONDS // 60}min, "
            f"candle already {hit['candle_pct']:+.2%} from open. Price now {format_price(hit['price'])}.\n"
            f"No pattern/volume/funding/OI confirmation yet -- this is speed, not quality. "
            f"Run /fut_{short_symbol}_50 for the confirmed-signal view.",
            token, chat_id,
        )
        state[key] = boundary
        print(f"Early warning: {symbol} {hit['direction']} ({hit['velocity_pct']:+.2%})")

    save_state(state)


if __name__ == "__main__":
    state = load_state()
    symbols = _resolve_symbols(DEFAULT_TOP_N)
    poll_once(state, symbols)
    if os.environ.get("RUN_ONCE"):
        raise SystemExit(0)
    while True:
        time.sleep(EARLY_WARNING_POLL_SECONDS)
        try:
            poll_once(state, symbols)
        except Exception as e:
            print(f"Poll failed: {e} -- continuing")
