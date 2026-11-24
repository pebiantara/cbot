"""Standalone "quick scalp": fast, frequent, small-profit-above-fee futures
signal -- /scalp_<symbol>_<leverage>.

Deliberately separate from system 3 (scalp_pattern_signal.py, /fut_,
1h/4h-anchored, SL 40% flat / TP 20-150% confidence-tiered). /fut_ targets
fewer, higher-conviction, bigger-ROI setups with a wide stop built to
survive a real pullback before confirming it was wrong -- this targets
MANY small trades resolving well under 1h, each banking a profit just
comfortably above Binance's round-trip fee rather than letting a winner
run. Built 2026-11-09 at the user's explicit request, after flagging that
/fut_'s 40% SL (data-validated for ITS style) would give this style a
terrible risk/reward if just reused -- this has its OWN, as-yet-completely-
unvalidated SL/TP. Re-check these numbers once quick_scalp_outcomes.py
shows real results -- same lesson as /fut_'s original 30%/50% guess that
later got corrected from real data.

Binance futures round-trip taker fee is ~0.10% of notional -- in ROI terms
that's fee_pct_of_margin = 0.10% x leverage (~5% ROI at 50x, ~3% at 30x,
~10% at 100x). QUICK_SCALP_TP_ROI is sized to sit meaningfully above that
at the leverages this is meant for (30-50x), not just barely over it --
but note the margin shrinks a lot at very high leverage since TP is a flat
ROI number, same inherent tradeoff /fut_ has.

PRIMARY GATE -- rolling order flow, not a candle bucket (redesigned
2026-11-09, same day as the simpler versions below, after the user hit a
real problem testing it live: direction kept flipping within minutes).
History: first version checked delta on a single 15m candle; that candle
is a BUCKET that resets and recomputes from scratch at each 15m boundary,
so the SAME underlying flow could read as +15% at :02 past the hour and
-15% at :09 past it, purely from which trades happened to land in the
still-forming bucket so far -- not genuine reversal, just how buckets
work. Requiring 2 consecutive 15m candles to agree was tried and
abandoned immediately (0/25 symbols qualified in testing -- a prior
closed candle is often much calmer than a bursty current one even when
pointed the same way, too strict for the stated frequency goal).

The actual fix: _rolling_delta() aggregates 1-MINUTE candles over a
trailing QUICK_DELTA_WINDOW_MINUTES window, recomputed fresh every call --
a continuously sliding window, not a bucket with a hard reset boundary.
This reacts just as fast (updates every minute) while being far less
noisy (aggregating real trade flow over several minutes smooths one
lopsided moment rather than amplifying it). Also requires at least
QUICK_DELTA_MIN_TRADE_COUNT trades in that window -- a cheap way to tell
"broad real participation" apart from "one or two large orders skewed the
ratio" (that data, trade_count, comes from Binance's kline `numberOfTrades`
field, added to history.py's parser specifically for this). The floor
(1000) was picked by checking real trade counts on the quietest coins in
the tracked universe at the time (4000-14000 per 10min window even for
the calmest ones) -- comfortably below all of them, so it's a sanity
floor against a genuine outlier, not a routine blocker.

  1. PRIMARY GATE: rolling delta over QUICK_DELTA_WINDOW_MINUTES of 1m
     candles >= QUICK_DELTA_CONFIRM_PCT (30% as of 2026-11-11, was 20%), backed by
     >= QUICK_DELTA_MIN_TRADE_COUNT trades in that window.
  2. Funding not crowded against it, 1h/4h mid-period trend not opposing
     it -- light sanity checks, not a full confirmation stack.

UPDATE 2026-11-10: delta-only turned out to be too noisy in practice --
~41 signals/hour with hit rate (direction right AND moved >= the 0.3% TP
threshold) only 17-27% across every horizon, near-zero/negative average
return. Pure order-flow delta was firing on transient imbalance with no
real price follow-through -- "small moving up and down," not a real
signal. Three checks ADDED BACK on top of the delta gate (none of this
replaces it, delta is still the primary trigger):
  3. `_quick_momentum_confirms()`: real price move over the trailing
     QUICK_MOMENTUM_LOOKBACK_CANDLES 15m candles, >= QUICK_MOMENTUM_MIN_MOVE_PCT,
     agreeing with delta's direction -- the original pre-redesign check,
     reinstated.
  4. `_quick_volume_confirms()`: projected-volume-vs-average check, same
     partial-candle projection fix as scalp_pattern_signal.py's
     `_volume_confirms` (adapted to 15m here) -- a real volume spike, not
     just the delta gate's trade-count floor (which only screens out
     outlier-skewed quiet markets, doesn't require elevated volume).
  5. `_quick_candle_confirms()`: the latest closed 15m candle must have a
     real body (not a doji/indecision bar) in the signal's own direction --
     cheap guard against triggering on a candle with no actual directional
     conviction at the candle level.
All three still first-guess/unvalidated like the delta thresholds --
re-check against quick_scalp_outcomes.py once enough post-2026-11-10
signals accumulate.

UPDATE 2026-11-10 (second update same day) -- CHASE FLIPPED TO FADE.
Even with momentum/volume/candle added, the user manually took LTC/XRP/ENA
trades off real alerts and reported LTC+XRP reversed shortly after entry
(ENA only profited from an early exit on a brief favorable wick before it
also reversed). Research into professional order-flow scalping technique
("absorption", "delta divergence", "liquidity sweep/stop-hunt") found
these treat exactly this module's PRIMARY GATE -- a strong one-sided delta
spike -- as a likely EXHAUSTION/stop-hunt signal to FADE, not a
continuation signal to chase; the recommended play is to enter opposite
the spike once it's confirmed, not with it. Backtested this directly
against the FULL logged history before changing anything (not just the 3
anecdotes): for every already-logged prediction, computed what the
outcome would have been had the direction been flipped. Result, n=1145,
every horizon: fading beat chasing every time (15m: 18.8% orig vs 21.7%
fade; 30m: 21.3% vs 27.3%; 1h: 27.3% vs 30.6%), and the original chase
direction's average signed return was NEGATIVE at every single horizon
(-0.02% to -0.16%) -- the system's own directional call has been
systematically wrong on average, not just occasionally unlucky. The edge
is real but modest (not a flip to a strong positive edge, more "chasing
had a real negative edge, fading removes most of it") -- LONG-origin
signals showed the biggest gap (1h: 23.8% chase vs 39.3% fade).

Implementation: `detect_quick_signal()` still uses delta to find which
side just pushed hard (`push_is_long`), and still requires
momentum/volume/candle to confirm that push was real (validates it's a
genuine sweep worth fading, not pure noise) -- none of that logic
changed. What changed: the EMITTED signal's direction (`is_long`) is now
the OPPOSITE of the push (`fade_is_long = not push_is_long`), and the
funding/mid-trend sanity checks are now evaluated against the FADE
(actual trade) direction, not the push direction, since those are meant
to avoid fighting the broader trend with the trade you're actually
taking. `_quick_signal_header()`'s order-flow line now explicitly says
"fading" to avoid looking contradictory (e.g. a LONG signal showing
"sell-dominant" order flow is correct and intentional post-flip, not a
bug). Still first-guess/unvalidated in degree (the backtest shows
direction, not that THIS exact entry/TP/SL mechanism captures it) --
re-check real outcomes once enough post-flip signals accumulate.
"""

import datetime
import os
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from entry_price import (
    fetch_live_futures_price,
    format_price,
    refresh_with_live_price,
    resistance_based_short,
    support_based_entry,
)
from breakout_patterns import breakout_lean, next_key_level
from candlestick_patterns import reversal_lean
from ema_ribbon_vwap import ribbon_direction, vwap_distance_pct, vwap_favors_fade
from futures_context import funding_confirms_bearish, funding_confirms_bullish, oi_confirms_reversal
from history import get_futures_klines
from leverage_risk import MAX_PORTFOLIO_ALLOCATION_PCT, maintenance_margin_rate_for, position_sizing
from portfolio_ledger import total_committed_margin
from scalp_pattern_signal import _cap_tp_before_structure, _resolve_symbols, mid_period_trend
from technical import ema

# Own universe size, separate from System 3's DEFAULT_TOP_N (29) -- bumped
# to 50 on 2026-11-10 at the user's request since the stacked
# momentum/volume/candle/funding/mid-trend gates leave long quiet
# stretches when scanning only 29 symbols. System 3 (/fut_) untouched.
QUICK_SCALP_TOP_N = 50

# Added 2026-11-12 -- a forward-walk backtest (14d then 30d, both
# directions consistent) showed BTCUSDT losing net-of-fees under every
# SL/TP config tested, getting WORSE with a wider stop each time, unlike
# every other symbol tried. BTC is hardcoded into scalp_pattern_signal.py's
# shared PRIORITY_SYMBOLS (used by System 3 too), so it's always in this
# module's universe unless filtered out here -- scoped to quick-scalp
# only, System 3's own use of BTC is untouched/unvalidated either way.
QUICK_SCALP_EXCLUDED_SYMBOLS = {
    "BTCUSDT",
    # RWUSDUSDT -- added 2026-11-15, a repeat offender: timed out
    # ("no response") 5 separate times across 2026-11-14/15 alone, more
    # than any other symbol by far. RWUSD is a stablecoin, so this pair
    # has near-zero real volatility anyway -- excluding it costs nothing
    # strategy-wise and removes a recurring source of scan slowness.
    "RWUSDUSDT",
}

QUICK_SCALP_INTERVAL = "15m"  # still used for the entry zone (EMA20/50/200) -- doesn't need to be as fast as the trigger

# Rolling order-flow window -- see module docstring for why this replaced
# a 15m-candle-bucket check. Not validated, first guess like everything
# else here.
QUICK_DELTA_WINDOW_MINUTES = 10
QUICK_DELTA_CONFIRM_PCT = 0.20  # reverted to 0.20 2026-11-11 (same day as the 0.30 raise)
# -- 0.30 WAS backtested as better quality (40.0% fade-hit vs 37.0% at 0.20,
# n=200) but cut real live frequency to ~1.8 signals/hour across the whole
# ~33-symbol universe (from ~41/hour originally) -- too thin for the user's
# actual goal ("trade as much as we can, even just small profit"). Reverted
# specifically to recover frequency; the OTHER lever (QUICK_TREND_LOOKBACK_
# CANDLES's "already running" filter, below) stays ON since it's a cheaper
# frequency cost for a real quality gain (~14% of signals cut, 31.7% vs
# 20.6% fade-hit). Trade-count was tested too and found to go the WRONG way
# (more trades = worse, likely just major-coin noise) -- NOT used as a filter.
QUICK_DELTA_MIN_TRADE_COUNT = 1000

# Market-breadth veto -- added 2026-11-15 after a real live incident: a
# BTC-led broad rally made DOGE/SOL/ENA/TRUMP/FIL/ICP all show a strong
# buy-dominant push within the same ~35min window, firing 6 independent
# SHORT fades that ALL lost (price kept rising -1.1% to -2.4% against
# every one of them). Backtested the exact mechanism on 5 days x 8
# symbols' real 1m data (fee-aware TP/SL-hit-first simulator, raw
# delta-only gate, no other filters stacked): bucketing candidates by
# what fraction of OTHER tracked symbols were ALSO leaning the same
# direction at that same moment --
#   breadth 0.0-0.2 (isolated, coin-specific): +0.50%/trade (n=502) --
#     the ONLY bucket that was net positive on its own
#   breadth 0.2-0.4: -2.73%/trade (n=446)
#   breadth 0.4-0.6: -4.74%/trade (n=1213)
#   breadth 0.6-1.0 (crowded/market-wide, e.g. the incident above): -7.98%/trade (n=1187)
# Monotonic and large -- fading an isolated push works, fading into a
# market-wide move doesn't, because it's not symbol-specific exhaustion
# at all, it's the whole market still trending. BREADTH_VETO_THRESHOLD
# set at the one bucket boundary that was actually profitable; see
# _market_breadth()/_build_delta_map() for how this is computed live.
BREADTH_LEAN_DELTA = 0.10          # another symbol counts as "leaning the same way" above this (lower bar than the 0.20 trigger itself)
BREADTH_VETO_THRESHOLD = 0.2

# CHASE mode -- added 2026-11-15, same day, at the user's request to make
# the engine do SOMETHING useful during exactly the high-breadth windows
# the veto above sits out, instead of just going quiet. Re-ran the SAME
# backtest bucketing but compared FADE vs CHASE (same direction as the
# push, not opposite) per bucket:
#   breadth 0.0-0.2: FADE +1.79%/trade  vs CHASE -2.32%/trade  (n=540)
#   breadth 0.2-0.4: FADE -1.19%/trade  vs CHASE +0.00%/trade  (n=456)
#   breadth 0.4-0.6: FADE -3.87%/trade  vs CHASE +0.24%/trade  (n=1159)
#   breadth 0.6-1.0: FADE -9.71%/trade  vs CHASE +4.78%/trade  (n=1253)
# A genuine crossover, not just "fade stops working" -- below 0.2,
# fading an isolated push wins; above ~0.4, riding a crowded push wins
# instead. CHASE_BREADTH_FLOOR set at that crossover point. Layering the
# SAME momentum/volume/candle confirmation the fade engine already uses
# (checked against the PUSH direction here, not the fade direction) on
# top of the raw breadth>=0.4 trigger took naive chase (+3.74%/trade,
# n=2273) to confirmed chase (+16.53%/trade, n=202) -- same quality lift
# pattern as every other confirmation layer in this file, re-validated
# independently for chase rather than assumed to transfer from fade.
# funding/mid_trend_1h_4h/ribbon_or_vwap_favorable/candle-veto are
# DELIBERATELY NOT reused in detect_quick_signal_chase -- those were
# tuned for a mean-reversion thesis, not re-validated for continuation,
# don't assume they transfer without testing first.
CHASE_BREADTH_FLOOR = 0.4

# Extension cap -- added 2026-11-16 after the user flagged chase entries
# as "too noisy and late, in the middle of the move, not the start."
# First tested TIME since the push's delta crossing (require <=3min
# old) -- backtested WORSE, not better (stale pushes >3min old: +20.28%
# /trade, n=1839; fresh <=3min: +7.80%/trade, n=1074 -- a sustained push
# is more trustworthy than a brand-new one, confirmed at both 5min and
# 1min step granularity). So instead tested PRICE EXTENSION (how far
# price already moved in the push's own direction over the same 10min
# window) as a separate variable from time -- NOT monotonic, but a real
# dead zone: extension 0-1% covered 86% of all candidates and was
# consistently good (+15.96%/trade at 0-0.5%, +17.77%/trade at 0.5-1%),
# while 1-2% extension was actively bad (-2.32%/trade, n=253). The
# 2%+ bucket looked great (+58.52%) but n=32 is too thin to trust.
# CHASE_EXTENSION_MAX_PCT set at that 1% boundary -- cuts the one
# confirmed-bad pocket, keeps the two confirmed-good ones, doesn't rely
# on the untrustworthy tail. (Separately tested picking only the
# single highest breadth*|delta| "strength" candidate per cycle instead
# of alerting every qualifying one -- made results WORSE, not better,
# +12.70%/trade vs +15.99% taking every signal -- NOT used.)
CHASE_EXTENSION_LOOKBACK_MINUTES = 10  # matches the delta window itself
CHASE_EXTENSION_MAX_PCT = 1.0

# Added back 2026-11-10 (see module docstring UPDATE) -- delta-only was
# too noisy/unvalidated. All first-guess, unvalidated numbers.
QUICK_SCALP_INTERVAL_SECONDS = 15 * 60
QUICK_MOMENTUM_LOOKBACK_CANDLES = 3  # the original pre-redesign window
QUICK_MOMENTUM_MIN_MOVE_PCT = 0.3  # matches quick_scalp_outcomes.RISE_THRESHOLD_PCT -- shouldn't demand more than the "hit" bar itself
QUICK_VOLUME_LOOKBACK = 20
QUICK_VOLUME_CONFIRM_MULTIPLIER = 1.2  # same default System 3 uses
QUICK_CANDLE_BODY_MIN_FRACTION = 0.3  # body/range -- filters out dojis/indecision bars

# "Already running" trend filter (2026-11-11) -- see QUICK_SCALP_VERSION
# below for rollback. Backtested against ~236 sampled historical fade
# outcomes before implementing: signals where price had already moved
# >= QUICK_TREND_MOVE_PCT_THRESHOLD in the PUSH direction over the
# trailing QUICK_TREND_LOOKBACK_CANDLES had a 20.6% fade-hit rate vs 31.7%
# for the rest -- a real, validated gap (first of four filter ideas
# tried that actually showed one). Does NOT catch every miss -- e.g. the
# ONDOUSDT case that motivated this had only a -0.62% prior move, below
# this threshold; a looser 0.5% threshold would catch that case but
# reverses the aggregate signal (32.2% vs 28.9%, backwards) -- kept at 1%
# since the validated number matters more than one anecdote.
QUICK_TREND_LOOKBACK_CANDLES = 6  # 1.5h of 15m candles
QUICK_TREND_MOVE_PCT_THRESHOLD = 1.0

# 50 matches exactly what the EMA-ribbon/VWAP backtest fetched (2026-11-13)
# -- fewer candles would make live EMA13/VWAP noisier than what was
# actually validated, since EMA13 needs real history to converge.
QUICK_RIBBON_VWAP_CANDLES_NEEDED = 50
QUICK_CANDLE_FETCH_LIMIT = max(QUICK_VOLUME_LOOKBACK + 1, QUICK_MOMENTUM_LOOKBACK_CANDLES + 1,
                                QUICK_TREND_LOOKBACK_CANDLES + 1, QUICK_RIBBON_VWAP_CANDLES_NEEDED)

# Bump this whenever detect_quick_signal's gating logic changes, and tag
# a coin_finder/quick_scalp_signal.py.v_<name>_<date> backup alongside it
# (see cbot_quick_scalp_signal.md memory for the rollback list) -- lets
# the user name a version in conversation ("go back to v_fade-only")
# without needing git (this project has no git repo).
QUICK_SCALP_VERSION = "v_fade+trend-filter+delta02+wide-sl04+no-btc+candle-veto+ribbon-vwap+breadth-veto_2026-11-15"

# Binance futures round-trip taker fee, ~0.10% of notional -- used only to
# size SL/TP meaningfully above cost in the docstring/output math below,
# not charged or tracked anywhere in this file.
BINANCE_ROUNDTRIP_FEE_PCT = 0.10

# Widened 2026-11-12 -- a fee-aware TP/SL-hit-first backtest (not just
# "return at a fixed horizon") on 344 real logged signals showed the old
# flat 0.10/0.30 floor got stopped out by ordinary noise 67.4% of the
# time regardless of whether the direction call was right, for a net
# -0.0553%/trade (-19.0% cumulative) after Binance's real ~0.10%
# round-trip fee. Sweeping the SL width at the SAME 3:1 reward:risk shape
# flipped that to +0.0552%/trade (+19.0% cumulative) at 0.40/1.20 -- the
# best point found in that sweep (0.50/1.50 was close behind, slightly
# worse). Rollback if this makes live results worse:
# quick_scalp_signal.py.v_fade+trend-filter+delta02+dynamic-sl_2026-11-11_before-sl-widen-btc-exclude
QUICK_SCALP_SL_ROI = 0.40
QUICK_SCALP_TP_ROI = 1.20
# Sanity ceiling on the dynamic (structural) TP -- when no closer
# structural level exists to cap into, the 3:1 ratio off a widened SL can
# imply a target that would take far longer than this system's intended
# hold to reach. Set equal to the new flat QUICK_SCALP_TP_ROI so the
# un-widened case is exactly the backtest-validated 0.40/1.20 and
# structural widening never pushes TP past what was actually tested.
QUICK_SCALP_TP_ROI_CEILING = 1.20
LIQUIDATION_SAFETY_FRACTION = 0.8

# 30x dropped 2026-11-10 at the user's request -- /scalp_ now only supports
# 50x/100x (the command rejects anything else, see webhook_server.py's
# scalp_match handler); the push alert defaults to 50x only (was 30x+50x
# baked in together).
QUICK_SCALP_ALLOWED_LEVERAGES = (50, 100)
ALERT_LEVERAGE = 50

# ============================================================================
# PRO MODE (structure-first) -- added 2026-11-11, NOT a replacement of the
# fade engine above. A separate, slower, opt-in detection path
# (detect_quick_signal_pro / _quick_confluence_score / _quick_pro_levels
# below) built per the user's explicit request to stop bolting independent
# filters onto a reactive flow-first trigger and instead require real
# market structure (a support/resistance "wall") to exist BEFORE order flow
# is even evaluated -- flow becomes confirmation of pressure AT the wall,
# not the originating signal. Confluence is scored, not independent
# AND-gated. SL anchors to the wall (real invalidation, allowed WIDER than
# the flat-engine's 10% ROI when structure calls for it -- confirmed
# decision); TP is an R-multiple of that SL distance, not an independent
# flat ROI number.
#
# Every threshold below is a FIRST GUESS, same convention as every other
# first-guess constant in this file -- this whole path is UNVALIDATED and
# must go through forward-walk backtesting (Stage A) and live shadow-
# logging (Stage B) before it is ever wired into a real alert or Telegram
# command. See /Users/pebiantara/.claude/plans/cozy-waddling-peacock.md
# for the full design rationale.
# ============================================================================

# Quick-scalp's own (faster) structural interval set, separate from System
# 3's hardcoded 1h/4h (scalp_pattern_signal.BREAKOUT_INTERVALS) -- matches
# quick-scalp's own much shorter intended hold.
QUICK_PRO_STRUCTURAL_INTERVALS = ("15m", "1h")
QUICK_PRO_STRUCTURAL_CANDLE_LIMIT = 120  # same convention as System 3's BREAKOUT_CANDLE_LIMIT

# How close (% away) a structural wall must be to count as "the push is
# hitting it right now" rather than just some level that happens to exist.
QUICK_PRO_WALL_MAX_DISTANCE_PCT = 1.0

# Confluence score floor to emit a signal at all -- the real selectivity
# lever. Max possible score is 10 (see _quick_confluence_score); 7 means
# most components must agree, not just a bare majority.
QUICK_PRO_SCORE_FLOOR = 6  # raised 2026-11-11 (3rd pass) -- floor=5 (n=840) was 15.0% fade-hit,
# pure noise; floor=6 (n=351) cleanly separates at 32.2% -- floor=5 was letting through a
# flood of weak signals that dragged the blended average down. Still below the >50% target
# but a real, validated jump from floor=5.

# How far beyond the structural wall the stop sits (as a fraction of the
# wall price) -- the real invalidation point: if the wall breaks and holds
# against the fade, the thesis is wrong.
QUICK_PRO_SL_BUFFER_PCT = 0.002


def _rolling_delta(symbol, window_minutes=QUICK_DELTA_WINDOW_MINUTES):
    """Aggregates taker buy/sell volume + trade count over the trailing
    `window_minutes` of 1-minute candles -- a continuously sliding window,
    not a bucket that resets at a boundary (see module docstring for why
    that distinction is the whole point). Returns (delta_pct, trade_count),
    or (None, 0) on any fetch failure or insufficient data."""
    try:
        candles = get_futures_klines(symbol, "1m", limit=window_minutes)
    except Exception:
        return None, 0
    if len(candles) < window_minutes:
        return None, 0
    total_volume = sum(c["volume"] for c in candles)
    total_buy = sum(c["taker_buy_volume"] for c in candles)
    trade_count = sum(c["trade_count"] for c in candles)
    if total_volume <= 0:
        return None, trade_count
    return (2 * total_buy - total_volume) / total_volume, trade_count


def _quick_momentum_confirms(is_long, candles):
    """Real price move over the trailing QUICK_MOMENTUM_LOOKBACK_CANDLES
    15m candles, agreeing with the delta gate's direction -- the original
    pre-2026-11-09 check, reinstated 2026-11-10 (see module docstring)."""
    n = QUICK_MOMENTUM_LOOKBACK_CANDLES
    if len(candles) < n + 1:
        return False
    start_close = candles[-(n + 1)]["close"]
    end_close = candles[-1]["close"]
    if start_close <= 0:
        return False
    move_pct = (end_close - start_close) / start_close * 100
    return move_pct >= QUICK_MOMENTUM_MIN_MOVE_PCT if is_long else move_pct <= -QUICK_MOMENTUM_MIN_MOVE_PCT


def _quick_volume_confirms(candles, lookback=QUICK_VOLUME_LOOKBACK, multiplier=QUICK_VOLUME_CONFIRM_MULTIPLIER):
    """Projected-volume-vs-average check -- same partial-candle projection
    fix as scalp_pattern_signal.py's _volume_confirms, adapted to this
    module's 15m interval. Without the projection, a candle a few minutes
    into its 15m window would almost never clear the bar regardless of
    real pace (same bug class found in System 3 at its 1h anchor)."""
    if len(candles) < lookback + 1:
        return False
    avg = sum(c["volume"] for c in candles[-(lookback + 1):-1]) / lookback
    if avg <= 0:
        return False
    cur = candles[-1]
    elapsed_seconds = max(time.time() - cur["open_time"] / 1000, 30)
    projected_volume = cur["volume"] * (QUICK_SCALP_INTERVAL_SECONDS / elapsed_seconds)
    return projected_volume >= multiplier * avg


def _quick_candle_confirms(is_long, candles):
    """The latest closed 15m candle must have a real body (not a
    doji/indecision bar) in the signal's own direction -- cheap guard
    against triggering on a candle with no actual directional conviction
    at the candle level. Uses the second-to-last candle (candles[-2]), not
    the still-forming current one, since a forming candle's body can
    still flip before close."""
    if len(candles) < 2:
        return False
    c = candles[-2]
    body = abs(c["close"] - c["open"])
    rng = c["high"] - c["low"]
    if rng <= 0 or body / rng < QUICK_CANDLE_BODY_MIN_FRACTION:
        return False
    return c["close"] > c["open"] if is_long else c["close"] < c["open"]


def _quick_already_running(push_is_long, candles):
    """True if price has already moved >= QUICK_TREND_MOVE_PCT_THRESHOLD in
    the PUSH direction over the trailing QUICK_TREND_LOOKBACK_CANDLES 15m
    candles -- i.e. this push looks like continuation of an
    already-moving trend, not a fresh isolated spike worth fading. See
    QUICK_TREND_LOOKBACK_CANDLES above for the backtest behind this."""
    n = QUICK_TREND_LOOKBACK_CANDLES
    if len(candles) < n + 1:
        return False
    start_close = candles[-(n + 1)]["close"]
    end_close = candles[-1]["close"]
    if start_close <= 0:
        return False
    move_pct = (end_close - start_close) / start_close * 100
    agrees = (move_pct > 0) == push_is_long
    return agrees and abs(move_pct) >= QUICK_TREND_MOVE_PCT_THRESHOLD


# --- PRO MODE (structure-first) helpers -- see the constants block above ---

def _quick_nearest_wall(symbol, is_long, candles_by_interval=None):
    """Nearest next_key_level() across QUICK_PRO_STRUCTURAL_INTERVALS --
    mirrors scalp_pattern_signal._nearest_structural_level but over
    quick-scalp's own (faster) interval set rather than System 3's
    hardcoded 1h/4h. `candles_by_interval` lets a caller pass
    already-fetched candles for one interval (e.g. the 15m anchor) to
    avoid a redundant fetch for that leg."""
    candidates = []
    for interval in QUICK_PRO_STRUCTURAL_INTERVALS:
        if candles_by_interval and interval in candles_by_interval:
            candles = candles_by_interval[interval]
        else:
            try:
                candles = get_futures_klines(symbol, interval, limit=QUICK_PRO_STRUCTURAL_CANDLE_LIMIT)
            except Exception:
                continue
        level = next_key_level(candles, is_long)
        if level is not None:
            candidates.append(level)
    if not candidates:
        return None
    return min(candidates) if is_long else max(candidates)


def _quick_structural_wall(push_is_long, candles_15m, symbol):
    """Nearest structural wall AHEAD of price in the PUSH's own direction
    (resistance if push_is_long, support if not) -- where a continuing
    push would run into real structure. Returns None if no wall is close
    enough (QUICK_PRO_WALL_MAX_DISTANCE_PCT) to matter right now, or if
    breakout_lean() shows that wall already broken-and-held IN THE PUSH'S
    DIRECTION (that's continuation evidence, not an exhaustion setup
    worth fading)."""
    close = candles_15m[-1]["close"]
    if close <= 0:
        return None
    wall = _quick_nearest_wall(symbol, push_is_long, candles_by_interval={"15m": candles_15m})
    if wall is None:
        return None
    distance_pct = abs(wall - close) / close * 100
    if distance_pct > QUICK_PRO_WALL_MAX_DISTANCE_PCT:
        return None
    lean = breakout_lean(candles_15m)
    if lean is not None:
        broke_in_push_direction = (lean["direction"] == "bullish") == push_is_long
        if broke_in_push_direction:
            return None
    return wall


def _quick_confluence_score(delta_pct, wall, close, momentum_ok, oi_ok, funding_ok, mid_ok):
    """Numeric confluence score (replaces detect_quick_signal's independent
    all(checks.values()) AND-gate). Max possible score is 7. Returns
    (score, components_dict) so the breakdown can be inspected/logged.

    Takes momentum_ok as an ALREADY-COMPUTED boolean rather than
    recomputing it from raw candles internally -- the live caller
    (detect_quick_signal_pro) and a historical backtest need different
    confirmation logic for some checks (live projects the still-forming
    candle's pace via time.time(), meaningless against a closed historical
    candle), so each computes its own and passes the result in.

    UPDATE 2026-11-11 (after Stage A's first run, n=147, 3 days): the
    original first-guess scored "flow_magnitude" (bigger delta = more
    points, reusing the flow-first system's validated finding) and
    "candle" (body-direction confirmation). Both tested BACKWARDS once a
    structural wall is already a precondition -- a bigger push AT a wall
    apparently means real breakout force, not exhaustion; the flow-first
    finding doesn't transfer to this context. Both DROPPED (not flipped --
    not enough evidence to trust a flipped weight either).

    UPDATE 2026-11-11 (after extending Stage A to 7 days then 30 days,
    n=890 then n=4023): `volume` looked real at n=147 (37.7% vs 24.3%) but
    FLIPPED SIGN at n=890 (23.9% vs 29.2%) and stayed flipped at n=4023
    (22.5% vs 27.9%) -- that was small-sample noise, not a real signal.
    DROPPED. `momentum` is the only component that stayed consistently,
    correctly signed across EVERY sample size tested (n=147: 38.4% vs
    16.7%; n=890: 31.8% vs 17.7%; n=4023: 27.5% vs 20.5%, narrowing but
    still real at scale) -- kept, the one component actually trusted at
    this point. `delta_pct` is still required as the original hard floor
    gate in detect_quick_signal_pro (>= QUICK_DELTA_CONFIRM_PCT) -- just
    not double-counted in the score on top of that."""
    components = {}

    distance_pct = abs(wall - close) / close * 100 if close > 0 else None
    if distance_pct is None:
        components["structure_proximity"] = 0
    elif distance_pct <= 0.3:
        components["structure_proximity"] = 2
    elif distance_pct <= QUICK_PRO_WALL_MAX_DISTANCE_PCT:
        components["structure_proximity"] = 1
    else:
        components["structure_proximity"] = 0

    components["momentum"] = 2 if momentum_ok else 0
    components["oi_not_fresh"] = 1 if oi_ok else 0
    components["funding"] = 1 if funding_ok else 0
    components["mid_trend"] = 1 if mid_ok else 0

    return sum(components.values()), components


def _quick_pro_tp_r_multiple(score):
    """Confidence-tiered TP target as a multiple of SL distance (R) --
    mirrors scalp_pattern_signal._tp_roi_for_lean()'s confidence-tiering
    precedent, just expressed in R instead of flat ROI since SL distance
    now varies trade-to-trade (structural, not flat). FIRST GUESS tiers,
    not validated. Rescaled 2026-11-11 (2nd time) for the new max score of 7."""
    if score >= 6:
        return 3.0
    if score >= 5:
        return 2.0
    return 1.5


def _structural_stop_for_fade(wall, is_long, buffer_pct=QUICK_PRO_SL_BUFFER_PCT):
    """Stop placed just BEYOND the structural wall -- the real
    invalidation point: if the wall breaks and holds against the fade,
    the thesis is wrong. `is_long` is the TRADE (fade) direction, not the
    push direction. For a LONG fade (wall = support below price), SL sits
    just below it; for a SHORT fade (wall = resistance above price), SL
    sits just above it."""
    sign = 1 if is_long else -1
    return wall * (1 - sign * buffer_pct)


def _quick_pro_levels(entry_price, is_long, wall, leverage, score, symbol=None):
    """SL anchored to the structural wall (real invalidation, allowed
    WIDER than quick_scalp_levels()'s flat 10% ROI when structure calls
    for it -- confirmed decision, see module docstring PRO MODE section),
    TP as an R-multiple of that SL distance (_quick_pro_tp_r_multiple),
    still capped against real liquidation distance (same convention as
    quick_scalp_levels()) and against the NEXT structural level past the
    R-multiple target (reuses scalp_pattern_signal._cap_tp_before_structure
    unmodified -- it's generic, no System-3-specific state)."""
    stop_loss = _structural_stop_for_fade(wall, is_long)
    sl_distance_pct = abs(entry_price - stop_loss) / entry_price

    mmr = maintenance_margin_rate_for(symbol)
    liquidation_distance_pct = max(1 / leverage - mmr, 0.0005)
    sign = 1 if is_long else -1
    sl_capped = sl_distance_pct >= liquidation_distance_pct
    if sl_capped:
        sl_distance_pct = liquidation_distance_pct * LIQUIDATION_SAFETY_FRACTION
        stop_loss = entry_price * (1 - sign * sl_distance_pct)

    r_multiple = _quick_pro_tp_r_multiple(score)
    tp_distance_pct = sl_distance_pct * r_multiple
    take_profit = entry_price * (1 + sign * tp_distance_pct)

    levels = {
        "leverage": leverage,
        "maintenance_margin_rate": mmr,
        "liquidation_distance_pct": liquidation_distance_pct,
        "sl_distance_pct": sl_distance_pct,
        "tp_distance_pct": tp_distance_pct,
        "sl_roi_pct": sl_distance_pct * leverage,
        "tp_roi_pct": tp_distance_pct * leverage,
        "sl_capped_by_liquidation": sl_capped,
        "liquidation_price": entry_price * (1 - sign * liquidation_distance_pct),
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "r_multiple": r_multiple,
        "structural_wall": wall,
    }
    next_wall = _quick_nearest_wall(symbol, is_long)
    return _cap_tp_before_structure(levels, entry_price, is_long, next_wall)


def detect_quick_signal_pro(symbol, mid_trend):
    """PRO MODE (structure-first) -- a separate, slower, opt-in path
    alongside detect_quick_signal() below, NOT a replacement (see the
    constants block above for full rationale). A real structural wall
    must exist in the push's direction BEFORE flow is trusted; flow then
    confirms pressure at that wall rather than originating the signal;
    confluence is scored (_quick_confluence_score) instead of independent
    AND-gated. Returns the same shape of dict as detect_quick_signal()
    plus `wall`/`score`/`components`/`levels`, or None.

    UNVALIDATED -- first-guess thresholds throughout, must go through
    Stage A (forward-walk backtest) and Stage B (live shadow-logging)
    before ever being wired into a real alert or Telegram command."""
    delta_pct, trade_count = _rolling_delta(symbol)
    if delta_pct is None or trade_count < QUICK_DELTA_MIN_TRADE_COUNT:
        return None
    push_is_long = delta_pct > 0
    is_long = not push_is_long  # fade the push -- same convention as detect_quick_signal

    try:
        candles = get_futures_klines(
            symbol, QUICK_SCALP_INTERVAL, limit=max(QUICK_CANDLE_FETCH_LIMIT, QUICK_PRO_STRUCTURAL_CANDLE_LIMIT)
        )
    except Exception:
        return None

    wall = _quick_structural_wall(push_is_long, candles, symbol)
    if wall is None:
        return None  # nothing for the push to be hitting -- no setup here

    oi_ok = oi_confirms_reversal(symbol, period=QUICK_SCALP_INTERVAL)
    funding_ok = funding_confirms_bullish(symbol) if is_long else funding_confirms_bearish(symbol)
    mid_ok = mid_trend is None or mid_trend["combined"] != ("bearish" if is_long else "bullish")
    momentum_ok = _quick_momentum_confirms(push_is_long, candles)

    close = candles[-1]["close"]
    score, components = _quick_confluence_score(
        delta_pct, wall, close, momentum_ok, oi_ok, funding_ok, mid_ok,
    )
    if score < QUICK_PRO_SCORE_FLOOR:
        return None

    entry = quick_scalp_entry_for_symbol(symbol, is_long)
    if entry is None:
        return None

    return {
        "is_long": is_long,
        "push_is_long": push_is_long,
        "delta_pct": delta_pct,
        "trade_count": trade_count,
        "wall": wall,
        "score": score,
        "components": components,
        "entry": entry,
    }


def quick_scalp_entry_for_symbol(symbol, is_long):
    """EMA20/50/200 support/resistance entry zone from 15m candles -- same
    mechanism as scalp_pattern_signal.py's scalp_entry_for_symbol, just on
    this module's own QUICK_SCALP_INTERVAL."""
    try:
        candles = get_futures_klines(symbol, QUICK_SCALP_INTERVAL)
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


def detect_quick_signal(symbol, mid_trend, delta_map=None):
    """Returns {"is_long", "checks", "delta_pct", "trade_count"} if the
    rolling-delta gate + momentum/volume/candle confirmation + light
    sanity checks all confirm and an entry zone exists, else None.
    Fetches its own rolling 1m window (delta) and 15m candles
    (momentum/volume/candle, added back 2026-11-10) independently of
    whatever a caller might separately need for the entry zone.

    FADES the push, doesn't chase it (flipped 2026-11-10, see module
    docstring for the backtest behind this) -- delta/momentum/volume/
    candle all still confirm the PUSH direction (validating it was a real
    sweep, not noise), but the EMITTED/traded direction is the opposite
    of that push, and funding/mid-trend are checked against the trade
    direction, not the push. As of QUICK_SCALP_VERSION above, also
    requires the push NOT already look like a running trend, and now also
    (2026-11-13) blocks when `candlestick_patterns.reversal_lean` finds a
    real reversal shape (hammer/engulfing/shooting star) pointing AGAINST
    the fade -- backtested on 366 real signals: contradicting-shape trades
    had -0.007% expectancy vs +0.047% baseline (no pattern at all), while
    agreeing-shape trades ran meaningfully higher (+0.290% n=33). Used as
    a VETO only (block on contradiction), not a required confirmation
    (requiring agreement would have cut frequency ~89%, 366->40) --
    candlestick_patterns.py already plays this same veto-not-gate role in
    leverage_risk.py for the other systems.
    (_quick_already_running) -- rollback: restore
    coin_finder/quick_scalp_signal.py.v_fade-only_2026-11-11 if this
    makes things worse.

    `delta_map`, if given (see _build_delta_map()), is a {symbol:
    (delta_pct, trade_count)} snapshot of EVERY scanned symbol taken at
    the same moment -- used both as this symbol's own delta reading
    (skips a redundant _rolling_delta call) and to check market breadth
    (BREADTH_VETO_THRESHOLD) against every OTHER symbol in the same
    snapshot. Falls back to a fresh standalone _rolling_delta call (and
    skips the breadth check entirely -- no opinion, same convention as
    funding_confirms_bullish/bearish) when called without one, e.g. a
    manual /scalp_ check outside the full scan."""
    if delta_map is not None:
        delta_pct, trade_count = delta_map.get(symbol, (None, 0))
    else:
        delta_pct, trade_count = _rolling_delta(symbol)
    if delta_pct is None or trade_count < QUICK_DELTA_MIN_TRADE_COUNT or abs(delta_pct) < QUICK_DELTA_CONFIRM_PCT:
        return None
    push_is_long = delta_pct > 0
    is_long = not push_is_long  # fade the push -- this is the actual trade direction

    try:
        candles = get_futures_klines(symbol, QUICK_SCALP_INTERVAL, limit=QUICK_CANDLE_FETCH_LIMIT)
    except Exception:
        return None

    funding_ok = funding_confirms_bullish(symbol) if is_long else funding_confirms_bearish(symbol)
    mid_ok = mid_trend is None or mid_trend["combined"] != ("bearish" if is_long else "bullish")
    lean = reversal_lean(candles)
    fade_dir = "bullish" if is_long else "bearish"
    no_contradicting_candle = lean is None or lean["direction"] == fade_dir

    # Added 2026-11-13: backtested on 372 real signals -- the "neither"
    # bucket (ribbon disagrees with the fade AND VWAP stretch is in the
    # naive-reversion direction, not against it) was 58% of all signal
    # volume and net LOSING on its own (-25.39% cum), while "at least one
    # favorable" averaged +0.2438%/trade (+38.03% cum, n=156) -- roughly
    # 7x the unfiltered average. Gate on "at least one", not "both
    # required" (requiring both barely changes the result since the two
    # are highly correlated -- see cbot_quick_scalp_signal.md). Rollback:
    # quick_scalp_signal.py.v_fade+trend-filter+delta02+wide-sl04+no-btc+candle-veto_2026-11-13_before-ribbon-vwap
    ribbon = ribbon_direction(candles)
    ribbon_favors = ribbon == fade_dir
    vwap_dist_pct, _ = vwap_distance_pct(candles)
    vwap_favors = vwap_favors_fade(vwap_dist_pct, is_long)
    ribbon_or_vwap_favorable = ribbon_favors or vwap_favors

    breadth = _market_breadth(symbol, push_is_long, delta_map) if delta_map else None
    breadth_not_crowded = breadth is None or breadth < BREADTH_VETO_THRESHOLD

    checks = {
        "delta": True,
        "momentum": _quick_momentum_confirms(push_is_long, candles),
        "volume": _quick_volume_confirms(candles),
        "candle": _quick_candle_confirms(push_is_long, candles),
        "not_already_running": not _quick_already_running(push_is_long, candles),
        "funding": funding_ok,
        "mid_trend_1h_4h": mid_ok,
        "no_contradicting_candle": no_contradicting_candle,
        "ribbon_or_vwap_favorable": ribbon_or_vwap_favorable,
        "breadth_not_crowded": breadth_not_crowded,
    }
    if all(checks.values()) and quick_scalp_entry_for_symbol(symbol, is_long) is not None:
        return {"is_long": is_long, "checks": checks, "delta_pct": delta_pct, "trade_count": trade_count}
    return None


def _quick_chase_extension_ok(symbol, is_long, lookback_minutes=CHASE_EXTENSION_LOOKBACK_MINUTES,
                               max_pct=CHASE_EXTENSION_MAX_PCT):
    """True if price hasn't already moved too far in the push's own
    direction over the last `lookback_minutes` -- see
    CHASE_EXTENSION_MAX_PCT's comment for the backtest behind this.
    None-safe: if candles can't be fetched, don't block (no opinion),
    same convention as everywhere else in this file."""
    try:
        candles = get_futures_klines(symbol, "1m", limit=lookback_minutes + 1)
    except Exception:
        return True
    if len(candles) < lookback_minutes + 1:
        return True
    start_close, end_close = candles[0]["close"], candles[-1]["close"]
    if start_close <= 0:
        return True
    move_pct = (end_close - start_close) / start_close * 100
    extension = move_pct if is_long else -move_pct
    return extension < max_pct


def detect_quick_signal_chase(symbol, delta_map):
    """Rides the push instead of fading it -- see CHASE_BREADTH_FLOOR's
    comment above for the backtest behind this. Only fires when breadth
    is high enough that chasing backtested better than fading
    (CHASE_BREADTH_FLOOR); requires a REAL delta_map snapshot (unlike
    detect_quick_signal, no standalone-call fallback) since breadth is
    this function's core gate, not an optional extra check.

    Deliberately does NOT reuse detect_quick_signal's funding/mid-trend/
    ribbon-vwap/candle-veto checks -- those were tuned for fading, not
    re-validated for chasing, see CHASE_BREADTH_FLOOR's comment.

    VOLUME-ONLY confirmation (no momentum, no candle) -- changed
    2026-11-15/16 after the user flagged that chase alerts fired too
    late ("market started already"). _quick_momentum_confirms requires
    a 45-MINUTE price move to already be underway before it passes --
    that's the actual source of the lag, not an unavoidable cost of
    confirmation in general. Re-tested every subset of
    momentum/volume/candle on the same 5-day/8-symbol data:
    momentum-only was the WEAKEST of the three alone (+6.99%/trade,
    n=941) despite being the laggiest; volume-only alone nearly matched
    the full 3-check combo's quality (+14.55%/trade, n=578, vs
    +16.97%/trade, n=170 for all three) with no 45min wait and 3.4x the
    frequency (and a much higher cum_roi over the test window,
    +8408% vs +2885%, from that frequency). Adding candle on top of
    volume made it slightly WORSE, not better (+14.24%, n=264) -- not
    kept. Rollback: restore momentum+candle checks below if volume-only
    underperforms on real live data."""
    if delta_map is None:
        return None
    delta_pct, trade_count = delta_map.get(symbol, (None, 0))
    if delta_pct is None or trade_count < QUICK_DELTA_MIN_TRADE_COUNT or abs(delta_pct) < QUICK_DELTA_CONFIRM_PCT:
        return None
    push_is_long = delta_pct > 0
    is_long = push_is_long  # CHASE -- same direction as the push, not the fade

    breadth = _market_breadth(symbol, push_is_long, delta_map)
    if breadth is None or breadth < CHASE_BREADTH_FLOOR:
        return None

    try:
        candles = get_futures_klines(symbol, QUICK_SCALP_INTERVAL, limit=QUICK_CANDLE_FETCH_LIMIT)
    except Exception:
        return None

    checks = {
        "delta": True,
        "breadth_crowded_enough": True,  # already gated above; kept in the dict for visibility/symmetry
        "volume": _quick_volume_confirms(candles),
        "extension_not_overdone": _quick_chase_extension_ok(symbol, is_long),
    }
    if all(checks.values()) and quick_scalp_entry_for_symbol(symbol, is_long) is not None:
        # Final freshness re-check -- added 2026-11-16 after a real
        # incident: SUIUSDT's chase LONG was decided using delta_map's
        # reading of the push (built once at the start of the poll
        # cycle), but by the time this symbol's OWN per-symbol work
        # (candle fetch for volume/extension, entry-zone check) finished
        # a few minutes later, the push had already fully died (delta
        # collapsed from +0.51 to +0.03) -- the ENTRY PRICE was captured
        # fresh/live right after this, but the SIGNAL it was based on
        # was several minutes stale by then. Re-checking delta live,
        # right before committing, catches exactly this: a push that
        # looked great when delta_map was built but isn't live anymore
        # doesn't get to fire just because it once qualified.
        fresh_delta, fresh_trade_count = _rolling_delta(symbol)
        still_pushing = (
            fresh_delta is not None and fresh_trade_count >= QUICK_DELTA_MIN_TRADE_COUNT
            and (fresh_delta > 0) == push_is_long and abs(fresh_delta) >= QUICK_DELTA_CONFIRM_PCT
        )
        if not still_pushing:
            return None
        return {
            "is_long": is_long, "checks": checks, "delta_pct": delta_pct,
            "trade_count": trade_count, "breadth": breadth,
        }
    return None


def quick_scalp_levels(anchor_price, is_long, leverage, symbol=None):
    """SL/TP sizing -- DYNAMIC (structural) when a real support/resistance
    wall exists, flat QUICK_SCALP_SL_ROI/TP_ROI (this module's own
    first-guess targets) otherwise. Added 2026-11-11 after a SOL trade
    got stopped out by an ordinary wick before its favorable move played
    out at 50x's flat 0.2% SL distance -- too tight for that symbol's
    actual volatility. Reuses _quick_nearest_wall/_structural_stop_for_fade
    (built for the paused PRO MODE, see its section above) PURELY FOR
    SIZING here -- unlike PRO MODE, a wall is NOT required to emit a
    signal, this only widens the stop when one happens to exist nearby.
    The structural distance can only make the stop WIDER than the flat
    floor, never tighter (a close wall would otherwise recreate the exact
    same premature-stop problem this is meant to fix). TP keeps the SAME
    3:1 reward:risk ratio either way (QUICK_SCALP_TP_ROI/QUICK_SCALP_SL_ROI)
    -- only the absolute size becomes dynamic, not the risk:reward shape.
    Still capped against real liquidation distance same as
    scalp_pattern_signal.py's scalp_levels()."""
    mmr = maintenance_margin_rate_for(symbol)
    liquidation_distance_pct = max(1 / leverage - mmr, 0.0005)

    flat_sl_distance_pct = QUICK_SCALP_SL_ROI / leverage
    sl_distance_pct = flat_sl_distance_pct
    wall = _quick_nearest_wall(symbol, not is_long) if symbol else None
    if wall is not None and anchor_price > 0:
        structural_sl = _structural_stop_for_fade(wall, is_long)
        structural_distance_pct = abs(anchor_price - structural_sl) / anchor_price
        sl_distance_pct = max(flat_sl_distance_pct, structural_distance_pct)

    tp_distance_pct = sl_distance_pct * (QUICK_SCALP_TP_ROI / QUICK_SCALP_SL_ROI)

    sl_capped = sl_distance_pct >= liquidation_distance_pct
    if sl_capped:
        sl_distance_pct = liquidation_distance_pct * LIQUIDATION_SAFETY_FRACTION

    sign = 1 if is_long else -1
    levels = {
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
    # Cap TP at the next real structural level AHEAD of price in the
    # trade's own direction -- without this, a widened SL (above) also
    # blindly widens TP via the fixed 3:1 ratio, producing targets no
    # quick-scalp-style hold would realistically reach. Reuses
    # scalp_pattern_signal._cap_tp_before_structure unmodified (confirmed
    # generic, no System-3-specific state).
    tp_wall = _quick_nearest_wall(symbol, is_long) if symbol else None
    levels = _cap_tp_before_structure(levels, anchor_price, is_long, tp_wall)

    # Final sanity ceiling -- when no closer structural level exists to
    # cap into, don't let the 3:1 ratio imply a target this fast-hold
    # system shouldn't realistically expect to reach (see
    # QUICK_SCALP_TP_ROI_CEILING above).
    if levels["tp_roi_pct"] > QUICK_SCALP_TP_ROI_CEILING:
        ceiling_distance_pct = QUICK_SCALP_TP_ROI_CEILING / leverage
        levels["tp_distance_pct"] = ceiling_distance_pct
        levels["tp_roi_pct"] = ceiling_distance_pct * leverage
        levels["take_profit"] = anchor_price * (1 + sign * ceiling_distance_pct)
        levels["tp_capped_by_structure"] = False
        levels.pop("structural_level", None)
    return levels


def _market_breadth(symbol, push_is_long, delta_map):
    """Fraction of OTHER symbols in delta_map currently leaning the SAME
    direction as this symbol's push (matching sign, |delta|>=
    BREADTH_LEAN_DELTA), out of all OTHER symbols with a valid delta
    reading in the snapshot. None if no other symbol has valid data --
    treated as "no opinion, don't block", same convention as
    funding_confirms_bullish/bearish. See BREADTH_VETO_THRESHOLD above
    for the backtest behind this."""
    agree = total = 0
    for other_symbol, entry in delta_map.items():
        if other_symbol == symbol or entry is None:
            continue
        d, tc = entry
        if d is None or tc < QUICK_DELTA_MIN_TRADE_COUNT:
            continue
        total += 1
        if (d > 0) == push_is_long and abs(d) >= BREADTH_LEAN_DELTA:
            agree += 1
    return agree / total if total else None


SCAN_WORKER_COUNT = 8  # see scan_quick_scalp_signals' docstring


DELTA_MAP_PER_SYMBOL_TIMEOUT_SECONDS = 20  # _rolling_delta is only 10x 1m candles -- normally fast,
                                             # this just bounds a real network hang (see below)


def _rolling_delta_with_timeout(symbol, timeout=DELTA_MAP_PER_SYMBOL_TIMEOUT_SECONDS):
    """Same thread+timeout wrapper as _scan_one_symbol_with_timeout, for
    the exact same reason -- added 2026-11-15 after a real incident: this
    function originally called _rolling_delta directly inside the pool
    with no per-symbol bound, so one hung network call stalled this
    ENTIRE first pass (as_completed() with no timeout blocks until every
    future finishes, and exiting the pool's `with` block blocks on that
    same stuck worker) -- which in turn delayed the whole scan behind it,
    repeatedly tripping the 600s process watchdog with many symbols
    piling up as simultaneous timeouts. Exact same bug class the project
    already fixed once for the main per-symbol scan; this closes the gap
    in the newer delta-map pass instead of re-deriving a different fix."""
    result_box = queue.Queue(maxsize=1)

    def _worker():
        try:
            result_box.put(_rolling_delta(symbol))
        except Exception:
            result_box.put((None, 0))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        print(f"[scan] delta-map {symbol}: no response after {timeout}s -- excluded from breadth calc", flush=True)
        return (None, 0)
    try:
        return result_box.get_nowait()
    except queue.Empty:
        return (None, 0)


def _build_delta_map(symbols):
    """First pass over the whole scan universe: computes _rolling_delta
    for every symbol UP FRONT, in parallel, before any of the heavier
    per-symbol work (candle fetches, mid-trend, etc.) runs. Gives
    detect_quick_signal() one consistent snapshot to check market
    breadth against (BREADTH_VETO_THRESHOLD) instead of each symbol's
    own delta drifting out of sync with the others while slower
    per-symbol work runs (a symbol can take up to
    PER_SYMBOL_SCAN_TIMEOUT_SECONDS). Cheap call (10x 1m candles each),
    so this finishes fast even across the full universe -- bounded per
    symbol via _rolling_delta_with_timeout (see its docstring)."""
    delta_map = {}
    with ThreadPoolExecutor(max_workers=SCAN_WORKER_COUNT) as pool:
        futures = {pool.submit(_rolling_delta_with_timeout, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                delta_map[symbol] = future.result()
            except Exception as e:
                print(f"[scan] delta-map {symbol} raised {e} -- excluded from breadth calc", flush=True)
    return delta_map


# Added 2026-11-15, same day as the breadth veto above, at the user's
# request to flip that finding around: instead of only using high
# breadth to BLOCK a fade (this module's own job), surface it as its
# OWN alert -- "the market itself is moving as a block right now, here
# are the coins leading it" -- since the backtest showed fading INTO a
# breadth>=0.6 move was the single worst regime (-7.98%/trade), implying
# the opposite (riding the move, not fighting it) is the more sensible
# read in that regime. This is informational only -- no SL/TP, no
# trade direction recommendation beyond "these are the names moving
# hardest with the crowd right now" -- it's a watchlist ping, not a new
# trading engine, so it ships without the backtest-before-ship bar the
# fade engine's own gates go through.
MARKET_REGIME_BREADTH_THRESHOLD = 0.6  # matches the worst-performing fade bucket's lower edge
MARKET_REGIME_LEADER_COUNT = 8


def market_regime_snapshot(symbols=None, top_n=QUICK_SCALP_TOP_N, delta_map=None):
    """{"direction": "bullish"/"bearish", "breadth": frac, "leaders":
    [(symbol, delta_pct, trade_count), ...]} if >=MARKET_REGIME_BREADTH_THRESHOLD
    of the universe is currently leaning the same direction, else
    {"direction": None, "breadth": frac} (the stronger of the two
    sides, for visibility even when below threshold). None only if no
    symbol had valid data at all. `leaders` is sorted by |delta_pct|
    descending, capped at MARKET_REGIME_LEADER_COUNT -- the coins
    pushing hardest WITH the crowd, i.e. the plausible pump/dump
    candidates if the move keeps running. `trade_count` (added
    2026-11-16, display-only -- does NOT feed into the breadth/direction
    logic above, that was tested weighted and backtested WORSE, see
    cbot_quick_scalp_signal.md) is just the same rolling-window trade
    count already computed for the delta reading, surfaced for context
    ("volume/traders" the user asked to see alongside each leader).

    Also includes "resisters" (added 2026-11-22 at the user's request to
    find coins "small reducing drop or up" during a confirmed market
    move, after KERNEL/HUMA/SKY kept climbing through a broadly bearish
    tape) -- the mirror image of `leaders`: ranked by raw delta_pct
    (not magnitude) over EVERY symbol with valid delta data, most-
    positive-first in a bearish regime / most-negative-first in a
    bullish one. Deliberately ranked over ALL valid symbols rather than
    `valid` below (which already drops near-flat coins via
    BREADTH_LEAN_DELTA) -- a coin sitting flat while the crowd dumps is
    exactly the relative-strength signal this is meant to surface, so it
    must not be filtered out before ranking.

    `delta_map`, if given, skips this function's own _build_delta_map()
    call -- see scan_quick_scalp_signals' docstring for why (one shared
    snapshot per poll instead of three separate full-universe passes)."""
    if symbols is None:
        symbols = [s for s in _resolve_symbols(top_n) if s not in QUICK_SCALP_EXCLUDED_SYMBOLS]
    if delta_map is None:
        delta_map = _build_delta_map(symbols)
    all_valid = {
        s: (d, tc) for s, (d, tc) in delta_map.items()
        if d is not None and tc >= QUICK_DELTA_MIN_TRADE_COUNT
    }
    valid = {s: v for s, v in all_valid.items() if abs(v[0]) >= BREADTH_LEAN_DELTA}
    if not valid:
        return None

    bullish = {s: v for s, v in valid.items() if v[0] > 0}
    bearish = {s: v for s, v in valid.items() if v[0] < 0}
    total = len(valid)
    bullish_frac = len(bullish) / total
    bearish_frac = len(bearish) / total

    if bullish_frac >= bearish_frac:
        direction_if_regime, breadth, side = "bullish", bullish_frac, bullish
    else:
        direction_if_regime, breadth, side = "bearish", bearish_frac, bearish

    if breadth < MARKET_REGIME_BREADTH_THRESHOLD:
        return {"direction": None, "breadth": breadth}

    ranked = sorted(side.items(), key=lambda kv: abs(kv[1][0]), reverse=True)[:MARKET_REGIME_LEADER_COUNT]
    leaders = [(s, d, tc) for s, (d, tc) in ranked]

    resister_ranked = sorted(
        all_valid.items(), key=lambda kv: kv[1][0], reverse=(direction_if_regime == "bearish")
    )[:MARKET_REGIME_LEADER_COUNT]
    resisters = [(s, d, tc) for s, (d, tc) in resister_ranked]

    total_buy_usd, total_sell_usd = _market_wide_volume_totals(list(all_valid.keys()))

    return {
        "direction": direction_if_regime, "breadth": breadth, "leaders": leaders, "resisters": resisters,
        "total_buy_usd": total_buy_usd, "total_sell_usd": total_sell_usd,
        "window_minutes": QUICK_DELTA_WINDOW_MINUTES,
    }


def _market_wide_volume_totals(symbols, window_minutes=QUICK_DELTA_WINDOW_MINUTES):
    """Aggregate buy/sell USD volume across `symbols` over the trailing
    `window_minutes` of 1m candles -- added 2026-11-22 at the user's
    request to show total buyer vs. total seller volume on the
    market-wide regime alert. Same window as _rolling_delta's own
    per-symbol delta read, but in $ terms -- comparing base-asset buy/
    sell volume ACROSS different coins is meaningless (1 BTC and 1 PEPE
    aren't comparable amounts), so this uses quote_volume/
    taker_buy_quote_volume instead of _rolling_delta's base-asset fields.
    Kept as its OWN klines fetch, separate from delta_map/_rolling_delta
    (left untouched) -- that's the live fade engine's core signal, not
    worth the risk of changing its return shape just for this purely
    informational market-wide total. Returns (total_buy_usd,
    total_sell_usd); either side is 0.0 for any symbol that couldn't be
    fetched in time."""
    def _one(symbol):
        try:
            candles = get_futures_klines(symbol, "1m", limit=window_minutes)
        except Exception:
            return 0.0, 0.0
        if len(candles) < window_minutes:
            return 0.0, 0.0
        buy = sum(c["taker_buy_quote_volume"] for c in candles)
        sell = sum(c["quote_volume"] for c in candles) - buy
        return buy, sell

    total_buy = total_sell = 0.0
    with ThreadPoolExecutor(max_workers=SCAN_WORKER_COUNT) as pool:
        for buy, sell in pool.map(_one, symbols):
            total_buy += buy
            total_sell += sell
    return total_buy, total_sell


def _per_symbol_volume_detail(symbols, window_minutes=QUICK_DELTA_WINDOW_MINUTES):
    """{symbol: {"buy_usd", "sell_usd", "high", "low"}} -- added 2026-11-23
    at the user's request to break the market-regime alert's PER-COIN
    leaders/resisters lines down the same way big_mover_alert.py's own
    alerts already were (see that module's volume_confirmed() for the
    same buy/sell-in-$ + realized-range idea). Same window as the delta
    reading that put each coin on the list in the first place, so the
    aggression shown actually explains the delta, not some other period.
    Separate fetch from _market_wide_volume_totals (that one SUMS across
    the whole universe; this one keeps each symbol's own breakdown) and
    from delta_map/_rolling_delta (left untouched, same reasoning as
    _market_wide_volume_totals's docstring)."""
    def _one(symbol):
        try:
            candles = get_futures_klines(symbol, "1m", limit=window_minutes)
        except Exception:
            return symbol, None
        if len(candles) < window_minutes:
            return symbol, None
        buy = sum(c["taker_buy_quote_volume"] for c in candles)
        sell = sum(c["quote_volume"] for c in candles) - buy
        return symbol, {
            "buy_usd": buy, "sell_usd": sell,
            "high": max(c["high"] for c in candles), "low": min(c["low"] for c in candles),
        }

    detail = {}
    with ThreadPoolExecutor(max_workers=SCAN_WORKER_COUNT) as pool:
        for symbol, result in pool.map(_one, symbols):
            if result is not None:
                detail[symbol] = result
    return detail


def build_market_regime_message(snapshot):
    """Added live price per leader 2026-11-15 at the user's request ("best
    price to sign market") -- the current tradeable mark price, fetched
    fresh per symbol (parallelized, same worker count as the main scan),
    not a backfilled entry/SL/TP recommendation. This alert stays
    informational by design (see module docstring above) -- a live price
    just makes "possible pump/dump candidate" something you could
    actually act on without a second lookup, it doesn't turn this into a
    trade signal."""
    direction = snapshot["direction"]
    emoji = "\U0001f7e2" if direction == "bullish" else "\U0001f534"
    label = "BULLISH" if direction == "bullish" else "BEARISH"
    lines = [
        f"{emoji} <b>Market-wide {label} move detected</b> ({snapshot['breadth']:.0%} of tracked coins pushing {direction})",
    ]
    total_buy_usd = snapshot.get("total_buy_usd")
    total_sell_usd = snapshot.get("total_sell_usd")
    if total_buy_usd is not None and (total_buy_usd + total_sell_usd) > 0:
        window_minutes = snapshot.get("window_minutes", QUICK_DELTA_WINDOW_MINUTES)
        total = total_buy_usd + total_sell_usd
        sell_skew_pct = (total_sell_usd - total_buy_usd) / total
        skew_label = "sell" if sell_skew_pct > 0 else "buy"
        lines.append(
            f"\U0001f4b5 Last {window_minutes}min volume: buy=${total_buy_usd:,.0f}  sell=${total_sell_usd:,.0f}  "
            f"({abs(sell_skew_pct):.0%} {skew_label}-skewed)"
        )
    lines.append(
        "Not a trade signal -- these are the coins moving hardest WITH the crowd right now, "
        "possible pump/dump continuation candidates if the move keeps running "
        "(price = current live mark price, for reference only):"
    )
    leaders = snapshot["leaders"]
    resisters = snapshot.get("resisters", [])
    all_symbols = [symbol for symbol, _, _ in leaders + resisters]
    prices = {}
    with ThreadPoolExecutor(max_workers=SCAN_WORKER_COUNT) as pool:
        futures = {pool.submit(fetch_live_futures_price, symbol): symbol for symbol in all_symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                prices[symbol] = future.result()
            except Exception:
                prices[symbol] = None
    detail = _per_symbol_volume_detail(all_symbols)

    def _format_row(symbol, delta, trade_count):
        price = prices.get(symbol)
        price_str = f"{price:g}" if price is not None else "n/a"
        row = f"  {symbol:12} delta={delta:+.2f}  trades={trade_count:,}  price={price_str}"
        d = detail.get(symbol)
        if d is not None:
            total = d["buy_usd"] + d["sell_usd"]
            buy_pct = d["buy_usd"] / total if total > 0 else 0.5
            aggressor = "buyers" if buy_pct >= 0.5 else "sellers"
            row += (
                f"\n      buy=${d['buy_usd']:,.0f}  sell=${d['sell_usd']:,.0f}  "
                f"({aggressor} {max(buy_pct, 1 - buy_pct):.0%})  range={d['low']:g}-{d['high']:g}"
            )
        return row

    for symbol, delta, trade_count in leaders:
        lines.append(_format_row(symbol, delta, trade_count))

    if resisters:
        resist_label = "dropping less / climbing" if direction == "bearish" else "lagging behind"
        lines.append("")
        lines.append(f"\U0001f6e1 <b>Resisting the move</b> ({resist_label} the {direction} crowd):")
        for symbol, delta, trade_count in resisters:
            lines.append(_format_row(symbol, delta, trade_count))

    return "\n".join(lines)


def scan_quick_scalp_signals(symbols=None, top_n=QUICK_SCALP_TOP_N, delta_map=None):
    """{symbol: signal_dict} for every symbol with a confirmed rolling-delta
    trigger right now. Mirrors scalp_pattern_signal.py's
    scan_scalp_signals in shape.

    Parallelized 2026-11-13 -- found that a full SEQUENTIAL scan across
    the (now ~60-symbol, grown from the $10M->$5M universe-widening
    change) universe was legitimately taking 600-2000+ seconds end to
    end, repeatedly tripping quick_scalp_alert.py's process-level
    watchdog even though no single symbol was individually stuck (the
    per-symbol timeout below never fired during the slow cycles that
    were measured) -- it was accumulated per-symbol latency, not a hang.
    Each symbol's work is I/O-bound (waiting on HTTP calls), so a thread
    pool gives a real wall-clock speedup despite the GIL (it releases
    during I/O wait). SCAN_WORKER_COUNT=8 is a first-guess balance
    between speed and not tripping Binance's rate limits (429s were seen
    earlier this same project at higher concurrency/burst rates) --
    not yet tuned against real measurements, revisit if 429s show up in
    logs.

    `delta_map`, if given, skips this function's own _build_delta_map()
    call and reuses the caller's snapshot instead -- added so
    quick_scalp_alert.py can build ONE delta_map per poll and share it
    with scan_quick_scalp_chase_signals() too, instead of paying for two
    separate full-universe delta passes every single poll."""
    if symbols is None:
        symbols = [s for s in _resolve_symbols(top_n) if s not in QUICK_SCALP_EXCLUDED_SYMBOLS]

    if delta_map is None:
        delta_map = _build_delta_map(symbols)

    signals = {}
    with ThreadPoolExecutor(max_workers=SCAN_WORKER_COUNT) as pool:
        futures = {pool.submit(_scan_one_symbol_with_timeout, symbol, delta_map): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                signal = future.result()
            except Exception as e:
                print(f"[scan] {symbol} pool task raised {e} -- skipping", flush=True)
                continue
            if signal is not None:
                signals[symbol] = signal
    return signals


PER_SYMBOL_SCAN_TIMEOUT_SECONDS = 90  # see _scan_one_symbol_with_timeout


def _scan_one_symbol(symbol, result_box, delta_map=None):
    """Runs in a worker thread -- see _scan_one_symbol_with_timeout."""
    try:
        mid_trend = mid_period_trend(symbol)
        signal = detect_quick_signal(symbol, mid_trend, delta_map)
        if signal is not None:
            signal["close_price"] = fetch_live_futures_price(symbol)
            signal["mid_trend"] = mid_trend
        result_box.put(signal)
    except Exception as e:
        print(f"[scan] {symbol} raised {e} -- skipping", flush=True)
        result_box.put(None)


def _scan_one_symbol_with_timeout(symbol, delta_map=None, timeout=PER_SYMBOL_SCAN_TIMEOUT_SECONDS):
    """Added 2026-11-13 after a real incident: a single symbol's call
    chain (mid_period_trend/detect_quick_signal) hung for 615+ seconds --
    far beyond any bounded retry chain, a genuine stuck call somewhere
    (exact line not pinned down) -- and froze the ENTIRE scan, costing
    the whole poll cycle rather than just that one symbol. A thread-based
    timeout is the standard cross-platform-safe way to bound an arbitrary
    blocking call in Python (signal.alarm doesn't play well with threads
    and isn't available everywhere); the stuck thread itself is
    abandoned (daemon=True, left running harmlessly in the background --
    Python has no safe way to force-kill a thread) while the scan moves
    on to the next symbol within `timeout` seconds instead of losing the
    whole cycle. The process-level watchdog in quick_scalp_alert.py stays
    as a second line of defense in case this mechanism itself ever has a
    gap, not removed -- defense in depth, not a replacement."""
    result_box = queue.Queue(maxsize=1)
    t = threading.Thread(target=_scan_one_symbol, args=(symbol, result_box, delta_map), daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        print(f"[scan] {symbol}: no response after {timeout}s -- skipping, moving to next coin", flush=True)
        return None
    try:
        return result_box.get_nowait()
    except queue.Empty:
        return None


def _scan_one_symbol_chase(symbol, result_box, delta_map):
    """Runs in a worker thread -- see _scan_one_symbol_chase_with_timeout.
    No mid_trend fetch (detect_quick_signal_chase doesn't use it, unlike
    the fade path) -- one less network call per symbol."""
    try:
        signal = detect_quick_signal_chase(symbol, delta_map)
        if signal is not None:
            signal["close_price"] = fetch_live_futures_price(symbol)
        result_box.put(signal)
    except Exception as e:
        print(f"[chase scan] {symbol} raised {e} -- skipping", flush=True)
        result_box.put(None)


def _scan_one_symbol_chase_with_timeout(symbol, delta_map, timeout=PER_SYMBOL_SCAN_TIMEOUT_SECONDS):
    """Same bounded-timeout pattern as _scan_one_symbol_with_timeout, same
    reason -- see its docstring."""
    result_box = queue.Queue(maxsize=1)
    t = threading.Thread(target=_scan_one_symbol_chase, args=(symbol, result_box, delta_map), daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        print(f"[chase scan] {symbol}: no response after {timeout}s -- skipping, moving to next coin", flush=True)
        return None
    try:
        return result_box.get_nowait()
    except queue.Empty:
        return None


def scan_quick_scalp_chase_signals(symbols=None, top_n=QUICK_SCALP_TOP_N, delta_map=None):
    """CHASE-mode equivalent of scan_quick_scalp_signals() -- see
    CHASE_BREADTH_FLOOR's comment for the backtest behind this. Shares
    the SAME delta_map as the fade scan when the caller passes one in
    (see scan_quick_scalp_signals' own docstring) -- only builds its own
    if called standalone."""
    if symbols is None:
        symbols = [s for s in _resolve_symbols(top_n) if s not in QUICK_SCALP_EXCLUDED_SYMBOLS]
    if delta_map is None:
        delta_map = _build_delta_map(symbols)

    signals = {}
    with ThreadPoolExecutor(max_workers=SCAN_WORKER_COUNT) as pool:
        futures = {pool.submit(_scan_one_symbol_chase_with_timeout, symbol, delta_map): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                signal = future.result()
            except Exception as e:
                print(f"[chase scan] {symbol} pool task raised {e} -- skipping", flush=True)
                continue
            if signal is not None:
                signals[symbol] = signal
    return signals


def scan_quick_scalp_signals_pro(symbols=None, top_n=QUICK_SCALP_TOP_N):
    """PRO MODE equivalent of scan_quick_scalp_signals() -- {symbol:
    signal_dict} for every symbol detect_quick_signal_pro confirms right
    now. Used only for Stage B shadow-logging (quick_scalp_alert.py) as
    of this writing -- NOT wired into any live alert yet."""
    if symbols is None:
        symbols = [s for s in _resolve_symbols(top_n) if s not in QUICK_SCALP_EXCLUDED_SYMBOLS]

    signals = {}
    for symbol in symbols:
        mid_trend = mid_period_trend(symbol)
        signal = detect_quick_signal_pro(symbol, mid_trend)
        if signal is not None:
            signal["close_price"] = fetch_live_futures_price(symbol)
            signals[symbol] = signal
        time.sleep(0.05)
    return signals


def _append_quick_trade_block(lines, symbol, is_long, leverage, account_equity, risk_pct):
    """Entry/TP/SL/liquidation/sizing for one leverage -- factored out so
    the alert (quick_scalp_alert.py) can bake in multiple leverages the
    same way scalp_pattern_signal.py's _append_trade_block does for /fut_.

    Shows TWO versions of TP/SL/liquidation -- one anchored at the entry
    zone's low edge, one at its high edge (added 2026-11-09 at the user's
    request: "tp/sl in 2 version base of safe price lowest and biggest").
    The zone is a RANGE, not a point -- picking a single edge as THE
    anchor (the old behavior) hid how much the actual numbers shift
    depending on exactly where within that range you get filled."""
    entry = quick_scalp_entry_for_symbol(symbol, is_long)
    if entry is None:
        lines.append(
            f"\n{symbol}: momentum confirmed {'LONG' if is_long else 'SHORT'} but no valid "
            f"support/resistance level right now (price already past every 15m EMA)"
        )
        return

    low_levels = quick_scalp_levels(entry["entry_low"], is_long, leverage, symbol=symbol)
    high_levels = quick_scalp_levels(entry["entry_high"], is_long, leverage, symbol=symbol)
    # sizing/margin still anchored on the direction-conventional edge (same
    # choice the old single-anchor version used) -- the two edges are close
    # enough that duplicating a full sizing block for both isn't useful
    sizing_anchor = entry["entry_low"] if is_long else entry["entry_high"]

    emoji = "\U0001f7e2" if is_long else "\U0001f534"
    direction = "LONG" if is_long else "SHORT"
    lines.append(f"\n{emoji} <b>{direction} {symbol}</b>  (price now: {format_price(entry['current_price'])})")
    lines.append(f"Entry/safe price: {format_price(entry['entry_low'])} - {format_price(entry['entry_high'])}")

    if entry["at_entry_now"]:
        lines.append("✅ <b>ENTER NOW</b> -- price is at the entry zone")
    else:
        lines.append(
            f"⏳ <b>WAIT</b> -- do NOT enter at {format_price(entry['current_price'])}, "
            f"{entry['distance_from_entry_pct']:+.2f}% away from the entry zone, may not get hit"
        )

    edges = (("low", entry["entry_low"], low_levels), ("high", entry["entry_high"], high_levels))
    for label, anchor_price, lv in edges:
        lines.append(f"<b>From {format_price(anchor_price)} ({label}):</b>")
        lines.append(f"  TP: {format_price(lv['take_profit'])}  ({lv['tp_roi_pct']:.0%} ROI)")
        lines.append(f"  SL: {format_price(lv['stop_loss'])}  ({lv['sl_roi_pct']:.0%} ROI)")
        lines.append(
            f"  Liquidation: {format_price(lv['liquidation_price'])} "
            f"({lv['liquidation_distance_pct']:.3%} away, MMR {lv['maintenance_margin_rate']:.2%})"
        )
        if lv["sl_capped_by_liquidation"]:
            lines.append(f"  ⚠️ SL tightened below the {QUICK_SCALP_SL_ROI:.0%} ROI target -- liquidation distance was narrower")

    if account_equity:
        s = position_sizing(sizing_anchor, low_levels["sl_distance_pct"], leverage, account_equity, risk_pct,
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


def _quick_signal_header(signal):
    """Checks + order-flow lines shared by both the on-demand message and
    the alert. Reads delta_pct/trade_count straight from the signal dict
    (computed once by detect_quick_signal) rather than recomputing or
    needing candles passed in."""
    checks_line = "  ".join(f"{name}{'✅' if passed else '❌'}" for name, passed in signal["checks"].items())
    lines = [f"✅ <b>Momentum confirmed</b>  {checks_line}"]
    delta_pct = signal.get("delta_pct")
    if delta_pct is not None:
        side = "buy" if delta_pct > 0 else "sell"
        trade_count = signal.get("trade_count", 0)
        lines.append(
            f"Order flow (rolling {QUICK_DELTA_WINDOW_MINUTES}m): {abs(delta_pct):.0%} "
            f"{side}-dominant ({trade_count:,} trades) -- <b>fading</b> this push, not chasing it"
        )
    return lines


def _quick_chase_signal_header(signal):
    """CHASE equivalent of _quick_signal_header -- same shape, "chasing"
    instead of "fading" since the trade direction IS the push direction
    here, plus the breadth reading that gated this signal in the first
    place (see CHASE_BREADTH_FLOOR)."""
    checks_line = "  ".join(f"{name}{'✅' if passed else '❌'}" for name, passed in signal["checks"].items())
    lines = [f"✅ <b>Momentum confirmed</b>  {checks_line}"]
    delta_pct = signal.get("delta_pct")
    if delta_pct is not None:
        side = "buy" if delta_pct > 0 else "sell"
        trade_count = signal.get("trade_count", 0)
        breadth = signal.get("breadth")
        breadth_str = f", breadth={breadth:.0%}" if breadth is not None else ""
        lines.append(
            f"Order flow (rolling {QUICK_DELTA_WINDOW_MINUTES}m): {abs(delta_pct):.0%} "
            f"{side}-dominant ({trade_count:,} trades{breadth_str}) -- <b>chasing</b> this push, not fading it"
        )
    return lines


def build_quick_scalp_chase_alert_message(symbol, signal, leverages=(ALERT_LEVERAGE,), account_equity=None, risk_pct=0.01):
    """CHASE equivalent of build_quick_scalp_alert_message -- see
    CHASE_BREADTH_FLOOR's comment for the backtest behind this mode.
    Same SL/TP sizing (_append_quick_trade_block, flat QUICK_SCALP_SL_ROI/
    TP_ROI) as the fade engine -- that's what the backtest actually used,
    not a separately-tuned number."""
    is_long = signal["is_long"]
    emoji = "\U0001f7e2" if is_long else "\U0001f534"
    direction = "LONG" if is_long else "SHORT"
    lines = [f"{emoji} <b>Quick scalp CHASE READY TO TRADE: {direction} {symbol}</b>",
             "(riding a crowded market move, not a reversal fade -- see /pattern_ or the market-regime "
             "alert for more context before entering)"]
    lines += _quick_chase_signal_header(signal)

    for leverage in leverages:
        lines.append(f"\n<b>── {leverage}x ──</b>")
        _append_quick_trade_block(lines, symbol, is_long, leverage, account_equity, risk_pct)
    return "\n".join(lines)


def build_quick_scalp_message(symbol, leverage, account_equity=None, risk_pct=0.01):
    fee_roi_pct = BINANCE_ROUNDTRIP_FEE_PCT / 100 * leverage
    lines = [
        f"⚡ <b>Quick scalp {leverage}x</b> ({datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d %H:%M} UTC)",
        f"⚠️ Fast/frequent design, separate from /fut_ -- round-trip fee at {leverage}x is "
        f"~{fee_roi_pct:.0%} ROI; TP {QUICK_SCALP_TP_ROI:.0%} / SL {QUICK_SCALP_SL_ROI:.0%} are tight, "
        "unvalidated first-guess numbers (zero outcome history), not a result.",
    ]

    mid_trend = mid_period_trend(symbol)
    signal = detect_quick_signal(symbol, mid_trend)

    if signal is None:
        lines.append(f"\nNo momentum trigger for {symbol} right now.")
        return "\n".join(lines)

    lines += _quick_signal_header(signal)
    _append_quick_trade_block(lines, symbol, signal["is_long"], leverage, account_equity, risk_pct)
    return "\n".join(lines)


def build_quick_scalp_alert_message(symbol, signal, leverages=(ALERT_LEVERAGE,), account_equity=None, risk_pct=0.01):
    """"Ready to trade" push for one freshly-confirmed, freshly-ready
    rolling-delta signal -- bakes in every leverage in `leverages` at
    once, same shape as scalp_pattern_signal.py's build_scalp_alert_message
    for /fut_."""
    is_long = signal["is_long"]
    emoji = "\U0001f7e2" if is_long else "\U0001f534"
    direction = "LONG" if is_long else "SHORT"
    lines = [f"{emoji} <b>Quick scalp READY TO TRADE: {direction} {symbol}</b>"]
    lines += _quick_signal_header(signal)

    for leverage in leverages:
        lines.append(f"\n<b>── {leverage}x ──</b>")
        _append_quick_trade_block(lines, symbol, is_long, leverage, account_equity, risk_pct)
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Quick scalp: rolling order-flow signal scan")
    parser.add_argument("--leverage", type=int, default=50)
    parser.add_argument("--equity", type=float, help="account equity in USD")
    parser.add_argument("--risk-pct", type=float, default=1.0, help="%% of account to risk per trade")
    parser.add_argument("--symbol", required=True, help="e.g. BTCUSDT")
    args = parser.parse_args()

    print(build_quick_scalp_message(
        args.symbol, args.leverage, account_equity=args.equity, risk_pct=args.risk_pct / 100,
    ))
