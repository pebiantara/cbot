"""/watch_<symbol> -- short-lived, pre-entry confirmation watch, added
2026-11-13 after the user asked for a way to "watch until we're sure the
reverse happens or it's a wrong signal" right after an alert, before
committing to a trade. Explicitly NOT the "wait before entering" idea
already backtested and rejected the same day (that delayed the ENTRY
itself and made results worse, -38% cum vs +17% baseline) -- this instead
runs the same real-time diagnostic used manually on a live AVAX trade
earlier in the project (delta direction/strength + momentum, re-checked
live) and reports a verdict, WITHOUT changing when/whether a trade is
taken. Auto-expires after WATCH_DURATION_SECONDS (confirmed short with
the user -- this is a quick pre-entry read, not a full position tracker
through to resolution, which would need a much longer window matching the
backtest's 240min time-stop horizon instead).

Only notifies on a VERDICT CHANGE (confirms <-> weakening), not every
poll -- confirmed with the user via AskUserQuestion ("only on meaningful
change") rather than a fixed heartbeat.
"""

import json
import os
import sqlite3
import time

from quick_scalp_signal import _quick_momentum_confirms, _rolling_delta, get_futures_klines, QUICK_SCALP_INTERVAL

HERE = os.path.dirname(os.path.abspath(__file__))
PREDICTIONS_DB = os.path.join(HERE, "quick_scalp_predictions.db")
WATCH_STATE_JSON = os.path.join(HERE, "quick_scalp_watch_state.json")
WATCH_DURATION_SECONDS = 15 * 60


def _load_watches():
    if not os.path.exists(WATCH_STATE_JSON):
        return {}
    with open(WATCH_STATE_JSON) as f:
        return json.load(f)


def _save_watches(watches):
    with open(WATCH_STATE_JSON, "w") as f:
        json.dump(watches, f)


def _latest_signal(symbol):
    """Most recently logged (direction, close_price, snapshot_time) for
    this symbol, or None if it's never alerted. `direction` in the DB is
    already the TRADE direction (post-fade), see quick_scalp_alert.py's
    record_signal call -- push_is_long is therefore the opposite."""
    con = sqlite3.connect(PREDICTIONS_DB)
    try:
        row = con.execute(
            "SELECT direction, close_price, snapshot_time FROM quick_scalp_predictions "
            "WHERE symbol = ? ORDER BY snapshot_time DESC LIMIT 1",
            (symbol,),
        ).fetchone()
    finally:
        con.close()
    return row


def start_watch(symbol):
    """Returns a reply string. No prior alert for this symbol -> error message,
    nothing registered."""
    row = _latest_signal(symbol)
    if row is None:
        return f"⚠️ No alert on record for {symbol} -- nothing to watch."
    direction, close_price, snapshot_time = row
    push_is_long = direction == "SHORT"  # trade direction is the fade, opposite of the push
    watches = _load_watches()
    watches[symbol] = {
        "push_is_long": push_is_long,
        "trade_direction": direction,
        "entry_price": close_price,
        "started_at": time.time(),
        "last_verdict": "confirms",  # assume still-good at watch start, same as when it alerted
    }
    _save_watches(watches)
    return (
        f"👀 Watching {symbol} ({direction}, entry {close_price:.6g}) for "
        f"{WATCH_DURATION_SECONDS // 60} min -- will ping only if the read changes."
    )


def stop_watch(symbol):
    watches = _load_watches()
    if symbol not in watches:
        return f"{symbol} wasn't being watched."
    del watches[symbol]
    _save_watches(watches)
    return f"Stopped watching {symbol}."


def _verdict_for(symbol, push_is_long):
    """"confirms" if order flow + momentum still agree with the ORIGINAL
    push (i.e. the exhaustion thesis is still intact), "weakening" if
    either has let go. Mirrors the ad-hoc live check used manually on
    AVAX earlier in the project -- not a backtested gate, a real-time
    read, same honesty as that entry."""
    delta_pct, trade_count = _rolling_delta(symbol)
    try:
        candles = get_futures_klines(symbol, QUICK_SCALP_INTERVAL, limit=10)
        momentum_ok = _quick_momentum_confirms(push_is_long, candles)
    except Exception:
        momentum_ok = None

    if delta_pct is None:
        delta_agrees = None
    else:
        delta_agrees = (delta_pct > 0) == push_is_long

    if delta_agrees is False or momentum_ok is False:
        return "weakening"
    return "confirms"


def check_watches():
    """Call once per poll. Returns a list of (symbol, message) notifications
    to send via Telegram -- either a verdict change or an expiry. Expired/
    errored watches are removed from state; unchanged ones are left alone
    silently (no notification)."""
    watches = _load_watches()
    if not watches:
        return []

    notifications = []
    now = time.time()
    remaining = {}
    for symbol, w in watches.items():
        age = now - w["started_at"]
        if age >= WATCH_DURATION_SECONDS:
            notifications.append((symbol, f"⏱️ Watch on {symbol} expired ({WATCH_DURATION_SECONDS // 60}min) -- no longer tracking."))
            continue
        try:
            verdict = _verdict_for(symbol, w["push_is_long"])
        except Exception as e:
            notifications.append((symbol, f"⚠️ Watch on {symbol} stopped (error: {e})."))
            continue
        if verdict != w["last_verdict"]:
            if verdict == "weakening":
                notifications.append((symbol, f"⚠️ {symbol} ({w['trade_direction']}): order flow/momentum no longer confirms -- original push may be fully done, consider skipping."))
            else:
                notifications.append((symbol, f"✅ {symbol} ({w['trade_direction']}): still confirms -- original push intact."))
            w["last_verdict"] = verdict
        remaining[symbol] = w
    _save_watches(remaining)
    return notifications
