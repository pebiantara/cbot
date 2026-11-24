"""Binance USDT spot universe: pairs with enough 24h volume to trade without slippage."""

import time

import requests

from http_pool import SESSION

BINANCE_API = "https://api.binance.com"
COINGECKO_API = "https://api.coingecko.com"
MIN_QUOTE_VOLUME_USDT = 5_000_000  # lowered from 10M 2026-11-12 -- alerts were repeating the same
# handful of coins; widens the universe 30->45. Tradeoff: more slippage/liquidity
# risk on the newly-included lower-volume names than the original $10M floor
# accepted (previously kept at $10M deliberately for that reason).

# USD-pegged stablecoins: near-zero volatility means technical_score/ml_score
# are meaningless noise for these, and they'll never trade a real "GREEN" move.
# Not auto-detected (unlike bStocks) since there's no reliable API signal for
# "this coin is a stablecoin" and the actual set changes rarely.
STABLECOIN_SYMBOLS = {
    "USDC", "USD1", "RLUSD", "FDUSD", "TUSD", "USDP", "DAI", "BUSD",
    "PYUSD", "GUSD", "USDE", "FRAX",
}

# Commodity-pegged tokens: price tracks gold (or another commodity), not
# crypto market structure -- same reasoning as stablecoins, technical_score/
# ml_score are meaningless noise for these.
COMMODITY_PEGGED_SYMBOLS = {"XAUT", "PAXG"}

# Binance also lists "bStocks"/"xStocks" -- tokenized equities and ETFs (e.g.
# SOXLB -> SOXL, AAPLB -> AAPL) that trade as USDT pairs but aren't crypto.
# CoinGecko tags these with a "bstocks-tokenized-*" / "xstocks" coin id, which
# is the only reliable signal available -- Binance's own exchangeInfo doesn't
# distinguish them from real crypto at all.
_non_crypto_symbols_cache = None


# Retries transient network errors before giving up -- added 2026-11-13,
# this function was the one real gap found while chasing a quick-scalp-
# alert slowdown: history.py's _get() got the same retry fix 2026-11-12,
# this one (used only by get_universe(), called once per scan cycle) was
# missed. A single unretried blip here used to fail the whole cycle
# immediately with no recovery attempt.
_GET_RETRIES = 3
_GET_RETRY_BACKOFF_SECONDS = 0.5


def _get(path, base=BINANCE_API):
    url = f"{base}{path}"
    last_error = None
    for attempt in range(_GET_RETRIES):
        try:
            resp = SESSION.get(url, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.RequestException, TimeoutError, ConnectionError) as e:
            last_error = e
            if attempt < _GET_RETRIES - 1:
                time.sleep(_GET_RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise last_error


def get_non_crypto_symbols():
    """Base-asset symbols (upper-cased) that are tokenized stocks/ETFs, not crypto."""
    global _non_crypto_symbols_cache
    if _non_crypto_symbols_cache is not None:
        return _non_crypto_symbols_cache
    try:
        coins = _get("/api/v3/coins/list", base=COINGECKO_API)
        # CoinGecko ids use the singular "xstock"/"bstock" root (e.g.
        # "adobe-xstock", "circle-internet-group-bstock") -- matching the
        # plural "xstocks"/"bstocks-tokenized" only caught 101 of ~1,020
        # tokenized-stock ids, letting the rest (including most of the
        # ~915 xStock equities) into the "crypto only" universe.
        _non_crypto_symbols_cache = {
            c["symbol"].upper() for c in coins
            if "bstock" in c["id"] or "xstock" in c["id"]
        }
    except Exception:
        # CoinGecko hiccup shouldn't block the whole universe -- fall back to
        # including everything rather than crashing.
        _non_crypto_symbols_cache = set()
    return _non_crypto_symbols_cache


# exchangeInfo's TRADING/USDT-pair listing changes rarely (new listing/
# delisting), so a long TTL is safe. ticker/24hr (volume) actually moves,
# but only needs to be fresh within a poll cycle -- see get_universe()'s
# TTL below for why both are cached at all.
_USDT_PAIRS_CACHE = {"pairs": None, "fetched_at": 0.0}
USDT_PAIRS_TTL_SECONDS = 600

# quick_scalp_signal.py's _resolve_symbols() calls get_universe() up to 4x
# per single ~60s poll cycle (fade scan, chase scan, PRO shadow scan,
# market-regime snapshot) -- added 2026-11-17 after finding this was
# refetching + re-parsing the full exchangeInfo/ticker-24hr payloads 4
# times every minute with zero caching, a major real CPU/network cost
# (see http_pool.py's docstring for the related investigation). A TTL
# just under the usual poll interval means one real fetch per cycle,
# while still refreshing volume data every cycle rather than going stale.
_UNIVERSE_CACHE = {}  # min_quote_volume -> {"universe": [...], "fetched_at": ts}
UNIVERSE_TTL_SECONDS = 45


def get_usdt_pairs():
    """All Binance USDT spot pairs that are actively trading, excluding tokenized
    stocks/ETFs (bStocks/xStocks), USD-pegged stablecoins, and commodity-pegged
    tokens -- this project only trades crypto with real price movement."""
    now = time.time()
    if _USDT_PAIRS_CACHE["pairs"] is not None and now - _USDT_PAIRS_CACHE["fetched_at"] < USDT_PAIRS_TTL_SECONDS:
        return _USDT_PAIRS_CACHE["pairs"]

    info = _get("/api/v3/exchangeInfo")
    non_crypto = get_non_crypto_symbols()
    pairs = {
        s["symbol"]
        for s in info["symbols"]
        if s["quoteAsset"] == "USDT"
        and s["status"] == "TRADING"
        and s.get("isSpotTradingAllowed", False)
        and s["baseAsset"].upper() not in non_crypto
        and s["baseAsset"].upper() not in STABLECOIN_SYMBOLS
        and s["baseAsset"].upper() not in COMMODITY_PEGGED_SYMBOLS
    }
    _USDT_PAIRS_CACHE["pairs"] = pairs
    _USDT_PAIRS_CACHE["fetched_at"] = now
    return pairs


def get_universe(min_quote_volume=MIN_QUOTE_VOLUME_USDT):
    """USDT pairs with 24h quote volume >= min_quote_volume, sorted by volume desc."""
    now = time.time()
    cached = _UNIVERSE_CACHE.get(min_quote_volume)
    if cached is not None and now - cached["fetched_at"] < UNIVERSE_TTL_SECONDS:
        return cached["universe"]

    pairs = get_usdt_pairs()
    tickers = _get("/api/v3/ticker/24hr")

    universe = []
    for t in tickers:
        symbol = t["symbol"]
        if symbol not in pairs:
            continue
        quote_volume = float(t["quoteVolume"])
        if quote_volume >= min_quote_volume:
            universe.append({"symbol": symbol, "quote_volume_usdt": quote_volume})

    universe.sort(key=lambda x: x["quote_volume_usdt"], reverse=True)
    _UNIVERSE_CACHE[min_quote_volume] = {"universe": universe, "fetched_at": now}
    return universe


# Futures-only listings -- added 2026-11-22 after EVAAUSDT (-21% in 24h on
# $341M volume, a real liquid move) turned out to be invisible to every
# alert (big_mover_alert.py, market_regime_alert.py, quick_scalp_alert.py)
# because it's listed on Binance Futures but was never listed on Spot at
# all, and every alert's tracked universe up to this point came exclusively
# from get_usdt_pairs()/get_universe() (spot). Kept SEPARATE from
# get_universe() itself rather than merged into it -- history.py/
# technical.py use get_universe() to download candles from the SPOT klines
# endpoint for coin_finder's CSV pipeline, which doesn't have these symbols
# at all and would just error; only scalp_pattern_signal.py's
# _resolve_symbols() (the shared symbol source for every alert, whose
# klines already come from futures regardless) pulls this in.
FUTURES_API = "https://fapi.binance.com"

_FUTURES_ONLY_PAIRS_CACHE = {"pairs": None, "fetched_at": 0.0}
FUTURES_ONLY_PAIRS_TTL_SECONDS = 600


def get_futures_only_usdt_pairs():
    """USDT-M perpetual symbols NOT listed on Spot at all -- same
    stablecoin/commodity/non-crypto exclusions as get_usdt_pairs()."""
    now = time.time()
    if (_FUTURES_ONLY_PAIRS_CACHE["pairs"] is not None
            and now - _FUTURES_ONLY_PAIRS_CACHE["fetched_at"] < FUTURES_ONLY_PAIRS_TTL_SECONDS):
        return _FUTURES_ONLY_PAIRS_CACHE["pairs"]

    info = _get("/fapi/v1/exchangeInfo", base=FUTURES_API)
    spot_pairs = get_usdt_pairs()
    non_crypto = get_non_crypto_symbols()
    pairs = {
        s["symbol"]
        for s in info["symbols"]
        if s["quoteAsset"] == "USDT"
        and s["status"] == "TRADING"
        and s.get("contractType") == "PERPETUAL"
        and s["symbol"] not in spot_pairs
        and s["baseAsset"].upper() not in non_crypto
        and s["baseAsset"].upper() not in STABLECOIN_SYMBOLS
        and s["baseAsset"].upper() not in COMMODITY_PEGGED_SYMBOLS
    }
    _FUTURES_ONLY_PAIRS_CACHE["pairs"] = pairs
    _FUTURES_ONLY_PAIRS_CACHE["fetched_at"] = now
    return pairs


_FUTURES_ONLY_UNIVERSE_CACHE = {}
FUTURES_ONLY_UNIVERSE_TTL_SECONDS = 45


def get_futures_only_universe(min_quote_volume=MIN_QUOTE_VOLUME_USDT):
    """Same shape as get_universe() (sorted list of {"symbol",
    "quote_volume_usdt"}), but for the futures-only symbols from
    get_futures_only_usdt_pairs() -- see the note above this section for
    why it's kept separate rather than merged into get_universe() itself."""
    now = time.time()
    cached = _FUTURES_ONLY_UNIVERSE_CACHE.get(min_quote_volume)
    if cached is not None and now - cached["fetched_at"] < FUTURES_ONLY_UNIVERSE_TTL_SECONDS:
        return cached["universe"]

    pairs = get_futures_only_usdt_pairs()
    tickers = _get("/fapi/v1/ticker/24hr", base=FUTURES_API)

    universe = []
    for t in tickers:
        symbol = t["symbol"]
        if symbol not in pairs:
            continue
        quote_volume = float(t["quoteVolume"])
        if quote_volume >= min_quote_volume:
            universe.append({"symbol": symbol, "quote_volume_usdt": quote_volume})

    universe.sort(key=lambda x: x["quote_volume_usdt"], reverse=True)
    _FUTURES_ONLY_UNIVERSE_CACHE[min_quote_volume] = {"universe": universe, "fetched_at": now}
    return universe


# Binance Alpha-tagged futures listings -- added 2026-11-24 at the user's
# request to find volatile Binance Alpha coins specifically, restricted to
# ones actually tradeable on futures. Binance's own futures exchangeInfo
# tags these explicitly (symbol["underlyingSubType"] contains "Alpha") --
# confirmed against known volatile names already tracked this session
# (AIOT, BULLA, IDOL, EVAA all carry it; PROM notably does NOT despite
# similar behavior, so this is Binance's own curation, not a volatility
# measure on its own). Found a real gap checking this: 29 liquid (>=$5M
# volume) Alpha-tagged coins were completely missing from the shared
# top-50 tracked universe, crowded out by majors (BTC/ETH/BNB/SOL) that
# have huge volume but far less volatility -- several already mid-move
# (AIN -12.3%, TRADOOR -10.9%, LIGHT +11.1%) and invisible to every alert.
# Kept SEPARATE from both get_universe() and get_futures_only_universe()
# rather than merged in -- the fix for this is a dedicated scanner
# (alpha_mover_alert.py) that doesn't touch the shared symbol pool at all,
# so it adds zero load to the existing live-traded fade engine/alerts.
_ALPHA_FUTURES_PAIRS_CACHE = {"pairs": None, "fetched_at": 0.0}
ALPHA_FUTURES_PAIRS_TTL_SECONDS = 600


def get_alpha_futures_pairs():
    """USDT-M perpetual symbols tagged "Alpha" by Binance itself."""
    now = time.time()
    if (_ALPHA_FUTURES_PAIRS_CACHE["pairs"] is not None
            and now - _ALPHA_FUTURES_PAIRS_CACHE["fetched_at"] < ALPHA_FUTURES_PAIRS_TTL_SECONDS):
        return _ALPHA_FUTURES_PAIRS_CACHE["pairs"]

    info = _get("/fapi/v1/exchangeInfo", base=FUTURES_API)
    pairs = {
        s["symbol"]
        for s in info["symbols"]
        if s["quoteAsset"] == "USDT"
        and s["status"] == "TRADING"
        and s.get("contractType") == "PERPETUAL"
        and "Alpha" in (s.get("underlyingSubType") or [])
    }
    _ALPHA_FUTURES_PAIRS_CACHE["pairs"] = pairs
    _ALPHA_FUTURES_PAIRS_CACHE["fetched_at"] = now
    return pairs


_ALPHA_FUTURES_UNIVERSE_CACHE = {}
ALPHA_FUTURES_UNIVERSE_TTL_SECONDS = 45


def get_alpha_futures_universe(min_quote_volume=MIN_QUOTE_VOLUME_USDT):
    """Same shape as get_universe(), but for Binance Alpha-tagged futures
    symbols from get_alpha_futures_pairs() -- see the note above for why
    this is its own dedicated scanner's universe, not merged anywhere."""
    now = time.time()
    cached = _ALPHA_FUTURES_UNIVERSE_CACHE.get(min_quote_volume)
    if cached is not None and now - cached["fetched_at"] < ALPHA_FUTURES_UNIVERSE_TTL_SECONDS:
        return cached["universe"]

    pairs = get_alpha_futures_pairs()
    tickers = _get("/fapi/v1/ticker/24hr", base=FUTURES_API)

    universe = []
    for t in tickers:
        symbol = t["symbol"]
        if symbol not in pairs:
            continue
        quote_volume = float(t["quoteVolume"])
        if quote_volume >= min_quote_volume:
            universe.append({"symbol": symbol, "quote_volume_usdt": quote_volume})

    universe.sort(key=lambda x: x["quote_volume_usdt"], reverse=True)
    _ALPHA_FUTURES_UNIVERSE_CACHE[min_quote_volume] = {"universe": universe, "fetched_at": now}
    return universe


if __name__ == "__main__":
    universe = get_universe()
    for coin in universe:
        print(f"{coin['symbol']:<12} {coin['quote_volume_usdt']:>18,.0f} USDT")
    print(f"\n{len(universe)} pairs with 24h volume >= {MIN_QUOTE_VOLUME_USDT:,.0f} USDT")
