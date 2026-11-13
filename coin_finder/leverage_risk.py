"""Leverage-aware SL/TP/liquidation/position-size math, for exploring futures
leverage manually -- NOT wired into the trading bot, which stays spot-only by
default (see CLAUDE.md: "Never use leverage at the start").

entry_price.py's stop_loss/take_profit are a flat 3%/4% price move, which is
fine at 1x but meaningless at high leverage: at e.g. 100x, the position gets
force-liquidated around ~0.5-1% adverse move -- long before a 3% stop-loss
price is ever reached. This module derives SL from the liquidation distance
instead of a flat %, and sizes the position from account risk (not from
leverage) so the dollar risk per trade stays constant regardless of leverage.
"""

import os

from entry_price import (
    fetch_live_futures_price,
    format_price,
    refresh_with_live_price,
    resistance_based_short,
    support_based_entry,
)
from candlestick_patterns import MIN_CANDLES_NEEDED, bearish_reversal, bullish_reversal
from history import get_futures_klines
from portfolio_ledger import total_committed_margin
from technical import ema

# Binance Futures tier-1 (lowest notional / highest leverage tier) maintenance
# margin rates, from Binance's public margin/leverage tables. These DO change
# over time and vary by notional size -- treat as a rough default, not a
# substitute for checking the actual bracket for your symbol/position size in
# the Binance app before trading.
MAINTENANCE_MARGIN_RATE_BY_SYMBOL = {
    "BTCUSDT": 0.004,
    "ETHUSDT": 0.005,
}
DEFAULT_MAINTENANCE_MARGIN_RATE = 0.01  # conservative fallback for everything else
SL_BUFFER = 0.6          # keep the stop-loss at 60% of the distance to liquidation
REWARD_RISK_RATIO = 1.5  # take-profit distance = stop-loss distance * this

# This signal is swing-style by design: direction comes from 1h/4h/1d EMA200
# trend + whale flow deliberately smoothed to resist short-term noise (see
# combine.py/score.py), and it only refreshes hourly or on-demand -- it's
# slow on purpose. Above ~20x, the liquidation distance (1/leverage - mmr)
# drops under ~5%, a move easily covered within an hour, i.e. well inside
# the lag this signal is designed to ignore rather than react to. Below
# that, there's enough room for the signal's lag not to matter. This is a
# warning, not a hard cap -- nothing here executes a trade.
LEVERAGE_WARN_THRESHOLD = 20

# Cap on total committed margin (this trade + whatever's already open, per
# portfolio_ledger.py) as a fraction of account equity -- stacking several
# large leveraged trades was part of what the real trade history showed
# (see scalp_pattern_signal.py's module docstring). 80% leaves a cash
# reserve rather than letting every suggested size assume the full account
# is free, same idea as the portfolio-cap pattern in
# https://www.mindstudio.ai/blog/build-ai-trading-agent-claude-code-alpaca.
MAX_PORTFOLIO_ALLOCATION_PCT = 0.8


def maintenance_margin_rate_for(symbol):
    return MAINTENANCE_MARGIN_RATE_BY_SYMBOL.get(symbol, DEFAULT_MAINTENANCE_MARGIN_RATE)


def leveraged_levels(anchor_price, is_long, leverage, symbol=None, maintenance_margin_rate=None,
                      sl_buffer=SL_BUFFER, reward_risk_ratio=REWARD_RISK_RATIO):
    """SL/TP/liquidation distances derived from leverage + maintenance margin,
    not a fixed spot %. `anchor_price` is whatever price you'd actually enter
    at (e.g. entry_low for a long)."""
    mmr = maintenance_margin_rate if maintenance_margin_rate is not None else maintenance_margin_rate_for(symbol)
    liquidation_distance_pct = max(1 / leverage - mmr, 0.0005)
    sl_distance_pct = liquidation_distance_pct * sl_buffer
    tp_distance_pct = sl_distance_pct * reward_risk_ratio

    sign = 1 if is_long else -1
    return {
        "leverage": leverage,
        "maintenance_margin_rate": mmr,
        "liquidation_distance_pct": liquidation_distance_pct,
        "sl_distance_pct": sl_distance_pct,
        "tp_distance_pct": tp_distance_pct,
        "liquidation_price": anchor_price * (1 - sign * liquidation_distance_pct),
        "stop_loss": anchor_price * (1 - sign * sl_distance_pct),
        "take_profit": anchor_price * (1 + sign * tp_distance_pct),
    }


def position_sizing(entry_price, sl_distance_pct, leverage, account_equity, risk_pct, committed_margin_usd=0):
    """Position size such that hitting the SL loses exactly risk_pct of
    account_equity -- leverage only changes the margin tied up to hold that
    size, not the dollar risk itself (that's controlled by risk_pct).

    `committed_margin_usd` is whatever's already tied up in other open
    positions (see portfolio_ledger.py) -- defaults to 0 so callers that
    don't pass it (the CLI script, older call sites) behave exactly as
    before. When provided, this trade's margin is checked against
    MAX_PORTFOLIO_ALLOCATION_PCT of account_equity on top of what's already
    committed, not against the full account in isolation."""
    risk_amount = account_equity * risk_pct
    position_notional = risk_amount / sl_distance_pct
    margin_required = position_notional / leverage
    available_margin = account_equity * MAX_PORTFOLIO_ALLOCATION_PCT - committed_margin_usd
    return {
        "risk_amount_usd": risk_amount,
        "position_notional_usd": position_notional,
        "margin_required_usd": margin_required,
        "position_size": position_notional / entry_price,
        "insufficient_margin": margin_required > account_equity,
        "committed_margin_usd": committed_margin_usd,
        "available_margin_usd": available_margin,
        "exceeds_portfolio_cap": margin_required > max(available_margin, 0),
    }


def futures_entry_for_coin(symbol, is_long):
    """Recomputes the EMA20/50/200 support/resistance entry zone and current
    price from Binance FUTURES (perpetual) 1h candles instead of spot --
    spot has no leverage, so a /now_<x>x position is always actually opened
    on futures, and futures can diverge from spot during fast moves. Returns
    None (caller falls back to the spot-computed entry) if the symbol isn't
    listed on futures or futures data is otherwise unavailable."""
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


def futures_reversal_veto(symbol, is_long):
    """Checks the latest futures 1h and 4h candles for a reversal shape that
    contradicts this direction -- a bearish reversal candle (engulfing/
    shooting star) blocks a LONG, a bullish one (engulfing/hammer) blocks a
    SHORT. See candlestick_patterns.py for why this is a separate veto
    rather than folded into technical_score. Returns (vetoed, reason);
    (False, None) if futures data isn't available -- doesn't block on a
    missing check, same as the other optional gates in combine.py."""
    reversal_check = bearish_reversal if is_long else bullish_reversal
    for interval in ("1h", "4h"):
        try:
            candles = get_futures_klines(symbol, interval, limit=MIN_CANDLES_NEEDED)
        except Exception:
            continue
        if reversal_check(candles):
            shape = "bearish" if is_long else "bullish"
            return True, f"{shape} reversal candle on futures {interval}"
    return False, None


def leveraged_entry_for_coin(entry, is_long, leverage, symbol=None, account_equity=None, risk_pct=0.01,
                              sl_buffer=None, reward_risk_ratio=None):
    """Takes an existing spot `entry` dict (from entry_price.py, with
    entry_low/entry_high/current_price already computed) and returns a copy
    with leverage-adjusted stop_loss/take_profit/liquidation_price, using the
    same entry zone but leverage math instead of the flat spot 3%/4%.

    sl_buffer/reward_risk_ratio: None (default) uses this module's own
    SL_BUFFER/REWARD_RISK_RATIO, unchanged -- system 1 (/now_<x>x) and
    system 2 (/futures_<x>) both call this without passing these, so their
    behavior is identical to before. A caller that wants a different
    stop/target profile (e.g. system 3's tighter, faster-resolving trades)
    passes its own values here instead of duplicating leveraged_levels."""
    anchor_price = entry["entry_low"] if is_long else entry["entry_high"]
    levels = leveraged_levels(anchor_price, is_long, leverage, symbol=symbol,
                               sl_buffer=sl_buffer or SL_BUFFER, reward_risk_ratio=reward_risk_ratio or REWARD_RISK_RATIO)
    live_levels = leveraged_levels(entry["current_price"], is_long, leverage, symbol=symbol,
                                    sl_buffer=sl_buffer or SL_BUFFER, reward_risk_ratio=reward_risk_ratio or REWARD_RISK_RATIO)

    result = dict(entry)
    result.update({
        "leverage": leverage,
        "maintenance_margin_rate": levels["maintenance_margin_rate"],
        "liquidation_distance_pct": levels["liquidation_distance_pct"],
        "sl_distance_pct": levels["sl_distance_pct"],
        "tp_distance_pct": levels["tp_distance_pct"],
        "stop_loss": levels["stop_loss"],
        "take_profit": levels["take_profit"],
        "liquidation_price": levels["liquidation_price"],
        "if_buy_now_stop_loss": live_levels["stop_loss"],
        "if_buy_now_take_profit": live_levels["take_profit"],
        "if_buy_now_liquidation_price": live_levels["liquidation_price"],
    })
    if account_equity:
        result["sizing"] = position_sizing(
            anchor_price, levels["sl_distance_pct"], leverage, account_equity, risk_pct,
            committed_margin_usd=total_committed_margin(),
        )
    return result


def build_leverage_digest_message(payload, leverage, account_equity=None, risk_pct=0.01, top_n=10):
    """Same actionable-coins digest as telegram_alert.build_digest_message,
    but with leverage-derived SL/TP/liquidation/sizing instead of the flat
    spot %. Kept separate so the default spot `/now` path is untouched."""
    import datetime

    generated_at = datetime.datetime.fromtimestamp(payload["generated_at"])
    actionable = [c for c in payload["coins"] if c["label"] in ("GREEN", "RED") and c["entry"] is not None]
    actionable.sort(key=lambda c: c["final_score"], reverse=True)
    actionable = actionable[:top_n]

    lines = [
        f"\U0001f4ca {leverage}x leverage view ({generated_at:%Y-%m-%d %H:%M} UTC)",
        "⚠️ Approximate maintenance-margin assumption -- verify the real "
        "bracket for your symbol/size on Binance before trading. This bot trades "
        "spot only by default.",
    ]
    if leverage > LEVERAGE_WARN_THRESHOLD:
        lines.append(
            f"⚠️ {leverage}x is high for this signal -- direction comes from hourly/"
            "4h/1d trend + smoothed whale flow (swing-style, deliberately slow to "
            "react). At this leverage your liquidation distance is a few % or less, "
            "easily crossed within an hour. Consider 5-10x, where the signal's lag "
            "has room to not matter."
        )
    if not actionable:
        lines.append("\nNo clear LONG or SHORT signals right now.")
        return "\n".join(lines)

    for coin in actionable:
        is_long = coin["label"] == "GREEN"

        # Veto before computing anything else: a reversal candle on futures
        # contradicting this direction is exactly the "already extended,
        # about to bounce" case predictions.db showed hurting extreme-score
        # calls the most (see candlestick_patterns.py) -- skip the trade
        # entirely rather than show sizing/TP/SL for a setup just flagged
        # as risky.
        vetoed, veto_reason = futures_reversal_veto(coin["symbol"], is_long)
        if vetoed:
            emoji = "\U0001f7e2" if is_long else "\U0001f534"
            lines.append(
                f"\n{emoji} {coin['direction']} {coin['symbol']}  "
                f"⚠️ SKIPPED -- {veto_reason}, contradicts this direction"
            )
            continue

        # LONG/SHORT bias itself still comes from the spot-based pipeline
        # (trend/whale/ml scores) -- only the entry zone and live price get
        # recomputed against futures here, since that's what actually
        # determines where a leveraged position opens/exits/liquidates.
        futures_entry = futures_entry_for_coin(coin["symbol"], is_long)
        entry_source = "futures" if futures_entry is not None else "spot fallback"
        lev = leveraged_entry_for_coin(
            futures_entry or coin["entry"], is_long, leverage, symbol=coin["symbol"],
            account_equity=account_equity, risk_pct=risk_pct,
        )
        emoji = "\U0001f7e2" if is_long else "\U0001f534"
        price_note = "" if entry_source == "futures" else "  [futures data unavailable, using spot]"
        lines.append(f"\n{emoji} {coin['direction']} {coin['symbol']}  (price now: {format_price(lev['current_price'])}){price_note}")

        # TP/SL/liquidation below are anchored to the entry zone (entry_low/
        # entry_high), not to current_price in the header above -- if price
        # has already moved past that zone, showing only the anchored levels
        # next to "price now" reads as contradictory (e.g. a SHORT's TP
        # landing right next to, or above, the live price). Mirror
        # telegram_alert.build_digest_message's spot-version split: show the
        # zone-anchored levels only while price is still in the zone, else
        # also show what TP/SL/liquidation would be if acting at current
        # price instead (same leverage-derived %, different anchor).
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
            lines.append(
                f"  -> TP {format_price(lev['take_profit'])}  SL {format_price(lev['stop_loss'])}  "
                f"Liquidation {format_price(lev['liquidation_price'])}  "
                f"(MMR {lev['maintenance_margin_rate']:.2%})"
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
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Leverage-aware SL/TP/liquidation/position-size calculator")
    parser.add_argument("entry_price", type=float)
    parser.add_argument("--side", choices=["long", "short"], default="long")
    parser.add_argument("--leverage", type=int, default=100)
    parser.add_argument("--symbol")
    parser.add_argument("--equity", type=float, help="account equity in USD")
    parser.add_argument("--risk-pct", type=float, default=1.0, help="%% of account to risk per trade, e.g. 1 for 1%%")
    args = parser.parse_args()

    is_long = args.side == "long"
    levels = leveraged_levels(args.entry_price, is_long, args.leverage, symbol=args.symbol)
    print(f"Leverage: {args.leverage}x   Maintenance margin rate used: {levels['maintenance_margin_rate']:.2%}")
    print(f"Liquidation distance: {levels['liquidation_distance_pct']:.3%}  -> {format_price(levels['liquidation_price'])}")
    print(f"Stop-loss distance:   {levels['sl_distance_pct']:.3%}  -> {format_price(levels['stop_loss'])}")
    print(f"Take-profit distance: {levels['tp_distance_pct']:.3%}  -> {format_price(levels['take_profit'])}")
    if args.equity:
        sizing = position_sizing(args.entry_price, levels["sl_distance_pct"], args.leverage, args.equity, args.risk_pct / 100)
        print(f"\nRisking {args.risk_pct}% of ${args.equity:,.2f} = ${sizing['risk_amount_usd']:,.2f}")
        print(f"Position notional: ${sizing['position_notional_usd']:,.2f}   size: {sizing['position_size']:.6g}")
        warn = "  <-- exceeds account equity!" if sizing["insufficient_margin"] else ""
        print(f"Margin required: ${sizing['margin_required_usd']:,.2f}{warn}")
