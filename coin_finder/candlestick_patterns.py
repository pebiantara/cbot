"""Reversal-candle pattern detection (engulfing, hammer, shooting star).

Used as a veto gate on /now_<x>x's futures entries (leverage_risk.py) and as
system 2's standalone direction signal (futures_pattern_signal.py) -- not
folded into technical_score, since backtesting against predictions.db
(2026-11-04) showed averaging more signals into a single score made extreme
readings LESS reliable, not more. This stays a separate pass/fail check
instead, same pattern as combine.py's VAH/VAL overextension gates.

Shape alone isn't enough to call something a reversal, classically:
- Volume must confirm it -- a hammer/shooting star on dead volume is noise;
  the same shape on a volume spike (capitulation/climax) is a real signal.
- It must follow a real prior move in the opposite direction -- a hammer
  only means something after an actual decline; the identical shape
  appearing mid-uptrend isn't "reversing" anything.
Both are checked below (_volume_confirms, _prior_decline/_prior_rally)
before bullish_reversal()/bearish_reversal() return True, which is why they
need more history than just the pattern candle itself -- see
MIN_CANDLES_NEEDED below for how many candles callers must fetch.
"""

VOLUME_LOOKBACK = 20              # candles used for the "average volume" baseline
VOLUME_CONFIRM_MULTIPLIER = 1.2   # pattern candle's volume must be >= this x the average to count
TREND_LOOKBACK = 10               # candles checked for a real prior move in the opposite direction
MIN_CANDLES_NEEDED = max(VOLUME_LOOKBACK, TREND_LOOKBACK) + 2


def _body(c):
    return abs(c["close"] - c["open"])


def _upper_wick(c):
    return c["high"] - max(c["open"], c["close"])


def _lower_wick(c):
    return min(c["open"], c["close"]) - c["low"]


def is_bullish_engulfing(prev, cur):
    """prev candle bearish, cur candle bullish and its body engulfs prev's."""
    return (
        prev["close"] < prev["open"]
        and cur["close"] > cur["open"]
        and cur["open"] <= prev["close"]
        and cur["close"] >= prev["open"]
    )


def is_bearish_engulfing(prev, cur):
    return (
        prev["close"] > prev["open"]
        and cur["close"] < cur["open"]
        and cur["open"] >= prev["close"]
        and cur["close"] <= prev["open"]
    )


def is_hammer(c):
    """Small body near the top of the range, long lower wick -- bullish reversal."""
    body = _body(c)
    candle_range = c["high"] - c["low"]
    if candle_range == 0 or body == 0:
        return False
    return (
        _lower_wick(c) >= 2 * body
        and _upper_wick(c) <= body
        and body <= 0.3 * candle_range
    )


def is_shooting_star(c):
    """Small body near the bottom of the range, long upper wick -- bearish reversal."""
    body = _body(c)
    candle_range = c["high"] - c["low"]
    if candle_range == 0 or body == 0:
        return False
    return (
        _upper_wick(c) >= 2 * body
        and _lower_wick(c) <= body
        and body <= 0.3 * candle_range
    )


DOJI_BODY_MAX_FRACTION = 0.1  # body/range -- near-zero body, the dividing line of every doji variant


def is_doji(c):
    """Near-zero body, wicks on both sides -- indecision, not directional
    on its own (see the Neutral column of the standard cheat sheet)."""
    body = _body(c)
    candle_range = c["high"] - c["low"]
    if candle_range == 0:
        return False
    return body <= DOJI_BODY_MAX_FRACTION * candle_range


def is_gravestone_doji(c):
    """Doji with a long upper wick and almost no lower wick -- same
    geometry as a shooting star but with a near-zero body instead of
    just a small one. Bearish after a rally."""
    if not is_doji(c):
        return False
    candle_range = c["high"] - c["low"]
    return _upper_wick(c) >= 0.6 * candle_range and _lower_wick(c) <= 0.1 * candle_range


def is_dragonfly_doji(c):
    """Doji with a long lower wick and almost no upper wick -- same
    geometry as a hammer but with a near-zero body. Bullish after a decline."""
    if not is_doji(c):
        return False
    candle_range = c["high"] - c["low"]
    return _lower_wick(c) >= 0.6 * candle_range and _upper_wick(c) <= 0.1 * candle_range


TWEEZER_TOLERANCE_PCT = 0.05  # how close two highs/lows must be (% of price) to count as "matching"


def is_tweezer_bottom(prev, cur):
    """Two candles with closely-matching lows -- a level got tested twice
    and held both times. Bullish after a decline."""
    if cur["low"] <= 0:
        return False
    return abs(prev["low"] - cur["low"]) / cur["low"] <= TWEEZER_TOLERANCE_PCT / 100


def is_tweezer_top(prev, cur):
    """Two candles with closely-matching highs -- bearish after a rally."""
    if cur["high"] <= 0:
        return False
    return abs(prev["high"] - cur["high"]) / cur["high"] <= TWEEZER_TOLERANCE_PCT / 100


PIERCING_CLOSE_INTO_FRACTION = 0.5  # 2nd candle must close at least halfway into the 1st candle's body


def is_piercing_pattern(prev, cur):
    """Bullish reversal: prev candle bearish, cur candle opens below
    prev's low/close and closes back above the midpoint of prev's body --
    a "softer" engulfing (closes INTO the body by >=50%, doesn't need to
    fully engulf it)."""
    if prev["close"] >= prev["open"] or cur["close"] <= cur["open"]:
        return False
    prev_body = prev["open"] - prev["close"]
    if prev_body <= 0:
        return False
    midpoint = prev["close"] + PIERCING_CLOSE_INTO_FRACTION * prev_body
    return cur["open"] < prev["close"] and cur["close"] > midpoint and cur["close"] < prev["open"]


def is_dark_cloud_cover(prev, cur):
    """Bearish reversal: prev candle bullish, cur candle opens above
    prev's high/close and closes back below the midpoint of prev's body."""
    if prev["close"] <= prev["open"] or cur["close"] >= cur["open"]:
        return False
    prev_body = prev["close"] - prev["open"]
    if prev_body <= 0:
        return False
    midpoint = prev["close"] - PIERCING_CLOSE_INTO_FRACTION * prev_body
    return cur["open"] > prev["close"] and cur["close"] < midpoint and cur["close"] > prev["open"]


def is_three_white_soldiers(c1, c2, c3):
    """Bullish continuation: three consecutive green candles, each
    closing higher than the last, each opening within the prior candle's
    body (not gapping away)."""
    candles = (c1, c2, c3)
    if not all(c["close"] > c["open"] for c in candles):
        return False
    if not (c2["close"] > c1["close"] and c3["close"] > c2["close"]):
        return False
    return c2["open"] > c1["open"] and c3["open"] > c2["open"]


def is_three_black_crows(c1, c2, c3):
    """Bearish continuation: three consecutive red candles, each closing
    lower than the last."""
    candles = (c1, c2, c3)
    if not all(c["close"] < c["open"] for c in candles):
        return False
    if not (c2["close"] < c1["close"] and c3["close"] < c2["close"]):
        return False
    return c2["open"] < c1["open"] and c3["open"] < c2["open"]


SPINNING_TOP_BODY_MAX_FRACTION = 0.3   # small body...
SPINNING_TOP_MIN_WICK_FRACTION = 0.3   # ...with both wicks at least this big a fraction of the range


def is_spinning_top(c):
    """Small body, roughly balanced wicks on both sides, but NOT as
    extreme as a doji (body isn't near-zero, just small) -- indecision/
    low momentum, same neutral read as a doji but a distinct shape."""
    body = _body(c)
    candle_range = c["high"] - c["low"]
    if candle_range == 0:
        return False
    if body > SPINNING_TOP_BODY_MAX_FRACTION * candle_range or body <= DOJI_BODY_MAX_FRACTION * candle_range:
        return False
    return (
        _upper_wick(c) >= SPINNING_TOP_MIN_WICK_FRACTION * candle_range
        and _lower_wick(c) >= SPINNING_TOP_MIN_WICK_FRACTION * candle_range
    )


STAR_BODY_MAX_FRACTION = 0.3   # the middle "star" candle must have a small body
STAR_CLOSE_INTO_FRACTION = 0.5  # the 3rd candle must close at least halfway into the 1st candle's body


def is_morning_star(c1, c2, c3):
    """3-candle bullish reversal: big bearish candle, a small-bodied
    "star" (gap down not required on crypto's 24/7 market), then a big
    bullish candle closing well back into the first candle's body."""
    body1 = _body(c1)
    if body1 == 0 or c1["close"] >= c1["open"]:
        return False
    range2 = c2["high"] - c2["low"]
    if range2 == 0 or _body(c2) > STAR_BODY_MAX_FRACTION * range2:
        return False
    if c3["close"] <= c3["open"]:
        return False
    midpoint1 = c1["open"] - STAR_CLOSE_INTO_FRACTION * body1
    return c3["close"] >= midpoint1


def is_evening_star(c1, c2, c3):
    """3-candle bearish reversal: big bullish candle, a small-bodied
    star, then a big bearish candle closing well back into the first
    candle's body."""
    body1 = _body(c1)
    if body1 == 0 or c1["close"] <= c1["open"]:
        return False
    range2 = c2["high"] - c2["low"]
    if range2 == 0 or _body(c2) > STAR_BODY_MAX_FRACTION * range2:
        return False
    if c3["close"] >= c3["open"]:
        return False
    midpoint1 = c1["open"] + STAR_CLOSE_INTO_FRACTION * body1
    return c3["close"] <= midpoint1


def _recent_avg_volume(candles, lookback=VOLUME_LOOKBACK):
    """Average volume of the `lookback` candles BEFORE the current one
    (index -1 excluded) -- comparing a candle's volume to an average that
    includes itself would understate how unusual it actually was."""
    history = candles[:-1][-lookback:]
    if not history:
        return None
    return sum(c["volume"] for c in history) / len(history)


def _volume_confirms(candles):
    avg = _recent_avg_volume(candles)
    if not avg:
        return False
    return candles[-1]["volume"] >= VOLUME_CONFIRM_MULTIPLIER * avg


def _prior_decline(candles, lookback=TREND_LOOKBACK):
    """True if price was net lower `lookback` candles ago than just before
    the current (pattern) candle -- i.e. there was an actual decline for a
    bullish reversal to plausibly be reversing."""
    if len(candles) < lookback + 2:
        return False
    return candles[-2]["close"] < candles[-(lookback + 1)]["close"]


def _prior_rally(candles, lookback=TREND_LOOKBACK):
    if len(candles) < lookback + 2:
        return False
    return candles[-2]["close"] > candles[-(lookback + 1)]["close"]


def bullish_reversal(candles):
    """True only if the latest candle(s) form a bullish reversal shape AND
    it's backed by above-average volume AND it follows a real decline --
    see module docstring for why shape alone isn't enough. Needs at least
    MIN_CANDLES_NEEDED candles; returns False if given fewer."""
    if len(candles) < 2:
        return False
    prev, cur = candles[-2], candles[-1]
    shape = is_bullish_engulfing(prev, cur) or is_hammer(cur)
    return shape and _volume_confirms(candles) and _prior_decline(candles)


def bearish_reversal(candles):
    """True only if the latest candle(s) form a bearish reversal shape AND
    it's backed by above-average volume AND it follows a real rally."""
    if len(candles) < 2:
        return False
    prev, cur = candles[-2], candles[-1]
    shape = is_bearish_engulfing(prev, cur) or is_shooting_star(cur)
    return shape and _volume_confirms(candles) and _prior_rally(candles)


def reversal_lean(candles):
    """Like bullish_reversal()/bearish_reversal(), but always returns detail
    instead of a plain True/False -- lets a caller show a partial match
    (shape present but volume/trend didn't confirm) honestly labeled,
    instead of nothing at all. Returns None only when neither a bullish nor
    bearish candle SHAPE is present on this candle at all (no pattern to
    even partially report on)."""
    if len(candles) < 2:
        return None
    prev, cur = candles[-2], candles[-1]

    if is_bullish_engulfing(prev, cur):
        shape, direction = "bullish engulfing", "bullish"
    elif is_hammer(cur):
        shape, direction = "hammer", "bullish"
    elif is_bearish_engulfing(prev, cur):
        shape, direction = "bearish engulfing", "bearish"
    elif is_shooting_star(cur):
        shape, direction = "shooting star", "bearish"
    else:
        return None

    volume_confirms = _volume_confirms(candles)
    trend_confirms = _prior_decline(candles) if direction == "bullish" else _prior_rally(candles)
    return _finish_lean(shape, direction, candles, volume_confirms, trend_confirms)


def _finish_lean(shape, direction, candles, volume_confirms, trend_confirms):
    return {
        "direction": direction,
        "shape": shape,
        "volume_confirms": volume_confirms,
        "trend_confirms": trend_confirms,
        "confirmed": volume_confirms and trend_confirms,
    }


def full_reversal_lean(candles):
    """Like reversal_lean(), but checks the FULL standard cheat-sheet set
    (added 2026-11-13, backtest-only for now -- not wired into any live
    gate): hammer/inverted-hammer/hanging-man (same two shapes, direction
    decided by prior trend context since hammer/hanging-man share one
    shape and shooting-star/inverted-hammer share the other), engulfing,
    doji/gravestone/dragonfly, tweezer top/bottom, morning/evening star.
    Does NOT include the continuation patterns (Three Line Strike, Three
    Methods, Mat Hold) -- out of scope for a reversal-fade mechanism,
    skipped for time. Priority order below is deliberate: more specific/
    rarer shapes checked first so a doji doesn't get miscategorized as a
    generic hammer, etc."""
    if len(candles) < 2:
        return None
    prev, cur = candles[-2], candles[-1]
    prior_decline = _prior_decline(candles)
    prior_rally = _prior_rally(candles)

    shape = direction = None
    if len(candles) >= 3 and is_morning_star(candles[-3], candles[-2], candles[-1]):
        shape, direction = "morning star", "bullish"
    elif len(candles) >= 3 and is_evening_star(candles[-3], candles[-2], candles[-1]):
        shape, direction = "evening star", "bearish"
    elif len(candles) >= 3 and is_three_white_soldiers(candles[-3], candles[-2], candles[-1]):
        shape, direction = "three white soldiers", "bullish"
    elif len(candles) >= 3 and is_three_black_crows(candles[-3], candles[-2], candles[-1]):
        shape, direction = "three black crows", "bearish"
    elif is_dragonfly_doji(cur):
        shape, direction = ("dragonfly doji", "bullish") if prior_decline else ("dragonfly doji", "bearish")
    elif is_gravestone_doji(cur):
        shape, direction = ("gravestone doji", "bearish") if prior_rally else ("gravestone doji", "bullish")
    elif is_doji(cur):
        shape, direction = "doji", None  # genuinely neutral -- no directional lean to report
    elif is_spinning_top(cur):
        shape, direction = "spinning top", None  # also neutral/indecision, distinct shape from doji
    elif is_bullish_engulfing(prev, cur):
        shape, direction = "bullish engulfing", "bullish"
    elif is_bearish_engulfing(prev, cur):
        shape, direction = "bearish engulfing", "bearish"
    elif is_piercing_pattern(prev, cur):
        shape, direction = "piercing pattern", "bullish"
    elif is_dark_cloud_cover(prev, cur):
        shape, direction = "dark cloud cover", "bearish"
    elif is_tweezer_bottom(prev, cur):
        shape, direction = "tweezer bottom", "bullish"
    elif is_tweezer_top(prev, cur):
        shape, direction = "tweezer top", "bearish"
    elif is_hammer(cur):
        shape, direction = ("hammer", "bullish") if prior_decline else ("hanging man", "bearish")
    elif is_shooting_star(cur):
        shape, direction = ("shooting star", "bearish") if prior_rally else ("inverted hammer", "bullish")
    else:
        return None

    if direction is None:
        return {"direction": None, "shape": shape, "volume_confirms": _volume_confirms(candles), "trend_confirms": None, "confirmed": False}
    volume_confirms = _volume_confirms(candles)
    trend_confirms = prior_decline if direction == "bullish" else prior_rally
    return _finish_lean(shape, direction, candles, volume_confirms, trend_confirms)


if __name__ == "__main__":
    # Standalone check, independent of the /now_<x>x veto wiring in
    # leverage_risk.py -- e.g. `python candlestick_patterns.py BTCUSDT
    # --interval 4h` to manually check any symbol/timeframe on demand.
    import argparse

    from history import get_futures_klines

    parser = argparse.ArgumentParser(description="Check for a reversal candle pattern on Binance Futures")
    parser.add_argument("symbol")
    parser.add_argument("--interval", default="1h", choices=["1h", "4h", "1d"])
    args = parser.parse_args()

    candles = get_futures_klines(args.symbol, args.interval, limit=MIN_CANDLES_NEEDED)
    cur = candles[-1]
    print(
        f"{args.symbol} {args.interval} latest candle: "
        f"open={cur['open']} high={cur['high']} low={cur['low']} close={cur['close']} "
        f"volume={cur['volume']}"
    )
    avg_vol = _recent_avg_volume(candles)
    print(f"volume vs {VOLUME_LOOKBACK}-candle average: {cur['volume'] / avg_vol:.2f}x" if avg_vol else "volume: n/a")
    bull = bullish_reversal(candles)
    bear = bearish_reversal(candles)
    if bull:
        print("-> Bullish reversal pattern detected (shape + volume + prior decline all confirm)")
    if bear:
        print("-> Bearish reversal pattern detected (shape + volume + prior rally all confirm)")
    if not bull and not bear:
        print("-> No confirmed reversal pattern (shape, volume, and prior trend must all agree)")
