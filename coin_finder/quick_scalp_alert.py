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

CHASE confirmation delay + reversal warning (added 2026-11-19 after a data
analysis this project's conversation history ran against
quick_scalp_chase_predictions.db's first ~3.5 days: a chase trade that
hadn't moved >=CHASE_CONFIRM_MOVE_PCT favorably within 5 minutes of
triggering had roughly HALF the 1h hit rate of one that had (19.2% vs
38.8%, n=796/484) -- "the candle is still stuck" right after a chase
alert fires is a real, strong leading indicator of a loss, not noise.
Separately, a quarter of all chase triggers got contradicted by an
opposite-direction trigger on the SAME coin within 60 minutes, and when
that happened the ORIGINAL trigger's hit rate was already roughly half of
normal (14.0% vs 30.9%) -- a same-coin reversal isn't an independent new
signal, it's confirmation the first one was already dying.

So: a newly-ready chase trigger no longer alerts immediately. It's held
in CHASE_PENDING_JSON for CHASE_CONFIRM_DELAY_SECONDS (5min); only if
price has since moved >=CHASE_CONFIRM_MOVE_PCT (0.05%) in the triggered
direction does the alert actually go out (using the price/signal at
confirmation time, not the stale trigger-time snapshot). A trigger that
flips direction before its own confirmation window elapses overwrites the
pending entry with the new direction's own fresh clock -- no special-case
cancellation needed, a flipped setup simply won't confirm in its original
direction anyway. Separately, CHASE_RECENT_ALERTS_JSON remembers the last
CONFIRMED alert per symbol for CHASE_REVERSAL_WARNING_WINDOW_SECONDS (1h,
matching the engine's own longest outcome horizon) -- if a new confirmed
alert fires in the opposite direction within that window, the message is
prefixed with an explicit "this looks like it's reversing" warning
instead of landing as just another unrelated alert.
"""

import json
import os
import threading
import time

from orderbook_snapshot import log_snapshot as log_orderbook_snapshot
from quick_scalp_chase_outcomes import record_signal as record_signal_chase, resolve_matured as resolve_matured_chase
from quick_scalp_outcomes import record_signal, resolve_matured
from quick_scalp_pro_outcomes import record_signal as record_signal_pro, resolve_matured as resolve_matured_pro
from quick_scalp_signal import (
    QUICK_SCALP_EXCLUDED_SYMBOLS,
    QUICK_SCALP_TOP_N,
    _build_delta_map,
    _resolve_symbols,
    build_quick_scalp_alert_message,
    build_quick_scalp_chase_alert_message,
    quick_scalp_entry_for_symbol,
    scan_quick_scalp_chase_signals,
    scan_quick_scalp_signals,
    scan_quick_scalp_signals_pro,
)
from market_regime_alert import check_market_regime
from pattern_watch import check_pattern_watches
from quick_scalp_watch import check_watches
from telegram_alert import load_env, send_telegram_message

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_JSON = os.path.join(HERE, "quick_scalp_alert_state.json")
CHASE_STATE_JSON = os.path.join(HERE, "quick_scalp_chase_alert_state.json")
CHASE_PENDING_JSON = os.path.join(HERE, "quick_scalp_chase_pending_state.json")
CHASE_RECENT_ALERTS_JSON = os.path.join(HERE, "quick_scalp_chase_recent_alerts_state.json")
PRO_STATE_JSON = os.path.join(HERE, "quick_scalp_pro_shadow_state.json")
POLL_INTERVAL_SECONDS = int(os.environ.get("QUICK_SCALP_POLL_SECONDS", 60))

CHASE_CONFIRM_DELAY_SECONDS = int(os.environ.get("CHASE_CONFIRM_DELAY_SECONDS", 300))
CHASE_CONFIRM_MOVE_PCT = float(os.environ.get("CHASE_CONFIRM_MOVE_PCT", 0.05)) / 100
CHASE_REVERSAL_WARNING_WINDOW_SECONDS = 3600


def _current_universe_delta_map():
    """One delta_map per poll, shared by the fade scan, chase scan, and
    market-regime check -- added 2026-11-15 alongside CHASE mode so
    adding a second signal path didn't also add a second (or third)
    full-universe delta pass per poll. Real cost on a day with network
    degradation (see cbot_technical_gotchas.md): tripling this work would
    have made that problem meaningfully worse, not just slower in
    theory."""
    symbols = [s for s in _resolve_symbols(QUICK_SCALP_TOP_N) if s not in QUICK_SCALP_EXCLUDED_SYMBOLS]
    return _build_delta_map(symbols)


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


def poll_once(delta_map=None):
    env = load_env()
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("No TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID configured -- skipping")
        return
    account_equity = float(env["LEVERAGE_ACCOUNT_EQUITY_USDT"]) if env.get("LEVERAGE_ACCOUNT_EQUITY_USDT") else None
    risk_pct = float(env.get("LEVERAGE_RISK_PCT", "1")) / 100

    previous = load_state()
    signals = scan_quick_scalp_signals(delta_map=delta_map)

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


def poll_once_chase(delta_map=None):
    """CHASE-mode poll -- rides a crowded market push instead of fading
    it, see quick_scalp_signal.py's CHASE_BREADTH_FLOOR comment for the
    backtest behind this. Same "ready to trade" gating and dedup-on-
    transition pattern as poll_once() above, own state file and own DB
    (quick_scalp_chase_outcomes.py) so it can't collide with the fade
    engine's tracking.

    A newly-ready trigger doesn't alert immediately -- see this module's
    docstring for the confirmation-delay + reversal-warning rationale."""
    env = load_env()
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    account_equity = float(env["LEVERAGE_ACCOUNT_EQUITY_USDT"]) if env.get("LEVERAGE_ACCOUNT_EQUITY_USDT") else None
    risk_pct = float(env.get("LEVERAGE_RISK_PCT", "1")) / 100

    previous = load_state(CHASE_STATE_JSON)
    pending = load_state(CHASE_PENDING_JSON)
    recent_alerts = load_state(CHASE_RECENT_ALERTS_JSON)
    signals = scan_quick_scalp_chase_signals(delta_map=delta_map)
    now = time.time()

    new_state = {}
    for symbol, signal in signals.items():
        is_long = signal["is_long"]
        direction = "LONG" if is_long else "SHORT"
        entry = quick_scalp_entry_for_symbol(symbol, is_long)
        ready = entry is not None and entry["at_entry_now"]
        new_state[symbol] = {"direction": direction, "ready": ready}

        prev = previous.get(symbol)
        prev = prev if isinstance(prev, dict) else {}
        is_new_trigger = ready and (prev.get("direction") != direction or not prev.get("ready"))

        existing_pending = pending.get(symbol)
        if is_new_trigger and (not existing_pending or existing_pending.get("direction") != direction):
            # start (or restart, on a direction flip) this symbol's own confirmation clock
            pending[symbol] = {"direction": direction, "trigger_price": signal["close_price"], "trigger_time": now}

    for symbol, p in list(pending.items()):
        if now - p["trigger_time"] < CHASE_CONFIRM_DELAY_SECONDS:
            continue
        del pending[symbol]  # resolved either way this poll -- confirmed-and-alerted, or dropped
        direction = p["direction"]
        current_signal = signals.get(symbol)
        if current_signal is None or ("LONG" if current_signal["is_long"] else "SHORT") != direction:
            continue  # setup evaporated or flipped before confirmation -- nothing to confirm
        current_price = current_signal["close_price"]
        signed_move_pct = (current_price - p["trigger_price"]) / p["trigger_price"] * (1 if direction == "LONG" else -1)
        if signed_move_pct < CHASE_CONFIRM_MOVE_PCT:
            print(f"[chase] {symbol} {direction} filtered -- stalled at confirmation check "
                  f"({signed_move_pct:+.3%} vs {CHASE_CONFIRM_MOVE_PCT:+.3%} needed)")
            continue

        reversal_note = ""
        prior = recent_alerts.get(symbol)
        if prior and prior.get("direction") != direction and now - prior.get("alert_time", 0) < CHASE_REVERSAL_WARNING_WINDOW_SECONDS:
            age_min = (now - prior["alert_time"]) / 60
            reversal_note = (
                f"⚠️ <b>Reversal warning:</b> {symbol} {prior['direction']} was alerted "
                f"{age_min:.0f}min ago -- if that position is still open, this looks like it's "
                f"reversing now.\n\n"
            )

        message = reversal_note + build_quick_scalp_chase_alert_message(
            symbol, current_signal, account_equity=account_equity, risk_pct=risk_pct,
        )
        send_telegram_message(message, token, chat_id, parse_mode="HTML")
        record_signal_chase(symbol, direction, current_price)
        recent_alerts[symbol] = {"direction": direction, "alert_time": now}
        print(f"[chase] Alerted (confirmed @ +{CHASE_CONFIRM_DELAY_SECONDS // 60}min): {direction} {symbol}")

    recent_alerts = {
        s: v for s, v in recent_alerts.items()
        if now - v.get("alert_time", 0) < CHASE_REVERSAL_WARNING_WINDOW_SECONDS
    }

    save_state(new_state, CHASE_STATE_JSON)
    save_state(pending, CHASE_PENDING_JSON)
    save_state(recent_alerts, CHASE_RECENT_ALERTS_JSON)

    try:
        resolved = resolve_matured_chase()
        if resolved:
            print(f"[chase] Resolved {resolved} matured signal/horizon pair(s)")
    except Exception as e:
        print(f"[chase] resolve_matured failed: {e} -- continuing")


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


def poll_once_pattern_watch():
    """/watch_pattern_<symbol>'s poll -- checks every active pattern watch
    (see pattern_watch.py) and sends a Telegram message only when a pattern
    is found or the watch expires."""
    env = load_env()
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    for symbol, message in check_pattern_watches():
        send_telegram_message(message, token, chat_id, parse_mode="HTML")
        print(f"[pattern watch] {symbol}: {message}")


def poll_once_market_regime(delta_map=None):
    """Market-wide pump/dump watchlist poll -- see market_regime_alert.py."""
    env = load_env()
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    message = check_market_regime(delta_map=delta_map)
    if message:
        send_telegram_message(message, token, chat_id, parse_mode="HTML")
        print(f"[market regime] {message}")


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
    /Users/pebiantara/.claude/plans/cozy-waddling-peacock.md.

    CHASE mode (added 2026-11-15, see quick_scalp_signal.py's
    CHASE_BREADTH_FLOOR) shares the SAME delta_map built once here with
    the fade poll and the market-regime check -- see
    _current_universe_delta_map()'s docstring for why that matters on a
    day with real network degradation."""
    delta_map = None
    try:
        delta_map = _current_universe_delta_map()
    except Exception as e:
        print(f"[scan] shared delta-map build failed: {e} -- fade/chase/regime polls will each build their own", flush=True)

    try:
        poll_once(delta_map=delta_map)
    except Exception as e:
        print(f"Poll failed: {e} -- continuing")
    try:
        poll_once_chase(delta_map=delta_map)
    except Exception as e:
        print(f"[chase] poll failed: {e} -- continuing")
    try:
        poll_once_watch()
    except Exception as e:
        print(f"[watch] poll failed: {e} -- continuing")
    try:
        poll_once_pattern_watch()
    except Exception as e:
        print(f"[pattern watch] poll failed: {e} -- continuing")
    # Turned off 2026-11-24 at the user's request -- set
    # MARKET_REGIME_ALERT_ENABLED=1 to turn it back on. Gated here (not
    # removed) so re-enabling doesn't need a code change, just an env var.
    if os.environ.get("MARKET_REGIME_ALERT_ENABLED") == "1":
        try:
            poll_once_market_regime(delta_map=delta_map)
        except Exception as e:
            print(f"[market regime] poll failed: {e} -- continuing")
    if os.environ.get("QUICK_SCALP_PRO_SHADOW_ENABLED") == "1":
        try:
            poll_once_pro_shadow()
        except Exception as e:
            print(f"[PRO shadow] poll failed: {e} -- continuing")


MAX_POLL_SECONDS = int(os.environ.get("QUICK_SCALP_MAX_POLL_SECONDS", 600))
_watchdog_state = {"poll_started_at": time.time()}


def _watchdog():
    """Added 2026-11-13 after repeated real incidents where a poll cycle
    ran far longer than expected (many individually-bounded retried
    calls across ~60 symbols stacking up under bad network conditions --
    see cbot_technical_gotchas.md #15) and needed a MANUAL restart,
    sometimes several in a row, to recover. This makes recovery fully
    automatic: if a cycle ever runs longer than MAX_POLL_SECONDS (600s --
    generous enough that a legitimately slow-but-working cycle won't get
    killed, confirmed one real cycle took ~10min end to end), force-exit
    the whole process. `restart: unless-stopped` in docker-compose.yml
    brings it back up within seconds -- no human needs to notice or act.
    os._exit() (not sys.exit()) because this must work even if the main
    thread is blocked inside a C-level socket call that won't respond to
    a normal exception."""
    while True:
        time.sleep(15)
        elapsed = time.time() - _watchdog_state["poll_started_at"]
        if elapsed > MAX_POLL_SECONDS:
            print(f"[watchdog] poll cycle running for {elapsed:.0f}s (limit {MAX_POLL_SECONDS}s) "
                  f"-- force-exiting so Docker restarts cleanly", flush=True)
            os._exit(1)


if __name__ == "__main__":
    threading.Thread(target=_watchdog, daemon=True).start()
    _watchdog_state["poll_started_at"] = time.time()
    poll_once_all()
    if os.environ.get("RUN_ONCE"):
        raise SystemExit(0)
    while True:
        time.sleep(POLL_INTERVAL_SECONDS)
        _watchdog_state["poll_started_at"] = time.time()
        poll_once_all()  # isolates/logs its own failures, never raises
