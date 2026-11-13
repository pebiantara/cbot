"""Binance USDT spot universe: pairs with enough 24h volume to trade without slippage."""

import json
import urllib.request

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


def _get(path, base=BINANCE_API):
    url = f"{base}{path}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read())


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


def get_usdt_pairs():
    """All Binance USDT spot pairs that are actively trading, excluding tokenized
    stocks/ETFs (bStocks/xStocks), USD-pegged stablecoins, and commodity-pegged
    tokens -- this project only trades crypto with real price movement."""
    info = _get("/api/v3/exchangeInfo")
    non_crypto = get_non_crypto_symbols()
    return {
        s["symbol"]
        for s in info["symbols"]
        if s["quoteAsset"] == "USDT"
        and s["status"] == "TRADING"
        and s.get("isSpotTradingAllowed", False)
        and s["baseAsset"].upper() not in non_crypto
        and s["baseAsset"].upper() not in STABLECOIN_SYMBOLS
        and s["baseAsset"].upper() not in COMMODITY_PEGGED_SYMBOLS
    }


def get_universe(min_quote_volume=MIN_QUOTE_VOLUME_USDT):
    """USDT pairs with 24h quote volume >= min_quote_volume, sorted by volume desc."""
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
    return universe


if __name__ == "__main__":
    universe = get_universe()
    for coin in universe:
        print(f"{coin['symbol']:<12} {coin['quote_volume_usdt']:>18,.0f} USDT")
    print(f"\n{len(universe)} pairs with 24h volume >= {MIN_QUOTE_VOLUME_USDT:,.0f} USDT")
