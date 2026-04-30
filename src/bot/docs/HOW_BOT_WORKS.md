# How The Bot Works

## Overview
This service runs an IBKR (TWS/Gateway) trading bot and a web dashboard in the same Python process.
Market data is provider-driven:
- Primary feed: yfinance (5-minute bars).
- Fallback feed: broker historical keepUpToDate stream.

Core entrypoint: src/bot/main.py

Main responsibilities:
- Connect to IBKR API for account updates, positions, and order execution.
- Stream and process normalized 5-minute bars from the active market-data source.
- Compute strategy signals for the live bot.
- Execute real orders only when live trading is explicitly active.
- Auto-pause live order submission when primary feed is stale.
- Feed account and position updates to the dashboard.
- Run simulations in parallel through the same normalized market-data stream.

## Main Components
- main.py: Orchestration and lifecycle.
- core/ib_client.py: EWrapper/EClient router and callback dispatch.
- core/market_data_provider.py: Provider-agnostic bar schema.
- core/yfinance_provider.py: Polling provider for yfinance bars.
- core/feed_health.py: Source freshness, fallback and stale guard policy.
- strategies/bb_smi.py: Bollinger Band + SMI signal logic.
- web/dashboard.py: Flask routes (Live, Simulations, Config + APIs).
- simulation/sim_manager.py: Simulation manager and event export.

## Startup Flow
1. Load environment variables from .env.
2. Build the orchestrator object.
3. Load simulation config from simulation/config/simulation_config.json.
4. Start Flask dashboard in a daemon thread.
5. Connect to TWS/Gateway using host/port/client_id.
6. Start IB event loop in a daemon thread.
7. Request positions and account updates.
8. Start broker historical keepUpToDate stream for fallback feed.
9. Start yfinance provider for backfill + live polling.
10. Process bars from the active source and reevaluate feed health continuously.

## Live Trading Flow
On each normalized live bar from the active source:
1. Add bar to live strategy dataset.
2. Recompute indicators.
3. Compute signal (BUY/SELL/HOLD).
4. If BUY and no position, or SELL and long position, attempt execution.

Execution guard:
- If bot_state.is_live_active is false, order placement is skipped.
- If stale-data guard is active, order placement is blocked even when bot_state.is_live_active is true.
- This allows data processing and simulation to continue while real orders are blocked.

## Safety Controls
- Trading mode comes from TRADING_MODE in .env.
- Live order switch comes from IS_LIVE_ACTIVE and can be toggled from dashboard.
- Data staleness threshold comes from DATA_STALE_AFTER_SECONDS.
- AUTO_PAUSE_LIVE_ON_STALE enables automatic execution blocking when primary feed is stale.
- Fallback source switching is controlled by MARKET_DATA_FALLBACK_ENABLED.
- Signal handlers (SIGINT/SIGTERM/SIGBREAK) request clean disconnect.

## Dashboard Integration
Pages:
- /live
- /simulations
- /config

Key APIs used by dashboard:
- /api/bot/status
- /api/bot/toggle
- /api/live/account
- /api/live/positions
- /api/simulations
- /api/simulations/recent-signals
- /api/simulations/recent-trades
- /api/config/simulations (GET/POST)

## Important Runtime Notes
- IB API callback signatures can vary by version. The error callback is version-tolerant.
- Broker reqHistoricalData includes chartOptions=[] for compatibility with your ibapi build.
- yfinance intraday data is limited to recent history (up to ~60 days).
- Bar payloads are normalized so simulation and live processing consume the same bar shape.
- Dashboard status surfaces active data source, lag, and stale guard state.

## Troubleshooting
If the bot appears connected but not trading:
- Verify dashboard bot status is ACTIVE.
- Verify IS_LIVE_ACTIVE in .env.
- Verify TWS/Gateway API settings allow the client ID and local connection.

If no bars arrive:
- Check internet connectivity for yfinance polling.
- Check symbol is valid for yfinance.
- Check requested broker port and whether TWS/Gateway is running (fallback path).

If dashboard loads but data is empty:
- Wait for initial historical backfill to complete.
- Check simulation results file creation in simulation/results.
