"""Standalone futures signal ("system 2"): candlestick-pattern-only, no
trend/whale/ml at all -- deliberately separate from the main pipeline
(technical.py/score.py/combine.py -> webhook_server.py's /now_<x>x, "system
1"). Direction comes purely from the latest 1h, 4h, or 1d futures candle shape
(candlestick_patterns.py): a bullish reversal (engulfing/hammer) -> LONG, a
bearish reversal (engulfing/shooting star) -> SHORT.

This is for practicing futures trading off raw price action, independent of
and comparable against system 1 -- not a replacement for it. Patterns only
fire on specific candles, not continuously, so expect far fewer signals per
scan than /now_<x>x, and often none at all.

The leverage-derived SL/TP/liquidation/sizing math is identical to system 1
(leverage_risk.py) and intentionally reused as-is -- that math doesn't
depend on *why* a direction looks right, only on where price/EMAs currently
sit, so duplicating it here would just be two copies to keep in sync.
"""

import datetime
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "whale_scanner"))

from candlestick_patterns import MIN_CANDLES_NEEDED, bearish_reversal, bullish_reversal, reversal_lean
from entry_price import (
    fetch_live_futures_price,
    format_price,
    refresh_with_live_price,
    resistance_based_short,
    support_based_entry,
)
from futures_context import (
    funding_confirms_bearish,
    funding_confirms_bullish,
    liquidation_context,
    oi_confirms_reversal,
)
from history import get_futures_klines
from leverage_risk import LEVERAGE_WARN_THRESHOLD, MAX_PORTFOLIO_ALLOCATION_PCT, leveraged_entry_for_coin
from technical import ema
from universe import get_universe

SCAN_SLEEP_SECONDS = 0.05  # stay well under Binance rate limits across a full universe scan


def pattern_entry_for_symbol(symbol, is_long):
    """Same EMA20/50/200 support/resistance entry zone as leverage_risk.py's
    futures_entry_for_coin -- reused since the entry-zone math doesn't care
    why a direction looks right, only where price/EMAs currently sit."""
    try:
        candles = get_futures_klines(symbol, "1h")
    except Exception:
        return None
    closes = [c["close"] for c in candles]
    if len(closes) < 200:
        return None

    ema20, ema50, ema200 = ema(closes, 20)[-1], ema(closes, 50)[-1], ema(closes, 200)[-1]
    close = closes[-1]
    entry = (
        support_based_entry(close, ema20, ema50, ema200) if is_long
        else resistance_based_short(close, ema20, ema50, ema200)
    )
    if entry is None:
        return None

    live_price = fetch_live_futures_price(symbol)
    if live_price is not None:
        entry = refresh_with_live_price(entry, live_price, is_long)
    return entry


DEFAULT_TOP_N = 20  # scan the top N by 24h volume by default, not the whole universe -- ~27s for 20, still fast
# Always included regardless of where they rank by volume on a given day --
# pure volume ranking could occasionally bump one of these out of a top-10
# cutoff, but these are the ones actually meant to always be covered.
PRIORITY_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"]


def _resolve_symbols(top_n):
    """PRIORITY_SYMBOLS first, then filled up to `top_n` by 24h volume rank
    (None/0 = everything). Shared by scan_pattern_signals() and
    build_pattern_digest_message()'s fallback so both work off the exact
    same candidate list."""
    universe_symbols = [c["symbol"] for c in get_universe()]
    rest = [s for s in universe_symbols if s not in PRIORITY_SYMBOLS]
    if top_n:
        return PRIORITY_SYMBOLS + rest[:max(0, top_n - len(PRIORITY_SYMBOLS))]
    return PRIORITY_SYMBOLS + rest


def scan_pattern_signals(symbols=None, top_n=DEFAULT_TOP_N):
    """{symbol: (is_long, interval, close_price)} for every symbol whose
    latest 1h, 4h, or 1d futures candle shows a reversal pattern right now,
    confirmed by funding rate and
    open interest (see futures_context.py) on top of candlestick_patterns.py's
    own shape/volume/prior-trend checks -- a reversal candle backed by
    crowded opposite-side funding AND falling open interest (positions
    actually unwinding, not fresh ones piling in) is a stronger case than
    the candle shape alone. Also requires pattern_entry_for_symbol() to
    return an actual entry zone -- a newer futures listing can pass the
    pattern checks (only needs MIN_CANDLES_NEEDED, ~22 candles) while still
    lacking the 200 1h candles the entry zone's EMA200 needs, which used to
    let an alert fire for a symbol that then had no entry price to show.
    Reuses coin_finder/universe.py's already-vetted
    crypto-only spot universe (already sorted by 24h volume descending) as
    the candidate list by default, capped to the top `top_n` -- not every
    spot pair has a futures listing (symbols without one are silently
    skipped), and the full universe is more coins than matters for focused
    swing trading. PRIORITY_SYMBOLS are always included first, then the rest
    of `top_n` is filled by volume rank. Pass top_n=None (or explicit
    `symbols`) to scan everything."""
    if symbols is None:
        symbols = _resolve_symbols(top_n)

    signals = {}
    for symbol in symbols:
        for interval in ("1h", "4h", "1d"):
            try:
                candles = get_futures_klines(symbol, interval, limit=MIN_CANDLES_NEEDED)
            except Exception:
                continue
            if (bullish_reversal(candles) and funding_confirms_bullish(symbol)
                    and oi_confirms_reversal(symbol, interval) and pattern_entry_for_symbol(symbol, True) is not None):
                signals[symbol] = (True, interval, candles[-1]["close"])
                break
            if (bearish_reversal(candles) and funding_confirms_bearish(symbol)
                    and oi_confirms_reversal(symbol, interval) and pattern_entry_for_symbol(symbol, False) is not None):
                signals[symbol] = (False, interval, candles[-1]["close"])
                break
        time.sleep(SCAN_SLEEP_SECONDS)
    return signals


def best_lean_for_symbol(symbol):
    """Always returns a candidate direction for this symbol -- the
    strongest available read across 1h/4h/1d, even when nothing fully
    confirms. Three tiers, falling back only as far as needed:
      1. Full confirmation (shape + volume + trend + funding + OI + a
         computable entry zone, 6/6) -- same bar as scan_pattern_signals()'s
         actual alert/trade signal. The entry-zone check exists so
         "confirmed" can never contradict _append_trade_block coming back
         with "no entry zone available" right below it (a newer futures
         listing can have enough candles for the pattern check but not
         enough for the entry zone's 200-candle EMA).
      2. Partial pattern match -- a real candlestick shape (hammer/
         engulfing/shooting star) is present, but one or more of
         volume/trend/funding/OI/entry didn't confirm it. Still
         pattern-based, just labeled with exactly what's missing.
      3. No candlestick shape at all on either timeframe -- falls back to
         the plain latest-candle direction (bullish close vs bearish
         close), explicitly labeled as "no pattern" rather than silently
         dressed up as one. This is the only tier that isn't really
         candlestick analysis -- it exists so a single-symbol check always
         has something to show, not to pretend there's a signal.
    Returns a dict (never None): symbol, is_long, interval, shape (or
    None), checks (dict of bool), score (0-6), confirmed (score==6),
    close_price."""
    best = None
    for interval in ("1h", "4h", "1d"):
        try:
            candles = get_futures_klines(symbol, interval, limit=MIN_CANDLES_NEEDED)
        except Exception:
            continue
        if len(candles) < 2:
            continue

        lean = reversal_lean(candles)
        cur = candles[-1]
        if lean is not None:
            is_long = lean["direction"] == "bullish"
            funding_ok = funding_confirms_bullish(symbol) if is_long else funding_confirms_bearish(symbol)
            oi_ok = oi_confirms_reversal(symbol, interval)
            # a symbol can have enough candles for the pattern check
            # (MIN_CANDLES_NEEDED, ~22) but not enough for the entry zone's
            # 200-candle EMA (a newer futures listing) -- without this check
            # "confirmed" could say yes while the entry price comes back
            # None right below it, a contradictory-looking result.
            entry_ok = pattern_entry_for_symbol(symbol, is_long) is not None
            checks = {
                "shape": True, "volume": lean["volume_confirms"],
                "trend": lean["trend_confirms"], "funding": funding_ok, "oi": oi_ok, "entry": entry_ok,
            }
            shape = lean["shape"]
        else:
            is_long = cur["close"] > cur["open"]
            checks = {"shape": False, "volume": False, "trend": False, "funding": False, "oi": False, "entry": False}
            shape = None

        score = sum(checks.values())
        candidate = {
            "symbol": symbol, "is_long": is_long, "interval": interval, "shape": shape,
            "checks": checks, "score": score, "confirmed": score == len(checks),
            "close_price": cur["close"],
        }
        if best is None or candidate["score"] > best["score"]:
            best = candidate
    return best


def format_lean(lean):
    """Human-readable confidence label + check breakdown for a
    best_lean_for_symbol() result."""
    total = len(lean["checks"])
    if lean["confirmed"]:
        header = f"✅ Fully confirmed ({total}/{total} checks)"
    elif lean["shape"]:
        header = f"⚠️ Partial match ({lean['score']}/{total} checks) -- shape present, not fully confirmed"
    else:
        header = "ℹ️ No candlestick pattern on either timeframe -- plain last-candle direction only"
    checks_line = "  ".join(
        f"{name}{'✅' if passed else '❌'}" for name, passed in lean["checks"].items()
    )
    shape_line = f"Shape: {lean['shape']}" if lean["shape"] else "Shape: none"
    return header, shape_line, checks_line


def _append_trade_block(lines, symbol, is_long, leverage, account_equity, risk_pct, header_lines):
    """Shared entry/TP/SL/liquidation/sizing rendering -- used for both a
    fully-confirmed signal and a fallback lean, since this math doesn't
    depend on *why* the direction looks right (see futures_entry_for_coin's
    own docstring for the same point re: spot vs futures). `header_lines`
    is whatever's specific to this call (confidence label, shape, checks,
    liquidation context) printed right after the direction/price line."""
    entry = pattern_entry_for_symbol(symbol, is_long)
    if entry is None:
        try:
            have_enough_history = len(get_futures_klines(symbol, "1h")) >= 200
        except Exception:
            have_enough_history = False
        if have_enough_history:
            lines.append(
                f"\n{symbol}: no valid {'support' if is_long else 'resistance'} level for a "
                f"{'LONG' if is_long else 'SHORT'} entry right now (price already past every 1h EMA)"
            )
        else:
            lines.append(f"\n{symbol}: no futures entry zone available (not enough 1h candle history yet)")
        return
    lev = leveraged_entry_for_coin(
        entry, is_long, leverage, symbol=symbol,
        account_equity=account_equity, risk_pct=risk_pct,
    )
    emoji = "\U0001f7e2" if is_long else "\U0001f534"
    direction = "LONG" if is_long else "SHORT"
    lines.append(f"\n{emoji} {direction} {symbol}  (price now: {format_price(lev['current_price'])})")
    lines.extend(header_lines)

    if lev["at_entry_now"]:
        lines.append(f"Entry: {format_price(lev['entry_low'])} - {format_price(lev['entry_high'])}  (at entry zone now)")
        lines.append(
            f"TP {format_price(lev['take_profit'])} ({lev['tp_distance_pct']:.3%})  "
            f"SL {format_price(lev['stop_loss'])} ({lev['sl_distance_pct']:.3%})"
        )
        lines.append(
            f"Est. liquidation: {format_price(lev['liquidation_price'])} "
            f"({lev['liquidation_distance_pct']:.3%} away, MMR {lev['maintenance_margin_rate']:.2%})"
        )
    else:
        lines.append(
            f"Pullback entry: {format_price(lev['entry_low'])} - {format_price(lev['entry_high'])} "
            f"({lev['distance_from_entry_pct']:+.2f}% away, may not get hit)"
        )
        lines.append(f"If acting now at {format_price(lev['current_price'])} instead:")
        lines.append(
            f"  -> TP {format_price(lev['if_buy_now_take_profit'])} ({lev['tp_distance_pct']:.3%})  "
            f"SL {format_price(lev['if_buy_now_stop_loss'])} ({lev['sl_distance_pct']:.3%})  "
            f"Liquidation {format_price(lev['if_buy_now_liquidation_price'])} ({lev['liquidation_distance_pct']:.3%})"
        )
    if "sizing" in lev:
        s = lev["sizing"]
        warn = "  ⚠️ exceeds account equity" if s["insufficient_margin"] else ""
        lines.append(
            f"Size: {s['position_size']:.6g} ({format_price(s['position_notional_usd'])} notional)  "
            f"Margin needed: ${s['margin_required_usd']:,.2f}{warn}"
        )
        if s["exceeds_portfolio_cap"]:
            lines.append(
                f"⚠️ Would push committed margin to ${s['committed_margin_usd'] + s['margin_required_usd']:,.2f} "
                f"-- exceeds the {MAX_PORTFOLIO_ALLOCATION_PCT:.0%} portfolio cap "
                f"(${s['available_margin_usd']:,.2f} available). Run /positions to review what's open."
            )


def _liquidation_lines(symbol, is_long):
    # Informational only -- not a gate, see futures_context.py's
    # liquidation_context() docstring for why (no history to validate
    # against until the listener's run for a while).
    confirms, sell_usd, buy_usd = liquidation_context(symbol, is_long)
    if confirms is True:
        return [f"⚡ Recent liquidations support this (longs ${sell_usd:,.0f} vs shorts ${buy_usd:,.0f})"]
    if confirms is False:
        return [f"⚡ Recent liquidations lean the other way (longs ${sell_usd:,.0f} vs shorts ${buy_usd:,.0f})"]
    return []


def build_pattern_digest_message(leverage, account_equity=None, risk_pct=0.01, symbols=None, top_n=DEFAULT_TOP_N):
    """Same leverage-digest format as leverage_risk.build_leverage_digest_message,
    but direction comes from scan_pattern_signals() instead of green_list.json's
    trend/whale/ml label -- an independent second read, not a replacement.

    When nothing fully confirms, falls back to best_lean_for_symbol() so the
    command always shows something instead of going silent -- for a
    single-symbol check, that one symbol's best available read; for a full
    scan, just the single closest-to-confirmed candidate across the
    universe (not all of them, to avoid 20 lines of noise)."""
    signals = scan_pattern_signals(symbols, top_n=top_n)

    lines = [
        f"\U0001f56f️ Pattern-only {leverage}x futures view "
        f"({datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d %H:%M} UTC)",
        "⚠️ Direction here is candlestick pattern (engulfing/hammer/shooting star "
        "on futures 1h/4h/1d), confirmed by volume, prior trend, funding rate, and "
        "open interest -- no trend/whale/ml at all. Separate, independent system "
        "from /now_<x>x, for comparison/practice.",
    ]
    if leverage > LEVERAGE_WARN_THRESHOLD:
        lines.append(
            f"⚠️ {leverage}x -- verify the real maintenance-margin bracket for your "
            "symbol/size on Binance before trading."
        )

    if signals:
        for symbol, (is_long, _interval, _signal_close) in sorted(signals.items()):
            header = ["✅ Fully confirmed (6/6 checks)"] + _liquidation_lines(symbol, is_long)
            _append_trade_block(lines, symbol, is_long, leverage, account_equity, risk_pct, header)
        return "\n".join(lines)

    resolved_symbols = symbols if symbols is not None else _resolve_symbols(top_n)
    leans = [l for l in (best_lean_for_symbol(s) for s in resolved_symbols) if l is not None]
    if not leans:
        lines.append("\nNo data available for any tracked symbol right now.")
        return "\n".join(lines)

    leans.sort(key=lambda l: l["score"], reverse=True)
    show = leans if len(resolved_symbols) == 1 else leans[:1]
    lines.append(f"\nNothing fully confirmed -- closest available read{'s' if len(show) > 1 else ''}:")
    for lean in show:
        header_text, shape_line, checks_line = format_lean(lean)
        header = [header_text, shape_line, checks_line] + _liquidation_lines(lean["symbol"], lean["is_long"])
        _append_trade_block(lines, lean["symbol"], lean["is_long"], leverage, account_equity, risk_pct, header)
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Standalone candlestick-pattern-only futures signal scan")
    parser.add_argument("--leverage", type=int, default=20)
    parser.add_argument("--equity", type=float, help="account equity in USD")
    parser.add_argument("--risk-pct", type=float, default=1.0, help="%% of account to risk per trade")
    parser.add_argument("--symbol", help="check a single symbol instead of scanning the top-N universe")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N, help="scan the top N symbols by 24h volume (0 = everything)")
    args = parser.parse_args()

    symbols = [args.symbol] if args.symbol else None
    print(build_pattern_digest_message(
        args.leverage, account_equity=args.equity, risk_pct=args.risk_pct / 100,
        symbols=symbols, top_n=(args.top_n or None),
    ))
