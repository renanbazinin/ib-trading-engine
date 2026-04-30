# Bot Overview

## Runtime Shape

The live bot is orchestrated by `src/bot/main.py`. Running `python main.py` constructs `TWSBotOrchestrator` and calls `start()`.

The process owns several jobs at once:

- Flask dashboard in a daemon thread.
- IBKR broker connection, either Web API/IBeam or legacy TWS socket API.
- yfinance primary polling provider when configured.
- Broker historical data request for backfill/fallback.
- Live strategy state and order execution.
- Simulation manager fed by the same normalized bars.
- Live state persistence for dashboard recovery.

## Main Startup Flow

1. `load_dotenv()` loads local environment variables.
2. The orchestrator reads strategy, symbol, market-data, broker, polling, hours, and safety settings.
3. `create_strategy()` creates one live strategy instance.
4. Dashboard state is initialized.
5. Simulation config is loaded from `SIM_CONFIG_PATH` or `src/bot/simulation/config/simulation_config.json`.
6. yfinance provider is configured if `MARKET_DATA_PRIMARY=yfinance`.
7. The dashboard server starts.
8. The bot connects to IBKR.
9. Broker historical data is requested for `2 D`, the IBKR bar size mapped from `LIVE_BAR_INTERVAL`, `TRADES`, and `useRTH` derived from `EXTENDED_HOURS_TRADING`.
10. yfinance starts backfill and live polling.
11. The main loop runs every second.

## Main Loop

The loop in `main.py` sleeps for 1 second and repeatedly runs:

- Data health evaluation.
- Broker connection checks.
- Broker data polling.
- Account data polling.
- Pending signal retry.
- Order status polling.
- Recent trades/commission polling.

This means the process is always doing light supervision even when bars only arrive every 5 minutes.

## Market Data

The primary configured feed is yfinance:

- `YF_INTERVAL=5m`
- `YF_BACKFILL_PERIOD=5d`
- `YF_LOOKBACK_PERIOD=2d`
- `YF_POLL_SECONDS=20`
- `YF_MAX_BACKFILL_BARS=500`

The yfinance provider enforces a minimum poll interval of 5 seconds. Current config uses 20 seconds.

Backfill and live polling both use `prepost` from `EXTENDED_HOURS_TRADING`, so indicator warmup and live bars cover the same session set.

Broker data is also requested:

- Historical duration: `2 D`
- Bar size: mapped from `LIVE_BAR_INTERVAL`
- Data type: `TRADES`
- `useRTH=0` when `EXTENDED_HOURS_TRADING=True`; otherwise `useRTH=1` for regular-only history.
- `keepUpToDate=True` only for legacy TWS socket mode.
- In Web API mode, broker bars are re-polled through the bot's broker polling path.

## Feed Health And Fallback

`core/feed_health.py` tracks last primary and fallback bar timestamps.

Important settings:

- `DATA_STALE_AFTER_SECONDS=900`
- `AUTO_PAUSE_LIVE_ON_STALE=True`
- `MARKET_DATA_FALLBACK_ENABLED=True`

If the active source is stale and auto-pause is on, live orders are blocked. If primary yfinance becomes stale and broker fallback is fresh enough, the active source can switch to broker.

Fallback freshness is considered available when fallback lag is no more than the larger of:

- `DATA_STALE_AFTER_SECONDS * 2`
- `300` seconds

## Trading Hours

Trading hours are defined in `src/bot/core/trading_hours.py`.

Default and current local sessions:

```text
overnight:20:00-03:50
pre_market:04:00-09:30
regular:09:30-16:00
after_hours:16:00-20:00
```

Timezone:

```text
America/New_York
```

When `TRADING_HOURS_ENABLED=True`, each live bar must pass these gates:

1. The current time must be inside a configured open session.
2. The bar timestamp must be inside a configured open session.
3. The bar session must match the current session.
4. The bar must be complete. For a 5-minute bar, the bot waits until the bar close time has passed.
5. In `pre_market`, the bot waits for `PREMARKET_MIN_COMPLETED_BARS`.

Current pre-market warmup:

```text
PREMARKET_MIN_COMPLETED_BARS=3
```

With 5-minute bars, the first possible pre-market live evaluation is around 04:15 ET.

## Strategy Processing

For each accepted normalized bar:

1. The bar is added to the live strategy dataframe.
2. Indicators are recalculated.
3. The latest strategy signal is requested.
4. Dashboard candle and signal state is updated.
5. If trading is ready and the signal is actionable, order execution is attempted.
6. Simulations process the same bar.

The live position model is long-only and slot-aware:

- `NONE`: no broker-reported position for the configured symbol.
- `LONG`: broker reports a position for the configured symbol.
- Additional BUY signals can scale in while `LONG` when slot capacity and cash remain.

There is no intentional shorting. Slot capacity is based on a persisted live slot ledger using the daily/session starting NetLiq snapshot, with broker position exposure as a safety backstop.

## Order Execution Guards

Before an order is sent, the bot checks:

- Order cooldown is not active.
- Current time is inside configured trading hours.
- Broker is connected.
- Web API authentication is valid when using Web API.
- Stale-data guard is not active.
- Dashboard/live switch `is_live_active` is true.
- Daily BUY/order guardrails allow another entry order.
- Quantity can be calculated.
- Slot capacity, cash source, and hard caps allow the order.

Optional hard caps:

```text
MAX_ORDER_QUANTITY
MAX_ORDER_NOTIONAL
EXT_HOURS_MAX_ORDER_NOTIONAL
```

They are present in `.env.example` but not set in the current local `.env`.

## Live Sizing

BUY sizing:

```text
slot_percent mode:
slot_notional = starting NetLiquidation snapshot * LIVE_SLOT_ALLOCATION_PCT
max_symbol_exposure = slot_notional * LIVE_MAX_POSITION_SLOTS
quantity = floor(min(slot_notional, remaining_symbol_exposure, cash, caps) / current_price)
```

SELL sizing:

```text
quantity = full integer position currently reported for DEFAULT_SYMBOL
```

`LIVE_ORDER_SIZING_MODE=fixed_quantity` uses `LIVE_DEFAULT_QUANTITY`. `LIVE_BUY_ALLOCATION_PCT` applies only to `allocation_percent` mode. `DEFAULT_QUANTITY` is only a deprecated compatibility fallback.

## Order Type

The orchestrator creates a market order through the broker client.

For Web API extended-hours US equity orders, `core/ib_web_client.py` converts a market order request into a marketable limit order:

- BUY limit: ask or last plus `EXT_HOURS_BUY_LIMIT_PAD_PCT` / `EXT_HOURS_LAST_PRICE_PAD_PCT`.
- SELL limit: bid or last minus `EXT_HOURS_SELL_LIMIT_PAD_PCT` / `EXT_HOURS_LAST_PRICE_PAD_PCT`.
- `outsideRTH=True`.
- `tif=DAY`.

This is because market orders are often rejected outside regular trading hours.

## Pending Signal Retry

If a BUY/SELL signal cannot execute because of broker issues, the signal can be stored and retried.

Relevant setting:

```text
SIGNAL_RETRY_TTL_SECONDS=300
```

The bot abandons queued signals after repeated failed retries or after the TTL expires.

## Dashboard And State

The dashboard is served from `src/bot/web/dashboard.py`.

Current local dashboard port:

```text
5050
```

The dashboard surfaces:

- Live active switch.
- Effective live active status.
- Broker status.
- Feed health and active source.
- Latest price and signal.
- Near-signal diagnostics.
- Account, positions, orders, and recent trades.
- Simulation summaries.

Live state is persisted to `simulation/results/live_state.json` unless `LIVE_STATE_PATH` is set.

State persistence is meant for dashboard continuity. Some runtime controls, such as live active mode, strategy, symbol, and trading-hours config, are initialized from `.env` rather than restored from disk.

