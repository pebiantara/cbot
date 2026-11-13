"""Volume Profile approximated from OHLCV candles (no tick/order-book data
available from Binance's REST klines). Each candle's volume is spread evenly
across price bins that fall inside its high-low range -- standard approximation
when only candle data is available, more accurate than dumping all of a
candle's volume onto its close price.

POC (Point of Control): the price bin with the most traded volume -- the
"fair value" level the market spent the most time/volume agreeing on.
Value Area: the band of bins around the POC holding `value_area_pct` of total
volume, built by greedily adding whichever side (upper or lower) has more
volume until the target is reached -- the standard value-area construction.
"""

NUM_BINS = 24
VALUE_AREA_PCT = 0.70


def volume_profile(candles, num_bins=NUM_BINS, value_area_pct=VALUE_AREA_PCT):
    """candles: list of {"high", "low", "volume"} dicts (chronological, any
    length -- caller decides the lookback window). Returns None if there's
    not enough data or the price range in the window is zero (e.g. a
    stablecoin or completely flat series)."""
    if not candles:
        return None

    lo = min(c["low"] for c in candles)
    hi = max(c["high"] for c in candles)
    if hi <= lo:
        return None

    bin_width = (hi - lo) / num_bins
    bin_volume = [0.0] * num_bins

    for c in candles:
        c_lo, c_hi, vol = c["low"], c["high"], c["volume"]
        if vol <= 0:
            continue
        span = c_hi - c_lo
        if span <= 0:
            # single-price candle (no intrabar range) -- dump volume in its own bin
            idx = min(int((c_lo - lo) / bin_width), num_bins - 1)
            bin_volume[idx] += vol
            continue

        # distribute volume proportionally to how much of each bin the
        # candle's [low, high] range overlaps
        first_bin = max(0, int((c_lo - lo) / bin_width))
        last_bin = min(num_bins - 1, int((c_hi - lo) / bin_width))
        for idx in range(first_bin, last_bin + 1):
            bin_lo = lo + idx * bin_width
            bin_hi = bin_lo + bin_width
            overlap = min(c_hi, bin_hi) - max(c_lo, bin_lo)
            if overlap > 0:
                bin_volume[idx] += vol * (overlap / span)

    total_volume = sum(bin_volume)
    if total_volume <= 0:
        return None

    poc_idx = max(range(num_bins), key=lambda i: bin_volume[i])
    poc_price = lo + (poc_idx + 0.5) * bin_width

    # value area: grow outward from the POC bin, each step adding whichever
    # neighboring side currently holds more volume, until value_area_pct of
    # total volume is covered.
    low_idx = high_idx = poc_idx
    covered = bin_volume[poc_idx]
    target = total_volume * value_area_pct

    while covered < target and (low_idx > 0 or high_idx < num_bins - 1):
        next_low = low_idx - 1 if low_idx > 0 else None
        next_high = high_idx + 1 if high_idx < num_bins - 1 else None
        vol_low = bin_volume[next_low] if next_low is not None else -1
        vol_high = bin_volume[next_high] if next_high is not None else -1

        if vol_high >= vol_low:
            high_idx = next_high
            covered += vol_high
        else:
            low_idx = next_low
            covered += vol_low

    val_price = lo + low_idx * bin_width
    vah_price = lo + (high_idx + 1) * bin_width

    return {"poc": poc_price, "vah": vah_price, "val": val_price}
