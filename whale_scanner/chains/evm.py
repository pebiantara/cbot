"""EVM chain whale wallet tracker: Ethereum, Arbitrum, BSC, Base.

Watchlist mode: for each wallet in watchlist.csv, pull its recent ERC-20 transfers
and save them as whale events. Two providers because Etherscan V2 is free for
Ethereum/Arbitrum but paid-only for BSC/Base (see CLAUDE.md) -- those two go
through Alchemy instead (free tier, one API key covers both chains). Both
providers get normalized into the same raw-transfer shape before parse_transfer()
runs, so the rest of the pipeline doesn't care which provider a chain uses.
"""

import csv
import datetime
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # whale_scanner/
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "coin_finder"))
from store import get_connection, market_side, save_whale_event  # noqa: E402
from prices import token_usd_value  # noqa: E402
from token_map import to_binance_symbol  # noqa: E402

WATCHLIST_CSV = os.path.join(HERE, "watchlist.csv")
ENV_PATH = os.path.join(os.path.dirname(HERE), ".env")

ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
ALCHEMY_URL = "https://{network_slug}-mainnet.g.alchemy.com/v2/{api_key}"

# chain -> which provider + the id/param that provider needs for that chain
CHAIN_CONFIG = {
    "ethereum": {"provider": "etherscan", "chainid": 1},
    "arbitrum": {"provider": "etherscan", "chainid": 42161},
    "bsc": {"provider": "alchemy", "network_slug": "bnb"},
    "base": {"provider": "alchemy", "network_slug": "base"},
}

REQUIRED_ENV_KEY = {"etherscan": "ETHERSCAN_API_KEY", "alchemy": "ALCHEMY_API_KEY"}


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


def load_watchlist(chain, path=WATCHLIST_CSV):
    """watchlist.csv columns: chain,address,label,source"""
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return [row for row in csv.DictReader(f) if row["chain"] == chain]


def fetch_etherscan_transfers(chainid, address, api_key, start_block=0):
    """Etherscan V2 tokentx -> already in the common raw-transfer shape."""
    params = {
        "chainid": chainid,
        "module": "account",
        "action": "tokentx",
        "address": address,
        "startblock": start_block,
        "endblock": 99999999,
        "sort": "desc",
        "apikey": api_key,
    }
    resp = requests.get(ETHERSCAN_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "1":
        return []
    return data["result"]


def _fetch_alchemy_direction(url, headers, address, direction_param):
    """One alchemy_getAssetTransfers call, filtered to transfers where `address`
    is the sender (direction_param="fromAddress") or recipient ("toAddress")."""
    body = {
        "jsonrpc": "2.0",
        "method": "alchemy_getAssetTransfers",
        "params": [{
            direction_param: address,
            "category": ["erc20"],
            "withMetadata": True,
            "excludeZeroValue": True,
            "maxCount": "0x3e8",
        }],
        "id": 1,
    }
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("result", {}).get("transfers", [])


def fetch_alchemy_transfers(network_slug, address, api_key):
    """Alchemy alchemy_getAssetTransfers, normalized into the same shape Etherscan
    returns. Two calls per wallet (as sender, as recipient) since the API filters
    by one direction at a time; duplicates (e.g. a wallet transferring to itself)
    are harmless -- save_whale_event ignores them via the (chain, tx_hash) unique
    constraint."""
    url = ALCHEMY_URL.format(network_slug=network_slug, api_key=api_key)
    headers = {"accept": "application/json", "content-type": "application/json"}

    raw = []
    for direction_param in ("fromAddress", "toAddress"):
        for t in _fetch_alchemy_direction(url, headers, address, direction_param):
            contract_address = t["rawContract"]["address"]
            raw_value = t["rawContract"]["value"]
            decimal_hex = t["rawContract"]["decimal"]
            if contract_address is None or raw_value is None or decimal_hex is None:
                continue  # not a priced ERC-20 transfer (e.g. a token without decimals metadata)
            timestamp = datetime.datetime.fromisoformat(
                t["metadata"]["blockTimestamp"].replace("Z", "+00:00")
            ).timestamp()
            raw.append({
                "to": t["to"],
                "from": t["from"],
                "value": str(int(raw_value, 16)),
                "tokenDecimal": str(int(decimal_hex, 16)),
                "tokenSymbol": t["asset"],
                "contractAddress": contract_address,
                "timeStamp": str(int(timestamp)),
                "hash": t["hash"],
            })
    return raw


def fetch_token_transfers(chain, address, api_key):
    config = CHAIN_CONFIG[chain]
    if config["provider"] == "etherscan":
        return fetch_etherscan_transfers(config["chainid"], address, api_key)
    return fetch_alchemy_transfers(config["network_slug"], address, api_key)


def parse_transfer(chain, wallet_row, tx):
    """Common raw-transfer shape -> common whale event format."""
    wallet = wallet_row["address"].lower()
    amount = int(tx["value"]) / (10 ** int(tx["tokenDecimal"]))
    raw_side = "BUY" if tx["to"].lower() == wallet else "SELL"
    side = market_side(raw_side, wallet_row)
    token_address = tx["contractAddress"]
    # Falls back to the raw on-chain symbol (e.g. "USDC") when unmapped -- those
    # stay watch-only and never join into combine.py's Binance-symbol-keyed
    # whale_score lookup, by design (see token_map.csv / CLAUDE.md step 9).
    binance_symbol = to_binance_symbol(chain, token_address)
    usd_value = token_usd_value(chain, token_address, amount)
    return {
        "time": float(tx["timeStamp"]),
        "chain": chain,
        "wallet": wallet,
        "wallet_label": wallet_row.get("label"),
        "token_symbol": binance_symbol or tx["tokenSymbol"],
        "token_address": token_address,
        "side": side,
        "amount": amount,
        "usd_value": usd_value if usd_value is not None else 0.0,
        "tx_hash": tx["hash"],
    }


def scan(chain, conn=None, api_key=None):
    """Fetch token transfers for every watchlist wallet on `chain`, save new ones."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    provider = CHAIN_CONFIG[chain]["provider"]
    env_key = REQUIRED_ENV_KEY[provider]
    api_key = api_key or load_env().get(env_key)
    if not api_key:
        raise RuntimeError(f"{env_key} not set — add it to .env in the project root")

    saved = 0
    for wallet_row in load_watchlist(chain):
        for tx in fetch_token_transfers(chain, wallet_row["address"], api_key):
            save_whale_event(parse_transfer(chain, wallet_row, tx), conn=conn)
            saved += 1
        time.sleep(0.25)  # stay under free-tier rate limits

    if owns_conn:
        conn.close()
    return saved


if __name__ == "__main__":
    for chain in CHAIN_CONFIG:
        try:
            count = scan(chain)
            print(f"{chain}: scanned watchlist, {count} transfers saved (duplicates ignored)")
        except RuntimeError as e:
            print(f"{chain}: skipped — {e}")
