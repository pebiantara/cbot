# cbot

A personal crypto trading research project: a Binance Futures signal engine, a multi-chain whale-transaction tracker, and supporting infrastructure, all driven through Telegram. Built and iterated on entirely through real-data backtesting — every change in this repo's history was validated against logged outcomes before shipping, and several tested ideas were deliberately rejected and left undocumented in code (see commit history / internal notes for what didn't work and why).

**This is a research/personal-use project, not a packaged product.** It trades (or has traded) with real money at the user's own risk. Nothing here is financial advice, and most of the measured edges are thin — see the live engine's own accuracy caveats below before assuming any signal is reliable.

## What's running

Four Docker services, orchestrated via `docker-compose.yml`:

| Service | Purpose |
|---|---|
| `quick-scalp-alert` | The live trading engine — scans the tracked coin universe, fires Telegram alerts, logs every signal + outcome for later validation |
| `webhook-server` | FastAPI app behind a Cloudflare Tunnel — receives Telegram bot commands and on-chain whale webhooks (Helius/Alchemy) |
| `cloudflared` | Cloudflare Quick Tunnel — exposes `webhook-server` publicly so Telegram/Helius/Alchemy can reach it, no fixed domain needed |
| `webhook-registrar` | Watches `cloudflared`'s logs for its (randomly-assigned, changes-on-restart) tunnel URL and auto re-registers it with Telegram/Helius |

## The trading engine (`coin_finder/quick_scalp_signal.py`)

A **fade** strategy: it detects a strong, one-sided order-flow push (rolling 10-minute taker buy/sell delta) and trades the *opposite* direction, on the theory that a sharp one-sided push is often a stop-hunt/liquidity-sweep worth fading rather than a move worth chasing. This direction flip was itself backtested (not assumed) before shipping.

On top of the core delta trigger, a signal must also clear:
- Momentum, volume-spike, and candle-body confirmation (filters out noise)
- Not already mid-trend (an exhausted push already running isn't a fresh setup)
- Funding rate and 1h/4h trend alignment
- No contradicting candlestick reversal pattern (hammer/engulfing/shooting-star family, used as a veto)
- EMA ribbon (5/8/13) or VWAP-stretch confirmation — at least one must favor the fade direction

Stop-loss/take-profit sizing is a flat ROI floor that widens (never tightens) when a real structural support/resistance level calls for more room, capped against real liquidation distance.

**Known limitation, stated plainly**: raw directional accuracy in any short window (15-30 min) sits close to a coin flip. Whatever edge exists comes from asymmetric win/loss sizing over many trades, not from being right most of the time on any single one — confirmed via a proper TP/SL-hit-first, fee-aware backtest, not just a naive hit-rate count.

## Whale scanner (`whale_scanner/`)

Tracks a watchlist of known wallets (exchange hot wallets, notable addresses) across Ethereum, Arbitrum, BSC, Base, and Solana via Helius/Alchemy webhooks, plus Binance's own large-trade feed. Prices on-chain token transfers via a Binance-symbol map with a CoinGecko fallback for anything not on that short list, and fires a Telegram alert for any single transfer above a configurable USD threshold.

## Telegram commands

| Command | What it does |
|---|---|
| `/scalp_<coin>_<leverage>` | On-demand check of the quick-scalp signal for one coin (50x or 100x only) |
| `/watch_<coin>` / `/unwatch_<coin>` | Short (15 min) pre-entry confirmation watch — pings only when the read changes |
| `/calc_<price>_<leverage>_<roi_pct>` | Price-distance calculator for a given ROI% move at a given leverage |
| `/open_<coin>_<margin>` / `/close_<coin>` | Self-reported position log, used for portfolio exposure capping |

## Setup

1. Docker Desktop running.
2. Copy `.env.example` (if present) or create `.env` with: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `HELIUS_API_KEY`, `ALCHEMY_API_KEY` (optional), `LEVERAGE_ACCOUNT_EQUITY_USDT`, `LEVERAGE_RISK_PCT`. Never commit this file.
3. `docker compose up -d`
4. Check logs: `docker compose logs -f quick-scalp-alert`

## Project structure

```
coin_finder/        Core trading logic: signal engines, backtests, Telegram message building
whale_scanner/      On-chain whale tracking, webhook server, Telegram command dispatch
user_data/          Freqtrade strategy/config (separate, earlier-phase experiment)
pipeline/           Supporting data pipeline scripts
```

No single "run everything" entry point by design — each engine is independently queryable and independently backtested, not meant to be merged into one monolithic scorer.
