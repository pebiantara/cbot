"""Token-unlock reminder alert -- added 2026-11-24 at the user's request.

No reliable FREE API exists for token unlock schedules right now --
DeFiLlama's unlocks/emissions API moved behind a paid plan (402 Payment
Required as of this check), CoinGecko's free API has no vesting/unlock
field, CoinMarketCap gates it behind their paid Pro tier, and
tokenomist.ai (formerly token.unlocks.app) is a JS-rendered site with no
public API -- scraping it would be fragile (breaks on redesigns) and a
ToS risk for something running unattended. So this is deliberately a
MANUALLY-maintained calendar, not an automatic feed -- same convention
as PRIORITY_SYMBOLS/WATCHLIST_OVERRIDES/STABLECOIN_SYMBOLS already in
this codebase (hand-curated lists, not API-derived).

unlock_calendar.json: a flat list of
  {"symbol": "JUPUSDT", "date": "2026-12-01", "note": "team unlock, ~5% supply"}
Add entries by hand (or ask Claude to add one when you hear about an
unlock) -- "date" is the unlock date itself, YYYY-MM-DD.

Fires once per entry when the unlock is within ALERT_DAYS_BEFORE days,
then again on the day itself if it's today -- dedup via
unlock_alert_state.json so it doesn't repeat every poll. Polls once a
day by default (unlock dates don't need minute-level freshness).

Set RUN_ONCE=1 to run a single check and exit.
"""

import datetime
import json
import os
import time

from telegram_alert import load_env, send_telegram_message

HERE = os.path.dirname(os.path.abspath(__file__))
CALENDAR_JSON = os.path.join(HERE, "unlock_calendar.json")
STATE_JSON = os.path.join(HERE, "unlock_alert_state.json")

POLL_INTERVAL_SECONDS = int(os.environ.get("UNLOCK_ALERT_POLL_SECONDS", 24 * 60 * 60))
ALERT_DAYS_BEFORE = int(os.environ.get("UNLOCK_ALERT_DAYS_BEFORE", 3))


def load_calendar():
    if not os.path.exists(CALENDAR_JSON):
        return []
    with open(CALENDAR_JSON) as f:
        return json.load(f)


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

    today = datetime.date.today()
    for entry in load_calendar():
        symbol = entry["symbol"]
        unlock_date = datetime.date.fromisoformat(entry["date"])
        days_left = (unlock_date - today).days
        if days_left < 0 or days_left > ALERT_DAYS_BEFORE:
            continue  # too far out, or already passed -- nothing to say

        key = f"{symbol}:{entry['date']}:{days_left}"
        if key in state:
            continue  # already alerted this exact day-count for this entry

        when = "TODAY" if days_left == 0 else f"in {days_left} day{'s' if days_left != 1 else ''}"
        note = entry.get("note", "")
        send_telegram_message(
            f"\U0001f4c5 Token unlock reminder: {symbol} unlocks {when} ({entry['date']})."
            + (f"\n{note}" if note else ""),
            token, chat_id,
        )
        state[key] = time.time()
        print(f"[unlock] Alerted: {symbol} -- {entry['date']} ({when})")

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
