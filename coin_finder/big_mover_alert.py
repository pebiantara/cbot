"""Big-mover alert -- scans the tracked universe for a coin making a
large, sustained directional move (the kind of multi-hour parabolic
pump/dump a user screenshot flagged 2026-11-19: MMTUSDT +113% over ~16h
with a clear volume spike). Generalizes btc_crash_alert.py's velocity-
window technique from BTC-only to every tracked coin, with three tiers
sized to catch a move WHILE it's still developing rather than after it's
already topped, plus a volume-confirmation gate (same projected-volume-
vs-trailing-average technique as scalp_pattern_signal.py's
_volume_confirms, adapted to this module's own 15m interval) so a thin
move with no real participation doesn't alert.

Tiers (any one tripping, on either side, triggers a volume check):
  - fast:   10% move within 15 minutes
  - medium: 20% move within 2 hours
  - slow:   35% move within 6 hours

Price history comes from one batched fetch_live_futures_prices() call per
poll (covers the whole exchange in a single request, not one per tracked
symbol) -- cheap regardless of universe size. The volume check only runs
for the rare symbol that actually trips a price tier (a handful of klines
calls per trip, not per poll per symbol).

Dedup: per-symbol+tier+direction cooldown matching that tier's own window
(same convention as btc_crash_alert.py) -- a sustained move gets one
alert per tier per window, not one every poll while it's still running.

WATCHLIST_OVERRIDES (per-symbol tighter tiers, e.g. INJ/JUP) additionally
require a hit to persist for WATCHLIST_CONFIRM_SECONDS before alerting --
see _confirm_watchlist_hits. A tighter bar is more exposed to a quick
whipsaw reverting before it means anything, so a watchlist hit has to
hold up, not just touch the threshold once.

Diagnostic only -- no SL/TP, no entry, no trade direction. Just "this
coin is doing something big right now, worth a look."

Set RUN_ONCE=1 to run a single poll and exit.
"""

import json
import os
import time

from entry_price import fetch_live_futures_prices
from history import get_futures_klines
from support_resistance import support_resistance_for_interval
from quick_scalp_signal import QUICK_SCALP_EXCLUDED_SYMBOLS, QUICK_SCALP_TOP_N
from scalp_pattern_signal import _resolve_symbols
from technical import ema, rsi
from telegram_alert import load_env, send_telegram_message

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_JSON = os.path.join(HERE, "big_mover_alert_state.json")

POLL_INTERVAL_SECONDS = int(os.environ.get("BIG_MOVER_POLL_SECONDS", 60))

# "pump_only" on the fast tier added 2026-11-23 at the user's request --
# the fast (15min) tier now only alerts on an UP move; medium/slow keep
# alerting on both directions, same as before.
TIERS = [
    {"name": "fast", "window_seconds": 15 * 60, "threshold_pct": 0.10, "pump_only": True},
    {"name": "medium", "window_seconds": 2 * 60 * 60, "threshold_pct": 0.20},
    {"name": "slow", "window_seconds": 6 * 60 * 60, "threshold_pct": 0.35},
]

# Focused per-symbol watch -- added 2026-11-20 at the user's request to watch
# INJUSDT specifically for a possible bounce (flagged the same day as both
# the most oversold coin AND the top dump-risk coin in the tracked universe
# -- no support below, so genuinely could break either way). TIERS above are
# tuned to avoid noise across the whole ~40-coin universe, which means they'd
# catch a single coin's move late; this gives INJ a much tighter bar so it's
# caught earlier, without reopening the "too small and annoying" noise
# problem for every other coin. Remove this entry (or the whole dict) once
# the watch is no longer needed -- it's meant to be temporary, not a
# permanent per-coin tuning mechanism.
WATCHLIST_OVERRIDES = {
    "INJUSDT": [
        {"name": "fast", "window_seconds": 15 * 60, "threshold_pct": 0.04, "pump_only": True},
        {"name": "medium", "window_seconds": 2 * 60 * 60, "threshold_pct": 0.08},
        {"name": "slow", "window_seconds": 6 * 60 * 60, "threshold_pct": 0.15},
    ],
    # Added same day as INJ -- lowest composite technical_score of the whole
    # scanned group (most oversold on the composite read) plus real room
    # before resistance, unlike INJ without INJ's "no support below" risk.
    "JUPUSDT": [
        {"name": "fast", "window_seconds": 15 * 60, "threshold_pct": 0.04, "pump_only": True},
        {"name": "medium", "window_seconds": 2 * 60 * 60, "threshold_pct": 0.08},
        {"name": "slow", "window_seconds": 6 * 60 * 60, "threshold_pct": 0.15},
    ],
    # Added 2026-11-22 at the user's request after BULLAUSDT showed the same
    # volatile-futures-only-listing profile as IDOL/PIEVERSE (24h range
    # 0.0135-0.0192, +22.8%/24h but pulling back -4.9% in the last 15min) --
    # same tight-bar rationale as INJ/JUP above.
    "BULLAUSDT": [
        {"name": "fast", "window_seconds": 15 * 60, "threshold_pct": 0.04, "pump_only": True},
        {"name": "medium", "window_seconds": 2 * 60 * 60, "threshold_pct": 0.08},
        {"name": "slow", "window_seconds": 6 * 60 * 60, "threshold_pct": 0.15},
    ],
}
_MAX_WINDOW_SECONDS = max(
    t["window_seconds"] for tiers in ([TIERS] + list(WATCHLIST_OVERRIDES.values())) for t in tiers
)

# Confirmation gate for WATCHLIST_OVERRIDES symbols only -- added 2026-11-20
# at the user's request ("dont make alert false alarm... wait to make sure
# choppy happens") after tightening INJ/JUP's thresholds made them more
# exposed to a quick whipsaw reverting before it means anything. A hit has
# to still be tripping the SAME tier+direction after WATCHLIST_CONFIRM_SECONDS
# before it actually alerts -- if it disappears in the meantime (price
# reverted), the clock resets, same philosophy as quick_scalp_alert.py's
# chase-confirmation delay. Universe-default TIERS are unaffected -- they're
# already wide enough that this isn't the same risk.
WATCHLIST_CONFIRM_SECONDS = int(os.environ.get("BIG_MOVER_WATCHLIST_CONFIRM_SECONDS", 120))
_pending_watchlist = {}  # (symbol, tier, direction) -> first_seen_ts

VOLUME_INTERVAL = "15m"
VOLUME_INTERVAL_SECONDS = 15 * 60
VOLUME_LOOKBACK = 20
VOLUME_CONFIRM_MULTIPLIER = float(os.environ.get("BIG_MOVER_VOLUME_MULTIPLIER", 1.5))

# Persisted to PRICE_HISTORY_JSON every poll -- added 2026-11-20 after a
# restart mid-pump (two in quick succession, for the watchlist + confirm-gate
# changes the same session) wiped this and caused a real missed alert on
# KERNELUSDT: a clean +14.5%/15min move with a 32x volume spike, comfortably
# past both the fast tier and the volume-confirm bar, went completely
# undetected because the restart landed 77 minutes into the move and erased
# the pre-pump reference price needed to recognize it had happened at all.
# Previously in-memory only (same tradeoff as btc_crash_alert.py/
# scalp_early_warning.py) -- that tradeoff is fine for a routine restart
# between moves, but not when it lands mid-move, which is exactly what
# happened here.
_price_history = {}  # symbol -> [(ts, price), ...], oldest first
PRICE_HISTORY_JSON = os.path.join(HERE, "big_mover_price_history.json")


def load_price_history():
    if not os.path.exists(PRICE_HISTORY_JSON):
        return
    with open(PRICE_HISTORY_JSON) as f:
        raw = json.load(f)
    now = time.time()
    cutoff = now - _MAX_WINDOW_SECONDS
    for symbol, buf in raw.items():
        trimmed = [(ts, price) for ts, price in buf if ts >= cutoff]
        if trimmed:
            _price_history[symbol] = trimmed


def save_price_history():
    with open(PRICE_HISTORY_JSON, "w") as f:
        json.dump(_price_history, f)


def load_state():
    """{key: {"ts", "rsi"}} -- "rsi" (added 2026-11-23, see poll_once) is
    the RSI_INTERVAL RSI AT THE MOMENT this hit fired, so a downstream
    consumer (reversal_watch_alert.py's watch-entry gate) can check "was
    this overbought/oversold WHEN the alert fired" instead of re-querying
    live RSI later -- a fast-moving coin's RSI can cool back to neutral
    within the few minutes it takes to even reach that check, letting a
    genuinely-overbought hit slip through a live-RSI gate untouched.
    Migrates old entries (a bare float timestamp, pre-2026-11-23) to
    {"ts": <that float>, "rsi": None} on load."""
    if not os.path.exists(STATE_JSON):
        return {}
    with open(STATE_JSON) as f:
        raw = json.load(f)
    return {
        key: (value if isinstance(value, dict) else {"ts": value, "rsi": None})
        for key, value in raw.items()
    }


def save_state(state):
    with open(STATE_JSON, "w") as f:
        json.dump(state, f)


def _reference_price(buf, now_ts, window_seconds):
    """Price from ~window_seconds ago, or None if history doesn't go back
    far enough yet. Same technique as btc_crash_alert.py's helper of the
    same name, generalized to an arbitrary per-symbol buffer."""
    target = now_ts - window_seconds
    if not buf or buf[0][0] > target + POLL_INTERVAL_SECONDS:
        return None
    reference = buf[0][1]
    for ts, price in buf:
        if ts <= target + POLL_INTERVAL_SECONDS:
            reference = price
        else:
            break
    return reference


def _confirm_watchlist_hits(symbol, hits, now_ts):
    """Filters `hits` (from check_tiers) down to only those that have been
    continuously tripping for WATCHLIST_CONFIRM_SECONDS. Also clears any
    pending (symbol, tier, direction) entry that's no longer present this
    poll, so a reverted move has to start its clock over, not pick up
    where a now-irrelevant earlier trigger left off."""
    hit_keys = {(symbol, h["tier"], h["direction"]) for h in hits}
    for key in list(_pending_watchlist):
        if key[0] == symbol and key not in hit_keys:
            del _pending_watchlist[key]

    confirmed = []
    for hit in hits:
        key = (symbol, hit["tier"], hit["direction"])
        first_seen = _pending_watchlist.get(key)
        if first_seen is None:
            _pending_watchlist[key] = now_ts
        elif now_ts - first_seen >= WATCHLIST_CONFIRM_SECONDS:
            confirmed.append(hit)
    return confirmed


def check_tiers(symbol, now_ts, price):
    """Returns a list of {"tier", "direction", "pct", "reference_price"}
    for every tier currently tripped for this symbol."""
    buf = _price_history.setdefault(symbol, [])
    buf.append((now_ts, price))
    cutoff = now_ts - _MAX_WINDOW_SECONDS
    while len(buf) > 1 and buf[0][0] < cutoff - POLL_INTERVAL_SECONDS:
        buf.pop(0)

    hits = []
    for tier in WATCHLIST_OVERRIDES.get(symbol, TIERS):
        reference = _reference_price(buf, now_ts, tier["window_seconds"])
        if reference is None:
            continue
        pct = (price - reference) / reference
        if abs(pct) < tier["threshold_pct"]:
            continue
        if tier.get("pump_only") and pct <= 0:
            continue
        hits.append({
            "tier": tier["name"], "direction": "pump" if pct > 0 else "dump",
            "pct": pct, "reference_price": reference,
        })
    return hits


def volume_confirmed(symbol, window_seconds=None):
    """{"confirmed", "ratio", "buy_usd", "sell_usd", "high", "low"} --
    "confirmed"/"ratio" as before: True if the latest 15m candle's
    volume, PROJECTED to a full candle at its pace so far, is at least
    VOLUME_CONFIRM_MULTIPLIER x the trailing VOLUME_LOOKBACK-candle
    average. Same projection technique as scalp_pattern_signal.py's
    _volume_confirms (comparing a still-forming candle's RAW volume
    against a full-candle average would almost never confirm), adapted
    here to this module's own 15m interval instead of that module's.

    buy_usd/sell_usd/high/low added 2026-11-22 at the user's request to
    show volume AGGRESSION (buy vs. sell, in $, using Binance's own
    quote-volume kline fields -- see history.py's _parse_klines) and the
    price level actually touched so far, alongside the volume-confirm
    check that was already fetching these same candles. Deliberately the
    REALIZED high/low over the window, not a projected target -- this
    module is diagnostic-only by design (see module docstring), and a
    fabricated forward price target would cross into exactly the "trade
    signal" territory it's meant to avoid. All sliced to `window_seconds`
    (the tier's own window) if given, else the full fetched range --
    narrower than the move itself would understate the real aggression/
    range behind a "slow" (6h) tier hit."""
    empty = {"confirmed": False, "ratio": None, "buy_usd": None, "sell_usd": None, "high": None, "low": None}
    try:
        candles = get_futures_klines(symbol, VOLUME_INTERVAL, limit=VOLUME_LOOKBACK + 1)
    except Exception:
        return empty
    if len(candles) < VOLUME_LOOKBACK + 1:
        return empty
    avg = sum(c["volume"] for c in candles[-(VOLUME_LOOKBACK + 1):-1]) / VOLUME_LOOKBACK
    if avg <= 0:
        return empty
    cur = candles[-1]
    elapsed_seconds = max(time.time() - cur["open_time"] / 1000, 30)
    projected_volume = cur["volume"] * (VOLUME_INTERVAL_SECONDS / elapsed_seconds)
    ratio = projected_volume / avg

    span = candles
    if window_seconds:
        n = max(1, min(len(candles), round(window_seconds / VOLUME_INTERVAL_SECONDS)))
        span = candles[-n:]
    buy_usd = sum(c["taker_buy_quote_volume"] for c in span)
    sell_usd = sum(c["quote_volume"] - c["taker_buy_quote_volume"] for c in span)
    return {
        "confirmed": ratio >= VOLUME_CONFIRM_MULTIPLIER, "ratio": ratio,
        "buy_usd": buy_usd, "sell_usd": sell_usd,
        "high": max(c["high"] for c in span), "low": min(c["low"] for c in span),
    }


RSI_INTERVAL = "1h"
RSI_CANDLE_LIMIT = 100  # enough warmup for a stable RSI14 + EMA20 on RSI_INTERVAL
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30


def overbought_oversold_status(symbol, interval=RSI_INTERVAL):
    """{"rsi", "label", "ema20_extension_pct"} -- added 2026-11-23 at the
    user's request to show HOW overbought/oversold a coin already is at
    the moment it trips a pump/dump tier (ACE/BULLA were flagged RSI 9.8
    and RSI 85.8 respectively on this same interval right as this
    question came up -- i.e. "how many percent" into overbought/oversold
    territory, not just a yes/no label). RSI14 on RSI_INTERVAL (1h -- a
    steadier read than this module's own 15m tier interval, less noisy
    for a positioning read rather than a move-detection one) plus %
    extension above/below EMA20 (how far price has stretched from its
    own short-term average, the same two numbers used in the BULLA/ACE
    manual analysis this was modeled on). None on any fetch failure --
    callers should treat that as "status unavailable", not "neutral"."""
    try:
        candles = get_futures_klines(symbol, interval, limit=RSI_CANDLE_LIMIT)
    except Exception:
        return None
    closes = [c["close"] for c in candles]
    r = rsi(closes, 14)[-1]
    e20 = ema(closes, 20)[-1]
    if r is None or e20 is None:
        return None
    if r >= RSI_OVERBOUGHT:
        label = "OVERBOUGHT"
    elif r <= RSI_OVERSOLD:
        label = "OVERSOLD"
    else:
        label = "neutral"
    return {"rsi": r, "label": label, "ema20_extension_pct": (closes[-1] / e20 - 1) * 100}


SR_TARGET_INTERVAL = "4h"
SR_TARGET_LOOKBACK = 60  # matches support_resistance_alert.py's own LOOKBACK for this interval


def structural_target(symbol, direction, price, move_range, interval=SR_TARGET_INTERVAL, lookback=SR_TARGET_LOOKBACK):
    """One-line string: the nearest REAL resistance/support ahead (for
    "pump"/"dump" respectively, from support_resistance.py's own
    next_key_level + volume profile logic -- same building blocks the
    /sr_ command and support_resistance_alert.py already use), plus the
    next heavier-volume VAH/VAL zone beyond it if that's further out.

    Added 2026-11-23 at the user's request for "estimates" on a fast
    pump/dump -- these are REAL historical levels (built from where
    volume/price actually traded before), not a forecast of where price
    will go, same distinction as every other diagnostic-only number in
    this module.

    When there's no resistance/support AND no VAH/VAL ahead at all
    ("clear air" on BOTH structural checks) there's nothing real left to
    reference, so this falls back to a measured-move estimate: project
    `move_range` (the realized high-low range of THIS SAME volume-
    confirmed move, i.e. the aggression candle(s) that actually drove
    it -- see volume_confirmed()) one more time from the current price.
    Still not a forecast, just the simplest "if it keeps the same
    character" projection grounded in this move's own realized size
    rather than an invented number."""
    try:
        sr = support_resistance_for_interval(symbol, interval, lookback)
    except Exception:
        sr = None

    sign = 1 if direction == "pump" else -1
    level = (sr["resistance"] if direction == "pump" else sr["support"]) if sr else None
    heavier = (sr["volume_profile"].get("vah") if direction == "pump" else sr["volume_profile"].get("val")) if sr else None
    level_label = "resistance" if direction == "pump" else "support"
    heavier_label = "VAH" if direction == "pump" else "VAL"

    parts = []
    if level is not None:
        parts.append(f"nearest {level_label} {level:g} ({(level / price - 1) * 100:+.1f}%)")
        if heavier is not None and sign * (heavier - level) > 0:
            parts.append(f"next {heavier_label} {heavier:g} ({(heavier / price - 1) * 100:+.1f}%)")
    elif heavier is not None and sign * (heavier - price) > 0:
        parts.append(f"no {level_label} ahead (clear air) -- next {heavier_label} {heavier:g} "
                      f"({(heavier / price - 1) * 100:+.1f}%)")
    else:
        projected = price + sign * move_range
        parts.append(f"no {level_label}/{heavier_label} ahead (clear air) -- measured-move estimate off "
                     f"this move's own range: ~{projected:g} ({sign * move_range / price * 100:+.1f}%)")
    return "  |  ".join(parts)


def poll_once(state):
    env = load_env()
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("No TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID configured -- skipping")
        return

    symbols = [s for s in _resolve_symbols(QUICK_SCALP_TOP_N) if s not in QUICK_SCALP_EXCLUDED_SYMBOLS]
    prices = fetch_live_futures_prices()  # one request covers the whole exchange
    now = time.time()

    for symbol in symbols:
        price = prices.get(symbol)
        if price is None:
            continue
        hits = check_tiers(symbol, now, price)
        if symbol in WATCHLIST_OVERRIDES:
            hits = _confirm_watchlist_hits(symbol, hits, now)
        for hit in hits:
            key = f"{symbol}:{hit['tier']}:{hit['direction']}"
            symbol_tiers = WATCHLIST_OVERRIDES.get(symbol, TIERS)
            window_seconds = next(t["window_seconds"] for t in symbol_tiers if t["name"] == hit["tier"])
            if now - state.get(key, {}).get("ts", 0) < window_seconds:
                continue  # cooldown -- already alerted this tier+direction recently

            vc = volume_confirmed(symbol, window_seconds=window_seconds)
            if not vc["confirmed"]:
                print(f"[big mover] {symbol} {hit['direction']} ({hit['tier']}, {hit['pct']:+.1%}) "
                      f"-- no volume confirmation (ratio={vc['ratio']}), skipping")
                continue
            ratio = vc["ratio"]

            emoji = "\U0001f7e2" if hit["direction"] == "pump" else "\U0001f534"
            window_label = f"{window_seconds // 60}min" if window_seconds < 3600 else f"{window_seconds // 3600}h"
            short_symbol = symbol[:-4].lower() if symbol.endswith("USDT") else symbol.lower()
            watch_tag = "\U0001f441️ WATCHLIST -- " if symbol in WATCHLIST_OVERRIDES else ""

            total_usd = vc["buy_usd"] + vc["sell_usd"]
            buy_pct = vc["buy_usd"] / total_usd if total_usd > 0 else 0.5
            aggressor = "buyers" if buy_pct >= 0.5 else "sellers"
            extreme_label, extreme_price = ("high", vc["high"]) if hit["direction"] == "pump" else ("low", vc["low"])

            rsi_status = overbought_oversold_status(symbol)
            if rsi_status is not None:
                rsi_line = (
                    f"RSI({RSI_INTERVAL})={rsi_status['rsi']:.1f} -- {rsi_status['label']}  "
                    f"({rsi_status['ema20_extension_pct']:+.1f}% vs EMA20)\n"
                )
            else:
                rsi_line = ""

            try:
                target_line = structural_target(symbol, hit["direction"], price, vc["high"] - vc["low"]) + "\n"
            except Exception:
                target_line = ""

            send_telegram_message(
                f"{emoji} {watch_tag}BIG MOVE: {symbol} {hit['direction']}ing -- {hit['pct']:+.1%} in ~{window_label} "
                f"({hit['reference_price']:g} → {price:g}), volume {ratio:.1f}x trailing average.\n"
                f"Aggression: buy=${vc['buy_usd']:,.0f}  sell=${vc['sell_usd']:,.0f}  "
                f"({aggressor} in control, {max(buy_pct, 1 - buy_pct):.0%})\n"
                f"Realized {extreme_label} with that volume so far: {extreme_price:g}\n"
                f"{rsi_line}"
                f"{target_line}"
                f"Diagnostic only -- not a trade signal. Run /pattern_{short_symbol}_15m or /sr_{short_symbol} "
                f"for more context.",
                token, chat_id,
            )
            state[key] = {"ts": now, "rsi": rsi_status["rsi"] if rsi_status is not None else None}
            print(f"[big mover] Alerted: {symbol} {hit['direction']} ({hit['tier']}, {hit['pct']:+.1%}, "
                  f"vol={ratio:.1f}x)")

    save_state(state)
    save_price_history()


if __name__ == "__main__":
    state = load_state()
    load_price_history()
    poll_once(state)
    if os.environ.get("RUN_ONCE"):
        raise SystemExit(0)
    while True:
        time.sleep(POLL_INTERVAL_SECONDS)
        try:
            poll_once(state)
        except Exception as e:
            print(f"Poll failed: {e} -- continuing")
