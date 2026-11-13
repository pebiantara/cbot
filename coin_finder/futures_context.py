"""Futures-specific market context: funding rate, open interest trend, and
top-trader long/short positioning.

These don't exist on spot -- they measure leveraged positioning/crowding,
which is a genuinely different kind of information than price action (EMA
trend, candlestick shape, volume). Used as extra confirmation layers on top
of candlestick_patterns.py in system 2 (futures_pattern_signal.py), same
"stack independent, cheap checks" approach as that module's own
volume/prior-trend gates, rather than folding into one weighted score.

Top-trader long/short ratio (added 2026-11-07) is a different kind of
signal from funding/OI: it's actual positioning among Binance's top 20% of
accounts by margin balance, not something derived from price/volume at
all -- a LONG thesis while the biggest accounts are net SHORT is a real
divergence, independent of anything candlestick/volume/OI-based already
agreeing.

Missing/unavailable data is treated as a pass (True), not a block -- same
convention as combine.py's optional gates (e.g. extension_above_vah_pct is
None -> doesn't block). A failed API call shouldn't silently kill a signal
that's otherwise fully confirmed.
"""

import json
import time
import urllib.request

from liquidation_store import net_liquidation_usd

FUTURES_API = "https://fapi.binance.com"

# ~0.05% per 8h funding interval -- above this on either side, one side of
# the market is notably crowded/leveraged, not just mildly biased. Binance's
# funding rate cap is +/-0.0375% in calm conditions on majors, so 0.05% is
# already an elevated reading, not a routine one.
FUNDING_CROWD_THRESHOLD = 0.0005

OI_LOOKBACK_PERIODS = 5
# % rise in open interest over the lookback that counts as "still rising" --
# i.e. fresh positions are still piling into the existing trend, which
# contradicts an "exhausted, about to reverse" thesis regardless of direction.
OI_RISE_BLOCK_PCT = 1.0


def _get(path):
    url = f"{FUTURES_API}{path}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def get_funding_rate(symbol):
    """Current funding rate as a fraction (e.g. 0.0001 = 0.01%). None on any
    failure -- callers must treat that as "no opinion", not a block."""
    try:
        return float(_get(f"/fapi/v1/premiumIndex?symbol={symbol}")["lastFundingRate"])
    except Exception:
        return None


def get_open_interest_rising(symbol, period="1h", lookback=OI_LOOKBACK_PERIODS):
    """True if open interest rose by >= OI_RISE_BLOCK_PCT over the lookback
    window, False if flat/falling, None if the data wasn't available."""
    try:
        hist = _get(f"/futures/data/openInterestHist?symbol={symbol}&period={period}&limit={lookback + 1}")
    except Exception:
        return None
    if len(hist) < lookback + 1:
        return None
    oi_then = float(hist[0]["sumOpenInterest"])
    oi_now = float(hist[-1]["sumOpenInterest"])
    if oi_then == 0:
        return None
    return (oi_now / oi_then - 1) * 100 >= OI_RISE_BLOCK_PCT


def funding_confirms_bullish(symbol):
    """False if funding is already crowded long -- contradicts a
    bounce-after-decline thesis (a true short squeeze needs crowded
    SHORTS, not longs already paying up to stay long)."""
    rate = get_funding_rate(symbol)
    if rate is None:
        return True
    return rate <= FUNDING_CROWD_THRESHOLD


def funding_confirms_bearish(symbol):
    """Mirror of funding_confirms_bullish for a drop-after-rally thesis."""
    rate = get_funding_rate(symbol)
    if rate is None:
        return True
    return rate >= -FUNDING_CROWD_THRESHOLD


def oi_confirms_reversal(symbol, period="1h"):
    """False if open interest is still meaningfully rising -- fresh money is
    still entering in the existing trend's direction, contradicting
    "exhausted, about to reverse" for either a LONG or SHORT call."""
    rising = get_open_interest_rising(symbol, period=period)
    if rising is None:
        return True
    return not rising


TOP_TRADER_RATIO_LOW = 0.8    # top traders notably net SHORT below this -- contradicts a LONG thesis
TOP_TRADER_RATIO_HIGH = 1.25  # reciprocal of 0.8 -- top traders notably net LONG above this -- contradicts a SHORT thesis


def get_top_trader_long_short_ratio(symbol, period="1h"):
    """Position-weighted long/short ratio among Binance's top-trader cohort
    (top 20% of accounts by margin balance) -- >1 = net long, <1 = net
    short, 1 = 50/50. Position-weighted (not account-count-weighted) so one
    large account's real size counts more than many small ones, closer to
    "smart money" positioning than a simple headcount. None on any
    failure."""
    try:
        hist = _get(f"/futures/data/topLongShortPositionRatio?symbol={symbol}&period={period}&limit=1")
    except Exception:
        return None
    if not hist:
        return None
    try:
        return float(hist[-1]["longShortRatio"])
    except (KeyError, ValueError, TypeError):
        return None


def top_traders_confirm_bullish(symbol, period="1h"):
    """False only if top traders are clearly net SHORT (below
    TOP_TRADER_RATIO_LOW) -- the biggest accounts leaning against a LONG
    thesis is a real divergence worth blocking on, not just a mild lean.
    None/unavailable data passes, same convention as the other gates here."""
    ratio = get_top_trader_long_short_ratio(symbol, period=period)
    if ratio is None:
        return True
    return ratio >= TOP_TRADER_RATIO_LOW


def top_traders_confirm_bearish(symbol, period="1h"):
    """Mirror of top_traders_confirm_bullish for a SHORT thesis."""
    ratio = get_top_trader_long_short_ratio(symbol, period=period)
    if ratio is None:
        return True
    return ratio <= TOP_TRADER_RATIO_HIGH


def oi_confirms_breakout(symbol, period="15m"):
    """Opposite expectation from oi_confirms_reversal: a genuine breakout
    should be backed by RISING open interest (fresh positions actually
    entering the new direction, not just a thin-liquidity spike or a short
    squeeze with no real follow-through). False if OI is flat/falling.
    None/unavailable data is treated as a pass, same convention as the
    other gates here."""
    rising = get_open_interest_rising(symbol, period=period)
    if rising is None:
        return True
    return rising


LIQUIDATION_LOOKBACK_SECONDS = 3600


def liquidation_context(symbol, is_long, lookback_seconds=LIQUIDATION_LOOKBACK_SECONDS):
    """(confirms, sell_usd, buy_usd) from futures_liquidation_listener.py's
    logged data -- sell_usd is $ of LONG liquidations (capitulation selling),
    buy_usd is $ of SHORT liquidations (short-covering) for this symbol in
    the lookback window. confirms=True when the liquidation flow matches
    this direction's thesis (longs flushed out before calling a bottom,
    shorts flushed out before calling a top); None if there's no
    liquidation data for this symbol in the window at all.

    Informational only for now -- NOT wired into scan_pattern_signals() as a
    gate. The listener only started running 2026-11-04; there's no history
    yet to check this is actually predictive before trusting it that way
    (same reasoning as combine.py's Nov-9 accuracy re-check)."""
    sell_usd, buy_usd = net_liquidation_usd(symbol, time.time() - lookback_seconds)
    if sell_usd == 0 and buy_usd == 0:
        return None, 0.0, 0.0
    confirms = (sell_usd > buy_usd) if is_long else (buy_usd > sell_usd)
    return confirms, sell_usd, buy_usd
