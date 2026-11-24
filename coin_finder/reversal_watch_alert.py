"""Reversal watch -- added 2026-11-23 at the user's request to extend
big_mover_alert.py: a coin that trips a pump/dump tier AND is already at
or approaching overbought/oversold gets put under closer watch for signs
the move is exhausting (buyer/seller aggression fading back toward 50/50
+ price flattening/reversing against the move) -- the exact manual check
done for BULLAUSDT (RSI 85.8 overbought, pump already alerted, then
buyer aggression faded 54.4%->48.7% and price flipped +1.69%->-0.22%
right as this idea came up) and ACEUSDT (RSI 9.1 oversold, dump already
alerted), now automated instead of asked for each time.

Pipeline:
  1. Read big_mover_alert_state.json (shared file, no coupling needed --
     both containers bind-mount ./coin_finder) for any symbol:tier:
     direction key alerted within WATCH_ENTRY_LOOKBACK_SECONDS.
  2. Gate: only enter the watchlist if the alert's direction matches an
     RSI extreme in the SAME direction of exhaustion risk -- a "pump"
     hit needs RSI >= OVERBOUGHT_WATCH_RSI (watching for a bearish
     turn), a "dump" hit needs RSI <= OVERSOLD_WATCH_RSI (watching for
     a bullish turn). RSI band is 65/35 (at the user's request), not the
     strict 70/30 overbought/oversold label itself -- catches the setup
     approaching the extreme, not just once already past it.
  3. Each poll, every watched symbol gets checked for the exhaustion
     signature: aggression (buy% of $ volume) fading back toward 50%
     by at least EXHAUSTION_MIN_FADE_PCT, AND realized price in the
     recent window no longer extending the original move. Fires once,
     then drops off the watchlist -- a fresh big_mover hit can re-add
     it later if it's still extreme.
  4. Watched symbols expire after WATCH_MAX_AGE_SECONDS with no
     exhaustion signature, so the list doesn't grow stale forever.

Diagnostic only -- no SL/TP, no entry, no trade direction, same
convention as big_mover_alert.py.

Set RUN_ONCE=1 to run a single poll and exit.
"""

import json
import os
import time

from big_mover_alert import RSI_INTERVAL, overbought_oversold_status
from history import get_futures_klines
from telegram_alert import load_env, send_telegram_message

HERE = os.path.dirname(os.path.abspath(__file__))
BIG_MOVER_STATE_JSON = os.path.join(HERE, "big_mover_alert_state.json")
WATCH_STATE_JSON = os.path.join(HERE, "reversal_watch_state.json")

POLL_INTERVAL_SECONDS = int(os.environ.get("REVERSAL_WATCH_POLL_SECONDS", 60))
WATCH_ENTRY_LOOKBACK_SECONDS = 15 * 60  # a big_mover hit within the last 15min is still "fresh" enough to gate on
WATCH_MAX_AGE_SECONDS = 4 * 60 * 60  # drop a watched symbol after 4h with no exhaustion signature

OVERBOUGHT_WATCH_RSI = 65
OVERSOLD_WATCH_RSI = 35

EXHAUSTION_RECENT_MINUTES = 10
EXHAUSTION_PRIOR_MINUTES = 10
EXHAUSTION_MIN_FADE_PCT = 0.05  # raised from 0.03 to 0.05 2026-11-24 at the user's request
# (briefly tried 0.08 same day, walked back to 0.05) -- the 3pp bar was catching weak/
# noisy fades; 5pp still catches a real fade the size of BULLA's actual historical
# catch (~5.7pp) while cutting out the weakest noise below it


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def _save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


def _candidates_from_big_mover_state(now):
    """{symbol: {"direction", "rsi_at_hit"}} for every symbol:tier:
    direction key in big_mover_alert_state.json alerted within
    WATCH_ENTRY_LOOKBACK_SECONDS -- direction is "pump" or "dump", taking
    the MOST RECENT hit if a symbol has multiple fresh ones (tiers can
    overlap). "rsi_at_hit" (added 2026-11-23) is the RSI big_mover_alert.py
    itself saw AT THE MOMENT it fired -- used instead of a fresh live RSI
    query for the gate below, since a fast-moving coin's RSI can cool back
    to neutral within the few minutes it takes to even reach this check,
    letting a genuinely-overbought/oversold hit slip through a live-RSI
    gate untouched (a real miss caught on BULLAUSDT: fast-tier pump hit at
    RSI ~70+, but RSI had already cooled to 58 by the time this ran).
    Tolerates the pre-2026-11-23 state shape (a bare float) via
    big_mover_alert.load_state()'s own migration -- this file reads the
    same JSON directly rather than importing that function, so it
    duck-types the same way: dict value -> new shape, anything else (a
    bare float) -> old shape with no RSI available yet."""
    state = _load_json(BIG_MOVER_STATE_JSON, {})
    candidates = {}
    for key, value in state.items():
        if isinstance(value, dict):
            ts, rsi_at_hit = value.get("ts", 0), value.get("rsi")
        else:
            ts, rsi_at_hit = value, None
        if now - ts > WATCH_ENTRY_LOOKBACK_SECONDS:
            continue
        symbol, _tier, direction = key.split(":")
        if symbol not in candidates or ts > candidates[symbol]["ts"]:
            candidates[symbol] = {"direction": direction, "ts": ts, "rsi_at_hit": rsi_at_hit}
    return candidates


def _exhaustion_signature(symbol):
    """{"recent_buy_pct", "prior_buy_pct", "recent_chg_pct", "prior_chg_pct"}
    from 1m candles -- same two comparisons (aggression fade + price
    deceleration) used in the manual BULLA/ACE check this module
    automates. None on fetch failure."""
    limit = EXHAUSTION_RECENT_MINUTES + EXHAUSTION_PRIOR_MINUTES
    try:
        candles = get_futures_klines(symbol, "1m", limit=limit)
    except Exception:
        return None
    if len(candles) < limit:
        return None
    candles = candles[-limit:]  # exactly `limit` candles, oldest first
    prior = candles[:EXHAUSTION_PRIOR_MINUTES]
    recent = candles[EXHAUSTION_PRIOR_MINUTES:]

    def _buy_pct(span):
        total = sum(c["quote_volume"] for c in span)
        return sum(c["taker_buy_quote_volume"] for c in span) / total if total > 0 else 0.5

    return {
        "recent_buy_pct": _buy_pct(recent),
        "prior_buy_pct": _buy_pct(prior),
        "recent_chg_pct": (recent[-1]["close"] / recent[0]["open"] - 1) * 100,
        "prior_chg_pct": (prior[-1]["close"] / prior[0]["open"] - 1) * 100,
    }


def _check_exhaustion(direction, sig):
    """True if `sig` shows the exhaustion signature for `direction`'s
    original move ("pump" -> watching for buyer aggression fading +
    price no longer extending up; "dump" -> mirror)."""
    if direction == "pump":
        faded = (sig["prior_buy_pct"] - sig["recent_buy_pct"]) >= EXHAUSTION_MIN_FADE_PCT
        no_longer_buyer_led = sig["recent_buy_pct"] <= 0.52
        stalled = sig["recent_chg_pct"] <= 0
        return faded and no_longer_buyer_led and stalled
    else:
        faded = (sig["recent_buy_pct"] - sig["prior_buy_pct"]) >= EXHAUSTION_MIN_FADE_PCT
        no_longer_seller_led = sig["recent_buy_pct"] >= 0.48
        stalled = sig["recent_chg_pct"] >= 0
        return faded and no_longer_seller_led and stalled


def poll_once(watch):
    env = load_env()
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("No TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID configured -- skipping")
        return

    now = time.time()

    for symbol, candidate in _candidates_from_big_mover_state(now).items():
        if symbol in watch:
            continue
        direction = candidate["direction"]
        rsi = candidate["rsi_at_hit"]
        if rsi is None:
            # big_mover_alert couldn't get an RSI reading at hit time (or
            # this is a pre-2026-11-23 state entry with no RSI recorded
            # at all) -- fall back to a live query rather than skip the
            # symbol outright.
            rsi_status = overbought_oversold_status(symbol)
            rsi = rsi_status["rsi"] if rsi_status is not None else None
        if rsi is None:
            continue
        gated = (
            (direction == "pump" and rsi >= OVERBOUGHT_WATCH_RSI)
            or (direction == "dump" and rsi <= OVERSOLD_WATCH_RSI)
        )
        if gated:
            watch[symbol] = {"direction": direction, "entered_at": now, "rsi_at_entry": rsi}
            print(f"[reversal watch] {symbol} entered watch -- {direction} hit, "
                  f"RSI({RSI_INTERVAL})={rsi:.1f}")

    for symbol in list(watch):
        entry = watch[symbol]
        if now - entry["entered_at"] > WATCH_MAX_AGE_SECONDS:
            print(f"[reversal watch] {symbol} expired from watch (no exhaustion signature in "
                  f"{WATCH_MAX_AGE_SECONDS // 3600}h)")
            del watch[symbol]
            continue

        sig = _exhaustion_signature(symbol)
        if sig is None:
            continue
        if not _check_exhaustion(entry["direction"], sig):
            continue

        turn_label = "DUMP" if entry["direction"] == "pump" else "PUMP"
        short_symbol = symbol[:-4].lower() if symbol.endswith("USDT") else symbol.lower()
        send_telegram_message(
            f"⚠️ REVERSAL WATCH: {symbol} showing exhaustion after its {entry['direction']} "
            f"(entered watch at RSI {entry['rsi_at_entry']:.1f}) -- possible {turn_label} setting up.\n"
            f"Aggression faded: {sig['prior_buy_pct']:.0%} -> {sig['recent_buy_pct']:.0%} buy "
            f"over last {EXHAUSTION_RECENT_MINUTES}min.  "
            f"Price: {sig['prior_chg_pct']:+.2f}% -> {sig['recent_chg_pct']:+.2f}%.\n"
            f"Diagnostic only -- not a trade signal. Run /pattern_{short_symbol}_15m or /sr_{short_symbol} "
            f"for more context.",
            token, chat_id,
        )
        print(f"[reversal watch] Alerted: {symbol} exhaustion after {entry['direction']} "
              f"(entered RSI={entry['rsi_at_entry']:.1f})")
        del watch[symbol]

    _save_json(WATCH_STATE_JSON, watch)


if __name__ == "__main__":
    watch = _load_json(WATCH_STATE_JSON, {})
    poll_once(watch)
    if os.environ.get("RUN_ONCE"):
        raise SystemExit(0)
    while True:
        time.sleep(POLL_INTERVAL_SECONDS)
        try:
            poll_once(watch)
        except Exception as e:
            print(f"Poll failed: {e} -- continuing")
