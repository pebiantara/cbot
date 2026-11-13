"""Registers (or updates) the Helius and Alchemy webhooks to point at the
current Cloudflare quick tunnel URL -- run this after every restart, since the
quick tunnel's public URL changes every time (see docker-compose.yml). Safe to
re-run any time: it updates the existing webhook in place instead of creating
duplicates, tracked via webhook_state.json.

Must run on the HOST (needs the `docker` CLI to read the tunnel URL from
cloudflared's logs), not inside a container.
"""

import csv
import json
import os
import re
import subprocess
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_CSV = os.path.join(HERE, "watchlist.csv")
STATE_JSON = os.path.join(HERE, "webhook_state.json")
ENV_PATH = os.path.join(os.path.dirname(HERE), ".env")

HELIUS_API = "https://api.helius.xyz/v0/webhooks"
# Alchemy's Notify API uses chain-specific network names in its webhook config
ALCHEMY_NETWORKS = {"ethereum": "ETH_MAINNET", "arbitrum": "ARB_MAINNET",
                     "bsc": "BNB_MAINNET", "base": "BASE_MAINNET"}
ALCHEMY_API = "https://dashboard.alchemy.com/api"


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


def load_state():
    if not os.path.exists(STATE_JSON):
        return {}
    with open(STATE_JSON) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_JSON, "w") as f:
        json.dump(state, f, indent=2)


def get_tunnel_url(container_name="cbot-cloudflared"):
    """Current public URL from cloudflared's own logs -- the quick tunnel
    prints a new one every time the container (re)starts."""
    result = subprocess.run(
        ["docker", "logs", container_name], capture_output=True, text=True, timeout=15
    )
    matches = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", result.stdout + result.stderr)
    if not matches:
        raise RuntimeError(f"No trycloudflare.com URL found in `docker logs {container_name}` -- "
                            f"is the tunnel running? (docker compose up -d cloudflared)")
    return matches[-1]  # most recent


def load_watchlist_by_chain():
    by_chain = {}
    with open(WATCHLIST_CSV, newline="") as f:
        for row in csv.DictReader(f):
            by_chain.setdefault(row["chain"], []).append(row["address"])
    return by_chain


def register_helius(tunnel_url, api_key, addresses, state):
    webhook_url = f"{tunnel_url}/webhooks/helius"
    payload = {
        "webhookURL": webhook_url,
        "transactionTypes": ["Any"],
        "accountAddresses": addresses,
        "webhookType": "enhanced",
    }
    webhook_id = state.get("helius_webhook_id")

    if webhook_id:
        resp = requests.put(f"{HELIUS_API}/{webhook_id}?api-key={api_key}", json=payload, timeout=15)
        if resp.status_code == 200:
            print(f"helius: updated webhook {webhook_id} -> {webhook_url}")
            return webhook_id
        print(f"helius: update failed ({resp.status_code}: {resp.text}) -- creating a new one")

    resp = requests.post(f"{HELIUS_API}?api-key={api_key}", json=payload, timeout=15)
    resp.raise_for_status()
    webhook_id = resp.json()["webhookID"]
    print(f"helius: created webhook {webhook_id} -> {webhook_url}")
    return webhook_id


def register_alchemy(tunnel_url, auth_token, chain, addresses, state):
    webhook_url = f"{tunnel_url}/webhooks/alchemy"
    webhook_id = state.get("alchemy_webhook_ids", {}).get(chain)
    headers = {"X-Alchemy-Token": auth_token, "Content-Type": "application/json"}

    if webhook_id:
        resp = requests.patch(
            f"{ALCHEMY_API}/update-webhook",
            headers=headers,
            json={"webhook_id": webhook_id, "webhook_url": webhook_url, "addresses": addresses},
            timeout=15,
        )
        if resp.status_code == 200:
            print(f"alchemy[{chain}]: updated webhook {webhook_id} -> {webhook_url}")
            return webhook_id
        print(f"alchemy[{chain}]: update failed ({resp.status_code}: {resp.text}) -- creating a new one")

    resp = requests.post(
        f"{ALCHEMY_API}/create-webhook",
        headers=headers,
        json={
            "network": ALCHEMY_NETWORKS[chain], "webhook_type": "ADDRESS_ACTIVITY",
            "webhook_url": webhook_url, "addresses": addresses,
        },
        timeout=15,
    )
    resp.raise_for_status()
    webhook_id = resp.json()["data"]["id"]
    print(f"alchemy[{chain}]: created webhook {webhook_id} -> {webhook_url}")
    return webhook_id


def register_telegram(tunnel_url, bot_token):
    webhook_url = f"{tunnel_url}/telegram/webhook"
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/setWebhook",
        json={"url": webhook_url}, timeout=15,
    )
    resp.raise_for_status()
    print(f"telegram: webhook set -> {webhook_url}")


def register_all(tunnel_url):
    """Create/update every webhook (Helius + Alchemy + Telegram bot commands)
    to point at tunnel_url. Shared by the manual CLI below and watch_tunnel.py's
    automatic watcher."""
    env = load_env()
    state = load_state()
    print(f"Registering tunnel URL: {tunnel_url}\n")

    watchlist = load_watchlist_by_chain()

    helius_key = env.get("HELIUS_API_KEY")
    if helius_key and watchlist.get("solana"):
        state["helius_webhook_id"] = register_helius(tunnel_url, helius_key, watchlist["solana"], state)
    else:
        print("helius: skipped (no HELIUS_API_KEY or no solana watchlist entries)")

    alchemy_token = env.get("ALCHEMY_NOTIFY_AUTH_TOKEN")
    if alchemy_token:
        state.setdefault("alchemy_webhook_ids", {})
        for chain in ALCHEMY_NETWORKS:
            if watchlist.get(chain):
                state["alchemy_webhook_ids"][chain] = register_alchemy(
                    tunnel_url, alchemy_token, chain, watchlist[chain], state
                )
    else:
        print("alchemy: skipped (no ALCHEMY_NOTIFY_AUTH_TOKEN in .env -- this is a separate "
              "token from ALCHEMY_API_KEY, found in the Alchemy dashboard's Notify tab)")

    bot_token = env.get("TELEGRAM_BOT_TOKEN")
    if bot_token:
        register_telegram(tunnel_url, bot_token)
    else:
        print("telegram: skipped (no TELEGRAM_BOT_TOKEN in .env)")

    save_state(state)


def run():
    """Manual CLI entry point: detects the tunnel URL itself via `docker logs`
    (host-only, since it needs the docker CLI)."""
    register_all(get_tunnel_url())


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(f"Failed: {e}", file=sys.stderr)
        sys.exit(1)
