"""On-demand single-coin analysis, triggered by a big trade/transfer instead
of waiting for the next hourly batch. Reuses the same scoring logic as
combine.py but for one symbol, then sends an immediate Telegram alert.

Direction is informational only: LONG means "whale buying + good score ->
consider a spot buy", SHORT means "whale selling / bad score -> avoid or exit
an existing position". This project is spot-only, no leverage (see CLAUDE.md)
-- a SHORT bias never opens an actual short, there's nothing to short on spot.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "whale_scanner"))

from combine import NEUTRAL_SCORE, WEIGHTS, label_for  # noqa: E402
from entry_price import format_price, resistance_based_short, support_based_entry  # noqa: E402
from history import download_history  # noqa: E402
from ml_model import load_model, score_latest  # noqa: E402
from outcomes import record_snapshot  # noqa: E402
from technical import technical_score  # noqa: E402
from telegram_alert import load_env, send_telegram_message  # noqa: E402

_ml_model_cache = None


def _get_ml_model():
    global _ml_model_cache
    if _ml_model_cache is None:
        _ml_model_cache = load_model()
    return _ml_model_cache


def analyze_symbol(symbol):
    """Fresh candles -> technical/ml/whale scores -> final_score/label/entry,
    the same shape as a combine.py row. None if there's not enough history yet
    (e.g. a brand-new listing)."""
    history = download_history([symbol])[symbol]
    technical, indicators = technical_score(history)
    if technical is None:
        return None

    ml = score_latest(symbol, model=_get_ml_model())
    if ml is None:
        ml = NEUTRAL_SCORE

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "whale_scanner"))
    from score import whale_score_for_symbol  # noqa: E402
    whale = whale_score_for_symbol(symbol)

    final_score = (
        WEIGHTS["technical_score"] * technical
        + WEIGHTS["ml_score"] * ml
        + WEIGHTS["whale_score"] * whale
    )
    close = indicators["close"]
    ema20, ema50, ema200 = indicators["ema20"], indicators["ema50"], indicators["ema200"]
    above_ema200 = close > ema200
    # market_breadth is omitted here (None) -- it needs a full universe scan,
    # which defeats the point of this single-symbol on-demand path; the
    # overextension gates don't need that, so both apply same as combine.py.
    label = label_for(
        final_score, indicators["trend_score"], whale,
        extension_above_vah_pct=indicators["extension_above_vah_pct"],
        extension_below_val_pct=indicators["extension_below_val_pct"],
    )

    # mirrors combine.py's build_green_list() exactly: GREEN -> long entry,
    # RED -> short-bias info only (spot-only bot, never an actual short).
    direction = None
    entry = None
    if label == "GREEN":
        direction = "LONG"
        entry = support_based_entry(close, ema20, ema50, ema200)
    elif label == "RED":
        direction = "SHORT"
        entry = resistance_based_short(close, ema20, ema50, ema200)

    return {
        "symbol": symbol,
        "close": close,
        "final_score": round(final_score, 4),
        "technical_score": round(technical, 4),
        "ml_score": round(ml, 4),
        "whale_score": round(whale, 4),
        "above_ema200": above_ema200,
        "label": label,
        "direction": direction,
        "entry": entry,
    }


def build_early_signal_text(trigger):
    """Sent immediately on the raw whale event, before the (slower) EMA/ML/
    whale-score confirmation runs -- so you see something within seconds
    instead of waiting for label_for()'s lagging technical confirmation."""
    bias = "LONG bias" if trigger["side"] == "BUY" else "SHORT bias"
    return (
        f"⚡ Big {trigger['side']} detected: {trigger['symbol']} "
        f"${trigger['usd_value']:,.0f} ({trigger['chain']})\n"
        f"Early signal ({bias}, unconfirmed) -- full analysis follows shortly."
    )


def build_confirmation_text(trigger, result):
    """Only sent when the full analysis actually resolves to GREEN or RED --
    same entry/TP/SL format as the hourly digest (telegram_alert.py)."""
    emoji = "\U0001f7e2" if result["label"] == "GREEN" else "\U0001f534"
    lines = [
        f"{emoji} Confirmed: {result['direction']} {result['symbol']} "
        f"(final_score={result['final_score']:.4f})",
        f"Triggered by a {trigger['side']} of ${trigger['usd_value']:,.0f} ({trigger['chain']})",
    ]
    entry = result["entry"]
    if entry["at_entry_now"]:
        lines.append(f"Entry: {format_price(entry['entry_low'])} - {format_price(entry['entry_high'])}  (at entry zone now)")
        lines.append(f"TP: {format_price(entry['take_profit'])}  SL: {format_price(entry['stop_loss'])}")
    else:
        lines.append(f"Pullback entry: {format_price(entry['entry_low'])} - {format_price(entry['entry_high'])} "
                      f"({entry['distance_from_entry_pct']:+.1f}% away, may not get hit)")
        lines.append(f"  -> TP {format_price(entry['take_profit'])}  SL {format_price(entry['stop_loss'])}")
        lines.append(f"If acting now at {format_price(entry['current_price'])} instead:")
        lines.append(f"  -> TP {format_price(entry['if_buy_now_take_profit'])}  SL {format_price(entry['if_buy_now_stop_loss'])}")
    return "\n".join(lines)


def analyze_and_alert(trigger):
    """trigger: {"symbol", "side", "usd_value", "chain"} from the whale event
    that caused this. Sends an immediate unconfirmed ping, then a confirmation
    message only if the full (slower) analysis resolves to GREEN/RED -- YELLOW
    isn't actionable, so no second message for it. Returns the analysis result
    dict, or None if skipped (not enough candle history yet).

    Telegram pushes muted via SYSTEM1_INSTANT_ALERTS_ENABLED=0 in .env
    (2026-11-06, system 3 is what's actually traded off of now) -- the
    analysis + record_snapshot() logging below keeps running regardless, so
    accuracy can still be checked later even with alerts off."""
    env = load_env()
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    alerts_enabled = env.get("SYSTEM1_INSTANT_ALERTS_ENABLED", "1") != "0"

    if token and chat_id and alerts_enabled:
        send_telegram_message(build_early_signal_text(trigger), token, chat_id)

    result = analyze_symbol(trigger["symbol"])
    if result is None:
        return None

    record_snapshot([result], snapshot_time=time.time())

    if token and chat_id and alerts_enabled and result["label"] in ("GREEN", "RED"):
        send_telegram_message(build_confirmation_text(trigger, result), token, chat_id)

    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Manually trigger a single-coin analysis + alert")
    parser.add_argument("symbol")
    parser.add_argument("--side", choices=["BUY", "SELL"], default="BUY")
    parser.add_argument("--usd-value", type=float, default=250_000)
    parser.add_argument("--chain", default="binance")
    args = parser.parse_args()

    trigger = {"symbol": args.symbol, "side": args.side, "usd_value": args.usd_value, "chain": args.chain}
    result = analyze_and_alert(trigger)
    print(result)
