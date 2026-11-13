"""Streamlit dashboard: the Green Coin finder's combined output (technical +
ml + whale scores -> final_score -> GREEN/YELLOW/RED), read straight from
green_list.json (written by combine.py)."""

import datetime
import json
import os

import pandas as pd
import streamlit as st

from entry_price import format_price
from universe import STABLECOIN_SYMBOLS

GREEN_LIST_JSON = os.path.join(os.path.dirname(__file__), "green_list.json")
ACCURACY_REPORT_JSON = os.path.join(os.path.dirname(__file__), "accuracy_report.json")

STABLECOINS = {f"{sym}USDT" for sym in STABLECOIN_SYMBOLS}
LABEL_COLORS = {"GREEN": "#1a7f37", "YELLOW": "#9a6700", "RED": "#cf222e"}

st.set_page_config(page_title="Coin Finder", layout="wide")
st.title("Coin Finder — Green Coin List")

if not os.path.exists(GREEN_LIST_JSON):
    st.warning("No green_list.json found. Run `python combine.py` first "
               "(after technical.py / score.py / ml_model.py).")
    st.stop()

with open(GREEN_LIST_JSON) as f:
    payload = json.load(f)

generated_at = datetime.datetime.fromtimestamp(payload["generated_at"])
df = pd.DataFrame(payload["coins"])


def flatten_entry(row):
    entry = row.get("entry")
    if entry is None:
        reason = "RED, avoid" if row.get("label") == "RED" else "no support below price"
        return pd.Series({
            "entry_zone": reason,
            "stop_loss": "--", "take_profit": "--", "buy_now": "No",
        })
    buy_now = "Yes" if entry["at_entry_now"] else f"Wait ({entry['distance_from_entry_pct']:+.1f}% away)"
    return pd.Series({
        "entry_zone": f"{format_price(entry['entry_low'])} - {format_price(entry['entry_high'])}",
        "stop_loss": format_price(entry["stop_loss"]),
        "take_profit": format_price(entry["take_profit"]),
        "buy_now": buy_now,
    })


df = pd.concat([df.drop(columns=["entry"]), df.apply(flatten_entry, axis=1)], axis=1)

hide_stablecoins = st.checkbox(
    "Hide stablecoin pairs (near-zero volatility skews the score)", value=True
)
if hide_stablecoins:
    df = df[~df["symbol"].isin(STABLECOINS)]

counts = df["label"].value_counts()
cols = st.columns(3)
for col, label in zip(cols, ["GREEN", "YELLOW", "RED"]):
    col.metric(label, int(counts.get(label, 0)))

st.caption(f"{len(df)} coins, generated {generated_at:%Y-%m-%d %H:%M:%S}, "
           f"sorted by final_score (highest first)")


def style_label(label):
    color = LABEL_COLORS.get(label, "inherit")
    return f"color: {color}; font-weight: 600"


styled = df.sort_values("final_score", ascending=False).reset_index(drop=True)
st.dataframe(
    styled.style.map(style_label, subset=["label"]),
    width="stretch",
    hide_index=True,
)

st.divider()
st.subheader("Prediction accuracy (real outcomes, not backtest)")

if not os.path.exists(ACCURACY_REPORT_JSON):
    st.info("No accuracy_report.json yet. Run `python evaluate_accuracy.py` -- "
            "it only reports once predictions are old enough (1h/4h/1d) to check "
            "against what actually happened.")
else:
    with open(ACCURACY_REPORT_JSON) as f:
        accuracy = json.load(f)

    horizon_cols = st.columns(len(accuracy["by_horizon"]))
    for col, (horizon, data) in zip(horizon_cols, accuracy["by_horizon"].items()):
        with col:
            st.markdown(f"**{horizon}**")
            if data["resolved"] == 0:
                st.caption("no matured predictions yet")
                continue
            st.caption(f"{data['resolved']} resolved")
            for label, stats in data["by_label"].items():
                color = LABEL_COLORS.get(label, "inherit")
                st.markdown(
                    f"<span style='color:{color};font-weight:600'>{label}</span> "
                    f"n={stats['count']} hit_rate={stats['hit_rate']:.0%} "
                    f"avg_return={stats['avg_return_pct']:+.2f}%",
                    unsafe_allow_html=True,
                )
            if "ml_score_threshold_0.5" in data:
                m = data["ml_score_threshold_0.5"]
                st.caption(f"ml_score>=0.5: accuracy={m['accuracy']}, precision={m['precision']}")
