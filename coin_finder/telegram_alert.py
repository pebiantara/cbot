"""Sends Telegram alerts from green_list.json: a top-N digest every run, plus
a one-off "just turned GREEN" ping when a coin's label flips (previous run's
labels kept in green_list_prev.json to detect that transition).

Run this right after combine.py (e.g. both on the same hourly cron/schedule).
"""

import datetime
import json
import os
import time

import requests

from entry_price import format_price

TOP_N = 10
LABEL_EMOJI = {"GREEN": "\U0001f7e2", "YELLOW": "\U0001f7e1", "RED": "\U0001f534"}

HERE = os.path.dirname(os.path.abspath(__file__))
GREEN_LIST_JSON = os.path.join(HERE, "green_list.json")
PREV_LABELS_JSON = os.path.join(HERE, "green_list_prev.json")
ENV_PATH = os.path.join(os.path.dirname(HERE), ".env")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def load_env(path=ENV_PATH):
    env = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def load_green_list():
    with open(GREEN_LIST_JSON) as f:
        return json.load(f)


def load_current_labels():
    return {c["symbol"]: c["label"] for c in load_green_list()["coins"]}


def build_digest_message(payload, top_n=TOP_N):
    """Only actionable coins (GREEN=LONG, RED=SHORT-bias) with entry/TP/SL --
    YELLOW (mixed signals, no trade) is deliberately left out."""
    generated_at = datetime.datetime.fromtimestamp(payload["generated_at"])
    coins = payload["coins"]
    actionable = [c for c in coins if c["label"] in ("GREEN", "RED")]
    actionable.sort(key=lambda c: c["final_score"], reverse=True)
    actionable = actionable[:top_n]

    lines = [f"\U0001f4ca Trade signals ({generated_at:%Y-%m-%d %H:%M} UTC)"]
    if not actionable:
        lines.append(f"\nNo clear LONG or SHORT signals right now "
                      f"({len(coins)} coins, all YELLOW/mixed).")
        return "\n".join(lines)

    for coin in actionable:
        entry = coin["entry"]
        emoji = LABEL_EMOJI.get(coin["label"], "")
        lines.append(f"\n{emoji} {coin['direction']} {coin['symbol']}  (price now: {format_price(entry['current_price'])})")
        if entry["at_entry_now"]:
            lines.append(f"Entry: {format_price(entry['entry_low'])} - {format_price(entry['entry_high'])}  (at entry zone now)")
            lines.append(f"TP: {format_price(entry['take_profit'])}  SL: {format_price(entry['stop_loss'])}")
        else:
            lines.append(f"Pullback entry: {format_price(entry['entry_low'])} - {format_price(entry['entry_high'])} "
                          f"({entry['distance_from_entry_pct']:+.1f}% away, may not get hit)")
            lines.append(f"  -> TP {format_price(entry['take_profit'])}  SL {format_price(entry['stop_loss'])}")
            lines.append(f"If buying now at {format_price(entry['current_price'])} instead:")
            lines.append(f"  -> TP {format_price(entry['if_buy_now_take_profit'])}  SL {format_price(entry['if_buy_now_stop_loss'])}")
    return "\n".join(lines)


def load_previous_labels():
    if not os.path.exists(PREV_LABELS_JSON):
        return {}
    with open(PREV_LABELS_JSON) as f:
        return json.load(f)


def save_labels(labels):
    with open(PREV_LABELS_JSON, "w") as f:
        json.dump(labels, f)


def newly_green(previous, current):
    """Symbols that are GREEN now but weren't GREEN in the previous run."""
    return sorted(
        symbol for symbol, label in current.items()
        if label == "GREEN" and previous.get(symbol) != "GREEN"
    )


TELEGRAM_SEND_RETRIES = 3  # added 2026-11-12 -- a real alert was seen dropped
TELEGRAM_SEND_RETRY_BACKOFF_SECONDS = 0.5  # to a transient SSLEOFError, not a real Telegram failure


def send_telegram_message(text, token, chat_id, parse_mode=None):
    """parse_mode="HTML" lets a caller use <b>/<i> etc. for readability --
    opt-in per call (not default) so existing plain-text callers aren't
    newly exposed to Telegram's HTML entity-escaping rules (stray </>/&
    in message text would need escaping once HTML parsing is on).

    Retries transient network errors (connection reset, SSL EOF) before
    giving up -- an alert actually failing to send due to a network blip
    is worse than any other failure mode here, unlike a scan cycle
    finding nothing."""
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    last_error = None
    for attempt in range(TELEGRAM_SEND_RETRIES):
        try:
            resp = requests.post(
                TELEGRAM_API.format(token=token),
                json=payload,
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < TELEGRAM_SEND_RETRIES - 1:
                time.sleep(TELEGRAM_SEND_RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise last_error


def run():
    if not os.path.exists(GREEN_LIST_JSON):
        raise RuntimeError("No green_list.json found -- run combine.py first")

    payload = load_green_list()
    current = {c["symbol"]: c["label"] for c in payload["coins"]}
    previous = load_previous_labels()
    newly_green_coins = newly_green(previous, current)

    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID")

    result = {"digest_sent": False, "green_alerts_sent": []}
    if not (token and chat_id):
        print("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID aren't set in .env -- skipping Telegram sends")
        save_labels(current)
        return result

    send_telegram_message(build_digest_message(payload), token, chat_id)
    result["digest_sent"] = True

    for symbol in newly_green_coins:
        send_telegram_message(f"\U0001f7e2 {symbol} just turned GREEN", token, chat_id)
        result["green_alerts_sent"].append(symbol)

    save_labels(current)
    return result


if __name__ == "__main__":
    result = run()
    print(f"Digest sent: {result['digest_sent']}")
    if result["green_alerts_sent"]:
        print(f"New GREEN alerts sent: {result['green_alerts_sent']}")
