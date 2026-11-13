"""Map on-chain tokens (chain + contract address) to their Binance symbol.

Tokens not in token_map.csv aren't listed on Binance (or just not mapped yet) --
callers should treat those as "watch only": keep the event, but don't price it.
"""

import csv
import os

TOKEN_MAP_CSV = os.path.join(os.path.dirname(__file__), "token_map.csv")


def load_token_map(path=TOKEN_MAP_CSV):
    """{(chain, token_address.lower()): binance_symbol}"""
    mapping = {}
    if not os.path.exists(path):
        return mapping
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            mapping[(row["chain"], row["token_address"].lower())] = row["binance_symbol"]
    return mapping


_MAP = load_token_map()


def to_binance_symbol(chain, token_address):
    """Binance symbol for this on-chain token, or None if it isn't mapped (watch-only)."""
    return _MAP.get((chain, token_address.lower()))
