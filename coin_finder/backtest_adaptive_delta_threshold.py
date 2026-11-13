"""Backtest: adaptive (Bollinger-band-style) delta threshold vs the live
engine's fixed QUICK_DELTA_CONFIRM_PCT=0.20 cutoff, on the SAME underlying
delta time series -- idea borrowed from a real open-source bot
(dineshpinto/orderbook-delta-bot) which triggers when its delta metric is
beyond mean +/- 2 std dev over a rolling window, rather than one flat
number for every symbol/regime.

Forward-walk, not retroactive-replay: builds a delta time series by
computing the rolling 10-min delta at regular steps across real historical
1m candles, then evaluates BOTH trigger definitions directly against that
series -- apples-to-apples on identical underlying data.
"""

import json
import sys
import time
import urllib.request
from collections import defaultdict

sys.path.insert(0, __file__.rsplit("/", 1)[0])

FUTURES_API = "https://fapi.binance.com"
DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
    "LTCUSDT", "AVAXUSDT", "LINKUSDT",
]
BACKTEST_DAYS = 5
STEP_MINUTES = 5       # cadence between scan points (reduces redundant near-duplicate signals)
DELTA_WINDOW_MINUTES = 10  # matches QUICK_DELTA_WINDOW_MINUTES
BB_PERIODS = 20         # matches the reference bot's 20-period Bollinger window
BB_STD_MULTIPLIER = 2.0
FIXED_THRESHOLD = 0.20  # matches live QUICK_DELTA_CONFIRM_PCT
MIN_TRADE_COUNT = 1000
OUTCOME_HORIZON_SECONDS = 3600
RISE_THRESHOLD_PCT = 0.3


def _fetch_range(symbol, interval, start_ms, end_ms):
    out = []
    cursor = start_ms
    while cursor < end_ms:
        url = (f"{FUTURES_API}/fapi/v1/klines?symbol={symbol}&interval={interval}"
               f"&startTime={cursor}&endTime={end_ms}&limit=1500")
        try:
            raw = json.loads(urllib.request.urlopen(url, timeout=15).read())
        except Exception as e:
            print(f"  fetch failed {symbol} {interval}: {e}")
            break
        if not raw:
            break
        out.extend(raw)
        last_open = raw[-1][0]
        if last_open <= cursor:
            break
        cursor = last_open + 1
        time.sleep(0.05)
    return out


def _parse(raw):
    return [
        {
            "open_time": k[0], "close": float(k[4]), "volume": float(k[5]),
            "taker_buy_volume": float(k[9]), "trade_count": int(k[8]),
        }
        for k in raw
    ]


def _delta_at(candles_1m, idx, window_minutes=DELTA_WINDOW_MINUTES):
    start = idx - window_minutes + 1
    if start < 0:
        return None, 0
    window = candles_1m[start:idx + 1]
    total_volume = sum(c["volume"] for c in window)
    total_buy = sum(c["taker_buy_volume"] for c in window)
    trade_count = sum(c["trade_count"] for c in window)
    if total_volume <= 0:
        return None, trade_count
    return (2 * total_buy - total_volume) / total_volume, trade_count


def walk_symbol(symbol, days=BACKTEST_DAYS):
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000
    print(f"Fetching {symbol}: 1m over {days}d...")
    candles_1m = _parse(_fetch_range(symbol, "1m", start_ms, end_ms))
    if len(candles_1m) < BB_PERIODS * STEP_MINUTES + DELTA_WINDOW_MINUTES:
        print(f"  insufficient data for {symbol}, skipping")
        return [], []

    # build delta series at every 1-min step (cheap, already fetched data)
    delta_series = []
    for i in range(len(candles_1m)):
        d, tc = _delta_at(candles_1m, i)
        delta_series.append((candles_1m[i]["open_time"], d, tc))

    time_idx = {c["open_time"]: i for i, c in enumerate(candles_1m)}
    step_ms = STEP_MINUTES * 60 * 1000
    lookback_points = BB_PERIODS  # in units of STEP_MINUTES

    fixed_candidates, adaptive_candidates = [], []
    scan_times = sorted(time_idx.keys())
    for t in scan_times[::STEP_MINUTES]:
        i = time_idx[t]
        d, tc = delta_series[i][1], delta_series[i][2]
        if d is None or tc < MIN_TRADE_COUNT:
            continue

        # rolling mean/std over the trailing BB_PERIODS samples, STEP_MINUTES apart
        hist_vals = []
        for k in range(1, lookback_points + 1):
            j = i - k * STEP_MINUTES
            if j < 0:
                break
            dv = delta_series[j][1]
            if dv is not None:
                hist_vals.append(dv)
        if len(hist_vals) < lookback_points:
            continue
        mean = sum(hist_vals) / len(hist_vals)
        var = sum((v - mean) ** 2 for v in hist_vals) / len(hist_vals)
        std = var ** 0.5

        push_is_long_fixed = d > 0
        is_fixed_signal = abs(d) >= FIXED_THRESHOLD
        is_adaptive_signal = (d > mean + BB_STD_MULTIPLIER * std) or (d < mean - BB_STD_MULTIPLIER * std)

        if not is_fixed_signal and not is_adaptive_signal:
            continue

        outcome_ts = t + OUTCOME_HORIZON_SECONDS * 1000
        future = [c for c in candles_1m if c["open_time"] >= outcome_ts]
        if not future:
            continue
        entry_close = candles_1m[i]["close"]
        outcome_close = future[0]["close"]
        return_pct = (outcome_close - entry_close) / entry_close * 100
        is_long_fade = not push_is_long_fixed
        signed_fade_return = return_pct if is_long_fade else -return_pct
        fade_hit = 1 if signed_fade_return >= RISE_THRESHOLD_PCT else 0

        row = {"symbol": symbol, "time": t, "delta": d, "fade_hit": fade_hit}
        if is_fixed_signal:
            fixed_candidates.append(row)
        if is_adaptive_signal:
            adaptive_candidates.append(row)

    return fixed_candidates, adaptive_candidates


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=BACKTEST_DAYS)
    parser.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    args = parser.parse_args()

    all_fixed, all_adaptive = [], []
    for sym in args.symbols:
        f, a = walk_symbol(sym, days=args.days)
        all_fixed.extend(f)
        all_adaptive.extend(a)

    def report(label, rows, days, nsym):
        n = len(rows)
        print(f"\n=== {label} ===")
        if n == 0:
            print("no candidates")
            return
        hit_rate = sum(r["fade_hit"] for r in rows) / n
        print(f"n={n} ({n/days/nsym:.1f}/day/symbol)  fade_hit={hit_rate:.1%}")
        by_symbol = defaultdict(list)
        for r in rows:
            by_symbol[r["symbol"]].append(r["fade_hit"])
        for sym, hits in sorted(by_symbol.items()):
            print(f"  {sym:10} n={len(hits):4} fade_hit={sum(hits)/len(hits):.1%}")

    report(f"FIXED threshold (|delta|>={FIXED_THRESHOLD}, current live)", all_fixed, args.days, len(args.symbols))
    report(f"ADAPTIVE threshold (Bollinger {BB_PERIODS}x{STEP_MINUTES}min, {BB_STD_MULTIPLIER} std)", all_adaptive, args.days, len(args.symbols))
