"""Download Binance candle history (1h/4h/1d) for coins in the universe."""

import csv
import os
import time

import requests

from http_pool import SESSION
from universe import get_universe

BINANCE_API = "https://api.binance.com"
BINANCE_FUTURES_API = "https://fapi.binance.com"
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
TIMEFRAMES = ["1h", "4h", "1d"]
CANDLE_LIMIT = 300  # enough warmup for EMA200

COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "taker_buy_volume", "trade_count"]

# Retries transient network errors (DNS blips, connection resets) before
# giving up -- added 2026-11-12 after quick-scalp-alert's logs showed
# "Temporary failure in name resolution" on roughly 1-in-3 polls over
# several hours, silently skipping that cycle's scan. Callers already
# catch and continue on a final failure (poll_once_all()'s try/except),
# so this only reduces how often that happens, doesn't change the
# contract if every attempt fails.
_GET_RETRIES = 3
_GET_RETRY_BACKOFF_SECONDS = 0.5


def _get(base_url, path):
    """Uses the shared pooled SESSION (http_pool.py) -- added 2026-11-17,
    replacing a fresh urllib.request.urlopen() per call (a new TCP+TLS
    connection every single time) with a reused, keep-alive connection
    to the same host. Real, measured fix for sustained high CPU usage
    at this function's call volume (this is the single most-called `_get`
    in the whole container: delta_map, fade scan, and chase scan all
    route their candle fetches through get_futures_klines -> here)."""
    url = f"{base_url}{path}"
    last_error = None
    for attempt in range(_GET_RETRIES):
        try:
            resp = SESSION.get(url, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < _GET_RETRIES - 1:
                time.sleep(_GET_RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise last_error


def _parse_klines(raw):
    return [
        {
            "open_time": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "close_time": k[6],
            # taker buy base asset volume (index 9) -- sell volume is just
            # volume - taker_buy_volume, both already in base-asset units
            "taker_buy_volume": float(k[9]),
            # number of individual trades (index 8) -- lets a caller tell
            # "broad participation" apart from "one or two large orders
            # skewed this candle's volume/delta", which raw volume alone
            # can't distinguish
            "trade_count": int(k[8]),
        }
        for k in raw
    ]


def get_klines(symbol, interval, limit=CANDLE_LIMIT):
    raw = _get(BINANCE_API, f"/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}")
    return _parse_klines(raw)


def get_futures_klines(symbol, interval, limit=CANDLE_LIMIT):
    """Binance Futures (USDT-M perpetual) candles -- used by the leverage-specific
    /now_<x>x path so EMA/entry-zone levels match where a leveraged position would
    actually be opened (spot has no leverage). Many smaller-cap symbols in the spot
    universe aren't listed on futures at all; callers should catch the exception and
    fall back to spot."""
    raw = _get(BINANCE_FUTURES_API, f"/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}")
    return _parse_klines(raw)


def save_candles_csv(symbol, interval, candles):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{symbol}_{interval}.csv")
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(candles)
    return path


def download_history(symbols, timeframes=TIMEFRAMES):
    """Download and save candles for each symbol/timeframe. Returns {symbol: {tf: candles}}."""
    history = {}
    for symbol in symbols:
        history[symbol] = {}
        for tf in timeframes:
            candles = get_klines(symbol, tf)
            save_candles_csv(symbol, tf, candles)
            history[symbol][tf] = candles
            time.sleep(0.1)  # stay well under Binance rate limits
    return history


if __name__ == "__main__":
    universe = get_universe()
    symbols = [c["symbol"] for c in universe]
    print(f"Downloading {len(symbols)} symbols x {TIMEFRAMES} candles...")
    download_history(symbols)
    print(f"Saved candle CSVs to {DATA_DIR}")
