"""Runs the full Green Coin pipeline on a loop: candle history -> technical
scores -> whale chain scans -> whale_score -> ml_score -> combine -> accuracy
evaluation -> morning-picks check-in -> Telegram alert. Intended as the
`pipeline` service in docker-compose.yml so the whole thing runs with
`docker compose up` instead of calling each script by hand from a local venv.

Set RUN_ONCE=1 to run through the steps once and exit (useful for testing).
Set PIPELINE_INTERVAL_SECONDS to change the loop cadence (default 3600 = 1h).
morning_report.py only actually sends a Telegram message when a new 2h/4h/.../1d
checkpoint resolves for the day's first run, so it's safe to run every hour.
"""

import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COIN_FINDER = os.path.join(ROOT, "coin_finder")
WHALE_SCANNER = os.path.join(ROOT, "whale_scanner")

INTERVAL_SECONDS = int(os.environ.get("PIPELINE_INTERVAL_SECONDS", 3600))

STEPS = [
    (COIN_FINDER, "history.py"),
    (COIN_FINDER, "technical.py"),
    (WHALE_SCANNER, "chains/evm.py"),
    (WHALE_SCANNER, "chains/solana.py"),
    (WHALE_SCANNER, "score.py"),
    (COIN_FINDER, "ml_model.py"),
    (COIN_FINDER, "combine.py"),
    (COIN_FINDER, "evaluate_accuracy.py"),
    # morning_report.py and telegram_alert.py removed 2026-11-04 -- user wants
    # system 1 used on-demand via Telegram commands (/now, /now_<x>x,
    # /status_accuracy, /list) only, not automatic pushes. Neither command
    # depends on these two scripts' own state/alert-sending -- /status_accuracy
    # reads accuracy_report.json (still written by evaluate_accuracy.py above),
    # /list reads predictions.db directly -- so removing them doesn't break
    # anything on-demand, just stops the unsolicited hourly digest + 2h-checkpoint
    # push and the "just turned GREEN" ping.
]


def run_step(cwd, script):
    print(f"\n=== {script} ===", flush=True)
    result = subprocess.run([sys.executable, script], cwd=cwd)
    if result.returncode != 0:
        print(f"!! {script} exited with code {result.returncode} -- continuing", flush=True)


def run_once():
    for cwd, script in STEPS:
        run_step(cwd, script)


if __name__ == "__main__":
    run_once()
    if os.environ.get("RUN_ONCE"):
        sys.exit(0)
    while True:
        print(f"\nSleeping {INTERVAL_SECONDS}s until next run...", flush=True)
        time.sleep(INTERVAL_SECONDS)
        run_once()
