"""CoinGecko market cap / volume data, keyed by upper-cased base symbol."""

import json
import time
import urllib.request

COINGECKO_API = "https://api.coingecko.com"
MARKETS_PAGES = 4   # top 1000 by market cap -- generous margin over today's
                    # ~20-30 symbol universe ($20M+ 24h volume filter)
PER_PAGE = 250

_market_cap_cache = None


def _get(path, base=COINGECKO_API):
    with urllib.request.urlopen(f"{base}{path}", timeout=15) as resp:
        return json.loads(resp.read())


def get_market_cap_data():
    """{"BTC": {"market_cap":, "total_volume":, "market_cap_rank":}, ...}.
    Empty dict on any failure -- informational data shouldn't block technical.py."""
    global _market_cap_cache
    if _market_cap_cache is not None:
        return _market_cap_cache

    result = {}
    try:
        for page in range(1, MARKETS_PAGES + 1):
            coins = _get(
                "/api/v3/coins/markets"
                f"?vs_currency=usd&order=market_cap_desc&per_page={PER_PAGE}&page={page}"
            )
            if not coins:
                break  # ran out of pages before hitting MARKETS_PAGES
            for c in coins:
                symbol = c["symbol"].upper()
                # endpoint is already sorted market_cap_desc, so first-seen
                # wins on symbol collisions (e.g. a minor coin sharing a
                # ticker with a major one).
                if symbol in result:
                    continue
                result[symbol] = {
                    "market_cap": c.get("market_cap"),
                    "total_volume": c.get("total_volume"),
                    "market_cap_rank": c.get("market_cap_rank"),
                }
            if page < MARKETS_PAGES:
                time.sleep(1.5)  # stay well under CoinGecko's free-tier rate limit
    except Exception:
        _market_cap_cache = {}
        return _market_cap_cache

    _market_cap_cache = result
    return result
