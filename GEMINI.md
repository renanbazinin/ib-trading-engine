# Gemini CLI Project Context: TWS API & Trading Bot

This workspace contains the Interactive Brokers (IBKR) Trader Workstation (TWS) API SDK across multiple languages, along with a custom Python-based trading bot and simulator.

## Project Structure

- **`source/`**: Core API client source code.
    - `cppclient/`: C++ API source.
    - `CSharpClient/`: C# API source.
    - `JavaClient/`: Java API source.
    - `pythonclient/`: Python API source (`ibapi`).
- **`samples/`**: Official example applications for each supported language.
- **`src/bot/`**: A custom, event-driven trading bot and simulator.
    - `core/`: IBKR client wrappers, market data providers (yfinance fallback), and feed health monitoring.
    - `strategies/`: Trading strategy implementations (e.g., Bollinger Bands + SMI).
    - `simulation/`: Backtesting engine and results.
    - `web/`: Flask-based dashboard for monitoring live and simulated portfolios.
- **`tests/`**: C# test suite for the TWS library.

## Building and Running

### 1. Python API (`ibapi`) Installation
The Python trading bot requires the `ibapi` package to be installed from the source:
```bash
cd source/pythonclient
python setup.py sdist
python setup.py bdist_wheel
pip install dist/ibapi-*.whl
```

### 2. Trading Bot (`src/bot`)
The bot can be run locally or via Docker.

**Local Run:**
1. Navigate to `src/bot`.
2. Install dependencies: `pip install -r requirements.txt`.
3. Configure `.env` (copy from `.env.example`).
4. Run the bot: `python main.py`.

**Docker Run:**
```bash
cd src/bot
docker-compose up -d --build
```
The dashboard will be available at `http://localhost:5000`.

### 3. C++ Client
Built using CMake:
```bash
mkdir build && cd build
cmake ..
cmake --build .
```

### 4. Other Clients (Java, C#, VB)
- **Java**: Located in `source/JavaClient`, uses Maven (`pom.xml`).
- **C#**: Solutions found in `source/CSharpClient` and `samples/CSharp`.

## Development Conventions

- **Event-Driven Architecture**: The trading bot uses an `EventRouterClient` (in `src/bot/core/ib_client.py`) which inherits from `EWrapper` and `EClient` to route IBKR callbacks to registered listeners.
- **Market Data**: The bot uses a primary feed (defaulting to `yfinance`) with an optional fallback to IBKR historical data if the primary feed becomes stale.
- **Environment Variables**: All configuration (TWS port, trading mode, symbols, strategy parameters) should be managed via `src/bot/.env`.
- **Simulation**: The bot supports a "Simulation" mode that runs concurrently with or instead of live trading, saving results to `src/bot/simulation/results/`.

## Key Files
- `src/bot/main.py`: Entry point for the trading bot orchestrator.
- `src/bot/core/ib_client.py`: Centralized IBKR API event handling.
- `source/pythonclient/ibapi/`: Source of the `ibapi` library.
- `src/bot/.env.example`: Template for bot configuration.
