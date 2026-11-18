"""Support-based entry zone: where to actually buy, not just a 0-1 score.

Uses the nearest EMA (20/50/200) at or below the current close as "support" --
the classic idea that a pullback toward a moving average in an uptrend is a
better entry than chasing the current price. Stop-loss/take-profit percentages
match MyStrategy.py's existing numbers (stoploss -3%, minimal_roi +4%) so this
stays consistent with what the bot itself would actually do.
"""

from http_pool import SESSION

STOP_LOSS_PCT = 0.03
TAKE_PROFIT_PCT = 0.04
ENTRY_ZONE_BUFFER_PCT = 0.01  # entry zone extends 1% above the support level


def fetch_live_price(symbol):
    """Binance's live ticker price -- fast, no auth needed. Used to refresh
    `entry["current_price"]`-dependent fields on demand (e.g. /now) without
    re-running the full hourly pipeline, which is too slow for a quick check
    and wouldn't change the EMA-based entry_low/entry_high meaningfully anyway."""
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        resp = SESSION.get(url, timeout=5)
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception:
        return None


def fetch_live_futures_price(symbol):
    """Binance Futures (USDT-M perpetual) ticker -- spot has no leverage, so a
    /now_<x>x position is always actually opened on futures. Futures usually
    tracks spot closely but can diverge during fast moves (liquidation
    cascades, funding-driven basis), so the leverage digest prices itself off
    this instead of spot. None if the symbol isn't listed on futures."""
    try:
        url = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}"
        resp = SESSION.get(url, timeout=5)
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception:
        return None


def fetch_live_futures_prices():
    """All Binance Futures (USDT-M) ticker prices in a single call -- symbol
    -> price. Same endpoint as fetch_live_futures_price() but without the
    `symbol=` filter, so checking many symbols costs one request instead of
    one per symbol. Used by scalp_early_warning.py's poll loop."""
    try:
        url = "https://fapi.binance.com/fapi/v1/ticker/price"
        resp = SESSION.get(url, timeout=5)
        resp.raise_for_status()
        rows = resp.json()
        return {row["symbol"]: float(row["price"]) for row in rows}
    except Exception:
        return {}


def refresh_with_live_price(entry, live_price, is_long):
    """New dict with current_price/distance_from_entry_pct/at_entry_now/
    if_buy_now_* recomputed against a fresh price, keeping the same (slow-
    moving, EMA-based) entry_low/entry_high/take_profit/stop_loss as-is."""
    refreshed = dict(entry)
    refreshed["current_price"] = live_price
    if is_long:
        refreshed["distance_from_entry_pct"] = round((live_price / entry["entry_low"] - 1) * 100, 2)
        refreshed["at_entry_now"] = live_price <= entry["entry_high"]
        refreshed["if_buy_now_stop_loss"] = live_price * (1 - STOP_LOSS_PCT)
        refreshed["if_buy_now_take_profit"] = live_price * (1 + TAKE_PROFIT_PCT)
    else:
        refreshed["distance_from_entry_pct"] = round((entry["entry_high"] / live_price - 1) * 100, 2)
        refreshed["at_entry_now"] = live_price >= entry["entry_low"]
        refreshed["if_buy_now_stop_loss"] = live_price * (1 + STOP_LOSS_PCT)
        refreshed["if_buy_now_take_profit"] = live_price * (1 - TAKE_PROFIT_PCT)
    return refreshed


def support_based_entry(close, ema20, ema50, ema200):
    """None if price is below every EMA (no support underneath -- bearish,
    matches combine.py's RED rule for `not above_ema200`)."""
    candidates = [e for e in (ema20, ema50, ema200) if e is not None and e <= close]
    if not candidates:
        return None

    support = max(candidates)
    entry_low = support
    entry_high = support * (1 + ENTRY_ZONE_BUFFER_PCT)
    distance_pct = (close / entry_low - 1) * 100

    return {
        "current_price": close,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop_loss": entry_low * (1 - STOP_LOSS_PCT),
        "take_profit": entry_low * (1 + TAKE_PROFIT_PCT),
        "distance_from_entry_pct": round(distance_pct, 2),
        "at_entry_now": close <= entry_high,
        # if the pullback-to-support zone never gets hit (common in a strong
        # trend) and you buy at the current price instead, here's where that
        # trade's TP/SL would actually land -- same % margins, different anchor.
        "if_buy_now_stop_loss": close * (1 - STOP_LOSS_PCT),
        "if_buy_now_take_profit": close * (1 + TAKE_PROFIT_PCT),
    }


def resistance_based_short(close, ema20, ema50, ema200):
    """Mirror of support_based_entry for the bearish case: nearest EMA at or
    above the current close is "resistance" -- a bounce up into it is a
    better short-information level than the current price. This project is
    spot-only (no leverage, see CLAUDE.md), so this never opens an actual
    short position -- it's informational price levels for a SHORT-bias coin
    (avoid holding / exit any existing position), not an executable order."""
    candidates = [e for e in (ema20, ema50, ema200) if e is not None and e >= close]
    if not candidates:
        return None

    resistance = min(candidates)
    entry_high = resistance
    entry_low = resistance * (1 - ENTRY_ZONE_BUFFER_PCT)
    distance_pct = (entry_high / close - 1) * 100

    return {
        "current_price": close,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop_loss": entry_high * (1 + STOP_LOSS_PCT),
        "take_profit": entry_high * (1 - TAKE_PROFIT_PCT),
        "distance_from_entry_pct": round(distance_pct, 2),
        "at_entry_now": close >= entry_low,
        "if_buy_now_stop_loss": close * (1 + STOP_LOSS_PCT),
        "if_buy_now_take_profit": close * (1 - TAKE_PROFIT_PCT),
    }


def format_price(x):
    """4+ significant figures regardless of magnitude (so sub-cent coins like
    PEPE don't get rounded to 0.00 the way a fixed 2-4 decimal format would)."""
    if x == 0:
        return "0"
    if abs(x) >= 1:
        return f"{x:,.4f}"
    formatted = f"{x:.10f}".rstrip("0")
    return formatted if not formatted.endswith(".") else formatted + "0"
