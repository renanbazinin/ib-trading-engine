# Bot Docs Learn

This folder documents how the trading bot currently works after reading the live bot code, strategy code, simulation code, `.env.example`, and the local `.env`.

Secrets from `.env` are intentionally not copied here. The local `.env` contains real broker and Telegram credentials, so this documentation describes the settings without exposing passwords, tokens, or chat identifiers.

## What This Bot Is

The bot is a Python trading process for Interactive Brokers. It runs one live strategy, a Flask dashboard, broker/account polling, market-data polling, and parallel simulations in the same process.

The live entrypoint is:

```text
src/bot/main.py
```

The main class is:

```text
TWSBotOrchestrator
```

At startup it:

1. Loads `.env`.
2. Builds the selected live strategy through `strategies/strategy_factory.py`.
3. Starts the dashboard.
4. Connects to IBKR through either Client Portal Web API/IBeam or legacy TWS socket API.
5. Requests broker historical bars.
6. Starts yfinance polling when configured.
7. Processes each allowed bar through the strategy.
8. Places orders only if all safety gates allow it.
9. Runs configured simulations on the same normalized bars.

## Current Local Trading Setup

The local `.env` currently says the intended live setup is:

- Live switch: `IS_LIVE_ACTIVE=True`
- Trading mode label: `TRADING_MODE=live`
- Live strategy: `STRATEGY=RSI_BB_FEE_AWARE`
- Symbol: `DEFAULT_SYMBOL=TSLA`
- Broker route: `IBKR_WEB_API_ENABLED=True`
- IBeam paper flag: `IBEAM_PAPER=False`
- Extended hours: `EXTENDED_HOURS_TRADING=True`
- Market data primary: `MARKET_DATA_PRIMARY=yfinance`
- Broker fallback: `MARKET_DATA_FALLBACK_ENABLED=True`
- Bar interval: `5m`
- Dashboard port: `5050`
- Telegram notifications: enabled, with fills enabled and order-sent notifications disabled

Important: `TRADING_MODE=live` is mostly a label/safety UX setting. The actual destination depends on the IBKR account/gateway session you connect to. The bot will send orders through the connected broker session when live trading is active and guards pass.

## Documentation Map

- `bot_overview.md`: lifecycle, polling, broker integration, data flow, state, and order flow.
- `runtime_defaults.md`: defaults from code, `.env.example`, and current local `.env`.
- `overall_strategy_model.md`: how all strategies fit together and what kind of trading system this is.
- `strategies/README.md`: quick comparison of all registered strategies.
- `strategies/bb_smi/README.md`: Bollinger + SMI crossover strategy.
- `strategies/rsi_bb/README.md`: RSI + Bollinger + trend filter strategy.
- `strategies/rsi_bb_fee_aware/README.md`: current live fee-aware RSI + Bollinger strategy.
- `strategies/rsi_only/README.md`: pure RSI strategy.

## Key Safety Takeaways

- The bot is flat-or-long only. It does not intentionally short.
- BUY sizing now uses configurable live sizing. The safer default is slot-based sizing from the daily/session starting `NetLiquidation`, tracked in the live slot ledger, and capped by `LIVE_MAX_POSITION_SLOTS`, broker position exposure, cash, and order caps.
- SELL sizing liquidates the full current position for the configured symbol.
- `LIVE_DEFAULT_QUANTITY` is used by `LIVE_ORDER_SIZING_MODE=fixed_quantity`; `DEFAULT_QUANTITY` remains only a parser fallback for older local configs.
- Daily risk guards default to `MAX_BUYS_PER_DAY=4` and `MAX_ORDERS_PER_DAY=8`; exits are still allowed so the bot can close risk.
- Trading hours are enforced before strategy processing and again before order execution.
- Stale active market data can block live orders.
- Web API order failures create a 30-second cooldown.
- Signals can be queued and retried briefly when broker issues prevent immediate execution.

