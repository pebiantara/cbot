import json
import os

from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta

# user_data/strategies/ -> project root -> coin_finder/green_list.json
GREEN_LIST_JSON = os.path.join(
    os.path.dirname(__file__), "..", "..", "coin_finder", "green_list.json"
)


def load_green_symbols():
    """{"BTCUSDT", ...} -- symbols currently labeled GREEN. Empty set if the
    file is missing or stale data hasn't produced any GREEN coins yet."""
    if not os.path.exists(GREEN_LIST_JSON):
        return set()
    with open(GREEN_LIST_JSON) as f:
        payload = json.load(f)
    return {c["symbol"] for c in payload["coins"] if c["label"] == "GREEN"}


class MyStrategyGreen(IStrategy):
    """Same FreqAI model as MyStrategyML, gated by coin_finder/green_list.json:
    only enters a pair when combine.py currently labels it GREEN (technical +
    ml + whale score all agreeing). Until the whale scanner has real API keys
    and enough history, whale_score stays neutral everywhere and no coin can
    reach GREEN -- so this strategy is expected to trade nothing yet. That's
    intentional: it's safer to enter no trades than to enter on a fake signal.
    """

    INTERFACE_VERSION = 3
    timeframe = "1h"
    minimal_roi = {"0": 0.04}
    stoploss = -0.03
    startup_candle_count = 200
    can_short = False

    entry_threshold = 0.02
    exit_threshold = 0.0

    @property
    def protections(self):
        """Safety rails before this strategy trades anything for real: a
        losing streak or a drawdown pauses new entries instead of continuing
        to open trades blindly. config.json's "protections" key is deprecated
        in this Freqtrade version -- must be set here instead."""
        return [
            {
                "method": "CooldownPeriod",
                "stop_duration_candles": 5,
            },
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 24,
                "trade_limit": 2,
                "stop_duration_candles": 12,
                "only_per_pair": False,
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 48,
                "trade_limit": 10,
                "stop_duration_candles": 24,
                "max_allowed_drawdown": 0.15,
            },
        ]

    def feature_engineering_expand_all(self, dataframe: DataFrame, period, metadata, **kwargs) -> DataFrame:
        dataframe["%-rsi-period"] = ta.RSI(dataframe, timeperiod=period)
        dataframe["%-ema-period"] = ta.EMA(dataframe, timeperiod=period)
        dataframe["%-close-ema-ratio-period"] = dataframe["close"] / dataframe["%-ema-period"]
        dataframe["%-atr-period"] = ta.ATR(dataframe, timeperiod=period)
        dataframe["%-atr-pct-period"] = dataframe["%-atr-period"] / dataframe["close"]
        return dataframe

    def feature_engineering_expand_basic(self, dataframe: DataFrame, metadata, **kwargs) -> DataFrame:
        macd = ta.MACD(dataframe)
        dataframe["%-macd"] = macd["macd"]
        dataframe["%-macd-signal"] = macd["macdsignal"]
        dataframe["%-pct-change"] = dataframe["close"].pct_change()
        dataframe["%-volume-pct-change"] = dataframe["volume"].pct_change()
        return dataframe

    def feature_engineering_standard(self, dataframe: DataFrame, metadata, **kwargs) -> DataFrame:
        dataframe["%-day_of_week"] = (dataframe["date"].dt.dayofweek + 1) / 7
        dataframe["%-hour_of_day"] = (dataframe["date"].dt.hour + 1) / 25
        return dataframe

    def set_freqai_targets(self, dataframe: DataFrame, metadata, **kwargs) -> DataFrame:
        label_period = self.freqai_info["feature_parameters"]["label_period_candles"]
        dataframe["&-future_return"] = (
            dataframe["close"].shift(-label_period) / dataframe["close"] - 1
        )
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe = self.freqai.start(dataframe, metadata, self)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair_symbol = metadata["pair"].replace("/", "")
        if pair_symbol not in load_green_symbols():
            return dataframe  # no enter_long column set -> no entries for this pair

        dataframe.loc[
            (dataframe["do_predict"] == 1) &
            (dataframe["&-future_return"] > self.entry_threshold) &
            (dataframe["close"] > dataframe["ema200"]) &
            (dataframe["volume"] > 0),
            "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["do_predict"] == 1) &
            (dataframe["&-future_return"] < self.exit_threshold),
            "exit_long"] = 1
        return dataframe
