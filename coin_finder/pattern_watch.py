"""/watch_pattern_<symbol>[_<interval>] (interval optional, defaults to
DEFAULT_PATTERN_WATCH_INTERVAL) -- polls a symbol/interval for any
standard candlestick pattern (reusing candlestick_patterns.py's full
16-pattern detector, same one /pattern_<symbol>_<interval> uses) until
EITHER a pattern shows up OR PATTERN_WATCH_DURATION_SECONDS elapses,
whichever comes first -- added 2026-11-14 at the user's request. Same
"watch and ping me instead of me re-checking manually" shape as
quick_scalp_watch.py's /watch_, just aimed at pattern-hunting rather than
pre-entry confirmation, and one-shot (stops the moment it finds something)
rather than repeat-until-verdict-change.

Checks only the LAST CLOSED candle each poll (same candle-boundary
convention as everywhere else in this project, see ema_ribbon_vwap.py's
module docstring for why) -- a 30m watch therefore only gets a genuinely
new read once per 30m candle close; polling more often than the interval
just re-observes the same closed candle until the next one forms.
"""

import json
import os
import time

from candlestick_patterns import MIN_CANDLES_NEEDED, full_reversal_lean
from history import get_futures_klines

HERE = os.path.dirname(os.path.abspath(__file__))
WATCH_STATE_JSON = os.path.join(HERE, "pattern_watch_state.json")
PATTERN_WATCH_DURATION_SECONDS = 15 * 60
DEFAULT_PATTERN_WATCH_INTERVAL = "30m"


def _load_watches():
    if not os.path.exists(WATCH_STATE_JSON):
        return {}
    with open(WATCH_STATE_JSON) as f:
        return json.load(f)


def _save_watches(watches):
    with open(WATCH_STATE_JSON, "w") as f:
        json.dump(watches, f)


def start_pattern_watch(symbol, interval=DEFAULT_PATTERN_WATCH_INTERVAL):
    watches = _load_watches()
    watches[symbol] = {"interval": interval, "started_at": time.time()}
    _save_watches(watches)
    return (
        f"Watching {symbol} {interval} for any standard candlestick pattern, "
        f"up to {PATTERN_WATCH_DURATION_SECONDS // 60} min -- will ping on the first one found, "
        "or when the watch expires."
    )


def stop_pattern_watch(symbol):
    watches = _load_watches()
    if symbol not in watches:
        return f"{symbol} pattern watch wasn't active."
    del watches[symbol]
    _save_watches(watches)
    return f"Stopped pattern watch on {symbol}."


def _format_found(symbol, interval, lean):
    shape, direction = lean["shape"], lean["direction"]
    if direction is None:
        return f"\U0001f56f️ Pattern found -- {symbol} ({interval}): <b>{shape}</b> -- neutral/indecision."
    emoji = "\U0001f7e2" if direction == "bullish" else "\U0001f534"
    status = "confirmed" if lean["confirmed"] else "shape only, not confirmed"
    return f"\U0001f56f️ Pattern found -- {symbol} ({interval}): <b>{shape}</b> ({direction}) {emoji} -- {status}."


def check_pattern_watches():
    """Call once per poll. Returns a list of (symbol, message) notifications
    -- a found pattern or an expiry. Either case removes the watch (found =
    one-shot by design, expiry = done); anything still active and empty-
    handed is left alone silently, no heartbeat notification."""
    watches = _load_watches()
    if not watches:
        return []

    notifications = []
    now = time.time()
    remaining = {}
    for symbol, w in watches.items():
        age = now - w["started_at"]
        if age >= PATTERN_WATCH_DURATION_SECONDS:
            notifications.append((
                symbol,
                f"⏱️ Pattern watch on {symbol} ({w['interval']}) expired "
                f"({PATTERN_WATCH_DURATION_SECONDS // 60}min) -- no standard pattern found.",
            ))
            continue
        try:
            candles = get_futures_klines(symbol, w["interval"], limit=MIN_CANDLES_NEEDED + 1)
            lean = full_reversal_lean(candles[:-1])
        except Exception as e:
            notifications.append((symbol, f"⚠️ Pattern watch on {symbol} stopped (error: {e})."))
            continue
        if lean is not None:
            notifications.append((symbol, _format_found(symbol, w["interval"], lean)))
            continue  # one-shot: stop watching once something is found
        remaining[symbol] = w
    _save_watches(remaining)
    return notifications
