"""Short-squeeze EARLY-WARNING alert -- added 2026-11-24 after a real
data-driven finding: analyzing 41 historical fast-pump events (from
big_mover_alert.py/alpha_mover_alert.py's own logged hits) against
futures_liquidation_listener.py's already-running shadow-logged data
found that short-liquidations (forced buy-covering) made up an average
72.0% (median 83.4%) of total liquidation $ in the 30min BEFORE those
pumps -- a real, consistent precursor, unlike price/volume/order-flow
(all tested first, all showed no reliable signal, see the chat history
behind this module). The mechanism makes sense: a cascade of short
liquidations IS itself a wave of forced market buys, so it can be the
actual trigger for the price move, not just a side-effect of it.

This fires BEFORE big_mover_alert/alpha_mover_alert's own price-based
tiers necessarily confirm anything -- it's reading the liquidation feed
directly (net_liquidation_usd(), same table futures_context.py's own
liquidation_context() already reads), not price action, so it can catch
the squeeze building in real time rather than waiting for the resulting
price move to clear a 10%/15min bar. Genuinely earlier, not a
replacement for those alerts -- still diagnostic only, no SL/TP, no
entry, no trade direction; a short squeeze doesn't guarantee a
sustained pump, it just means forced buying is actively happening.

Scans EVERY symbol with recent liquidation activity (not restricted to
any pre-defined tracked universe) -- futures_liquidation_listener.py's
!forceOrder@arr stream already covers all USDT-M futures symbols
globally, and the whole point here is catching a squeeze wherever it's
building, including a coin not yet in any other tracker.

Set RUN_ONCE=1 to run a single poll and exit.
"""

import json
import os
import time

from big_mover_alert import RSI_INTERVAL, overbought_oversold_status
from entry_price import fetch_live_futures_price
from http_pool import SESSION
from liquidation_store import get_connection, net_liquidation_usd
from telegram_alert import load_env, send_telegram_message

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_JSON = os.path.join(HERE, "liquidation_cascade_alert_state.json")

POLL_INTERVAL_SECONDS = int(os.environ.get("LIQUIDATION_CASCADE_POLL_SECONDS", 30))
ROLLING_WINDOW_SECONDS = int(os.environ.get("LIQUIDATION_CASCADE_WINDOW_SECONDS", 5 * 60))

# Replaced the flat $20k floor 2026-11-24 at the user's request with a
# coin-size-normalized gate instead: liq_usd as a % of that SAME coin's
# own 24h volume predicts move size far better than a flat $ amount does
# (a $20k liquidation means a lot more for a thin coin than a deep one).
# Linear fit against the 51-event sample (pumps+dumps): move_pct = 7.82 +
# 105.03 * (liq_usd/vol24h_pct), R^2=0.592 -- genuinely explains ~59% of
# the variance in move size, vs raw $ which only explained ~39% (0.626^2)
# and raw liquidation COUNT which explained even less (0.455^2 ~ 21%,
# tested and rejected -- more traders liquidated isn't the same as more
# $ liquidated, and $ is what actually moves price).
MOVE_ESTIMATE_INTERCEPT = 7.82
MOVE_ESTIMATE_SLOPE = 105.03
MIN_PREDICTED_MOVE_PCT = float(os.environ.get("LIQUIDATION_CASCADE_MIN_PREDICTED_MOVE_PCT", 10))

MIN_LIQ_COUNT = int(os.environ.get("LIQUIDATION_CASCADE_MIN_COUNT", 5))
# 65% -- just under the sample's "strongly dominated" (>70%) bucket, which
# alone covered 59% of real events; this catches a bit more while still
# requiring a clear short-covering lean, not just general liquidation noise.
MIN_DOMINANCE_PCT = float(os.environ.get("LIQUIDATION_CASCADE_MIN_DOMINANCE", 0.65))
COOLDOWN_SECONDS = int(os.environ.get("LIQUIDATION_CASCADE_COOLDOWN_SECONDS", 15 * 60))

# Lead-time stats from backtesting these exact thresholds against the 41
# historical events (33 would have fired) -- added 2026-11-24 at the user's
# request to show an expected timing window in the alert itself, not just
# the trigger. Genuinely wide, not a countdown: 27% of catches fired almost
# simultaneously with the move (<5min true lead), 30% landed in 5-15min,
# and 39% showed 20-30min+ (several hit exactly the 30min backtest window
# edge, meaning their real lead time could be even longer than that -- the
# backtest just couldn't see further back). Hardcoded from that one-time
# backtest, not recomputed live -- recomputing this on every alert would
# need the same historical big_mover state + liquidation join every time,
# which is unnecessary for a calibration constant that doesn't change poll
# to poll.
LEAD_TIME_MEDIAN_MINUTES = 10
LEAD_TIME_RANGE_MINUTES = (0.5, 30)

FUTURES_API = "https://fapi.binance.com"


def fetch_24h_volume(symbol):
    """Live 24h quote volume for `symbol` -- used to normalize this
    poll's liquidation $ into a %-of-volume ratio (see
    MOVE_ESTIMATE_INTERCEPT/SLOPE above). None on any fetch failure."""
    try:
        resp = SESSION.get(f"{FUTURES_API}/fapi/v1/ticker/24hr", params={"symbol": symbol}, timeout=8)
        resp.raise_for_status()
        return float(resp.json()["quoteVolume"])
    except Exception:
        return None


def estimate_move_pct(liq_usd, vol_24h_usd):
    """Predicted move %, from the linear fit against the 51-event sample
    (see module-level comment). None if vol_24h_usd isn't usable."""
    if not vol_24h_usd or vol_24h_usd <= 0:
        return None
    ratio_pct = (liq_usd / vol_24h_usd) * 100
    return MOVE_ESTIMATE_INTERCEPT + MOVE_ESTIMATE_SLOPE * ratio_pct


def load_state():
    if not os.path.exists(STATE_JSON):
        return {}
    with open(STATE_JSON) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_JSON, "w") as f:
        json.dump(state, f)


def poll_once(state):
    env = load_env()
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("No TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID configured -- skipping")
        return

    now = time.time()
    since = now - ROLLING_WINDOW_SECONDS
    conn = get_connection()
    try:
        symbols = [r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM liquidations WHERE time >= ?", (since,)
        ).fetchall()]

        # (direction, liquidation side that drives it, backtest sample size, median lead minutes)
        # Dump side added 2026-11-24 after backtesting the exact mirrored
        # thresholds against 10 historical fast-dump events (7 would have
        # fired, median 8.5min lead) -- smaller sample than the pump side's
        # 41-event backtest (33 fired, median 10min), but directionally
        # consistent (avg SELL-liq dominance 70.4% vs pump's BUY-liq 72.0%).
        DIRECTIONS = [
            ("pump", "BUY", "SHORT SQUEEZE WARNING", "short liquidation(s)", "forcing buy-side covering", 33, LEAD_TIME_MEDIAN_MINUTES, LEAD_TIME_RANGE_MINUTES),
            ("dump", "SELL", "LONG LIQUIDATION WARNING", "long liquidation(s)", "forcing sell-side closing", 7, 8.5, (4.0, 30.0)),
        ]

        for symbol in symbols:
            sell_usd, buy_usd = net_liquidation_usd(symbol, since, conn=conn)
            total = sell_usd + buy_usd
            if total <= 0:
                continue

            for direction, side, label, liq_noun, action, n_backtest, median_lead, lead_range in DIRECTIONS:
                state_key = f"{symbol}:{direction}"
                if now - state.get(state_key, 0) < COOLDOWN_SECONDS:
                    continue  # cooldown -- already alerted this symbol+direction recently

                side_usd = buy_usd if side == "BUY" else sell_usd
                dominance = side_usd / total
                if dominance < MIN_DOMINANCE_PCT:
                    continue
                side_count = conn.execute(
                    "SELECT COUNT(*) FROM liquidations WHERE symbol = ? AND side = ? AND time >= ?",
                    (symbol, side, since),
                ).fetchone()[0]
                if side_count < MIN_LIQ_COUNT:
                    continue

                # Predicted-move gate -- fetched only once the cheap DB-only
                # checks above already passed, since this needs a live API
                # call per candidate.
                vol_24h = fetch_24h_volume(symbol)
                predicted_move_pct = estimate_move_pct(side_usd, vol_24h)
                if predicted_move_pct is None or predicted_move_pct < MIN_PREDICTED_MOVE_PCT:
                    continue

                try:
                    price = fetch_live_futures_price(symbol)
                except Exception:
                    price = None
                price_str = f"{price:g}" if price is not None else "n/a"

                rsi_status = overbought_oversold_status(symbol)
                rsi_line = (
                    f"RSI({RSI_INTERVAL})={rsi_status['rsi']:.1f} -- {rsi_status['label']}\n"
                    if rsi_status is not None else ""
                )

                window_label = f"{ROLLING_WINDOW_SECONDS // 60}min"
                short_symbol = symbol[:-4].lower() if symbol.endswith("USDT") else symbol.lower()
                lo, hi = lead_range
                send_telegram_message(
                    f"\U0001f6a8 {label}: {symbol} -- {side_count} {liq_noun} worth "
                    f"${side_usd:,.0f} in the last {window_label} ({dominance:.0%} of all liquidation volume, "
                    f"{side_usd/vol_24h*100:.3f}% of 24h volume), {action}. Live price: {price_str}.\n"
                    f"Predicted move: ~{predicted_move_pct:.0f}% (R²=0.59 fit, noisy -- a rough magnitude "
                    f"lean, not a precise target)\n"
                    f"{rsi_line}"
                    f"Historical lead time before price confirms (n={n_backtest} backtest): median ~{median_lead:g}min, "
                    f"wide range {lo:g}-{hi:g}min -- NOT a countdown.\n"
                    f"EARLY signal, not confirmed by price yet -- this is BEFORE any big_mover/alpha_mover tier "
                    f"necessarily trips. Diagnostic only, not a trade signal. Run /pattern_{short_symbol}_15m "
                    f"for more context.",
                    token, chat_id,
                )
                state[state_key] = now
                print(f"[liquidation cascade] Alerted: {symbol} {direction} -- ${side_usd:,.0f} {side}-liq "
                      f"({dominance:.0%} dominance, n={side_count}, predicted_move={predicted_move_pct:.0f}%)")
    finally:
        conn.close()

    save_state(state)


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
