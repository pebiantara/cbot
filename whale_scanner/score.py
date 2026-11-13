"""Whale score per coin: net buy/sell $ from whale_events, over last 1h/4h/24h."""

import csv
import json
import math
import os
import time

from store import get_connection

OUTPUT_CSV = os.path.join(os.path.dirname(__file__), "whale_scores.csv")
STATE_FILE = os.path.join(os.path.dirname(__file__), "whale_score_state.json")

WINDOWS = {"1h": 3600, "4h": 4 * 3600, "24h": 24 * 3600}
WEIGHTS = {"1h": 0.5, "4h": 0.3, "24h": 0.2}
SCALE_USD = 2_000_000  # weighted net flow (in $) that pushes the score near 0 or 1

# The 1h window carries the most weight above but also the most noise -- a
# single whale trade entering/leaving that window between two /now_<x>x calls
# a few minutes apart can swing the raw score enough to flip GREEN<->RED on
# its own. Smoothing the output (not the inputs) keeps the score reacting to
# a real, sustained shift over a few runs instead of a one-trade blip. ALPHA
# is how much weight the newest raw reading gets; 1-ALPHA carries over from
# the last computed (already-smoothed) score for that symbol.
SMOOTHING_ALPHA = 0.3


def _load_smoothing_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE) as f:
        return json.load(f)


def _save_smoothing_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def _smoothed(symbol, raw_score, state):
    previous = state.get(symbol)
    smoothed = raw_score if previous is None else (
        SMOOTHING_ALPHA * raw_score + (1 - SMOOTHING_ALPHA) * previous
    )
    state[symbol] = smoothed
    return round(smoothed, 4)


def net_usd(conn, symbol, since):
    row = conn.execute(
        """SELECT
               SUM(CASE WHEN side = 'BUY' THEN usd_value ELSE -usd_value END) AS net,
               COUNT(*) AS trades
           FROM whale_events
           WHERE token_symbol = ? AND time >= ?""",
        (symbol, since),
    ).fetchone()
    return (row[0] or 0.0), row[1]


def whale_score_for_symbol(symbol, conn=None):
    """Same math as whale_scores(), for one symbol on demand (e.g. right after
    an instant big-trade trigger) instead of waiting for the next full scan."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()

    now = time.time()
    net_by_window = {}
    for label, seconds in WINDOWS.items():
        net, _ = net_usd(conn, symbol, now - seconds)
        net_by_window[label] = net

    weighted_net = sum(WEIGHTS[label] * net_by_window[label] for label in WINDOWS)
    score = (math.tanh(weighted_net / SCALE_USD) + 1) / 2

    state = _load_smoothing_state()
    score = _smoothed(symbol, score, state)
    _save_smoothing_state(state)

    if owns_conn:
        conn.close()
    return score


def whale_scores(conn=None):
    """{symbol: {...}} for every symbol with at least one stored whale event."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()

    # unmapped on-chain tokens (watch-only, no Binance symbol -- see token_map.csv)
    # never carry a priced trade; skip them so spam/airdropped tokens don't flood
    # the output with meaningless neutral (0.5) rows.
    symbols = [r[0] for r in conn.execute(
        "SELECT DISTINCT token_symbol FROM whale_events WHERE usd_value > 0"
    )]
    now = time.time()
    state = _load_smoothing_state()

    results = {}
    for symbol in symbols:
        net_by_window = {}
        trades_by_window = {}
        for label, seconds in WINDOWS.items():
            net, trades = net_usd(conn, symbol, now - seconds)
            net_by_window[label] = net
            trades_by_window[label] = trades

        weighted_net = sum(WEIGHTS[label] * net_by_window[label] for label in WINDOWS)
        score = (math.tanh(weighted_net / SCALE_USD) + 1) / 2
        score = _smoothed(symbol, score, state)

        results[symbol] = {
            "symbol": symbol,
            "whale_score": score,
            "net_usd_1h": round(net_by_window["1h"], 2),
            "net_usd_4h": round(net_by_window["4h"], 2),
            "net_usd_24h": round(net_by_window["24h"], 2),
            "trades_24h": trades_by_window["24h"],
        }

    _save_smoothing_state(state)
    if owns_conn:
        conn.close()
    return results


FIELDNAMES = ["symbol", "whale_score", "net_usd_1h", "net_usd_4h", "net_usd_24h", "trades_24h"]


if __name__ == "__main__":
    results = whale_scores()
    rows = sorted(results.values(), key=lambda r: r["whale_score"], reverse=True)

    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} coins to {OUTPUT_CSV}\n")
    print(f"{'symbol':<12}{'whale_score':>12}{'net_usd_24h':>16}")
    for r in rows:
        print(f"{r['symbol']:<12}{r['whale_score']:>12.4f}{r['net_usd_24h']:>16,.2f}")
