from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta

class MyStrategy(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    minimal_roi = {"0": 0.04}
    stoploss = -0.03
    startup_candle_count = 200

    def populate_indicators(self, df: DataFrame, metadata: dict) -> DataFrame:
        df["ema20"] = ta.EMA(df, timeperiod=20)
        df["ema50"] = ta.EMA(df, timeperiod=50)
        df["ema200"] = ta.EMA(df, timeperiod=200)
        df["rsi"] = ta.RSI(df, timeperiod=14)
        return df

    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        df.loc[
            (df["close"] > df["ema200"]) &
            (df["ema20"] > df["ema50"]) &
            (df["ema20"].shift(1) <= df["ema50"].shift(1)) &
            (df["rsi"] < 70) &
            (df["volume"] > 0),
            "enter_long"] = 1
        return df

    def populate_exit_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        df.loc[(df["ema20"] < df["ema50"]) | (df["rsi"] > 80), "exit_long"] = 1
        return df
