~# Crypto Bot Project — Context for Claude

## Goal
Build a crypto trading app on my laptop that:
1. Pulls price history (candles) from Binance.
2. Predicts which coins are good to trade (ML model).
3. Runs a "whale scanner" that tracks very large trades and uses them in the predictions.
4. Trades automatically with Freqtrade, starting in dry-run mode (fake money).

Important: predictions are signals, not guarantees. Always backtest and dry-run before real money.
Never use leverage at the start. Any API key must have withdrawals OFF.

## Chosen stack
- Freqtrade running in Docker (Docker Desktop)
- Strategy in Python: `user_data/strategies/MyStrategy.py`
- Later: FreqAI (XGBoost) for predictions, a separate whale scanner script, Telegram alerts
- Dashboard: FreqUI at http://localhost:8080

## Roadmap
- [ ] Phase 1: Install Docker, set up Freqtrade, download data, backtest MyStrategy, start dry-run
- [ ] Phase 2: Add FreqAI (XGBoost) prediction model
- [ ] Phase 3: Whale scanner (Binance aggTrade websocket, trades >= $250k) -> whale score per coin -> strategy only buys when whale score is positive
- [ ] Phase 3b: Multi-chain on-chain whale scanner (Ethereum, BSC, Base, Arbitrum, Solana). See section below.
- [ ] Phase 4: Telegram alerts; after weeks of good dry-run results, a small live test

## Phase 1 steps

1. Install Docker Desktop and make sure it's running.
2. In the project folder:
```bash
curl.exe https://raw.githubusercontent.com/freqtrade/freqtrade/stable/docker-compose.yml -o docker-compose.yml
docker compose pull
docker compose run --rm freqtrade create-userdir --userdir user_data
docker compose run --rm freqtrade new-config --config user_data/config.json
```
new-config answers: dry run Yes, stake currency USDT, timeframe 1h, exchange binance,
API keys empty, Telegram No, API server Yes (listen 0.0.0.0, pick your own username and password).

3. In `user_data/config.json`, use a fixed coin list (needed for backtesting):
```json
"pair_whitelist": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"],
"pairlists": [{ "method": "StaticPairList" }],
```

4. Download price history:
```bash
docker compose run --rm freqtrade download-data --config user_data/config.json --timeframes 1h --days 500
```

5. Create `user_data/strategies/MyStrategy.py`:
```python
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
```

6. Backtest:
```bash
docker compose run --rm freqtrade backtesting --config user_data/config.json --strategy MyStrategy --timerange 20250101-
```

7. Dry-run: in docker-compose.yml change `--strategy SampleStrategy` to `--strategy MyStrategy`, then
`docker compose up -d` and open http://localhost:8080. Stop with `docker compose down`, logs with `docker compose logs -f`.

## Phase 3b: Multi-chain whale scanner (design)

### Data sources (check current pricing/limits before building)
| Chain | Explorer (manual checks) | API for the scanner |
|---|---|---|
| Ethereum | etherscan.io | Etherscan API V2 (chainid=1, free tier OK) or Alchemy/Moralis |
| Arbitrum | arbiscan.io | Etherscan API V2 (chainid=42161, free tier OK) |
| BSC | bscscan.com | Etherscan V2 chainid=56 is PAID ONLY -> use BNB Chain MegaNode/BSCTrace (NodeReal), Moralis, or Alchemy |
| Base | basescan.org | Etherscan V2 chainid=8453 is PAID ONLY -> use Alchemy or Moralis |
| Solana | solscan.io | Helius (API + webhooks); Birdeye for token/top-trader data |
| All chains | — | Whale Alert API (paid) for large transfers; Arkham for wallet labels |

### Two ways to find whale moves
1. **Watchlist mode (main):** follow a list of known whale / "smart money" wallets.
   - Build the list from: Arkham labels, top holders on each explorer, top traders on Birdeye/GMGN (Solana).
   - Get real-time alerts with webhooks (Helius for Solana; Alchemy Notify or Moralis Streams for EVM chains)
     instead of polling, to stay inside free rate limits.
2. **Discovery mode:** catch any large transfer (e.g. >= $250k) of major assets
   (USDT, USDC, WETH/ETH, WBNB/BNB, SOL) and large deposits/withdrawals to exchange wallets.
   - Whale sends coins TO an exchange -> possible selling. Whale withdraws FROM an exchange -> possible holding/buying.

### Folder layout
```
whale_scanner/
  config.yaml            # API keys (via .env), chains, thresholds, watchlist file
  watchlist.csv          # chain,address,label,source
  chains/evm.py          # Ethereum, BSC, Base, Arbitrum (same code, different chain id / provider)
  chains/solana.py       # Helius
  binance_trades.py      # Binance aggTrade websocket (big trades on the exchange)
  normalize.py           # convert every event to one format (below)
  prices.py              # USD value via Binance / CoinGecko prices
  store.py               # SQLite: whale_events table
  score.py               # whale score per coin
  webhook_server.py      # small FastAPI app to receive Helius/Alchemy/Moralis webhooks
```

### One common event format (all chains)
```
time, chain, wallet, wallet_label, token_symbol, token_address,
side (BUY / SELL / TO_EXCHANGE / FROM_EXCHANGE), amount, usd_value, tx_hash
```

### Whale score per coin (last 1h / 4h / 24h)
- net_whale_usd = whale buys - whale sells (all chains + Binance big trades)
- unique_whales_buying = number of different wallets buying
- exchange_netflow = coins sent to exchanges - coins withdrawn
- score = combination of the above; Freqtrade strategy only enters when score > 0,
  and it's also added as a feature to the FreqAI model.

### Safety rules
- Many on-chain "whale buys" are small DEX tokens: check liquidity, holder count, and contract
  (honeypot / rug check) before trusting a signal. Only trade coins listed on Binance in Freqtrade.
- API keys in .env only; never commit them.

## Main feature: "Green Coin" finder (ties everything together)

Goal: every hour, output a ranked list of coins that are GREEN (good to trade) on Binance.

### Pipeline
1. **Binance universe:** get all Binance USDT spot pairs (`/api/v3/exchangeInfo`), keep those with
   24h volume above a minimum (e.g. $20M, from `/api/v3/ticker/24hr`) so trades fill without slippage.
2. **Historical check (per coin):** pull candles (`/api/v3/klines`, 1h + 4h + 1d) and compute
   trend (EMA 20/50/200), momentum (RSI, MACD), volatility (ATR), volume change -> `technical_score`.
3. **Prediction:** ML model (FreqAI / XGBoost) gives probability the coin rises >= 2% in 24h -> `ml_score`.
4. **Whale scanner (all networks):** Ethereum, BSC, Base, Arbitrum, Solana + Binance big trades.
   Map each on-chain token to its Binance symbol (token contract address -> CoinGecko id -> Binance symbol;
   keep a `token_map.csv`). Tokens NOT listed on Binance go to a separate "watch only" list.
   -> `whale_score` (net whale buying, unique whales buying, exchange netflow).
5. **Combine:** `final_score = w1*technical_score + w2*ml_score + w3*whale_score` (start with equal weights,
   tune with backtests).
6. **Label:**
   - GREEN: final_score high AND price above EMA200 AND whales net buying AND no big exchange inflow
   - YELLOW: mixed signals -> watch
   - RED: downtrend or whales sending coins to exchanges -> avoid
7. **Output:** `green_list.json` + dashboard table (Streamlit) + Telegram alert when a coin turns GREEN.
8. **Trading:** Freqtrade reads `green_list.json` (custom pairlist / strategy filter) and only trades GREEN coins,
   in dry-run first.

### Extra folders
```
coin_finder/
  universe.py        # Binance pairs + volume filter
  history.py         # candles download + indicators
  technical.py       # technical_score
  combine.py         # final_score + GREEN/YELLOW/RED
  token_map.csv      # chain,token_address,coingecko_id,binance_symbol
  dashboard.py       # Streamlit
```

## BUILD PLAN (follow in order; don't start a step until the previous one is "done")

| Week | Step | What to build | Done when |
|---|---|---|---|
| 1 | 1. Setup | Docker, Freqtrade, config, MyStrategy | Backtest runs and prints results |
| 1 | 2. Dry-run | Bot running with fake money | FreqUI at localhost:8080 shows the bot running |
| 2 | 3. Binance universe | `universe.py` | Prints all USDT pairs with 24h volume > $20M |
| 2 | 4. History + indicators | `history.py`, `technical.py` | CSV of candles + `technical_score` for every coin |
| 2 | 5. First dashboard | `dashboard.py` (Streamlit) | Table of coins sorted by `technical_score` |
| 3 | 6. Binance whale trades | `binance_trades.py`, `store.py` (SQLite) | Big trades (>= $250k) saved to the database |
| 3 | 7. Whale score v1 | `score.py` (Binance only) | `whale_score` per coin shown on the dashboard |
| 4 | 8. Ethereum + Arbitrum | `chains/evm.py` with Etherscan V2 (free) | Watchlist wallets' buys/sells saved in common format |
| 4 | 9. Token mapping | `token_map.csv` + mapper | On-chain tokens matched to Binance symbols; others -> watch-only list |
| 5 | 10. BSC + Base | same `evm.py`, provider = NodeReal / Alchemy / Moralis | BSC and Base events appear in the database |
| 5 | 11. Solana | `chains/solana.py` with Helius | Solana events appear in the database |
| 5 | 12. Webhooks | `webhook_server.py` + ngrok / Cloudflare Tunnel | Real-time alerts arrive without polling |
| 6 | 13. ML model | FreqAI (XGBoost) with technical + whale features | Backtest with ML beats the plain strategy (after fees) |
| 6 | 14. Green Coin finder | `combine.py` -> `green_list.json` | Every hour: GREEN / YELLOW / RED list on dashboard |
| 7 | 15. Connect to bot | Freqtrade trades only GREEN coins | Dry-run trades only coins from `green_list.json` |
| 7 | 16. Alerts | Telegram bot | Message arrives when a coin turns GREEN |
| 8-11 | 17. Paper trading | Let it run 4+ weeks, review weekly | Results are positive after fees and drawdown is acceptable |
| 12+ | 18. Small live test (optional) | Read+trade API key, withdrawals OFF, small amount | Only if step 17 was good; keep stop-loss and daily loss limit |

Rules while building:
- Test each file on its own before connecting it to the next.
- Keep secrets in `.env`; add `.env` to `.gitignore`.
- Use git: commit after each step works.
- If something is unclear, ask me (the user) before guessing.~
