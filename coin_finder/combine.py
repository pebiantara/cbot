"""Green Coin finder: combine technical_score + ml_score + whale_score into
final_score, label each coin GREEN / YELLOW / RED, write green_list.json.

whale_score already encodes exchange netflow -- exchange wallets in watchlist.csv
have BUY/SELL flipped at ingestion (deposit-to-exchange = sell pressure), so a
low whale_score here means "net selling or net exchange inflow" without needing
a separate column.
"""

import csv
import json
import os
import time

from entry_price import resistance_based_short, support_based_entry
from outcomes import record_snapshot

TECHNICAL_CSV = os.path.join(os.path.dirname(__file__), "technical_scores.csv")
ML_CSV = os.path.join(os.path.dirname(__file__), "ml_scores.csv")
WHALE_CSV = os.path.join(os.path.dirname(__file__), "..", "whale_scanner", "whale_scores.csv")
OUTPUT_JSON = os.path.join(os.path.dirname(__file__), "green_list.json")

WEIGHTS = {"technical_score": 1 / 3, "ml_score": 1 / 3, "whale_score": 1 / 3}
NEUTRAL_SCORE = 0.5  # used when a coin has no ml_score / whale_score yet

GREEN_THRESHOLD = 0.6
RED_THRESHOLD = 0.4

# accuracy_report.json showed GREEN calls skewing negative-return across every
# horizon -- the common thread in the predictions.db history was entries that
# had already chased price well above the recent value area (overbought, not
# "about to continue"). Capping how far above VAH a GREEN entry can be close
# filters out that chase-the-top case without touching technical_score's
# weights (which are already backtested elsewhere).
MAX_EXTENSION_ABOVE_VAH_PCT = 1.5
# Symmetric case for RED: extreme-score RED calls have historically hit WORSE
# than moderate ones (61% vs 49% at the 6h horizon, checked against
# predictions.db) -- the common thread is a coin that's already fallen well
# below its own recent value area, closer to a mean-reversion bounce than a
# continuation. extension_below_val_pct was already computed by technical.py
# but never checked here, unlike VAH above -- this closes that gap.
MAX_EXTENSION_BELOW_VAL_PCT = 1.5

# A single coin's bullish signal doesn't mean much if the whole market is
# falling -- the retroactive check against predictions.db showed even the
# non-overextended GREEN calls still had a 0% hit rate at the 1d horizon
# during a broad market decline (29-30 Oct 2026). market_breadth (fraction of
# the universe trading above its own EMA200) is a free-to-compute proxy for
# "is this a bull or bear regime right now" -- below the threshold, hold GREEN
# back to YELLOW even if everything else about that one coin looks bullish.
MARKET_BREADTH_MIN = 0.4
# Symmetric case for RED: a coin that looks bearish on its own often just
# bounces back (not a sustained drop) when the broader market is strongly
# bullish -- same mean-reversion risk as chasing GREEN into a bear market,
# mirrored. Unlike MARKET_BREADTH_MIN, this hasn't been retroactively checked
# against real data yet (predictions.db so far only covers a declining
# market) -- it's the same reasoning applied symmetrically, not yet confirmed
# the same way. Re-check once a bull-market stretch accumulates in outcomes.
MARKET_BREADTH_MAX_FOR_RED = 0.6

# GREEN/RED used to gate on above_ema200, which is the 1h EMA200 only (~8 days
# of lookback) -- a coin recovering on 1h EMA20/50 but still just under its 1h
# EMA200 got stuck RED/YELLOW on every refresh even while 4h/1d looked fine,
# and whale_score noise alone could flip it to RED on any given /now_<x>x
# rerun. trend_score (from technical.py) already blends 1h (50%) + 4h (25%) +
# 1d (25%) EMA200/EMA50 agreement, so gating on it instead means all three
# timeframes have to actually agree bullish/bearish, not just the shortest one.
TREND_BULLISH_MIN = 0.6
TREND_BEARISH_MAX = 0.4


def load_csv(path, key="symbol"):
    if not os.path.exists(path):
        return {}
    with open(path, newline="") as f:
        return {row[key]: row for row in csv.DictReader(f)}


def label_for(final_score, trend_score, whale_score, extension_above_vah_pct=None, market_breadth=None,
              extension_below_val_pct=None):
    not_overextended_up = (
        extension_above_vah_pct is None  # no volume-profile data yet -- don't block on it
        or extension_above_vah_pct <= MAX_EXTENSION_ABOVE_VAH_PCT
    )
    not_overextended_down = (
        extension_below_val_pct is None
        or extension_below_val_pct <= MAX_EXTENSION_BELOW_VAL_PCT
    )
    market_not_bearish = market_breadth is None or market_breadth >= MARKET_BREADTH_MIN
    market_not_bullish = market_breadth is None or market_breadth <= MARKET_BREADTH_MAX_FOR_RED

    if (final_score >= GREEN_THRESHOLD and trend_score >= TREND_BULLISH_MIN and whale_score > NEUTRAL_SCORE
            and not_overextended_up and market_not_bearish):
        return "GREEN"
    # symmetric with GREEN: all three signals must agree bearish, not just one.
    # (previously any single bearish signal triggered RED -- too loose to call
    # it an actual trade signal, since e.g. a brief dip below EMA200 alone
    # isn't meaningfully different from a GREEN coin's opposite single signal.)
    if (final_score <= RED_THRESHOLD and trend_score <= TREND_BEARISH_MAX and whale_score < RED_THRESHOLD
            and market_not_bullish and not_overextended_down):
        return "RED"
    return "YELLOW"


def build_green_list():
    technical = load_csv(TECHNICAL_CSV)
    ml = load_csv(ML_CSV)
    whale = load_csv(WHALE_CSV)

    # pass 1: per-coin scores + above_ema200, and the market_breadth that
    # needs all of them computed first.
    prepared = []
    for symbol, t_row in technical.items():
        technical_score = float(t_row["technical_score"])
        ml_score = float(ml[symbol]["ml_score"]) if symbol in ml else NEUTRAL_SCORE
        whale_score = float(whale[symbol]["whale_score"]) if symbol in whale else NEUTRAL_SCORE

        final_score = (
            WEIGHTS["technical_score"] * technical_score
            + WEIGHTS["ml_score"] * ml_score
            + WEIGHTS["whale_score"] * whale_score
        )
        close = float(t_row["close"])
        ema20, ema50, ema200 = float(t_row["ema20"]), float(t_row["ema50"]), float(t_row["ema200"])
        above_ema200 = close > ema200
        trend_score = float(t_row["trend_score"])

        # .get() + "" check tolerates a technical_scores.csv written before
        # these columns existed, same pattern as market_cap below.
        extension_above_vah_pct = (
            float(t_row["extension_above_vah_pct"])
            if t_row.get("extension_above_vah_pct") else None
        )
        extension_below_val_pct = (
            float(t_row["extension_below_val_pct"])
            if t_row.get("extension_below_val_pct") else None
        )
        market_cap = float(t_row["market_cap"]) if t_row.get("market_cap") else None
        volume_mcap_ratio = float(t_row["volume_mcap_ratio"]) if t_row.get("volume_mcap_ratio") else None

        prepared.append({
            "symbol": symbol, "close": close, "ema20": ema20, "ema50": ema50, "ema200": ema200,
            "technical_score": technical_score, "ml_score": ml_score, "whale_score": whale_score,
            "final_score": final_score, "above_ema200": above_ema200, "trend_score": trend_score,
            "extension_above_vah_pct": extension_above_vah_pct,
            "extension_below_val_pct": extension_below_val_pct,
            "market_cap": market_cap, "volume_mcap_ratio": volume_mcap_ratio,
        })

    market_breadth = (
        sum(1 for p in prepared if p["above_ema200"]) / len(prepared) if prepared else None
    )

    # pass 2: labels (needs market_breadth from pass 1) + entry levels.
    rows = []
    for p in prepared:
        label = label_for(
            p["final_score"], p["trend_score"], p["whale_score"],
            p["extension_above_vah_pct"], market_breadth,
            extension_below_val_pct=p["extension_below_val_pct"],
        )

        # GREEN -> long entry (buy the pullback to support). RED -> short-bias
        # info only (spot-only bot, never an actual short order -- see
        # CLAUDE.md "never use leverage"). YELLOW -> no actionable entry either way.
        direction = None
        entry = None
        if label == "GREEN":
            direction = "LONG"
            entry = support_based_entry(p["close"], p["ema20"], p["ema50"], p["ema200"])
        elif label == "RED":
            direction = "SHORT"
            entry = resistance_based_short(p["close"], p["ema20"], p["ema50"], p["ema200"])

        rows.append({
            "symbol": p["symbol"],
            "close": p["close"],
            "final_score": round(p["final_score"], 4),
            "technical_score": round(p["technical_score"], 4),
            "ml_score": round(p["ml_score"], 4),
            "whale_score": round(p["whale_score"], 4),
            "above_ema200": p["above_ema200"],
            "trend_score": round(p["trend_score"], 4),
            "extension_above_vah_pct": p["extension_above_vah_pct"],
            "extension_below_val_pct": p["extension_below_val_pct"],
            "market_breadth": round(market_breadth, 4) if market_breadth is not None else None,
            "label": label,
            "direction": direction,
            "entry": entry,
            "market_cap": p["market_cap"],
            "volume_mcap_ratio": p["volume_mcap_ratio"],
        })

    rows.sort(key=lambda r: r["final_score"], reverse=True)
    return rows


if __name__ == "__main__":
    rows = build_green_list()
    generated_at = time.time()
    output = {"generated_at": generated_at, "coins": rows}

    with open(OUTPUT_JSON, "w") as f:
        json.dump(output, f, indent=2)

    record_snapshot(rows, snapshot_time=generated_at)

    counts = {"GREEN": 0, "YELLOW": 0, "RED": 0}
    for r in rows:
        counts[r["label"]] += 1

    print(f"Wrote {len(rows)} coins to {OUTPUT_JSON}")
    print(f"GREEN: {counts['GREEN']}  YELLOW: {counts['YELLOW']}  RED: {counts['RED']}\n")
    print(f"{'symbol':<12}{'final_score':>12}  label")
    for r in rows[:15]:
        print(f"{r['symbol']:<12}{r['final_score']:>12.4f}  {r['label']}")
