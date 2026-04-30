# Trading Bot System Context

This directory contains a modular, event-driven trading bot and simulation engine that interfaces with Interactive Brokers (IBKR).

## System Architecture

The bot is built on a provider-agnostic data model, allowing it to ingest market data from multiple sources (yfinance, IBKR) and route it to live trading strategies and concurrent simulations.

### Core Components (`core/`)
- **`ib_client.py`**: A robust `EWrapper`/`EClient` implementation with an event router pattern. It handles connection management and dispatches IBKR callbacks to registered listeners.
- **`market_data_provider.py`**: Defines the `NormalizedBar` schema and `MarketDataProvider` protocol. This ensures consistency across different data sources.
- **`yfinance_provider.py`**: Implements polling for primary market data using `yfinance`.
- **`feed_health.py`**: Monitors data freshness and manages the automatic fallback to IBKR historical data if the primary feed lags.

### Trading Logic (`strategies/`)
- **Strategy Pattern**: Strategies (like `bb_smi.py`) are decoupled from API dependencies. They consume `NormalizedBar` objects and return trading signals (BUY, SELL, HOLD).
- **Current Strategy**: Bollinger Bands (BB) + Stochastic Momentum Index (SMI).

### Simulation & Backtesting (`simulation/`)
- **`sim_manager.py`**: Manages multiple sub-portfolios running in parallel with the live bot.
- **Results**: Simulation metrics and trades are exported to `simulation/results/`.
- **Configuration**: Managed via `simulation/config/simulation_config.json`.

### Web Dashboard (`web/`)
- A Flask-based UI for real-time monitoring of account status, open positions, active signals, and simulation performance.

## Execution Model

1. **Initialization**: Loads `.env`, initializes the `TWSBotOrchestrator`, and starts the Flask dashboard.
2. **Connectivity**: Establishes a socket connection to TWS/Gateway and starts the EReader thread.
3. **Data Ingestion**: Polls `yfinance` for backfill and live bars while maintaining a "keepUpToDate" historical stream from IBKR as a fallback.
4. **Signal Processing**: Each new bar is routed to the live strategy and all active simulation sub-portfolios.
5. **Safety Guards**:
   - `IS_LIVE_ACTIVE`: Master switch for real order execution.
   - `AUTO_PAUSE_LIVE_ON_STALE`: Blocks orders if the market data feed is detected as stale.

## Development Guidelines

- **Decoupled Logic**: Keep strategy logic in `strategies/` independent of the IBKR API. Use the `NormalizedBar` interface.
- **Event Routing**: Register callbacks with `EventRouterClient` for specific IBKR events (e.g., `position`, `accountValue`).
- **Safety First**: Always verify `TRADING_MODE` (PAPER/LIVE) and `IS_LIVE_ACTIVE` in `.env` before running.
- **Logging**: Use the structured logging provided in `main.py` for debugging data flow and execution issues.

## Documentation
- `docs/HOW_BOT_WORKS.md`: Detailed architecture and startup flow.
- `docs/HOW_SIMULATIONS_WORK.md`: Explanation of the concurrent simulation engine.
- `docs/DEPLOY_LINUX_DOCKER.md`: Production deployment guide.
