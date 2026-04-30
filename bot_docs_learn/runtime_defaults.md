# Runtime Defaults And Local Configuration

This file summarizes defaults from code, `.env.example`, and the current local `.env`.

Secrets are redacted. Do not copy broker passwords, TOTP secrets, Telegram tokens, or chat IDs into documentation.

## Current Local `.env`

The local `.env` is configured for an active live TSLA bot:

```text
IS_LIVE_ACTIVE=True
TRADING_MODE=live
STRATEGY=RSI_BB_FEE_AWARE
DEFAULT_SYMBOL=TSLA
LIVE_ORDER_SIZING_MODE=slot_percent
LIVE_DEFAULT_QUANTITY=100
LIVE_SLOT_ALLOCATION_PCT=0.25
LIVE_MAX_POSITION_SLOTS=4
LIVE_BUY_CASH_SOURCE=settled_cash
LIVE_BUY_CASH_FALLBACKS=
MAX_BUYS_PER_DAY=4
MAX_ORDERS_PER_DAY=8
MIN_SECONDS_BETWEEN_ORDERS=0
DAILY_LOSS_STOP_PCT=
EXTENDED_HOURS_TRADING=True
TRADING_HOURS_ENABLED=True
IBKR_WEB_API_ENABLED=True
IBKR_WEB_API_BASE_URL=https://127.0.0.1:5000/v1/api
IBEAM_PAPER=False
MARKET_DATA_PRIMARY=yfinance
MARKET_DATA_FALLBACK_ENABLED=True
BROKER_POLL_SECONDS=60
YF_INTERVAL=5m
YF_BACKFILL_PERIOD=5d
YF_LOOKBACK_PERIOD=2d
YF_POLL_SECONDS=20
YF_MAX_BACKFILL_BARS=500
DATA_STALE_AFTER_SECONDS=900
AUTO_PAUSE_LIVE_ON_STALE=True
LIVE_BAR_INTERVAL=5m
LIVE_TRADES_LOOKBACK_DAYS=3
LIVE_TRADES_POLL_SECONDS=30
LIVE_ORDERS_POLL_SECONDS=5
DASHBOARD_PORT=5050
TELEGRAM_ENABLED=True
TELEGRAM_NOTIFY_ORDER_SENT=False
TELEGRAM_NOTIFY_ORDER_FILLED=True
```

Redacted values present locally:

- IBKR username.
- IBKR password.
- Telegram bot token.
- Telegram chat ID.

## Trading Hours Defaults

Code default and current local value:

```text
TRADING_HOURS_TIMEZONE=America/New_York
TRADING_SESSIONS=overnight:20:00-03:50,pre_market:04:00-09:30,regular:09:30-16:00,after_hours:16:00-20:00
PREMARKET_MIN_COMPLETED_BARS=3
```

Behavior:

- Overnight is Sunday evening through Friday morning.
- Non-overnight sessions are Monday through Friday.
- If trading hours are disabled, the trading-hours module reports the market as open.
- If enabled, the bot requires both current time and bar time to be in the same open session.

## Strategy Defaults In Code

The orchestrator builds one shared strategy config with these defaults:

```text
STRATEGY=BB_SMI
RSI_PERIOD=14
BB_LENGTH=20
BB_STD=2.2
OVERBOUGHT=70
OVERSOLD=33
TREND_SMA_PERIOD=50
BEAR_DIP_RSI=28
BB_TOLERANCE=0.0015
SMI_FAST=10
SMI_SLOW=3
SMI_SIG=3
NEAR_BAND_PCT=0.001 for BB_SMI strategy construction
FEE_PER_TRADE=2.5
FEE_AWARE_ESTIMATED_TRADE_NOTIONAL=1000.0
FEE_AWARE_MIN_REWARD_PCT=0.006
FEE_AWARE_REWARD_FEE_MULTIPLE=3.0
FEE_AWARE_MIN_BB_WIDTH_PCT=0.008
FEE_AWARE_REQUIRE_CONFIRMATION=True
FEE_AWARE_EXIT_RSI=55.0
```

Note: the orchestrator also reads `NEAR_BAND_PCT` separately for dashboard near-signal diagnostics, with default `0.003`.

## Broker Defaults

Web API mode:

```text
IBKR_WEB_API_ENABLED=False
IBKR_WEB_API_BASE_URL=https://127.0.0.1:5000/v1/api
WEB_API_RECONNECT_ENABLED=True
WEB_API_RECONNECT_MAX_ATTEMPTS=10
WEB_API_RECONNECT_INTERVAL_SECONDS=60
WEB_API_HEALTH_CHECK_INTERVAL_SECONDS=30
WEB_API_ORDER_PREVIEW_ENABLED=False
```

Legacy TWS socket mode:

```text
TWS_HOST=127.0.0.1
TWS_PORT=4001
CLIENT_ID=1
```

Current local `.env` uses Web API/IBeam, not legacy socket mode.

## Polling Defaults

Main loop:

```text
1 second
```

yfinance:

```text
YF_POLL_SECONDS=20
minimum enforced by provider: 5 seconds
```

Broker/Web API historical bar polling:

```text
BROKER_POLL_SECONDS=60
```

Account data in Web API mode:

```text
30 seconds
```

Order status:

```text
LIVE_ORDERS_POLL_SECONDS=5
```

Recent trades and commissions:

```text
LIVE_TRADES_POLL_SECONDS=30
LIVE_TRADES_LOOKBACK_DAYS=3
allowed range in code: 1 to 7 days
```

Signal retry:

```text
SIGNAL_RETRY_TTL_SECONDS=300
```

## Safety Defaults

```text
IS_LIVE_ACTIVE=False in .env.example
TRADING_MODE=paper in .env.example
DATA_STALE_AFTER_SECONDS=900
AUTO_PAUSE_LIVE_ON_STALE=True
MARKET_DATA_FALLBACK_ENABLED=True
EXTENDED_HOURS_TRADING=False in .env.example
TRADING_HOURS_ENABLED=True
```

The local `.env` changes several of these to live operation:

```text
IS_LIVE_ACTIVE=True
TRADING_MODE=live
EXTENDED_HOURS_TRADING=True
IBEAM_PAPER=False
```

## Operational Warnings

- Keep `.env` out of commits and shared docs. It contains real credentials.
- Treat `IS_LIVE_ACTIVE=True` plus `TRADING_MODE=live` plus `IBEAM_PAPER=False` as a real-money posture.
- Prefer `LIVE_DEFAULT_QUANTITY` for fixed-share live sizing; `DEFAULT_QUANTITY` is only a compatibility fallback for older local configs.
- Slot sizing uses a daily/session starting NetLiq snapshot and persisted live slot ledger, not current mark-to-market NetLiq on every bar.
- Broker historical bars are mapped from `LIVE_BAR_INTERVAL` and use `EXTENDED_HOURS_TRADING` to choose regular-only vs extended-hours history.
- yfinance backfill and live polling use the same `prepost` setting.

