"""Crypto news alert -- watches CoinDesk + Cointelegraph's free public RSS
feeds (no API key, no paid plan needed) for headlines mentioning a tracked
coin, and sends a Telegram heads-up. Added 2026-11-17 after CryptoPanic
(originally planned for this) and every token-unlock-calendar provider
checked (DeFiLlama's emissions API, tokenomist.ai, CryptoRank) turned out
to now require a paid plan -- RSS straight from the outlets themselves is
free, unauthenticated, and arguably more "trusted" anyway since there's no
aggregator/scoring layer in between.

Matching is name-based (e.g. "Bitcoin", "Solana"), not bare-ticker-based --
a 2-4 letter symbol like SUI/OP/FET collides too often with ordinary
English words in free-text headlines to match reliably without a lot of
false positives. NEWS_NAME_MAP only covers common majors/large-caps; a
coin not in the map is silently never matched (no alert), rather than risk
noise from guessing at a name.

Every alert includes a "possible effect" read (added 2026-11-18 at the
user's request), combining two things for each matched coin:
  1. Headline tone -- a plain KEYWORD-COUNT heuristic (HEADLINE_BULLISH_WORDS
     vs HEADLINE_BEARISH_WORDS), explicitly NOT real sentiment analysis/NLP.
     This is deliberately crude; treat the tone label as a rough cue, not a
     verified read of what the article actually argues.
  2. The coin's current technical read -- same technical_score() the Green
     Coin pipeline uses (EMA/RSI/MACD/ATR), with combine.py's GREEN/RED
     cutoffs (0.6/0.4) mirrored here for a consistent bullish/neutral/bearish
     label. Technical-only (no ml_score/whale_score), so this is a narrower
     signal than combine.py's actual GREEN/YELLOW/RED.
The two are combined into one line: tone agreeing with the existing trend
reads as "could extend the move," tone disagreeing reads as "mixed signal,"
and ATR% is included so a move can be judged against the coin's own normal
volatility instead of in a vacuum. Still informational only, not a trade
signal -- same convention as market_regime_alert.py/btc_crash_alert.py.

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
from history import get_futures_klines
from http_pool import SESSION
from technical import technical_score
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

# Plain keyword-count heuristic for headline tone -- see module docstring's
# caveat: this is NOT sentiment analysis/NLP, just a rough directional cue.
HEADLINE_BEARISH_WORDS = [
    r"\bcrash(?:es|ed|ing)?\b", r"\bslump(?:s|ed|ing)?\b", r"\bdrop(?:s|ped|ping)?\b",
    r"\bplung(?:e|es|ed|ing)\b", r"\bsinks?\b", r"\bfalls?\b", r"\bfell\b",
    r"\bsell-?off\b", r"\boutflow(?:s)?\b", r"\bhack(?:s|ed|ing)?\b", r"\bexploit(?:s|ed)?\b",
    r"\blawsuit(?:s)?\b", r"\bbans?\b", r"\bbanned\b", r"\bfraud\b", r"\bwarns?\b", r"\bwarning\b",
    r"\bfear\b", r"\brisks?\b", r"\brisky\b", r"\bliquidat(?:ion|ed|es)\b", r"\bdump(?:s|ed|ing)?\b",
    r"\bbearish\b", r"\bdeclin(?:e|es|ed)\b", r"\bloss(?:es)?\b", r"\blosing\b", r"\bwritedown\b",
    r"\bprobe(?:s|d)?\b", r"\bseiz(?:e|es|ed|ure)\b", r"\bsuspend(?:s|ed|ing)?\b",
    r"\bbreach(?:ed|es)?\b", r"\bcollapse(?:s|d)?\b", r"\bslides?\b", r"\bselloff\b",
]
HEADLINE_BULLISH_WORDS = [
    r"\bsurge(?:s|d)?\b", r"\brall(?:y|ies|ied)\b", r"\bgains?\b", r"\bgained\b",
    r"\bjump(?:s|ed|ing)?\b", r"\bsoar(?:s|ed|ing)?\b", r"\bpartnerships?\b", r"\badoption\b",
    r"\bapprov(?:al|es|ed)\b", r"\binflow(?:s)?\b", r"\bbullish\b", r"\brecord high\b",
    r"\bbreakout\b", r"\bupgrad(?:e|es|ed)\b", r"\bbuybacks?\b", r"\boutperform(?:s|ed)?\b",
    r"\brebound(?:s|ed)?\b", r"\brecover(?:s|y|ed)?\b", r"\blistings?\b", r"\bexpansion\b",
    r"\bmilestone\b",
]
_BEARISH_RE = [re.compile(p, re.IGNORECASE) for p in HEADLINE_BEARISH_WORDS]
_BULLISH_RE = [re.compile(p, re.IGNORECASE) for p in HEADLINE_BULLISH_WORDS]

# Mirrors combine.py's GREEN_THRESHOLD/RED_THRESHOLD (not imported directly --
# combine.py pulls in outcomes.py's file I/O, more than this needs) so the
# bullish/neutral/bearish label here reads consistently with the rest of the
# project, even though this is technical_score only, not the full final_score.
TECH_BULLISH_MIN = 0.6
TECH_BEARISH_MAX = 0.4


def headline_tone(title, description):
    text = f"{title} {description}"
    bull_hits = sum(1 for p in _BULLISH_RE if p.search(text))
    bear_hits = sum(1 for p in _BEARISH_RE if p.search(text))
    if bull_hits > bear_hits:
        return "bullish"
    if bear_hits > bull_hits:
        return "bearish"
    return "neutral"


def technical_snapshot(symbol):
    """Price + trend label + RSI + ATR% for one coin, or None if there isn't
    enough candle history yet (e.g. a very new futures listing)."""
    try:
        history_by_tf = {
            "1h": get_futures_klines(symbol, "1h"),
            "4h": get_futures_klines(symbol, "4h"),
            "1d": get_futures_klines(symbol, "1d"),
        }
        score, indicators = technical_score(history_by_tf)
    except Exception as e:
        print(f"technical_snapshot failed for {symbol}: {e} -- continuing")
        return None
    if score is None:
        return None
    if score >= TECH_BULLISH_MIN:
        label = "bullish"
    elif score <= TECH_BEARISH_MAX:
        label = "bearish"
    else:
        label = "neutral"
    return {
        "price": indicators["close"],
        "label": label,
        "score": score,
        "rsi": indicators["rsi"],
        "atr_pct": indicators["atr_pct"],
    }


def possible_effect_line(symbol, tone, snapshot):
    """One line combining headline tone + the coin's existing technical read
    into a rough "possible effect" -- agreement reads as a continuation cue,
    disagreement reads as a mixed signal. ATR% is included so the reader can
    judge any move against the coin's own normal volatility."""
    if snapshot is None:
        return f"\U0001f4ca {symbol}: tone {tone} (no technical snapshot available -- insufficient candle history)"

    price_str = f"${snapshot['price']:,.4f}" if snapshot["price"] < 1 else f"${snapshot['price']:,.2f}"
    base = (f"\U0001f4ca {symbol}: {price_str} | trend {snapshot['label']} (tech {snapshot['score']:.2f}) "
            f"| RSI {snapshot['rsi']:.0f} | 1h ATR {snapshot['atr_pct']:.1f}%")

    if tone == "neutral":
        read = "headline tone neutral -- no strong directional cue from the text itself"
    elif tone == snapshot["label"]:
        read = f"headline tone {tone} agrees with the existing {snapshot['label']} trend -- could extend the move"
    elif snapshot["label"] == "neutral":
        read = f"headline tone {tone}, but {symbol}'s trend is currently neutral -- no strong confirmation either way"
    else:
        read = f"headline tone {tone} conflicts with the existing {snapshot['label']} trend -- mixed signal, watch for confirmation"

    return f"{base}\n\U0001f9ed {read} (heuristic, not verified sentiment)"


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

        tone = headline_tone(item["title"], item["description"])
        now = time.time()
        effect_lines = []
        for symbol in coins:
            snapshot = technical_snapshot(f"{symbol}USDT")
            effect_lines.append(possible_effect_line(symbol, tone, snapshot))

            price = snapshot["price"] if snapshot else fetch_live_futures_price(f"{symbol}USDT")
            if price is not None:
                record_news_signal(f"{symbol}USDT", price, item["title"], snapshot_time=now)

        message = (f"\U0001f4f0 News ({'/'.join(coins)}): {item['title']}\n{item['link']}\n\n"
                   + "\n\n".join(effect_lines))
        send_telegram_message(message, token, chat_id)
        print(f"News alert: {coins} -- {item['title']}")

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
