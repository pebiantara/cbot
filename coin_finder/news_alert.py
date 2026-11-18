"""Crypto news alert -- watches CoinDesk + Cointelegraph's free public RSS
feeds (no API key, no paid plan needed) for headlines mentioning a tracked
coin, and sends a Telegram heads-up. Added 2026-11-17 after CryptoPanic
(originally planned for this) and every token-unlock-calendar provider
checked (DeFiLlama's emissions API, tokenomist.ai, CryptoRank) turned out
to now require a paid plan -- RSS straight from the outlets themselves is
free, unauthenticated, and arguably more "trusted" anyway since there's no
aggregator/scoring layer in between.

Headline-only, no sentiment scoring -- just "this named coin is in the
news right now," informational context same as
market_regime_alert.py/btc_crash_alert.py, not a trade signal.

Matching is name-based (e.g. "Bitcoin", "Solana"), not bare-ticker-based --
a 2-4 letter symbol like SUI/OP/FET collides too often with ordinary
English words in free-text headlines to match reliably without a lot of
false positives. NEWS_NAME_MAP only covers common majors/large-caps; a
coin not in the map is silently never matched (no alert), rather than risk
noise from guessing at a name.

Dedup: persists seen item guids to NEWS_STATE_JSON so a restart doesn't
re-alert the whole feed. The first poll after a fresh start seeds "seen"
from whatever's currently in the feed WITHOUT alerting (baseline) -- only
genuinely new items from then on trigger a message.

Set RUN_ONCE=1 to run a single poll and exit.
"""

import json
import os
import re
import time
import xml.etree.ElementTree as ET

from entry_price import fetch_live_futures_price
from http_pool import SESSION
from news_outcomes import record_signal as record_news_signal, resolve_matured as resolve_matured_news
from telegram_alert import load_env, send_telegram_message

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_JSON = os.path.join(HERE, "news_alert_state.json")

POLL_INTERVAL_SECONDS = int(os.environ.get("NEWS_POLL_SECONDS", 600))
MAX_SEEN_GUIDS = 1000  # trims state file growth -- feeds only ever show ~20-30 recent items anyway

FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
]

# symbol -> name keywords to match in a headline/description, case-insensitive,
# word-boundary where the keyword alone could be an ordinary English word
# (see module docstring for why this is name-based, not ticker-based).
NEWS_NAME_MAP = {
    "BTC": ["bitcoin"],
    "ETH": ["ethereum", r"\bether\b"],
    "SOL": ["solana"],
    "BNB": ["binance coin", r"\bbnb\b"],
    "XRP": [r"\bripple\b", r"\bxrp\b"],
    "DOGE": ["dogecoin"],
    "ADA": ["cardano"],
    # bare "avalanche" is a common idiom ("an avalanche of outflows") in exactly
    # this kind of financial writing -- narrowed to avoid that false-positive.
    "AVAX": ["avalanche network", r"\bavax\b"],
    "LINK": ["chainlink"],
    "LTC": ["litecoin"],
    "DOT": ["polkadot"],
    # bare "tron" with no boundary matched "strong" (s-TRON-g) in production --
    # see conversation this was fixed in. Word-boundary everything below that
    # could otherwise embed inside an unrelated common word.
    "TRX": [r"\btron\b"],
    "MATIC": [r"\bpolygon\b"],
    "TON": ["toncoin", r"\bton\b"],
    "SUI": [r"\bsui\b"],
    "NEAR": ["near protocol"],
    "APT": ["aptos"],
    "ARB": ["arbitrum"],
    # bare "optimism" is an ordinary English word used constantly in financial
    # writing ("investor optimism") -- narrowed to specific project phrases.
    "OP": ["optimism superchain", "optimism collective", r"\bop mainnet\b"],
    "ATOM": ["cosmos"],
    "UNI": ["uniswap"],
    "FIL": ["filecoin"],
    "ICP": ["internet computer"],
    "ETC": ["ethereum classic"],
    "BCH": ["bitcoin cash"],
    "SHIB": ["shiba inu"],
    "PEPE": [r"\bpepe\b"],
    "HYPE": ["hyperliquid"],
    "WLD": ["worldcoin"],
    # bare "stellar" is a near-universal adjective in financial reporting
    # ("stellar quarter", "stellar guidance") -- narrowed to avoid that.
    "XLM": ["stellar lumens", r"\bxlm\b"],
    "XMR": ["monero"],
    "AAVE": [r"\baave\b"],
    "MKR": ["makerdao", r"\bmkr\b"],
    "ALGO": ["algorand"],
    "VET": ["vechain"],
    "SAND": ["the sandbox"],
    "MANA": ["decentraland"],
    "FTM": ["fantom"],
    "INJ": ["injective"],
    "SEI": [r"\bsei\b"],
    "TIA": ["celestia"],
    "RUNE": ["thorchain"],
    "KAS": ["kaspa"],
    "PYTH": ["pyth network"],
    "JUP": [r"\bjupiter\b"],
    "WIF": ["dogwifhat"],
    "BONK": [r"\bbonk\b"],
    "FLOKI": ["floki"],
}

_COMPILED = {
    symbol: [re.compile(kw, re.IGNORECASE) for kw in keywords]
    for symbol, keywords in NEWS_NAME_MAP.items()
}


def load_state():
    if not os.path.exists(STATE_JSON):
        return {"seen": []}
    with open(STATE_JSON) as f:
        return json.load(f)


def save_state(state):
    state["seen"] = state["seen"][-MAX_SEEN_GUIDS:]
    with open(STATE_JSON, "w") as f:
        json.dump(state, f)


def fetch_feed(url):
    """[{"guid", "title", "description", "link"}, ...] for one feed, or []
    on any fetch/parse failure -- one feed being down shouldn't block the
    other."""
    try:
        resp = SESSION.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        print(f"Feed fetch failed ({url}): {e} -- continuing")
        return []

    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        description = (item.findtext("description") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = item.findtext("guid") or link or title
        if guid and title:
            items.append({"guid": guid, "title": title, "description": description, "link": link})
    return items


def matched_coins(title, description):
    text = f"{title} {description}"
    return [symbol for symbol, patterns in _COMPILED.items() if any(p.search(text) for p in patterns)]


def poll_once(state):
    env = load_env()
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("No TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID configured -- skipping")
        return

    seen = set(state.get("seen", []))
    is_first_run = not seen  # don't alert on the whole feed the very first time we ever run

    all_items = []
    for feed_url in FEEDS:
        all_items.extend(fetch_feed(feed_url))

    new_items = [item for item in all_items if item["guid"] not in seen]
    if not new_items:
        return

    for item in new_items:
        state.setdefault("seen", []).append(item["guid"])
        if is_first_run:
            continue  # baseline only -- don't blast the whole feed on first start

        coins = matched_coins(item["title"], item["description"])
        if not coins:
            continue

        send_telegram_message(
            f"\U0001f4f0 News ({'/'.join(coins)}): {item['title']}\n{item['link']}",
            token, chat_id,
        )
        print(f"News alert: {coins} -- {item['title']}")

        now = time.time()
        for symbol in coins:
            price = fetch_live_futures_price(f"{symbol}USDT")
            if price is not None:
                record_news_signal(f"{symbol}USDT", price, item["title"], snapshot_time=now)

    save_state(state)

    try:
        resolve_matured_news()
    except Exception as e:
        print(f"news_outcomes resolve_matured failed: {e} -- continuing")


if __name__ == "__main__":
    state = load_state()
    poll_once(state)
    if os.environ.get("RUN_ONCE"):
        raise SystemExit(0)
    while True:
        time.sleep(POLL_INTERVAL_SECONDS)
        try:
            poll_once(state)
        except Exception as e:
            print(f"Poll failed: {e} -- continuing")
