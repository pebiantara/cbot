"""Standalone XGBoost classifier: P(close is >= 2% higher in 24h) for every coin
in the universe. Separate from Freqtrade's FreqAI (MyStrategyML), which only
covers the bot's 5-pair whitelist -- this one scores the full ~40-130 coin
universe for the Green Coin finder (step 14), reusing the 1h candle CSVs
history.py already downloaded and the indicator math from technical.py.
"""

import csv
import glob
import os
import sys

import joblib
import numpy as np
from xgboost import XGBClassifier

from technical import atr, ema, macd, rsi

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
MODEL_PATH = os.path.join(os.path.dirname(__file__), "ml_model.joblib")
OUTPUT_CSV = os.path.join(os.path.dirname(__file__), "ml_scores.csv")

LABEL_HORIZON = 24   # candles ahead (1h tf -> 24h)
LABEL_RISE_PCT = 2.0  # "good to trade" threshold
WARMUP = 200          # matches ema200

FEATURE_NAMES = [
    "rsi", "macd_hist", "close_ema20_ratio", "close_ema50_ratio",
    "close_ema200_ratio", "ema20_ema50_ratio", "atr_pct", "volume_ratio",
]


def load_1h_candles(symbol):
    path = os.path.join(DATA_DIR, f"{symbol}_1h.csv")
    if not os.path.exists(path):
        return None
    with open(path, newline="") as f:
        return [
            {k: float(v) for k, v in row.items() if k not in ("open_time", "close_time")}
            for row in csv.DictReader(f)
        ]


def build_features(candles):
    """Feature matrix for every candle (NaN-padded warmup/horizon rows excluded by caller)."""
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)
    rsi14 = rsi(closes, 14)
    macd_line, macd_signal = macd(closes)
    atr14 = atr(highs, lows, closes, 14)

    rows = []
    for i in range(len(candles)):
        if None in (ema20[i], ema50[i], ema200[i], rsi14[i], macd_line[i], macd_signal[i], atr14[i]):
            rows.append(None)
            continue
        window = volumes[max(0, i - 20):i + 1]
        avg_volume = sum(window) / len(window)
        rows.append({
            "rsi": rsi14[i],
            "macd_hist": macd_line[i] - macd_signal[i],
            "close_ema20_ratio": closes[i] / ema20[i],
            "close_ema50_ratio": closes[i] / ema50[i],
            "close_ema200_ratio": closes[i] / ema200[i],
            "ema20_ema50_ratio": ema20[i] / ema50[i],
            "atr_pct": atr14[i] / closes[i] * 100,
            "volume_ratio": volumes[i] / avg_volume if avg_volume else 1.0,
        })
    return rows


def build_training_set():
    """Pooled (X, y) across every symbol with cached 1h data, chronological per-symbol split."""
    X_train, y_train, X_test, y_test = [], [], [], []

    for path in sorted(glob.glob(os.path.join(DATA_DIR, "*_1h.csv"))):
        symbol = os.path.basename(path).replace("_1h.csv", "")
        candles = load_1h_candles(symbol)
        if candles is None or len(candles) < WARMUP + LABEL_HORIZON + 20:
            continue

        feature_rows = build_features(candles)
        closes = [c["close"] for c in candles]

        usable = []
        for i in range(WARMUP, len(candles) - LABEL_HORIZON):
            if feature_rows[i] is None:
                continue
            future_return_pct = (closes[i + LABEL_HORIZON] / closes[i] - 1) * 100
            label = 1 if future_return_pct >= LABEL_RISE_PCT else 0
            usable.append(([feature_rows[i][f] for f in FEATURE_NAMES], label))

        if len(usable) < 20:
            continue

        split = int(len(usable) * 0.8)
        for x, y in usable[:split]:
            X_train.append(x)
            y_train.append(y)
        for x, y in usable[split:]:
            X_test.append(x)
            y_test.append(y)

    return np.array(X_train), np.array(y_train), np.array(X_test), np.array(y_test)


def train():
    X_train, y_train, X_test, y_test = build_training_set()
    print(f"Training set: {len(X_train)} rows ({y_train.mean():.1%} positive)")
    print(f"Test set: {len(X_test)} rows ({y_test.mean():.1%} positive)")

    model = XGBClassifier(
        n_estimators=100, max_depth=3, learning_rate=0.1,
        eval_metric="logloss", random_state=42,
    )
    model.fit(X_train, y_train)

    train_acc = model.score(X_train, y_train)
    test_acc = model.score(X_test, y_test)
    print(f"Train accuracy: {train_acc:.3f}  |  Test accuracy: {test_acc:.3f}")
    print(f"Baseline (always predict majority class): {max(1 - y_test.mean(), y_test.mean()):.3f}")

    joblib.dump(model, MODEL_PATH)
    print(f"Saved model to {MODEL_PATH}")
    return model


def load_model():
    if not os.path.exists(MODEL_PATH):
        return None
    return joblib.load(MODEL_PATH)


def score_latest(symbol, model=None):
    """P(close rises >= LABEL_RISE_PCT% in the next LABEL_HORIZON candles), or None."""
    model = model or load_model()
    if model is None:
        return None
    candles = load_1h_candles(symbol)
    if candles is None or len(candles) < WARMUP:
        return None
    feature_rows = build_features(candles)
    latest = feature_rows[-1]
    if latest is None:
        return None
    x = np.array([[latest[f] for f in FEATURE_NAMES]])
    return float(model.predict_proba(x)[0][1])


if __name__ == "__main__":
    # --predict-only: re-score every symbol's already-downloaded candles against
    # the existing saved model, skipping train() -- used by the /now refresh path
    # (webhook_server.py) so ml_score isn't left stale between hourly retrains.
    predict_only = "--predict-only" in sys.argv
    if predict_only:
        model = load_model()
        if model is None:
            print(f"No saved model at {MODEL_PATH}, nothing to score -- run without --predict-only first")
            sys.exit(1)
    else:
        model = train()

    rows = []
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "*_1h.csv"))):
        symbol = os.path.basename(path).replace("_1h.csv", "")
        ml_score = score_latest(symbol, model=model)
        if ml_score is not None:
            rows.append({"symbol": symbol, "ml_score": round(ml_score, 4)})

    rows.sort(key=lambda r: r["ml_score"], reverse=True)
    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["symbol", "ml_score"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {len(rows)} coins to {OUTPUT_CSV}")
