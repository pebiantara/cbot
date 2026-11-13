"""One-off retroactive check: for every historical GREEN prediction already
logged in predictions.db, recompute extension_above_vah_pct using the candle
data available at that snapshot time, and see whether the new MAX_EXTENSION_
ABOVE_VAH_PCT gate in combine.py would have filtered it out -- then compare
real outcomes (outcomes.py) between the kept and filtered groups.
"""

import os
import sqlite3

from combine import MAX_EXTENSION_ABOVE_VAH_PCT
from outcomes import DB_PATH, HORIZONS, _load_candles
from volume_profile import volume_profile

VP_LOOKBACK_CANDLES = 100


def extension_at(symbol, snapshot_time, close_price):
    candles = _load_candles(symbol, "1h")
    prior = [c for c in candles if c["open_time"] <= snapshot_time]
    if len(prior) < 20:
        return None

    # _load_candles only keeps open_time/close -- reload with high/low/volume
    # for the volume profile calc.
    path = os.path.join(os.path.dirname(__file__), "data", f"{symbol}_1h.csv")
    import csv
    with open(path, newline="") as f:
        full = [
            {"open_time": int(r["open_time"]) / 1000, "high": float(r["high"]),
             "low": float(r["low"]), "volume": float(r["volume"])}
            for r in csv.DictReader(f)
        ]
    window = [c for c in full if c["open_time"] <= snapshot_time][-VP_LOOKBACK_CANDLES:]
    vp = volume_profile(window)
    if vp is None:
        return None
    return max(0.0, (close_price - vp["vah"]) / vp["vah"] * 100)


def main():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, symbol, snapshot_time, close_price FROM predictions WHERE label='GREEN'"
    ).fetchall()

    kept, filtered = [], []
    for pred_id, symbol, snapshot_time, close_price in rows:
        ext = extension_at(symbol, snapshot_time, close_price)
        (filtered if ext is not None and ext > MAX_EXTENSION_ABOVE_VAH_PCT else kept).append(
            (pred_id, symbol, ext)
        )

    print(f"GREEN predictions: {len(rows)} total | kept: {len(kept)} | filtered out: {len(filtered)}\n")
    print("Filtered out (extension_above_vah_pct > %.1f%%):" % MAX_EXTENSION_ABOVE_VAH_PCT)
    for pred_id, symbol, ext in filtered:
        print(f"  id={pred_id:<5} {symbol:<10} extension={ext:.2f}%")
    print("\nKept GREEN:")
    for pred_id, symbol, ext in kept:
        print(f"  id={pred_id:<5} {symbol:<10} extension={('%.2f%%' % ext) if ext is not None else 'n/a'}")

    def avg_outcomes(pred_ids, horizon):
        if not pred_ids:
            return None
        rows = conn.execute(
            f"""SELECT return_pct, hit FROM outcomes
                WHERE horizon=? AND prediction_id IN ({','.join('?' * len(pred_ids))})""",
            [horizon, *pred_ids],
        ).fetchall()
        if not rows:
            return None
        n = len(rows)
        avg_return = sum(r[0] for r in rows) / n
        hit_rate = sum(r[1] for r in rows) / n
        return n, avg_return, hit_rate

    kept_ids = [p[0] for p in kept]
    filtered_ids = [p[0] for p in filtered]

    print(f"\n{'horizon':<8}{'kept n/avg_ret/hit':<28}{'filtered n/avg_ret/hit'}")
    for horizon in HORIZONS:
        k = avg_outcomes(kept_ids, horizon)
        f = avg_outcomes(filtered_ids, horizon)
        k_str = f"n={k[0]} {k[1]:+.2f}% {k[2]:.0%}" if k else "no data"
        f_str = f"n={f[0]} {f[1]:+.2f}% {f[2]:.0%}" if f else "no data"
        print(f"{horizon:<8}{k_str:<28}{f_str}")

    conn.close()


if __name__ == "__main__":
    main()
