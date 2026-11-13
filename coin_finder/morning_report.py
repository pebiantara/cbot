"""Tracks the specific coins picked at the day's first (morning) pipeline run:
shows each one's real price change at every 2h checkpoint since then, and
pushes a Telegram update whenever a new checkpoint resolves (roughly every 2h,
since that's the HORIZONS spacing in outcomes.py) -- not a generic backtest
number, the actual outcome of that morning's specific picks.
"""

import datetime
import json
import os
import sys

from outcomes import HORIZONS, get_connection, resolve_matured
from telegram_alert import load_env, send_telegram_message

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_JSON = os.path.join(HERE, "morning_report_state.json")

TOP_N = 10
# ordered the same way HORIZONS is defined in outcomes.py: 2h,4h,6h,8h,10h,12h,1d
HORIZON_ORDER = list(HORIZONS.keys())


def get_morning_snapshot_time(conn):
    """Earliest snapshot_time whose UTC date is today; falls back to the most
    recent snapshot overall if nothing has run yet today (e.g. manual testing)."""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    rows = conn.execute("SELECT DISTINCT snapshot_time FROM predictions ORDER BY snapshot_time").fetchall()
    for (snapshot_time,) in rows:
        if datetime.datetime.fromtimestamp(snapshot_time, datetime.timezone.utc).date() == today:
            return snapshot_time
    return rows[-1][0] if rows else None


def load_state():
    if not os.path.exists(STATE_JSON):
        return {}
    with open(STATE_JSON) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_JSON, "w") as f:
        json.dump(state, f)


def build_report(conn, snapshot_time, top_n=TOP_N):
    """{"snapshot_time", "coins": [{"symbol", "label", "close_price",
    "checkpoints": {horizon: return_pct or None}}]}"""
    predictions = conn.execute(
        """SELECT id, symbol, label, final_score, close_price FROM predictions
           WHERE snapshot_time = ? ORDER BY final_score DESC LIMIT ?""",
        (snapshot_time, top_n),
    ).fetchall()

    coins = []
    resolved_count = 0
    for pred_id, symbol, label, final_score, close_price in predictions:
        outcomes = dict(conn.execute(
            "SELECT horizon, return_pct FROM outcomes WHERE prediction_id = ?", (pred_id,)
        ).fetchall())
        resolved_count += len(outcomes)
        checkpoints = {h: outcomes.get(h) for h in HORIZON_ORDER}
        coins.append({
            "symbol": symbol, "label": label, "final_score": final_score,
            "close_price": close_price, "checkpoints": checkpoints,
        })
    return {"snapshot_time": snapshot_time, "coins": coins}, resolved_count


def format_report_text(report):
    dt = datetime.datetime.fromtimestamp(report["snapshot_time"], datetime.timezone.utc)
    lines = [f"\U0001f4c8 Morning picks check-in ({dt:%Y-%m-%d %H:%M} UTC start)"]
    up_count = 0
    checked_count = 0
    for coin in report["coins"]:
        cells = []
        for h in HORIZON_ORDER:
            pct = coin["checkpoints"][h]
            if pct is None:
                cells.append(f"{h}:--")
            else:
                cells.append(f"{h}:{pct:+.1f}%")
                checked_count += 1
                if pct > 0:
                    up_count += 1
        lines.append(f"{coin['symbol']} ({coin['label']})  " + "  ".join(cells))
    if checked_count:
        lines.append(f"\nUp at {up_count}/{checked_count} checked checkpoints so far today")
    return "\n".join(lines)


def run():
    conn = get_connection()
    resolve_matured(conn=conn)

    snapshot_time = get_morning_snapshot_time(conn)
    if snapshot_time is None:
        conn.close()
        print("No predictions logged yet -- run combine.py first")
        return {"sent": False}

    report, resolved_count = build_report(conn, snapshot_time)
    conn.close()

    state = load_state()
    key = str(snapshot_time)
    previously_resolved = state.get(key, 0)

    print(format_report_text(report))

    sent = False
    if resolved_count > previously_resolved:
        env = load_env()
        token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
        if token and chat_id:
            send_telegram_message(format_report_text(report), token, chat_id)
            sent = True
        else:
            print("\n(New checkpoint resolved, but TELEGRAM_BOT_TOKEN/CHAT_ID not set -- not sent)")

    state[key] = resolved_count
    save_state(state)
    return {"sent": sent, "resolved_count": resolved_count}


if __name__ == "__main__":
    result = run()
    sys.exit(0)
