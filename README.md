# Interactive Brokers Trading Bot

An automated, fee-aware trading bot built on the Interactive Brokers API (IBAPI). This bot utilizes technical indicators (like RSI and Bollinger Bands) to execute trades and manage live sessions, bridging historical data with live execution.

## Features
- **Live Trading & Paper Trading Support**: Easily switch between paper trading simulation and live environments.
- **Multiple Strategies**: Built-in strategies for RSI, Bollinger Bands, and Fee-aware executions.
- **IBKR Web API & Desktop Support**: Connects to IBKR via the modern Web Gateway or the legacy TWS/IB Gateway socket.
- **Market Data Fallbacks**: Integrates YFinance and AlphaVantage as primary or fallback data feeds.
- **Extended Hours**: Optional pre-market and after-hours trading support.
- **Telegram Notifications**: Live trade updates pushed straight to your phone.

---

## 🚀 Getting Started

### 1. Prerequisites
- Python 3.10+
- An Interactive Brokers Account (Pro or Lite, though Pro is recommended for API trading).
- IBKR Desktop (TWS) / IB Gateway installed and running, OR the bot's Dockerized IB Web Gateway setup.

### 2. Installation
Clone the repository, then set up your virtual environment and install dependencies:

```bash
git clone https://github.com/your-username/your-repo-name.git
cd your-repo-name/src/bot

python -m venv venv
# Windows
venv\Scripts\activate
# Mac/Linux
source venv/bin/activate

pip install -r requirements.txt
pip install ibapi-10.35.1-py3-none-any.whl
```

### 3. Environment Configuration ⚙️

You must configure the bot before running it. We use a `.env` file to securely load credentials.

1. Navigate to `src/bot/`.
2. Copy `.env.example` to a new file named `.env`.
3. Open `.env` and fill out your specific variables.

#### Core Parameters
*   `TRADING_MODE`: Set to `paper` to test without real money, or `live` to trade real capital.
*   `IS_LIVE_ACTIVE`: Master switch. Set to `True` to enable trading.
*   `STRATEGY`: The strategy to use (e.g., `RSI_BB_FEE_AWARE_V4B`).
*   `DEFAULT_SYMBOL`: The default ticker to trade (e.g., `AAPL`).

#### Authentication (IBKR Web API / IBEAM)
If using the Web API gateway (e.g., via Docker):
*   `IBKR_WEB_API_ENABLED=True`
*   `IBKR_USER="your_username"`
*   `IBKR_PW="your_password"`
*   `IBEAM_PAPER=True` (Set to `False` for live production trading)

#### Legacy TWS / IB Gateway Connections
If using the classic desktop TWS application:
*   `TWS_HOST=127.0.0.1`
*   `TWS_PORT=4001` (4001 for IB Gateway Paper, 4002 for Live, 7497 for TWS Paper)
*   `CLIENT_ID=1`

#### Telegram Setup (Optional)
To receive execution alerts on your phone:
*   `TELEGRAM_ENABLED=True`
*   `TELEGRAM_BOT_TOKEN="your_botfather_token"`
*   `TELEGRAM_CHAT_ID="your_channel_or_user_id"`

---

## 📈 Running the Bot

### Running locally
Ensure TWS or your Gateway is running and authenticated, then start the bot:
```bash
python main.py
```

### Running with Docker Compose
The project includes self-contained Docker setups which can run the IBKR Web Gateway and the Bot simultaneously:

```bash
# To spin up the stack
docker-compose -f docker-compose.yml up -d
```
*Note: Make sure your `.env` file the IBKR credentials properly set so the IBeam gateway container can authenticate your session.*

## 📐 Bot Strategy & Tuning

You can tweak the strategy parameters directly in the `.env` file to optimize performance.
*   `RSI_PERIOD`: Standard RSI Lookback.
*   `BB_LENGTH` & `BB_STD`: Bollinger Band periods and standard deviation.
*   `LIVE_BUY_ALLOCATION_PCT`: The percentage of your portfolio to allocate per trade.

## ⚠️ Disclaimer
**Use at your own risk.** This software is provided for educational and research purposes only. Trading the financial markets involves significant risk of loss. Always test your strategies extensively in `paper` trading mode before committing real capital. The authors are not responsible for any financial losses incurred from using this bot.
