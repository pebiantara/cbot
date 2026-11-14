"""EMA ribbon (5/8/13 fanning) and VWAP reversion -- added 2026-11-13,
backtest-only so far, neither wired into any live gate yet. Two more
common scalping techniques the project never had at all (confirmed via
code search before building these): a moving-average-ribbon momentum
read, and VWAP as a mean-reversion reference.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from technical import ema  # noqa: E402

RIBBON_PERIODS = (5, 8, 13)


def ribbon_direction(candles, periods=RIBBON_PERIODS):
    """"bullish" if the fast/mid/slow EMAs are fanned in bullish order
    (fast > mid > slow), "bearish" if fanned bearish, None if tangled/not
    fanned at all (no clean momentum read).

    Excludes the still-forming current candle (added 2026-11-14) --
    found a real live case where this flipped PASS->FAIL within 60
    seconds of a 15m candle boundary, because a brand-new candle's
    close is essentially just its open print for the first stretch of
    its life, and EMA5 (shortest period = most weight on the newest
    point) swings hard on that noise. Same "don't trust the forming
    candle" convention already used by candlestick_patterns.py's
    _quick_candle_confirms (candles[-2], not candles[-1])."""
    closed = candles[:-1]
    closes = [c["close"] for c in closed]
    if not closes:
        return None
    emas = [ema(closes, p) for p in periods]
    if any(e[-1] is None for e in emas):
        return None
    fast, mid, slow = (e[-1] for e in emas)
    if fast > mid > slow:
        return "bullish"
    if fast < mid < slow:
        return "bearish"
    return None


def vwap(candles):
    """Volume-weighted average price over exactly the candles given --
    caller decides the window (this module doesn't anchor to session/day
    start, crypto trades 24/7 so there's no natural session boundary)."""
    total_vol = sum(c["volume"] for c in candles)
    if total_vol <= 0:
        return None
    total_pv = sum(((c["high"] + c["low"] + c["close"]) / 3) * c["volume"] for c in candles)
    return total_pv / total_vol


def vwap_distance_pct(candles, lookback=20):
    """(distance_pct, vwap_value) -- the last CLOSED candle's %, distance
    from the VWAP of the `lookback` closed candles before that. Positive
    = price above VWAP, negative = below. None if not enough data.

    Excludes the still-forming current candle entirely (added
    2026-11-14, same fix/reasoning as ribbon_direction above) -- both
    the VWAP window AND the "current close" being compared against it
    now anchor to the last real closed candle, not whatever the
    still-forming one happens to show in its first few seconds."""
    closed = candles[:-1]
    if len(closed) < lookback + 1:
        return None, None
    window = closed[-(lookback + 1):-1]
    v = vwap(window)
    if v is None or v <= 0:
        return None, None
    close = closed[-1]["close"]
    return (close - v) / v * 100, v


VWAP_STRETCH_THRESHOLD_PCT = 0.3  # matches the backtest's threshold


def vwap_favors_fade(dist_pct, is_long, threshold=VWAP_STRETCH_THRESHOLD_PCT):
    """True if price is stretched AWAY from VWAP in the push's own
    direction (the counter-intuitive, backtest-validated good case --
    n=100, +44.41% cum vs -23.01% for the naive "price already near/
    past VWAP so expect reversion" case, 2026-11-13). False if stretched
    the naive-reversion way, or too close to VWAP to count either way.
    Mirrors the exact bucket logic from that backtest -- don't re-derive
    the sign from intuition, this read backwards once already."""
    if dist_pct is None or abs(dist_pct) < threshold:
        return False
    stretched_bullish_favoring = dist_pct < 0
    favors_naive_reversion = (stretched_bullish_favoring and is_long) or (not stretched_bullish_favoring and not is_long)
    return not favors_naive_reversion
