"""Self-reported open-position ledger, used to cap total portfolio exposure
across all three signal systems (System 1 /now_<x>x, System 2 /futures_<x>,
System 3 /fut_<symbol>_<leverage>).

None of the systems here are connected to a Binance API key -- they're
signal-only, you execute trades manually -- so there's no way to know your
real open positions automatically. Instead you tell the bot when you open/
close a trade (see webhook_server.py's /open, /close, /positions Telegram
commands), and position_sizing() in leverage_risk.py checks the running
total against MAX_PORTFOLIO_ALLOCATION_PCT before suggesting a new size.
Accuracy depends entirely on you keeping this up to date -- it's a guard
rail, not a source of truth.
"""

import json
import os
import time

LEDGER_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio_ledger.json")


def _load():
    if not os.path.exists(LEDGER_JSON):
        return {}
    with open(LEDGER_JSON) as f:
        return json.load(f)


def _save(positions):
    with open(LEDGER_JSON, "w") as f:
        json.dump(positions, f)


def record_open(symbol, margin_usd):
    """Adds/overwrites this symbol's committed margin (re-opening the same
    symbol replaces, not adds -- you're telling the bot the current state,
    not logging a transaction history)."""
    positions = _load()
    positions[symbol] = {"margin_usd": margin_usd, "opened_at": time.time()}
    _save(positions)
    return positions


def record_close(symbol):
    """Removes the symbol if present. Returns True if it was actually open."""
    positions = _load()
    existed = positions.pop(symbol, None) is not None
    _save(positions)
    return existed


def list_open_positions():
    return _load()


def total_committed_margin():
    return sum(p["margin_usd"] for p in _load().values())
