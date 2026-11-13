"""Continuation/breakout detection -- the counterpart to
candlestick_patterns.py's reversal detection. Reversal patterns bet AGAINST
the recent move (exhaustion); these bet WITH a confirmed structural break,
same spirit as the methodologies in real trader channels: volume-profile
break-and-hold, and trendline/channel breakout (which covers triangles,
rounding tops with a horizontal neckline, and ascending/descending
channels/wedges -- geometrically the same check: fit a boundary line from
recent swing highs/lows, signal when price decisively breaks through it).

Used by system 3 (scalp_pattern_signal.py) as a second, independent signal
path alongside reversal patterns -- a coin can get a signal from either
path, never both claiming the same direction is required.
"""

from volume_profile import volume_profile

# Lookback for both detectors is longer than the trade's own 1-4h resolution
# target -- needs enough history to form real structure (a value area, a
# proper swing high/low sequence) before judging a break of it as
# meaningful, not just noise.
VAH_VAL_LOOKBACK_CANDLES = 96   # 24h of 15m candles
TRENDLINE_LOOKBACK_CANDLES = 96  # 24h of 15m candles
# Window checked for "did this cross recently" -- catches an ongoing hold
# above/below the level, not just the single exact candle it crossed on
# (a periodic scan can easily land several candles after the actual cross).
RECENT_WINDOW_CANDLES = 8  # 2h of 15m candles
SWING_LEFT = 2
SWING_RIGHT = 2
MIN_SWING_POINTS = 3


def vah_val_breakout(candles, lookback=VAH_VAL_LOOKBACK_CANDLES, recent_window=RECENT_WINDOW_CANDLES):
    """True/False/None for break-and-hold above VAH (bullish) or below VAL
    (bearish): currently on the breakout side, AND was on/near the other
    side at some point within the recent window -- confirms this is an
    actual recent transition, not price that's simply always been there."""
    window = candles[-lookback:]
    if len(window) < 20 + recent_window:
        return None
    vp = volume_profile(window[:-recent_window])  # fit the profile from history BEFORE the recent window
    if vp is None:
        return None

    recent = candles[-recent_window:]
    recent_closes = [c["close"] for c in recent]
    close = recent_closes[-1]

    breakout_up = close > vp["vah"] and min(recent_closes) <= vp["vah"]
    breakout_down = close < vp["val"] and max(recent_closes) >= vp["val"]
    return {
        "breakout_up": breakout_up, "breakout_down": breakout_down,
        "poc": vp["poc"], "vah": vp["vah"], "val": vp["val"],
    }


def _find_swing_points(candles, left=SWING_LEFT, right=SWING_RIGHT):
    """Indices of local-max highs / local-min lows -- a candle must be the
    extreme among `left` candles before and `right` after it. Excludes the
    most recent `right` candles by construction (can't confirm a swing
    point without candles after it), which is exactly what's wanted: the
    breakout candle itself is never one of the points the lines are fit
    from, it's what gets tested against them."""
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    n = len(candles)
    swing_highs, swing_lows = [], []
    for i in range(left, n - right):
        if highs[i] == max(highs[i - left:i + right + 1]):
            swing_highs.append(i)
        if lows[i] == min(lows[i - left:i + right + 1]):
            swing_lows.append(i)
    return swing_highs, swing_lows


def _fit_line(indices, values):
    """Simple linear regression y = slope*x + intercept."""
    n = len(indices)
    if n < 2:
        return None
    x_mean = sum(indices) / n
    y_mean = sum(values) / n
    denom = sum((x - x_mean) ** 2 for x in indices)
    if denom == 0:
        return None
    slope = sum((indices[i] - x_mean) * (values[i] - y_mean) for i in range(n)) / denom
    intercept = y_mean - slope * x_mean
    return {"slope": slope, "intercept": intercept}


def _line_value_at(line, x):
    return line["slope"] * x + line["intercept"]


def trendline_breakout(candles, lookback=TRENDLINE_LOOKBACK_CANDLES, recent_window=RECENT_WINDOW_CANDLES):
    """Fits a resistance line through recent swing highs and a support line
    through recent swing lows (from candles BEFORE the recent window, so
    the breakout itself can't skew the line it's being tested against),
    then checks whether price is currently beyond one of them AND was on/
    near the other side within the recent window -- same "broke recently,
    still holding" logic as vah_val_breakout, not just the single exact
    crossing candle. Covers triangles (lines converging), channels/wedges
    (lines ~parallel), and flat support/resistance (one line ~horizontal)
    with one method -- they're all just different slope combinations of
    the same two fitted lines. Returns None if there aren't enough swing
    points to fit a meaningful line."""
    window = candles[-lookback:]
    n = len(window)
    if n < 20 + recent_window:
        return None

    fit_window = window[:-recent_window]
    swing_high_idx, swing_low_idx = _find_swing_points(fit_window)
    if len(swing_high_idx) < MIN_SWING_POINTS or len(swing_low_idx) < MIN_SWING_POINTS:
        return None

    resistance = _fit_line(swing_high_idx, [fit_window[i]["high"] for i in swing_high_idx])
    support = _fit_line(swing_low_idx, [fit_window[i]["low"] for i in swing_low_idx])
    if resistance is None or support is None:
        return None

    fit_len = len(fit_window)
    recent = window[-recent_window:]
    recent_closes = [c["close"] for c in recent]
    close = recent_closes[-1]

    # extrapolate each fitted line forward across the recent window's indices
    resistance_vals = [_line_value_at(resistance, fit_len + i) for i in range(recent_window)]
    support_vals = [_line_value_at(support, fit_len + i) for i in range(recent_window)]
    resistance_now = resistance_vals[-1]
    support_now = support_vals[-1]

    breakout_up = close > resistance_now and any(c <= r for c, r in zip(recent_closes, resistance_vals))
    breakout_down = close < support_now and any(c >= s for c, s in zip(recent_closes, support_vals))

    return {
        "breakout_up": breakout_up, "breakout_down": breakout_down,
        "resistance_now": resistance_now, "support_now": support_now,
        "resistance_slope": resistance["slope"], "support_slope": support["slope"],
    }


def next_key_level(candles, is_long, lookback=TRENDLINE_LOOKBACK_CANDLES):
    """Nearest resistance (LONG) or support (SHORT) level AHEAD of current
    price, from whichever of VAH/trendline is tighter -- the first
    structural level price is likely to react to. Used so a take-profit can
    sit just before it instead of overshooting into resistance/support and
    giving back the move (a real risk at a 1-4h hold: by the time flat 50%+
    ROI is hit, price may already be stalling at the next level)."""
    window = candles[-lookback:]
    if len(window) < 20:
        return None
    close = window[-1]["close"]
    candidates = []

    vp = volume_profile(window)
    if vp is not None:
        candidates.append(vp["vah"] if is_long else vp["val"])

    swing_high_idx, swing_low_idx = _find_swing_points(window)
    if is_long and len(swing_high_idx) >= MIN_SWING_POINTS:
        line = _fit_line(swing_high_idx, [window[i]["high"] for i in swing_high_idx])
        if line is not None:
            candidates.append(_line_value_at(line, len(window) - 1))
    if not is_long and len(swing_low_idx) >= MIN_SWING_POINTS:
        line = _fit_line(swing_low_idx, [window[i]["low"] for i in swing_low_idx])
        if line is not None:
            candidates.append(_line_value_at(line, len(window) - 1))

    ahead = [c for c in candidates if (c > close if is_long else c < close)]
    if not ahead:
        return None
    return min(ahead) if is_long else max(ahead)


def breakout_lean(candles):
    """Combines both detectors into one read: True (bullish break), False
    (bearish break), or None (no breakout from either method). If both fire
    in the SAME direction, that's a stronger case (two independent methods
    agreeing) -- exposed via `confirmed_by` so callers can tell the
    difference between a single-method and a double-confirmed breakout."""
    vah_val = vah_val_breakout(candles)
    trend = trendline_breakout(candles)

    up_sources = []
    down_sources = []
    if vah_val:
        if vah_val["breakout_up"]:
            up_sources.append("vah_val")
        if vah_val["breakout_down"]:
            down_sources.append("vah_val")
    if trend:
        if trend["breakout_up"]:
            up_sources.append("trendline")
        if trend["breakout_down"]:
            down_sources.append("trendline")

    if up_sources and not down_sources:
        return {"direction": "bullish", "confirmed_by": up_sources}
    if down_sources and not up_sources:
        return {"direction": "bearish", "confirmed_by": down_sources}
    return None
