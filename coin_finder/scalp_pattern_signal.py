"""Standalone "system 3": 1h/4h quick-trade signal.

Was originally 15m-anchored; moved to 1h/4h focus 2026-11-06 at the user's
request (15m patterns were resolving/reversing too fast to act on cleanly).
Separate from system 2 (futures_pattern_signal.py, also 1h/4h/1d candles)
which is left entirely unchanged -- this is a parallel system, not a
replacement. The two now overlap more in timeframe than at launch, but
system 3 keeps its own distinct stack on top (funding/OI/order-flow
confirmation, flat-ROI SL/TP, two-path reversal+breakout detection) -- not
meant to be merged.

Spec (from the user, after reviewing real trade history of 15-40 minute
holds at 50-100x leverage against system 2's 1h/4h/1d-tuned signals):
  - Predict direction for the next ~1h, resolving within 4h at the latest.
  - SL capped at 40% account/margin loss -- flat ROI, not derived from
    liquidation distance like leverage_risk.py's default (see
    scalp_levels() below for why that distinction matters).
  - TP is confidence-tiered (2026-11-09, see TP_ROI_* below), NOT a flat
    target -- how far a winner is allowed to run scales with how much real
    evidence backs the call.
  - Leverage 20x-100x, tested at 50x.

ARCHITECTURE (flipped 2026-11-09 at the user's request -- "indicator
trends should not be the primary check", real order flow should be):

  PRIMARY GATE -- _flow_trigger(): a volume spike AND a clear net taker
  buy/sell imbalance on the signal candle. This alone decides the candidate
  DIRECTION (buy-dominant flow -> is_long=True). No candlestick shape or
  structural break is even looked at unless this fires first -- pattern
  and trend are secondary confirmation now, not the trigger.

  Once flow fires, current open-interest direction classifies which
  SECONDARY THESIS is more likely and gets tried first (the other is still
  tried as a fallback if the first doesn't confirm):

    CONTINUATION thesis (_breakout_confirm, OI rising = fresh money):
      structural break (VAH/VAL or trendline, 1h AND 4h via
      multi_timeframe_breakout_lean -- breakout_patterns.py) agreeing with
      the flow direction, funding not crowded against it, OI actually
      rising, not fighting the 1h/4h mid-period trend, top-trader
      positioning not against it.

    EXHAUSTION thesis (_reversal_confirm, OI flat/falling = no fresh
      money backing the move): candlestick shape (hammer/engulfing/
      shooting star -- candlestick_patterns.py) agreeing with the flow
      direction, funding not crowded against it, OI genuinely NOT rising,
      not fighting a fresh breakout the other way, not fighting the
      1h/4h mid-period trend, top-trader positioning not against it.

Top-trader long/short positioning (futures_context.py, Binance's
top-20%-by-margin cohort) is a hard gate on both theses -- a genuinely
different signal from funding/OI/flow since it's real positioning, not
derived from price/volume at all. Missing data passes (doesn't block),
same convention as funding/OI throughout.

RSI overbought(>=70)/oversold(<=30) is shown as context on every signal but
is NOT a hard gate on either thesis -- reversals often start exactly there,
and it's information to weigh, not an automatic veto.

This does NOT make high-leverage short-horizon trading safe -- CLAUDE.md's
rule still applies ("never use leverage at the start", "signals are not
guarantees"). Zero outcome-tracking history at launch -- unproven until
measured, not "safer because faster".
"""

import datetime
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "whale_scanner"))

from breakout_patterns import breakout_lean, next_key_level, trendline_breakout, vah_val_breakout
from candlestick_patterns import MIN_CANDLES_NEEDED, bearish_reversal, bullish_reversal
from entry_price import (
    fetch_live_futures_price,
    format_price,
    refresh_with_live_price,
    resistance_based_short,
    support_based_entry,
)
from futures_context import (
    funding_confirms_bearish,
    funding_confirms_bullish,
    get_open_interest_rising,
    liquidation_context,
    oi_confirms_breakout,
    oi_confirms_reversal,
    top_traders_confirm_bearish,
    top_traders_confirm_bullish,
)
from history import get_futures_klines
from leverage_risk import MAX_PORTFOLIO_ALLOCATION_PCT, maintenance_margin_rate_for, position_sizing
from portfolio_ledger import total_committed_margin
from scalp_outcomes import recent_alert_count
from technical import ema, rsi
from universe import get_futures_only_universe, get_universe

SCAN_SLEEP_SECONDS = 0.05
SCALP_INTERVAL = "1h"  # moved from "15m" 2026-11-06, see module docstring
SCALP_INTERVAL_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400}[SCALP_INTERVAL]
# Needs >=200 for momentum_context()'s RSI/EMA200 (also fixes a latent bug:
# at the old 15m anchor this was 100, which was ALWAYS < 200, so
# momentum_context() silently returned None on every call -- the RSI
# context line and the reversal path's EMA200 breakout-gate for SHORTs
# never actually evaluated anything other than their None-safe default).
CONTEXT_CANDLE_LIMIT = 220

# Flat ROI targets per the spec -- NOT derived from liquidation distance
# like leverage_risk.py's default (that approach scales SL/TP off how much
# room exists before liquidation; here the ask is a fixed, predictable
# risk/reward regardless of symbol, so this works backward from the ROI
# target to a price distance instead: price_move_pct = ROI_target / leverage.
# Widened 2026-11-06 from 30%/50% after checking scalp_outcomes.py: logged
# signals that eventually won but dipped against the call first needed up
# to ~33% ROI (at 30x) to survive that dip (p90 of n=8) -- 40% clears that
# with margin while staying well below the ~1.9%+ price move (median) that
# genuine losers run before failing, so it doesn't just give losers more
# rope. At 50x the same dip needs ~55% ROI to survive -- i.e. no flat ROI%
# fixes 50x's room problem, since leverage itself is what's compressing the
# price distance. Treat 30x as this system's real leverage, 50x as the
# knowingly-tighter aggressive option (see ALERT_LEVERAGES below).
SCALP_SL_ROI_MAX = 0.40  # flat regardless of confidence tier -- risk per trade doesn't change
# Confidence-tiered TP (2026-11-09, user's request): how far a winner is
# allowed to run scales with how much real evidence backs the call, not a
# flat target for every signal. Matches the "scalp fast, get out before
# momentum drops" philosophy -- a weak/exhaustion-driven read should bank a
# small move quickly, a structurally-backed one can be allowed to run
# further. See _tp_roi_for_lean() for the tier assignment logic.
TP_ROI_NOT_CONFIDENT = 0.20    # no flow trigger at all -- last-resort "plain candle color" read
TP_ROI_MEDIUM = 0.30           # flow triggered (real volume+delta) but didn't fully confirm
TP_ROI_CONFIDENT = 0.50        # fully confirmed -- any reversal, or a breakout without 4h confirmation
TP_ROI_SUPER_CONFIDENT = 1.50  # fully confirmed breakout WITH 4h confirmation specifically
# Safety floor regardless of the flat targets above: never let the stop sit
# at or beyond where the position actually gets force-liquidated (possible
# at high leverage + high-MMR altcoins). Keep the SL inside 80% of the real
# liquidation distance in that case.
LIQUIDATION_SAFETY_FRACTION = 0.8

DEFAULT_TOP_N = 29  # 5 priority + 24 other alts
PRIORITY_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"]

RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30

# Lookback windows for the dip/up range line, expressed as a count of 1h
# candles (1h = 1, 4h = 4, 1d = 24) so all three reuse the same 1h candle
# list already fetched for everything else here, rather than a separate
# fetch per period.
RANGE_WINDOWS = {"1h": 1, "4h": 4, "1d": 24}

# Exhaustion/reversal-risk warnings (2026-11-10, user's request): neither
# of these feeds signal confirmation or blocks a trade -- both are
# informational warning lines only, same convention as the liquidation
# confirmation line. First-guess thresholds, NOT validated against
# outcomes yet (see [[cbot_validate_before_trusting]] in memory) -- re-tune
# once scalp_predictions.db has enough repeat-alert/large-move occurrences
# to check whether these actually preceded a reversal or not.
REPEAT_ALERT_WINDOW_SECONDS = 12 * 3600  # trailing window for the same-direction repeat count
REPEAT_ALERT_WARN_COUNT = 3  # warn starting on the 3rd same-direction alert within the window
EXTENSION_MOVE_PCT_THRESHOLD = 5.0  # % high-low range over the 4h window that counts as "significant"
EXTENSION_POSITION_THRESHOLD = 0.8  # how close to the range extreme (0=low,1=high) counts as "already extended"

# Extra read-only info shown alongside the dip/up range: nearest
# resistance/support price and whether a breakout is currently showing, per
# timeframe -- using each timeframe's OWN native candles, so e.g. the 1d
# read reflects real multi-week structure. Purely informational: does NOT
# feed signal confirmation or TP capping, which stay on BREAKOUT_INTERVALS
# (1h/4h) above -- unrelated to this.
INFO_INTERVALS = ("1h", "4h", "1d")
INFO_CANDLE_LIMIT = 120  # same lookback-plus-headroom convention as BREAKOUT_CANDLE_LIMIT

VOLUME_LOOKBACK = 20
VOLUME_CONFIRM_MULTIPLIER = 1.2

# Net taker buy/sell imbalance required on the signal candle, as a fraction
# of that candle's own volume, for the move to count as driven by real
# aggressive order flow rather than high two-sided volume that nets to
# nothing.
DELTA_CONFIRM_PCT = 0.05

# Mid-period (1h/4h) trend read, used as a confirmation layer on top of the
# raw signal paths below -- a pattern that doesn't line up with where price
# actually sits structurally on 1h/4h is the kind of partial-signal usage
# that caused real losses in the trade history this system was built from.
# Mirrors technical.py's trend_score blend, just without 1d (a 1-4h-hold
# trade shouldn't anchor off a week of candles).
MID_PERIOD_INTERVALS = ("1h", "4h")
MID_PERIOD_CANDLE_LIMIT = 210

# Buffer kept between a take-profit and the next structural level it's
# capped against, so the TP actually fills before price tests/stalls at
# that level rather than sitting exactly on it.
TP_STRUCTURE_BUFFER_PCT = 0.001


def _resolve_symbols(top_n):
    """Spot universe + futures-only listings (e.g. EVAAUSDT -- never listed
    on Spot at all, see universe.py's get_futures_only_universe() docstring),
    merged and re-sorted by volume together so a liquid futures-only name
    isn't truncated out just for landing at the end of the spot list rather
    than ranked where its actual volume puts it."""
    combined = sorted(
        get_universe() + get_futures_only_universe(),
        key=lambda c: c["quote_volume_usdt"], reverse=True,
    )
    universe_symbols = [c["symbol"] for c in combined]
    rest = [s for s in universe_symbols if s not in PRIORITY_SYMBOLS]
    if top_n:
        return PRIORITY_SYMBOLS + rest[:max(0, top_n - len(PRIORITY_SYMBOLS))]
    return PRIORITY_SYMBOLS + rest


def _volume_confirms(candles, lookback=VOLUME_LOOKBACK, multiplier=VOLUME_CONFIRM_MULTIPLIER):
    """True if the current (still-forming) candle's volume, PROJECTED to a
    full candle at its pace so far, is >= multiplier x the average of the
    prior `lookback` CLOSED candles. Projecting (not comparing raw partial
    volume) matters specifically at the current 1h anchor -- a candle 5
    minutes into its hour has only ~8% of a full hour's volume by
    definition, so comparing it raw against a full-hour average would
    almost never confirm regardless of real pace (this bug was invisible
    at the old 15m anchor, where the partial-candle distortion was
    proportionally much smaller -- found 2026-11-09 when the flow gate
    wasn't firing for ANY of 23 scanned symbols simultaneously)."""
    if len(candles) < lookback + 1:
        return False
    avg = sum(c["volume"] for c in candles[-(lookback + 1):-1]) / lookback
    if avg <= 0:
        return False
    cur = candles[-1]
    elapsed_seconds = max(time.time() - cur["open_time"] / 1000, 30)  # floor avoids wild extrapolation right at candle open
    projected_volume = cur["volume"] * (SCALP_INTERVAL_SECONDS / elapsed_seconds)
    return projected_volume >= multiplier * avg


def _candle_delta_pct(candle):
    """(taker buy volume - sell volume) / total volume for one candle --
    positive = buy-dominant, negative = sell-dominant. Sell volume isn't
    returned by Binance directly, just volume - taker_buy_volume. None if
    the field is missing (e.g. an older cached candle) or volume is zero."""
    buy = candle.get("taker_buy_volume")
    volume = candle.get("volume")
    if buy is None or not volume:
        return None
    return (2 * buy - volume) / volume


def _delta_confirms(candle, is_long):
    """False only if order flow on the candle is clearly dominant AGAINST
    the trade direction. Missing data passes (True) -- same convention as
    futures_context.py's funding/OI checks, a failed/absent field shouldn't
    block an otherwise confirmed signal."""
    delta_pct = _candle_delta_pct(candle)
    if delta_pct is None:
        return True
    return delta_pct >= DELTA_CONFIRM_PCT if is_long else delta_pct <= -DELTA_CONFIRM_PCT


def scalp_levels(anchor_price, is_long, leverage, symbol=None, tp_roi_pct=TP_ROI_CONFIDENT):
    """SL is always flat (SCALP_SL_ROI_MAX); TP is confidence-tiered --
    pass one of the TP_ROI_* constants (see _tp_roi_for_lean()), not a
    fixed target for every signal. Both capped against the real
    liquidation distance for this symbol/leverage."""
    mmr = maintenance_margin_rate_for(symbol)
    liquidation_distance_pct = max(1 / leverage - mmr, 0.0005)

    sl_distance_pct = SCALP_SL_ROI_MAX / leverage
    tp_distance_pct = tp_roi_pct / leverage

    sl_capped = sl_distance_pct >= liquidation_distance_pct
    if sl_capped:
        sl_distance_pct = liquidation_distance_pct * LIQUIDATION_SAFETY_FRACTION

    sign = 1 if is_long else -1
    return {
        "leverage": leverage,
        "maintenance_margin_rate": mmr,
        "liquidation_distance_pct": liquidation_distance_pct,
        "sl_distance_pct": sl_distance_pct,
        "tp_distance_pct": tp_distance_pct,
        "sl_roi_pct": sl_distance_pct * leverage,
        "tp_roi_pct": tp_distance_pct * leverage,
        "sl_capped_by_liquidation": sl_capped,
        "liquidation_price": anchor_price * (1 - sign * liquidation_distance_pct),
        "stop_loss": anchor_price * (1 - sign * sl_distance_pct),
        "take_profit": anchor_price * (1 + sign * tp_distance_pct),
    }


CONFIDENCE_LABELS = {
    TP_ROI_NOT_CONFIDENT: "not confident",
    TP_ROI_MEDIUM: "medium",
    TP_ROI_CONFIDENT: "confident",
    TP_ROI_SUPER_CONFIDENT: "super confident",
}


def _tp_roi_for_lean(lean):
    """Picks the TP_ROI_* tier for a signal/lean dict -- see the tier
    constants above for the reasoning behind each threshold. Works on both
    a fully confirmed signal (from scan_scalp_signals/detect_signal, which
    always has confirmed=True and a method) and a fallback lean (from
    best_scalp_lean_for_symbol, confirmed=False with a checks score)."""
    if not lean.get("confirmed"):
        return TP_ROI_MEDIUM if lean.get("score", 0) > 0 else TP_ROI_NOT_CONFIDENT
    if lean.get("method") == "breakout" and lean.get("breakout_higher_tf_confirmed"):
        return TP_ROI_SUPER_CONFIDENT
    return TP_ROI_CONFIDENT


def scalp_entry_for_symbol(symbol, is_long):
    """EMA20/50/200 support/resistance entry zone from SCALP_INTERVAL (1h)
    candles -- used as the entry/"safe" reference price for both signal
    paths."""
    try:
        candles = get_futures_klines(symbol, SCALP_INTERVAL)
    except Exception:
        return None
    closes = [c["close"] for c in candles]
    if len(closes) < 200:
        return None

    ema20, ema50, ema200 = ema(closes, 20)[-1], ema(closes, 50)[-1], ema(closes, 200)[-1]
    close = closes[-1]
    entry = (
        support_based_entry(close, ema20, ema50, ema200) if is_long
        else resistance_based_short(close, ema20, ema50, ema200)
    )
    if entry is None:
        return None

    live_price = fetch_live_futures_price(symbol)
    if live_price is not None:
        entry = refresh_with_live_price(entry, live_price, is_long)
    return entry


def momentum_context(candles):
    """RSI(14) + whether price is above its own 1h EMA200 -- shown as
    context on every signal, and used by the reversal path's breakout gate
    (see detect_signal)."""
    closes = [c["close"] for c in candles]
    if len(closes) < 200:
        return None
    rsi_val = rsi(closes, 14)[-1]
    ema200_val = ema(closes, 200)[-1]
    close = closes[-1]
    return {
        "rsi": rsi_val,
        "overbought": rsi_val is not None and rsi_val >= RSI_OVERBOUGHT,
        "oversold": rsi_val is not None and rsi_val <= RSI_OVERSOLD,
        "above_ema200": close > ema200_val,
    }


def recent_range(candles, windows=RANGE_WINDOWS):
    """{"1h": {"low", "high"}, "4h": {...}} -- the lowest low ("dip") and
    highest high ("up") over the last N 1h candles for each window,
    counting back from the most recent candle (the request's "now"). Uses
    each candle's own high/low so an intra-candle wick counts, not just
    closes. Skips a window if there aren't enough candles for it yet."""
    result = {}
    for label, n in windows.items():
        window = candles[-n:]
        if len(window) < n:
            continue
        result[label] = {
            "low": min(c["low"] for c in window),
            "high": max(c["high"] for c in window),
        }
    return result


def _range_line(ranges):
    if not ranges:
        return None
    parts = [
        f"{label} dip {format_price(r['low'])} / up {format_price(r['high'])}"
        for label, r in ranges.items() if r is not None
    ]
    return "  |  ".join(parts) if parts else None


def level_info_for_symbol(symbol):
    """{"1h": {"resistance", "support", "breakout"}, "4h": {...}, "1d": {...}}
    -- nearest resistance (above price) / support (below price) and
    breakout_lean() per timeframe, from that timeframe's own native candles.
    Skips a timeframe on any fetch/data failure rather than failing the
    whole call."""
    info = {}
    for interval in INFO_INTERVALS:
        try:
            candles = get_futures_klines(symbol, interval, limit=INFO_CANDLE_LIMIT)
        except Exception:
            continue
        info[interval] = {
            "resistance": next_key_level(candles, is_long=True),
            "support": next_key_level(candles, is_long=False),
            "breakout": breakout_lean(candles),
        }
    return info


def _level_info_line(info):
    if not info:
        return None
    parts = []
    for interval in INFO_INTERVALS:
        entry = info.get(interval)
        if entry is None:
            continue
        bits = []
        if entry["resistance"] is not None:
            bits.append(f"res {format_price(entry['resistance'])}")
        if entry["support"] is not None:
            bits.append(f"sup {format_price(entry['support'])}")
        if entry["breakout"] is not None:
            bits.append(f"breakout {entry['breakout']['direction']}")
        if bits:
            parts.append(f"{interval}: " + " / ".join(bits))
    return "  |  ".join(parts) if parts else None


def mid_period_trend(symbol):
    """Blended 1h/4h directional read (EMA20/50/200 stack on each timeframe),
    used as a higher-timeframe filter on top of the signal paths below.
    Returns {"by_interval": {...}, "combined": "bullish"/"bearish"/"neutral"},
    or None if neither timeframe had enough data."""
    biases = {}
    for interval in MID_PERIOD_INTERVALS:
        try:
            candles = get_futures_klines(symbol, interval, limit=MID_PERIOD_CANDLE_LIMIT)
        except Exception:
            continue
        closes = [c["close"] for c in candles]
        if len(closes) < 200:
            continue
        ema20, ema50, ema200 = ema(closes, 20)[-1], ema(closes, 50)[-1], ema(closes, 200)[-1]
        close = closes[-1]
        if close > ema20 > ema50 > ema200:
            biases[interval] = "bullish"
        elif close < ema20 < ema50 < ema200:
            biases[interval] = "bearish"
        else:
            biases[interval] = "neutral"

    if not biases:
        return None
    values = list(biases.values())
    if values.count("bullish") > values.count("bearish"):
        combined = "bullish"
    elif values.count("bearish") > values.count("bullish"):
        combined = "bearish"
    else:
        combined = "neutral"
    return {"by_interval": biases, "combined": combined}


def _flow_trigger(candles):
    """Primary gate (flipped from pattern-first 2026-11-09 at the user's
    request -- indicator/pattern trends shouldn't be the primary check,
    real order flow should be): a candle with actual evidence of money
    moving -- a volume spike AND a clear net taker buy/sell imbalance --
    not a candlestick shape or structural break. Returns is_long (the flow
    direction) or None if there's no real flow signal to act on at all."""
    if not _volume_confirms(candles):
        return None
    delta_pct = _candle_delta_pct(candles[-1])
    if delta_pct is None or abs(delta_pct) < DELTA_CONFIRM_PCT:
        return None
    return delta_pct > 0


def _reversal_confirm(symbol, candles, context, is_long, mid_trend):
    """Secondary confirmation for a flow-triggered direction, under the
    EXHAUSTION thesis -- the flow move is happening WITHOUT fresh OI
    backing it (shorts covering into selling pressure, or similar), so
    pattern/trend here just need to not actively contradict the
    flow-given direction, not drive the call themselves. Always returns
    the checks dict (even on failure) so best_scalp_lean_for_symbol's
    fallback can show "how close did this get"; `result` is the full
    signal dict only if every check passes and an entry zone exists."""
    if is_long:
        shape_ok = bullish_reversal(candles)
        breakout_ok = context is None or not context["above_ema200"] or True
        # ^ a LONG breaking out ABOVE its own EMA200 is the continuation
        # case, not a contradiction -- only a SHORT fighting a fresh
        # breakUP is blocked (below). Kept explicit rather than just
        # `True` so the asymmetry is visible.
        funding_ok = funding_confirms_bullish(symbol)
        mid_ok = mid_trend is None or mid_trend["combined"] != "bullish"
    else:
        shape_ok = bearish_reversal(candles)
        breakout_ok = context is None or not context["above_ema200"]
        funding_ok = funding_confirms_bearish(symbol)
        mid_ok = mid_trend is None or mid_trend["combined"] != "bearish"

    oi_ok = oi_confirms_reversal(symbol, SCALP_INTERVAL)
    top_traders_ok = (
        top_traders_confirm_bullish(symbol, period=SCALP_INTERVAL) if is_long
        else top_traders_confirm_bearish(symbol, period=SCALP_INTERVAL)
    )
    checks = {
        "volume": True, "delta": True, "shape": shape_ok, "oi": oi_ok,
        "funding": funding_ok, "not_fighting_breakout": breakout_ok,
        "mid_trend_1h_4h": mid_ok, "top_traders": top_traders_ok,
    }
    result = None
    if all(checks.values()) and scalp_entry_for_symbol(symbol, is_long) is not None:
        result = {"is_long": is_long, "method": "reversal", "checks": checks}
    return checks, result


# Breakout detection runs on 1h AND 4h candles -- a VAH/VAL or trendline
# break on 4h reflects real multi-day structure, while 1h alone is more
# prone to noise. Checking both and requiring no direction conflict between
# them is what makes the signal trustworthy enough for a leveraged entry,
# not just the fastest timeframe available.
BREAKOUT_INTERVALS = ("1h", "4h")
BREAKOUT_CANDLE_LIMIT = 120  # same 96-candle-lookback + headroom margin as CONTEXT_CANDLE_LIMIT, per interval


def multi_timeframe_breakout_lean(symbol):
    """breakout_lean() independently on 1h and 4h candles. Returns None if
    no timeframe shows a breakout, OR if the timeframes that do show one
    disagree on direction (a 1h bullish break while 4h is breaking down is
    a contradiction, not a signal -- the whole point of checking multiple
    timeframes is to catch exactly that case rather than just trusting
    whichever one happened to fire). Otherwise returns
    {"direction", "timeframes": [...], "higher_tf_confirmed": bool, "detail": {...}}."""
    leans = {}
    for interval in BREAKOUT_INTERVALS:
        try:
            candles = get_futures_klines(symbol, interval, limit=BREAKOUT_CANDLE_LIMIT)
        except Exception:
            continue
        lean = breakout_lean(candles)
        if lean is None:
            continue
        lean = dict(lean)
        # kept alongside the direction/confirmed_by verdict so callers can
        # show the actual broken price level per timeframe, not just that
        # a break happened
        lean["vah_val"] = vah_val_breakout(candles)
        lean["trendline"] = trendline_breakout(candles)
        leans[interval] = lean

    if not leans:
        return None
    directions = {l["direction"] for l in leans.values()}
    if len(directions) > 1:
        return None

    direction = directions.pop()
    confirmed_intervals = sorted(leans.keys(), key=BREAKOUT_INTERVALS.index)
    return {
        "direction": direction,
        "timeframes": confirmed_intervals,
        "higher_tf_confirmed": "4h" in leans,
        "detail": leans,
    }


def _breakout_confirm(symbol, candles, is_long, mid_trend):
    """Secondary confirmation for a flow-triggered direction, under the
    CONTINUATION thesis -- OI is rising (fresh money), so check for an
    actual structural break (VAH/VAL/trendline) agreeing with the
    flow-given direction, rather than trusting the flow alone. Same
    (checks, result-or-None) return shape as _reversal_confirm -- see
    that docstring."""
    lean = multi_timeframe_breakout_lean(symbol)
    structure_ok = lean is not None and lean["direction"] == ("bullish" if is_long else "bearish")
    funding_ok = funding_confirms_bullish(symbol) if is_long else funding_confirms_bearish(symbol)
    oi_ok = oi_confirms_breakout(symbol, SCALP_INTERVAL)
    # A bullish breakout actively fighting a bearish 1h/4h trend (or vice
    # versa) is the lower-quality case -- allow neutral/agreeing, block the
    # contradiction.
    opposing = "bearish" if is_long else "bullish"
    mid_ok = mid_trend is None or mid_trend["combined"] != opposing
    top_traders_ok = (
        top_traders_confirm_bullish(symbol, period=SCALP_INTERVAL) if is_long
        else top_traders_confirm_bearish(symbol, period=SCALP_INTERVAL)
    )
    checks = {
        "volume": True, "delta": True, "structure": structure_ok,
        "oi_rising": oi_ok, "funding": funding_ok, "mid_trend_1h_4h": mid_ok,
        "top_traders": top_traders_ok,
    }
    result = None
    if all(checks.values()) and scalp_entry_for_symbol(symbol, is_long) is not None:
        result = {
            "is_long": is_long, "method": "breakout", "checks": checks,
            "breakout_timeframes": lean["timeframes"],
            "breakout_higher_tf_confirmed": lean["higher_tf_confirmed"],
            "breakout_detail": lean["detail"],
        }
    return checks, result


def detect_signal(symbol, candles, context, mid_trend):
    """Primary gate is _flow_trigger() (volume + order-flow delta) --
    flipped from pattern-first 2026-11-09 at the user's request, so
    pattern/trend only get checked at all (as secondary confirmation) once
    real flow evidence exists. Current OI direction picks which secondary
    thesis is tried first -- rising OI makes continuation (breakout) the
    more likely story, flat/falling/unknown makes exhaustion (reversal)
    more likely -- but either can still confirm on its own check
    regardless of that ordering. Returns a dict tagged with which method
    confirmed it, or None if neither confirms."""
    is_long = _flow_trigger(candles)
    if is_long is None:
        return None

    oi_rising = get_open_interest_rising(symbol, period=SCALP_INTERVAL)
    first, second = (
        (lambda: _breakout_confirm(symbol, candles, is_long, mid_trend),
         lambda: _reversal_confirm(symbol, candles, context, is_long, mid_trend))
        if oi_rising else
        (lambda: _reversal_confirm(symbol, candles, context, is_long, mid_trend),
         lambda: _breakout_confirm(symbol, candles, is_long, mid_trend))
    )
    _, result = first()
    if result is not None:
        return result
    _, result = second()
    return result


def scan_scalp_signals(symbols=None, top_n=DEFAULT_TOP_N):
    """{symbol: signal_dict} for every symbol confirmed by either path."""
    if symbols is None:
        symbols = _resolve_symbols(top_n)

    signals = {}
    for symbol in symbols:
        try:
            candles = get_futures_klines(symbol, SCALP_INTERVAL, limit=MIN_CANDLES_NEEDED)
            context_candles = get_futures_klines(symbol, SCALP_INTERVAL, limit=CONTEXT_CANDLE_LIMIT)
        except Exception:
            time.sleep(SCAN_SLEEP_SECONDS)
            continue

        context = momentum_context(context_candles)
        mid_trend = mid_period_trend(symbol)
        signal = detect_signal(symbol, candles, context, mid_trend)
        if signal is not None:
            signal["close_price"] = candles[-1]["close"]
            signal["mid_trend"] = mid_trend
            signal["confirmed"] = True
            signals[symbol] = signal
        time.sleep(SCAN_SLEEP_SECONDS)
    return signals


def best_scalp_lean_for_symbol(symbol):
    """Always-returns-something fallback for a single-symbol check: a
    confirmed signal from either path if one exists, else the closest
    partial match (reversal shape present but not fully confirmed), else a
    plain last-candle direction read. Mirrors system 2's
    best_lean_for_symbol in spirit."""
    try:
        candles = get_futures_klines(symbol, SCALP_INTERVAL, limit=MIN_CANDLES_NEEDED)
        context_candles = get_futures_klines(symbol, SCALP_INTERVAL, limit=CONTEXT_CANDLE_LIMIT)
    except Exception:
        return None
    if len(candles) < 2:
        return None

    context = momentum_context(context_candles)
    mid_trend = mid_period_trend(symbol)
    signal = detect_signal(symbol, candles, context, mid_trend)
    if signal is not None:
        signal["symbol"] = symbol
        signal["close_price"] = candles[-1]["close"]
        signal["score"] = len(signal["checks"])
        signal["confirmed"] = True
        signal["context"] = context
        signal["mid_trend"] = mid_trend
        return signal

    # Flow triggered but neither secondary confirm fully passed (if either
    # had, detect_signal() above would already have returned it) -- show
    # whichever of reversal/breakout got closer (by fraction of its own
    # checks passed, since the two have different check counts) as the
    # closest available read. No flow trigger at all is the weakest
    # fallback: plain last-candle direction, nothing else evaluated.
    cur = candles[-1]
    delta_pct = _candle_delta_pct(cur)
    is_long = _flow_trigger(candles)

    if is_long is not None:
        reversal_checks, _ = _reversal_confirm(symbol, candles, context, is_long, mid_trend)
        breakout_checks, _ = _breakout_confirm(symbol, candles, is_long, mid_trend)
        reversal_frac = sum(reversal_checks.values()) / len(reversal_checks)
        breakout_frac = sum(breakout_checks.values()) / len(breakout_checks)
        if breakout_frac > reversal_frac:
            method, checks = "breakout", breakout_checks
        else:
            method, checks = "reversal", reversal_checks
        shape = True  # flow was real -- distinguishes this from the no-signal-at-all case below
    else:
        is_long = cur["close"] > cur["open"]
        method = "reversal"
        checks = {"volume": False, "delta": False, "shape": False, "oi": False,
                  "funding": False, "not_fighting_breakout": False,
                  "mid_trend_1h_4h": False, "top_traders": False}
        shape = None

    score = sum(checks.values())
    return {
        "symbol": symbol, "is_long": is_long, "method": method, "shape": shape,
        "checks": checks, "score": score, "confirmed": score == len(checks),
        "close_price": cur["close"], "context": context, "mid_trend": mid_trend,
        "delta_pct": delta_pct, "ranges": recent_range(context_candles),
        "level_info": level_info_for_symbol(symbol),
    }


def format_lean(lean):
    total = len(lean["checks"])
    method_label = "breakout" if lean.get("method") == "breakout" else "reversal"
    if lean["confirmed"]:
        extra = f" on {'/'.join(lean['breakout_timeframes'])}" if lean.get("breakout_timeframes") else ""
        header = f"✅ <b>Fully confirmed</b> ({method_label}{extra}, {total}/{total} checks)"
    elif lean.get("shape"):
        header = f"⚠️ <b>Partial {method_label} match</b> ({lean['score']}/{total} checks) -- flow present, not fully confirmed"
    else:
        header = "ℹ️ No pattern or breakout on 1h/4h -- plain last-candle direction only"
    checks_line = "  ".join(f"{name}{'✅' if passed else '❌'}" for name, passed in lean["checks"].items())
    ctx = lean.get("context")
    rsi_line = None
    if ctx and ctx.get("rsi") is not None:
        tag = " (overbought)" if ctx["overbought"] else " (oversold)" if ctx["oversold"] else ""
        rsi_line = f"RSI(14): {ctx['rsi']:.1f}{tag}  |  {'above' if ctx['above_ema200'] else 'below'} {SCALP_INTERVAL} EMA200"
    delta_pct = lean.get("delta_pct")
    if delta_pct is not None:
        side = "buy" if delta_pct > 0 else "sell"
        rsi_line = (rsi_line + "\n" if rsi_line else "") + f"Order flow ({SCALP_INTERVAL}): {abs(delta_pct):.0%} {side}-dominant"
    range_line = _range_line(lean.get("ranges"))
    if range_line:
        rsi_line = (rsi_line + "\n" if rsi_line else "") + range_line
    level_line = _level_info_line(lean.get("level_info"))
    if level_line:
        rsi_line = (rsi_line + "\n" if rsi_line else "") + level_line
    mid = lean.get("mid_trend")
    if mid is not None:
        parts = "  ".join(f"{iv}:{bias}" for iv, bias in mid["by_interval"].items())
        rsi_line = (rsi_line + "\n" if rsi_line else "") + f"Mid-period trend ({parts}) → {mid['combined']}"
    return header, checks_line, rsi_line


def _cap_tp_before_structure(levels, anchor_price, is_long, structural_level):
    """Tighten the flat-ROI take-profit to sit just before the next
    structural level (VAH/trendline resistance for a LONG, VAL/trendline
    support for a SHORT) when that level is closer than the flat target --
    the point of a quick trade is to bank the move before price reacts at
    a level it's already approaching, not overshoot into it."""
    if structural_level is None:
        return levels
    flat_tp = levels["take_profit"]
    if is_long:
        capped = structural_level * (1 - TP_STRUCTURE_BUFFER_PCT)
        if capped >= flat_tp:
            return levels
    else:
        capped = structural_level * (1 + TP_STRUCTURE_BUFFER_PCT)
        if capped <= flat_tp:
            return levels
    new_levels = dict(levels)
    distance_pct = abs(capped - anchor_price) / anchor_price
    new_levels["take_profit"] = capped
    new_levels["tp_distance_pct"] = distance_pct
    new_levels["tp_roi_pct"] = distance_pct * levels["leverage"]
    new_levels["tp_capped_by_structure"] = True
    new_levels["structural_level"] = structural_level
    return new_levels


def _append_trade_block(lines, symbol, is_long, leverage, account_equity, risk_pct, header_lines,
                         tp_roi_pct=TP_ROI_CONFIDENT, context_candles=None):
    entry = scalp_entry_for_symbol(symbol, is_long)
    if entry is None:
        lines.append(
            f"\n{symbol}: no valid {'support' if is_long else 'resistance'} level for a "
            f"{'LONG' if is_long else 'SHORT'} entry right now (price already past every {SCALP_INTERVAL} EMA)"
        )
        return

    anchor_price = entry["entry_low"] if is_long else entry["entry_high"]
    levels = scalp_levels(anchor_price, is_long, leverage, symbol=symbol, tp_roi_pct=tp_roi_pct)
    live_levels = scalp_levels(entry["current_price"], is_long, leverage, symbol=symbol, tp_roi_pct=tp_roi_pct)

    structural_level = _nearest_structural_level(symbol, is_long, anchor_candles=context_candles)
    levels = _cap_tp_before_structure(levels, anchor_price, is_long, structural_level)
    live_levels = _cap_tp_before_structure(live_levels, entry["current_price"], is_long, structural_level)

    emoji = "\U0001f7e2" if is_long else "\U0001f534"
    direction = "LONG" if is_long else "SHORT"
    lines.append(f"\n{emoji} <b>{direction} {symbol}</b>  (price now: {format_price(entry['current_price'])})")
    lines.append(f"Confidence: {CONFIDENCE_LABELS[tp_roi_pct]} (TP target {tp_roi_pct:.0%} ROI)")
    lines.extend(header_lines)

    # "safe price" = the EMA-based support/resistance entry zone itself --
    # the level this whole setup (and its SL/TP) is anchored to.
    lines.append(f"Entry/safe price: {format_price(entry['entry_low'])} - {format_price(entry['entry_high'])}")

    if entry["at_entry_now"]:
        lines.append("✅ <b>ENTER NOW</b> -- price is at the entry zone")
        lines.append(f"TP: {format_price(levels['take_profit'])}  ({levels['tp_roi_pct']:.0%} ROI)")
        lines.append(f"SL: {format_price(levels['stop_loss'])}  ({levels['sl_roi_pct']:.0%} ROI)")
        if levels.get("tp_capped_by_structure"):
            lines.append(f"  (TP capped before next level at {format_price(levels['structural_level'])}, not the full {tp_roi_pct:.0%} ROI target)")
        lines.append(
            f"Liquidation: {format_price(levels['liquidation_price'])} "
            f"({levels['liquidation_distance_pct']:.3%} away, MMR {levels['maintenance_margin_rate']:.2%})"
        )
        if levels["sl_capped_by_liquidation"]:
            lines.append(f"⚠️ SL tightened below the {SCALP_SL_ROI_MAX:.0%} ROI target -- liquidation distance was narrower")
    else:
        lines.append(
            f"⏳ <b>WAIT</b> -- do NOT enter at {format_price(entry['current_price'])}, "
            f"{entry['distance_from_entry_pct']:+.2f}% away from the entry zone, may not get hit"
        )
        lines.append("If acting now instead:")
        lines.append(f"TP: {format_price(live_levels['take_profit'])}  ({live_levels['tp_roi_pct']:.0%} ROI)")
        lines.append(f"SL: {format_price(live_levels['stop_loss'])}  ({live_levels['sl_roi_pct']:.0%} ROI)")
        lines.append(f"Liquidation: {format_price(live_levels['liquidation_price'])}")
        if live_levels.get("tp_capped_by_structure"):
            lines.append(f"  (TP capped before next level at {format_price(live_levels['structural_level'])}, not the full {tp_roi_pct:.0%} ROI target)")
        if live_levels["sl_capped_by_liquidation"]:
            lines.append(f"⚠️ SL tightened below the {SCALP_SL_ROI_MAX:.0%} ROI target -- liquidation distance was narrower")

    if account_equity:
        s = position_sizing(anchor_price, levels["sl_distance_pct"], leverage, account_equity, risk_pct,
                             committed_margin_usd=total_committed_margin())
        warn = "  ⚠️ exceeds account equity" if s["insufficient_margin"] else ""
        lines.append(
            f"Size: {s['position_size']:.6g}  ({format_price(s['position_notional_usd'])} notional)  "
            f"Margin: ${s['margin_required_usd']:,.2f}{warn}"
        )
        if s["exceeds_portfolio_cap"]:
            lines.append(
                f"⚠️ Would push committed margin to ${s['committed_margin_usd'] + s['margin_required_usd']:,.2f} "
                f"-- exceeds the {MAX_PORTFOLIO_ALLOCATION_PCT:.0%} portfolio cap "
                f"(${s['available_margin_usd']:,.2f} available). Run /positions to review what's open."
            )


def _liquidation_lines(symbol, is_long):
    confirms, sell_usd, buy_usd = liquidation_context(symbol, is_long)
    if confirms is True:
        return [f"⚡ Recent liquidations support this (longs ${sell_usd:,.0f} vs shorts ${buy_usd:,.0f})"]
    if confirms is False:
        return [f"⚡ Recent liquidations lean the other way (longs ${sell_usd:,.0f} vs shorts ${buy_usd:,.0f})"]
    return []


def _repeat_alert_lines(symbol, is_long):
    """Warns when this is the Nth+ same-direction alert on this symbol
    within REPEAT_ALERT_WINDOW_SECONDS -- repeated same-direction signals
    on one coin can mean the move is getting chased/exhausted rather than
    freshly confirmed each time. Counts PRIOR logged alerts only (this
    occurrence hasn't been recorded yet when message-building runs)."""
    direction = "LONG" if is_long else "SHORT"
    prior = recent_alert_count(symbol, direction, REPEAT_ALERT_WINDOW_SECONDS)
    occurrence = prior + 1
    if occurrence < REPEAT_ALERT_WARN_COUNT:
        return []
    window_hours = REPEAT_ALERT_WINDOW_SECONDS // 3600
    return [
        f"⚠️ {occurrence}{_ordinal_suffix(occurrence)} {direction} alert on {symbol} in the "
        f"last {window_hours}h -- possible exhaustion, elevated reversal risk"
    ]


def _ordinal_suffix(n):
    if 11 <= n % 100 <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _extension_lines(is_long, candles):
    """Warns when price has already made a large (>=EXTENSION_MOVE_PCT_THRESHOLD)
    move over the trailing 4h AND sits near the extreme of that move in
    the signal's own direction -- i.e. this signal would be chasing an
    already-extended move, not catching it early."""
    window = recent_range(candles).get("4h")
    if window is None:
        return []
    low, high = window["low"], window["high"]
    if high <= low:
        return []
    move_pct = (high - low) / low * 100
    if move_pct < EXTENSION_MOVE_PCT_THRESHOLD:
        return []
    position = (candles[-1]["close"] - low) / (high - low)
    extended = position >= EXTENSION_POSITION_THRESHOLD if is_long else position <= 1 - EXTENSION_POSITION_THRESHOLD
    if not extended:
        return []
    edge = "top" if is_long else "bottom"
    return [
        f"⚠️ Large move: {move_pct:.1f}% range over the last 4h, price already near the {edge} "
        f"-- chasing an extended move, elevated reversal risk"
    ]


def _breakout_level_lines(is_long, timeframes, detail):
    """Shows the actual breakout/resistance price (VAH/VAL and/or trendline
    level) that was broken, per confirming timeframe -- the "breakout
    price" / "resistance price" the output should name explicitly."""
    direction = "bullish" if is_long else "bearish"
    lines = []
    for interval in timeframes:
        info = detail.get(interval)
        if info is None:
            continue
        vv, tl = info.get("vah_val"), info.get("trendline")
        if vv and "vah_val" in info["confirmed_by"]:
            level = vv["vah"] if direction == "bullish" else vv["val"]
            lines.append(
                f"📊 [{interval}] Volume profile {'VAH' if direction == 'bullish' else 'VAL'} "
                f"broken: {format_price(level)}  (POC {format_price(vv['poc'])})"
            )
        if tl and "trendline" in info["confirmed_by"]:
            level = tl["resistance_now"] if direction == "bullish" else tl["support_now"]
            lines.append(
                f"📈 [{interval}] Trendline {'resistance' if direction == 'bullish' else 'support'} "
                f"broken: {format_price(level)}"
            )
    return lines


def _nearest_structural_level(symbol, is_long, anchor_candles=None):
    """Nearest next_key_level() across BREAKOUT_INTERVALS (1h/4h) -- the
    TP-before-structure cap should respect whichever timeframe's level
    price is actually going to react at first, not just the anchor one.
    `anchor_candles` lets a caller pass already-fetched SCALP_INTERVAL
    candles to avoid a redundant fetch for that one leg."""
    candidates = []
    for interval in BREAKOUT_INTERVALS:
        if interval == SCALP_INTERVAL and anchor_candles is not None:
            candles = anchor_candles
        else:
            try:
                candles = get_futures_klines(symbol, interval, limit=BREAKOUT_CANDLE_LIMIT)
            except Exception:
                continue
        level = next_key_level(candles, is_long)
        if level is not None:
            candidates.append(level)
    if not candidates:
        return None
    return min(candidates) if is_long else max(candidates)


# Leverages baked into the push alert itself (scalp_pattern_alert.py) so a
# freshly-confirmed signal is immediately actionable without a follow-up
# /fut_ command -- the user's two usual sizes, not every leverage /fut_
# supports.
ALERT_LEVERAGES = (30, 50)


def build_scalp_alert_message(symbol, signal, leverages=ALERT_LEVERAGES, account_equity=None, risk_pct=0.01):
    """"Ready to trade" push message for one freshly-confirmed signal --
    unlike build_scalp_digest_message (one leverage per call, for the /fut_
    command), this bakes in every leverage in `leverages` at once so the
    alert itself carries full entry/TP/SL/liquidation detail, not just a
    pointer to run a follow-up command."""
    is_long, method = signal["is_long"], signal["method"]
    direction = "LONG" if is_long else "SHORT"
    emoji = "\U0001f7e2" if is_long else "\U0001f534"
    tf_note = ""
    if method == "breakout":
        tf_note = f" via {'/'.join(signal['breakout_timeframes'])}"
        if signal.get("breakout_higher_tf_confirmed"):
            tf_note += " (4h confirmed)"
    lines = [f"{emoji} <b>System 3 READY TO TRADE: {direction} {symbol}</b> ({method}{tf_note})"]

    candles = get_futures_klines(symbol, SCALP_INTERVAL, limit=CONTEXT_CANDLE_LIMIT)
    if method == "breakout":
        lines += _breakout_level_lines(is_long, signal["breakout_timeframes"], signal["breakout_detail"])
    delta_pct = _candle_delta_pct(candles[-1])
    if delta_pct is not None:
        side = "buy" if delta_pct > 0 else "sell"
        lines.append(f"Order flow ({SCALP_INTERVAL}): {abs(delta_pct):.0%} {side}-dominant")
    range_line = _range_line(recent_range(candles))
    if range_line:
        lines.append(range_line)
    level_line = _level_info_line(level_info_for_symbol(symbol))
    if level_line:
        lines.append(level_line)
    lines += _liquidation_lines(symbol, is_long)
    lines += _repeat_alert_lines(symbol, is_long)
    lines += _extension_lines(is_long, candles)
    mid = signal.get("mid_trend")
    if mid is not None:
        parts = "  ".join(f"{iv}:{bias}" for iv, bias in mid["by_interval"].items())
        lines.append(f"Mid-period trend ({parts}) → {mid['combined']}")

    tp_roi_pct = _tp_roi_for_lean(signal)
    for leverage in leverages:
        lines.append(f"\n<b>── {leverage}x ──</b>")
        _append_trade_block(lines, symbol, is_long, leverage, account_equity, risk_pct, [],
                             tp_roi_pct=tp_roi_pct, context_candles=candles)
    return "\n".join(lines)


def build_scalp_digest_message(leverage, account_equity=None, risk_pct=0.01, symbols=None, top_n=DEFAULT_TOP_N):
    signals = scan_scalp_signals(symbols, top_n=top_n)

    lines = [
        f"⚡ <b>Quick-trade {leverage}x view</b>, 1h/4h ({datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d %H:%M} UTC)",
        "⚠️ System 3 -- direction for the next ~1h (resolves within 4h max). Primary gate is "
        "order flow (volume + buy/sell delta), not price pattern -- OI direction then classifies "
        "it as continuation (breakout) or exhaustion (reversal), each with funding/structure/"
        "top-trader confirmation. SL capped at 40% ROI (flat); TP is confidence-tiered "
        "20-150% ROI, not a single flat target. Zero outcome history on this design -- "
        "unproven, not a recommendation this leverage/horizon is safe.",
    ]

    if signals:
        for symbol, signal in sorted(signals.items()):
            is_long = signal["is_long"]
            candles = get_futures_klines(symbol, SCALP_INTERVAL, limit=CONTEXT_CANDLE_LIMIT)
            if signal["method"] == "breakout":
                tf_note = "/".join(signal["breakout_timeframes"])
                strength = " (4h confirmed)" if signal["breakout_higher_tf_confirmed"] else " (1h only, no 4h confirmation)"
                header = [f"✅ <b>Fully confirmed</b> via breakout on {tf_note}{strength}"]
                header += _breakout_level_lines(is_long, signal["breakout_timeframes"], signal["breakout_detail"])
            else:
                header = [f"✅ <b>Fully confirmed</b> via {signal['method']}"]
            delta_pct = _candle_delta_pct(candles[-1])
            if delta_pct is not None:
                side = "buy" if delta_pct > 0 else "sell"
                header.append(f"Order flow ({SCALP_INTERVAL}): {abs(delta_pct):.0%} {side}-dominant")
            range_line = _range_line(recent_range(candles))
            if range_line:
                header.append(range_line)
            level_line = _level_info_line(level_info_for_symbol(symbol))
            if level_line:
                header.append(level_line)
            mid = signal.get("mid_trend")
            if mid is not None:
                parts = "  ".join(f"{iv}:{bias}" for iv, bias in mid["by_interval"].items())
                header.append(f"Mid-period trend ({parts}) → {mid['combined']}")
            header += _liquidation_lines(symbol, is_long)
            header += _repeat_alert_lines(symbol, is_long)
            header += _extension_lines(is_long, candles)
            _append_trade_block(lines, symbol, is_long, leverage, account_equity, risk_pct, header,
                                 tp_roi_pct=_tp_roi_for_lean(signal), context_candles=candles)
        return "\n".join(lines)

    resolved_symbols = symbols if symbols is not None else _resolve_symbols(top_n)
    leans = [l for l in (best_scalp_lean_for_symbol(s) for s in resolved_symbols) if l is not None]
    if not leans:
        lines.append("\nNo data available for any tracked symbol right now.")
        return "\n".join(lines)

    leans.sort(key=lambda l: l["score"], reverse=True)
    show = leans if len(resolved_symbols) == 1 else leans[:1]
    lines.append(f"\nNothing fully confirmed -- closest available read{'s' if len(show) > 1 else ''}:")
    for lean in show:
        header_text, checks_line, rsi_line = format_lean(lean)
        header = [header_text, checks_line]
        if rsi_line:
            header.append(rsi_line)
        header += _liquidation_lines(lean["symbol"], lean["is_long"])
        header += _repeat_alert_lines(lean["symbol"], lean["is_long"])
        _append_trade_block(lines, lean["symbol"], lean["is_long"], leverage, account_equity, risk_pct, header,
                             tp_roi_pct=_tp_roi_for_lean(lean))
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="System 3: 1h/4h quick-trade signal scan")
    parser.add_argument("--leverage", type=int, default=50)
    parser.add_argument("--equity", type=float, help="account equity in USD")
    parser.add_argument("--risk-pct", type=float, default=1.0, help="%% of account to risk per trade")
    parser.add_argument("--symbol", help="check a single symbol instead of scanning the top-N universe")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N, help="scan the top N symbols by 24h volume (0 = everything)")
    args = parser.parse_args()

    symbols = [args.symbol] if args.symbol else None
    print(build_scalp_digest_message(
        args.leverage, account_equity=args.equity, risk_pct=args.risk_pct / 100,
        symbols=symbols, top_n=(args.top_n or None),
    ))
