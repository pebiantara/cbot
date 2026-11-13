"""Resolves matured predictions against the real 1h/4h/1d candles and reports
how often the GREEN/YELLOW/RED calls (and the ml_score threshold) were actually
right -- a "morning review": run this any time, it only ever reports on
predictions whose horizon has already passed.
"""

import json
import os

from outcomes import HORIZONS, get_connection, resolve_matured

REPORT_JSON = os.path.join(os.path.dirname(__file__), "accuracy_report.json")


def build_report(conn=None):
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()

    report = {"by_horizon": {}}
    for horizon in HORIZONS:
        rows = conn.execute(
            """SELECT p.label, p.ml_score, o.return_pct, o.hit
               FROM outcomes o JOIN predictions p ON p.id = o.prediction_id
               WHERE o.horizon = ?""",
            (horizon,),
        ).fetchall()

        if not rows:
            report["by_horizon"][horizon] = {"resolved": 0}
            continue

        by_label = {}
        for label, ml_score, return_pct, hit in rows:
            bucket = by_label.setdefault(label, {"count": 0, "hits": 0, "return_sum": 0.0})
            bucket["count"] += 1
            bucket["hits"] += hit
            bucket["return_sum"] += return_pct

        label_stats = {
            label: {
                "count": b["count"],
                "hit_rate": round(b["hits"] / b["count"], 4),
                "avg_return_pct": round(b["return_sum"] / b["count"], 4),
            }
            for label, b in by_label.items()
        }

        horizon_report = {"resolved": len(rows), "by_label": label_stats}

        if horizon == "1d":
            # the ml_score classifier was trained specifically to predict this
            # horizon (rise >= 2% in 24h), so its threshold accuracy only means
            # something here.
            tp = sum(1 for _, ml_score, _, hit in rows if ml_score >= 0.5 and hit)
            fp = sum(1 for _, ml_score, _, hit in rows if ml_score >= 0.5 and not hit)
            fn = sum(1 for _, ml_score, _, hit in rows if ml_score < 0.5 and hit)
            tn = sum(1 for _, ml_score, _, hit in rows if ml_score < 0.5 and not hit)
            total = tp + fp + fn + tn
            horizon_report["ml_score_threshold_0.5"] = {
                "accuracy": round((tp + tn) / total, 4) if total else None,
                "precision": round(tp / (tp + fp), 4) if (tp + fp) else None,
                "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            }

        report["by_horizon"][horizon] = horizon_report

    if owns_conn:
        conn.close()
    return report


if __name__ == "__main__":
    conn = get_connection()
    resolved_now = resolve_matured(conn=conn)
    report = build_report(conn=conn)
    conn.close()

    with open(REPORT_JSON, "w") as f:
        json.dump(report, f, indent=2)

    print(f"Resolved {resolved_now} newly matured prediction/horizon pairs this run.\n")
    for horizon, data in report["by_horizon"].items():
        if data["resolved"] == 0:
            print(f"{horizon}: no matured predictions yet")
            continue
        print(f"{horizon}: {data['resolved']} resolved")
        for label, stats in data["by_label"].items():
            print(f"  {label:<8} n={stats['count']:<4} "
                  f"hit_rate={stats['hit_rate']:.1%}  avg_return={stats['avg_return_pct']:+.2f}%")
        if "ml_score_threshold_0.5" in data:
            m = data["ml_score_threshold_0.5"]
            print(f"  ml_score>=0.5 accuracy={m['accuracy']}  precision={m['precision']}  "
                  f"(tp={m['tp']} fp={m['fp']} fn={m['fn']} tn={m['tn']})")
        print()
