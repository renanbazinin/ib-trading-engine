# IBKR Modular Trading Bot & Simulator

This provides an event-driven bot mapping to TWS or IB Gateway, running live trading strategies concurrently with offline paper/simulation bots writing to JSON dynamically.

Current market-data model:
- Primary: yfinance 5-minute bars.
- Fallback: broker keepUpToDate historical bars.
- Safety: optional auto-pause of live orders when primary feed is stale.

## Getting Started

1. Set up your environment variables:
   ```bash
   cp .env.example .env
   ```
2. Adjust `TWS_PORT` (usually 7496 for live TWS, 7497 for paper TWS, 4001/4002 for IBGateway).
3. Configure feed behavior as needed in `.env`:
   - `MARKET_DATA_PRIMARY=yfinance`
   - `MARKET_DATA_FALLBACK_ENABLED=True`
   - `DATA_STALE_AFTER_SECONDS=900`
   - `AUTO_PAUSE_LIVE_ON_STALE=True`

## Architecture

* **`core/`**: Custom EWrapper and EClient handlers implementing an event router.
* **`core/yfinance_provider.py`**: Polling provider for primary market data.
* **`core/feed_health.py`**: Feed freshness checks and fallback decision support.
* **`strategies/`**: Defines logic separating data ingestion (`bb_smi.py`) from API dependencies.
* **`simulation/`**: Runs sub-portfolios on the same normalized bars used by the live strategy.
* **`web/`**: Lightweight Flask server to monitor the live account + view active simulated traces.

## Linux / Docker Deployment

```bash
docker-compose up -d --build
```
This runs the bot continuously and binds the web dashboard to `http://YOUR_VPS_IP:5000`

For a full production runbook (IB Gateway setup, env config, validation, hardening, and troubleshooting), see:

- `docs/DEPLOY_LINUX_DOCKER.md`

## Logs & Results
Simulated portfolio runs will be documented inside `simulation/results/`.