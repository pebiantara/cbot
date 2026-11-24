"""/sr_<symbol> -- on-demand support/resistance lookup on 1h and 4h
candles, reusing breakout_patterns.py's existing structural-level logic
(next_key_level: nearest VAH/trendline resistance or VAL/trendline
support ahead of price, whichever is tighter) and volume_profile.py's
POC/VAH/VAL -- the same building blocks System 3's own TP capping
already relies on, not a new detector. Diagnostic only: no SL/TP,
no entry, no trade direction.
"""

from breakout_patterns import next_key_level
from history import get_futures_klines
from volume_profile import volume_profile

# Candle counts chosen to cover a comparable real history per timeframe --
# ~1 day of 15m (matches breakout_patterns.py's own 96-candle default for
# this interval), ~4 days of 1h, ~10 days of 4h, ~3 months of 1d -- all
# well above next_key_level's/volume_profile's own minimum data
# requirements. Ordered fastest-to-slowest for a natural top-down read.
SR_INTERVALS = (("15m", 96), ("1h", 100), ("4h", 60), ("1d", 90))


def support_resistance_for_interval(symbol, interval, lookback):
    """Structural levels (next_key_level, volume_profile) are computed from
    CLOSED candles only -- the still-forming current candle's high/low/
    volume keeps changing as price ticks within it, which can shift VAH/
    VAL/POC and the swing-point trendline fit for reasons that have nothing
    to do with real structure (same class of bug already fixed for ribbon/
    VWAP elsewhere in this project, see quick_scalp_signal.py's
    _quick_candle_confirms -- "use candles[-2], not the still-forming
    one"). `close` still comes from the forming candle, since that's
    genuinely the current live price."""
    candles = get_futures_klines(symbol, interval, limit=lookback + 1)
    if len(candles) < 21:
        return None
    close = candles[-1]["close"]
    closed_candles = candles[:-1]
    resistance = next_key_level(closed_candles, is_long=True, lookback=lookback)
    support = next_key_level(closed_candles, is_long=False, lookback=lookback)
    vp = volume_profile(closed_candles)
    return {"close": close, "resistance": resistance, "support": support, "volume_profile": vp}


def build_support_resistance_message(symbol):
    lines = [f"📊 <b>{symbol} support / resistance</b>"]
    for interval, lookback in SR_INTERVALS:
        result = support_resistance_for_interval(symbol, interval, lookback)
        lines.append(f"\n<b>{interval}</b>")
        if result is None:
            lines.append("  not enough candle history yet")
            continue
        close = result["close"]
        lines.append(f"  price: {close:g}")

        resistance = result["resistance"]
        if resistance is not None:
            lines.append(f"  resistance: {resistance:g} ({(resistance - close) / close * 100:+.2f}%)")
        else:
            lines.append("  resistance: none found ahead (clear air)")

        support = result["support"]
        if support is not None:
            lines.append(f"  support: {support:g} ({(support - close) / close * 100:+.2f}%)")
        else:
            lines.append("  support: none found ahead (clear air)")

        vp = result["volume_profile"]
        if vp is not None:
            lines.append(f"  volume profile: VAH={vp['vah']:g}  POC={vp['poc']:g}  VAL={vp['val']:g}")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Support/resistance on 1h and 4h candles")
    parser.add_argument("symbol")
    args = parser.parse_args()
    print(build_support_resistance_message(args.symbol.upper()))
