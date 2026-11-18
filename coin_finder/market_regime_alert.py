"""Market-wide pump/dump watchlist alert -- added 2026-11-15, same day
as quick_scalp_signal.py's breadth veto, at the user's request to flip
that finding around: instead of only using high breadth to BLOCK a fade,
surface it as its own alert -- "the market is moving as a block right
now, here are the coins leading it."

Informational only. No SL/TP, no entry, no trade direction -- just a
heads-up watchlist ping naming the coins pushing hardest WITH whatever
broad move (BTC-led or otherwise) is currently dragging the universe
one way. Separate poll from quick_scalp_signal.py's own fade engine
(same underlying delta_map, different purpose) -- see
market_regime_snapshot()/build_market_regime_message() there.

Notifies on a regime TRANSITION (none -> bullish/bearish, bearish <->
bullish, or ending back to none), plus a periodic refresh every
REFRESH_INTERVAL_SECONDS while a regime persists (the leaderboard of
which coins are leading can rotate over a long move, and this is meant
to be a live multi-coin watchlist, not a one-shot ping) -- NOT a
notification on every single poll, same spam-avoidance convention as
quick_scalp_watch.py/pattern_watch.py.

DEBOUNCED as of 2026-11-16, STRENGTHENED same day -- a transition is
only confirmed (and alerted) once the SAME new direction is seen on
CONFIRM_POLLS_REQUIRED consecutive polls. First shipped requiring just
2 consecutive polls after breadth flickered bullish/bearish/bullish
within minutes near the 0.6 threshold -- but on a real incident right
after, BTC itself was flat/chopping sideways in a tight 3-hour range
(64.5k-65.2k, no real trend at all) and the market still produced
repeated 2-poll-confirmed bullish<->bearish flips, which the user
flagged as actively confusing ("got bullish then bearish for 1 coin")
rather than useful. Raised to 5 consecutive polls (~5min) -- a
genuinely sustained move still confirms with only a few extra minutes
of lag; short-lived chop (even chop that happens to hold for 2 whole
minutes) gets filtered out before ever reaching a notification.
"""

import json
import os
import time

from quick_scalp_signal import build_market_regime_message, market_regime_snapshot

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_JSON = os.path.join(HERE, "market_regime_state.json")
REFRESH_INTERVAL_SECONDS = 20 * 60
CONFIRM_POLLS_REQUIRED = 5


def _load_state():
    if not os.path.exists(STATE_JSON):
        return {"direction": None, "last_alert_at": 0, "pending_direction": None, "pending_count": 0}
    with open(STATE_JSON) as f:
        state = json.load(f)
    state.setdefault("pending_direction", None)
    state.setdefault("pending_count", 0)
    return state


def _save_state(state):
    with open(STATE_JSON, "w") as f:
        json.dump(state, f)


def check_market_regime(delta_map=None):
    """Call once per poll. Returns a message string to send via Telegram,
    or None if there's nothing worth notifying about right now.

    `delta_map`, if given, is reused instead of building a fresh one --
    see quick_scalp_signal.py's scan_quick_scalp_signals docstring for
    why (one shared snapshot per poll instead of several)."""
    snapshot = market_regime_snapshot(delta_map=delta_map)
    if snapshot is None:
        return None

    prev = _load_state()
    now = time.time()
    direction = snapshot["direction"]
    confirmed = prev.get("direction")

    if direction == confirmed:
        # no transition pending -- any prior flicker's count is stale, reset it
        if confirmed is not None and now - prev.get("last_alert_at", 0) >= REFRESH_INTERVAL_SECONDS:
            _save_state({"direction": confirmed, "last_alert_at": now, "pending_direction": None, "pending_count": 0})
            return build_market_regime_message(snapshot)
        _save_state({"direction": confirmed, "last_alert_at": prev.get("last_alert_at", now), "pending_direction": None, "pending_count": 0})
        return None

    # direction != confirmed -- a candidate transition
    count = prev.get("pending_count", 0) + 1 if prev.get("pending_direction") == direction else 1

    if count >= CONFIRM_POLLS_REQUIRED:
        # sustained for CONFIRM_POLLS_REQUIRED consecutive polls now -- confirmed, alert
        _save_state({"direction": direction, "last_alert_at": now, "pending_direction": None, "pending_count": 0})
        if direction is None:
            return f"⏱️ Market-wide {confirmed} move has cooled off -- back to normal, coin-specific moves."
        return build_market_regime_message(snapshot)

    # not sustained long enough yet -- don't alert, just update the running count
    _save_state({"direction": confirmed, "last_alert_at": prev.get("last_alert_at", 0), "pending_direction": direction, "pending_count": count})
    return None
