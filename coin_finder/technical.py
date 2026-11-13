"""Technical indicators (EMA/RSI/MACD/ATR) and technical_score per coin."""

import csv
import os

from history import download_history
from market_cap import get_market_cap_data
from universe import get_universe
from volume_profile import volume_profile

OUTPUT_CSV = os.path.join(os.path.dirname(__file__), "technical_scores.csv")

VP_LOOKBACK_CANDLES = 100  # ~4 days of 1h candles -- the "recently agreed fair value" window


def ema(values, period):
    """Exponential moving average, aligned with values (None where not enough data yet)."""
    if len(values) < period:
        return [None] * len(values)
    k = 2 / (period + 1)
    result = [None] * (period - 1)
    prev = sum(values[:period]) / period
    result.append(prev)
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
        result.append(prev)
    return result


def rsi(values, period=14):
    if len(values) <= period:
        return [None] * len(values)
    gains = [0.0]
    losses = [0.0]
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    result = [None] * period
    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period
    result.append(100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss))

    for i in range(period + 1, len(values)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result.append(100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss))
    return result


def macd(values, fast=12, slow=26, signal=9):
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    macd_line = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(ema_fast, ema_slow)
    ]
    valid = [v for v in macd_line if v is not None]
    signal_valid = ema(valid, signal)
    signal_line = [None] * (len(macd_line) - len(signal_valid)) + signal_valid
    return macd_line, signal_line


def atr(highs, lows, closes, period=14):
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    return ema(trs, period)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def trend_flag(candles):
    """1 if latest close is above this timeframe's EMA200, else 0. None if not enough candles."""
    if not candles:
        return None
    closes = [c["close"] for c in candles]
    ema200 = ema(closes, 200)
    if ema200[-1] is None:
        return None
    return 1 if closes[-1] > ema200[-1] else 0


def technical_score(history_by_tf):
    """history_by_tf: {"1h": candles, "4h": candles, "1d": candles} -> (score 0..1, indicators dict)."""
    candles_1h = history_by_tf["1h"]
    closes_1h = [c["close"] for c in candles_1h]
    highs_1h = [c["high"] for c in candles_1h]
    lows_1h = [c["low"] for c in candles_1h]
    volumes_1h = [c["volume"] for c in candles_1h]

    ema20 = ema(closes_1h, 20)
    ema50 = ema(closes_1h, 50)
    ema200 = ema(closes_1h, 200)
    rsi14 = rsi(closes_1h, 14)
    macd_line, macd_signal = macd(closes_1h)
    atr14 = atr(highs_1h, lows_1h, closes_1h, 14)

    if None in (ema200[-1], rsi14[-1], macd_line[-1], macd_signal[-1], atr14[-1]):
        return None, None

    close = closes_1h[-1]

    trend_1h = (
        (1 if close > ema200[-1] else 0)
        + (1 if ema20[-1] > ema50[-1] else 0)
        + (1 if ema50[-1] > ema200[-1] else 0)
    ) / 3
    trend_4h = trend_flag(history_by_tf.get("4h"))
    trend_1d = trend_flag(history_by_tf.get("1d"))
    # 4h weighted highest: smoother than 1h (less single-candle noise) but
    # reacts faster than waiting for the 1d EMA200 to confirm. fall back to
    # the 1h trend when a higher timeframe doesn't have enough candles yet.
    trend_score = (
        0.25 * trend_1h
        + 0.5 * (trend_4h if trend_4h is not None else trend_1h)
        + 0.25 * (trend_1d if trend_1d is not None else trend_1h)
    )

    momentum_score = clamp((rsi14[-1] - 30) / 40, 0, 1)  # RSI 30->0, 70->1
    macd_score = 1.0 if macd_line[-1] > macd_signal[-1] else 0.0

    window = volumes_1h[-20:]
    avg_volume = sum(window) / len(window)
    volume_change_pct = ((volumes_1h[-1] - avg_volume) / avg_volume * 100) if avg_volume else 0.0
    volume_score = (clamp(volume_change_pct / 100, -1, 1) + 1) / 2

    atr_pct = atr14[-1] / close * 100
    volatility_score = 1 - clamp(atr_pct / 10, 0, 1)  # very high volatility trims the score

    score = (
        0.35 * trend_score
        + 0.25 * momentum_score
        + 0.20 * macd_score
        + 0.10 * volume_score
        + 0.10 * volatility_score
    )

    # Volume Profile (POC/VAH/VAL) over the recent window -- where the market
    # actually agreed on "fair value", not just which way price has been
    # trending. Used by combine.py to avoid calling a coin GREEN when it's
    # already chased well above its own value area (the overextended entries
    # that accuracy_report.json showed reverting instead of continuing).
    vp = volume_profile(candles_1h[-VP_LOOKBACK_CANDLES:])
    if vp is not None:
        poc, vah, val = vp["poc"], vp["vah"], vp["val"]
        extension_above_vah_pct = max(0.0, (close - vah) / vah * 100)
        extension_below_val_pct = max(0.0, (val - close) / val * 100)
    else:
        poc = vah = val = None
        extension_above_vah_pct = extension_below_val_pct = None

    indicators = {
        "close": close,
        "ema20": ema20[-1],
        "ema50": ema50[-1],
        "ema200": ema200[-1],
        "rsi": rsi14[-1],
        "macd": macd_line[-1],
        "macd_signal": macd_signal[-1],
        "atr_pct": atr_pct,
        "volume_change_pct": volume_change_pct,
        "trend_score": trend_score,
        "momentum_score": momentum_score,
        "macd_score": macd_score,
        "volume_score": volume_score,
        "volatility_score": volatility_score,
        "poc": poc,
        "vah": vah,
        "val": val,
        "extension_above_vah_pct": extension_above_vah_pct,
        "extension_below_val_pct": extension_below_val_pct,
    }
    return score, indicators


FIELDNAMES = [
    "symbol", "technical_score", "close", "ema20", "ema50", "ema200", "rsi",
    "macd", "macd_signal", "atr_pct", "volume_change_pct",
    "trend_score", "momentum_score", "macd_score", "volume_score", "volatility_score",
    "poc", "vah", "val", "extension_above_vah_pct", "extension_below_val_pct",
    "market_cap", "volume_mcap_ratio",
]


if __name__ == "__main__":
    universe = get_universe()
    symbols = [c["symbol"] for c in universe]
    print(f"Downloading history for {len(symbols)} symbols...")
    history = download_history(symbols)

    mcap_data = get_market_cap_data()
    print(f"Fetched market cap data for {len(mcap_data)} CoinGecko symbols")

    rows = []
    for symbol in symbols:
        score, indicators = technical_score(history[symbol])
        if score is None:
            print(f"{symbol}: not enough candle history, skipped")
            continue
        row = {"symbol": symbol, "technical_score": round(score, 4)}
        for k, v in indicators.items():
            if not isinstance(v, float):
                row[k] = v
            # price fields need far more precision for sub-cent tokens (e.g. PEPE,
            # SHIB) -- rounding close to 4 decimals turns $0.000007 into $0.0
            elif k in ("close", "ema20", "ema50", "ema200", "poc", "vah", "val"):
                row[k] = round(v, 10)
            else:
                row[k] = round(v, 4)

        # informational only -- not folded into technical_score. "" (not 0)
        # when there's no CoinGecko match, so it doesn't look like zero cap.
        base_symbol = symbol[:-4] if symbol.endswith("USDT") else symbol
        mcap_entry = mcap_data.get(base_symbol)
        if mcap_entry and mcap_entry.get("market_cap"):
            market_cap = mcap_entry["market_cap"]
            total_volume = mcap_entry.get("total_volume")
            row["market_cap"] = round(market_cap, 2)
            row["volume_mcap_ratio"] = round(total_volume / market_cap, 4) if total_volume else ""
        else:
            row["market_cap"] = ""
            row["volume_mcap_ratio"] = ""

        rows.append(row)

    rows.sort(key=lambda r: r["technical_score"], reverse=True)

    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWrote {len(rows)} coins to {OUTPUT_CSV}\n")
    print(f"{'symbol':<12}{'technical_score':>16}")
    for r in rows:
        print(f"{r['symbol']:<12}{r['technical_score']:>16.4f}")
