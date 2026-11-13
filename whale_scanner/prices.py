"""USD prices via Binance's public ticker endpoint (no API key needed)."""

import os
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "coin_finder"))
from token_map import to_binance_symbol  # noqa: E402

TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
CACHE_TTL = 30  # seconds

_cache = {}  # symbol -> (price, fetched_at)


def binance_price(symbol):
    """Latest USDT price for a Binance symbol (e.g. "ETHUSDT"), or None on failure."""
    cached = _cache.get(symbol)
    if cached and time.time() - cached[1] < CACHE_TTL:
        return cached[0]

    try:
        resp = requests.get(TICKER_URL, params={"symbol": symbol}, timeout=10)
        resp.raise_for_status()
        price = float(resp.json()["price"])
    except (requests.RequestException, KeyError, ValueError):
        return None

    _cache[symbol] = (price, time.time())
    return price


# CoinGecko fallback -- added 2026-11-13 after finding 79.6% of 31,602 logged
# whale events had no usd_value at all: token_map.csv only covers 10 major
# (chain, token) pairs (WETH/USDC/WBNB/WSOL per chain), so any transfer of
# anything else fell through to None/0, which meant instant_trigger.py's
# $500k alert threshold could never fire for most real on-chain whale moves.
# CoinGecko's free contract-address price endpoint covers far more tokens
# than a hand-maintained CSV ever could, no API key needed.
COINGECKO_PLATFORM_IDS = {
    "ethereum": "ethereum",
    "arbitrum": "arbitrum-one",
    "bsc": "binance-smart-chain",
    "base": "base",
    "solana": "solana",
}
COINGECKO_TOKEN_PRICE_URL = "https://api.coingecko.com/api/v3/simple/token_price/{platform}"
_coingecko_cache = {}  # (chain, token_address.lower()) -> (price, fetched_at)


def coingecko_token_price(chain, token_address):
    """USD price for one on-chain token via CoinGecko's contract-address
    lookup, or None on failure/unmapped chain/unlisted token. Cached same as
    binance_price -- CoinGecko's free tier rate limit is tight."""
    key = (chain, token_address.lower())
    cached = _coingecko_cache.get(key)
    if cached and time.time() - cached[1] < CACHE_TTL:
        return cached[0]

    platform = COINGECKO_PLATFORM_IDS.get(chain)
    if platform is None:
        return None
    try:
        resp = requests.get(
            COINGECKO_TOKEN_PRICE_URL.format(platform=platform),
            params={"contract_addresses": token_address, "vs_currencies": "usd"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        # CoinGecko echoes the response key back in whatever case the
        # request was sent with -- Solana addresses are case-sensitive
        # base58, so lowercasing this lookup (unlike the cache key above,
        # which is just for our own dedup) silently returned None for
        # every Solana token until caught by testing against a real one (BONK).
        entry = data.get(token_address) or next(
            (v for k, v in data.items() if k.lower() == token_address.lower()), None
        )
        price = entry.get("usd") if entry else None
    except (requests.RequestException, KeyError, ValueError, AttributeError):
        price = None

    _coingecko_cache[key] = (price, time.time())
    return price


def token_usd_value(chain, token_address, amount):
    """USD value of `amount` units of an on-chain token. Tries the Binance
    token_map first (cheap, no rate limit concern), falls back to CoinGecko
    for anything not in that short list. None only if both miss."""
    symbol = to_binance_symbol(chain, token_address)
    if symbol is not None:
        price = binance_price(symbol)
        if price is not None:
            return amount * price

    price = coingecko_token_price(chain, token_address)
    if price is None:
        return None
    return amount * price
