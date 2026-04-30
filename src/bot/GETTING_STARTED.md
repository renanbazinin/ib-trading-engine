# TWS Bot - Getting Started

## Prerequisites

- Docker & Docker Compose installed
- An Interactive Brokers account with IB Key (IBKR Mobile) enabled
- The IBKR Mobile app on your phone for push-notification 2FA

---

## 1. Configure Environment

```bash
cd src/bot
cp .env.example .env
```

Edit `.env` and set your real credentials:

```
IBKR_USER=your_ibkr_username
IBKR_PW="your_ibkr_password"
IBKR_WEB_API_ENABLED=True
TRADING_MODE=paper          # "paper" or "live"
IBEAM_PAPER=True            # True for paper account, False for live
DEFAULT_SYMBOL=TSLA
STRATEGY=RSI_BB_FEE_AWARE_V4B
LIVE_ORDER_SIZING_MODE=slot_percent
LIVE_SLOT_ALLOCATION_PCT=0.25
LIVE_MAX_POSITION_SLOTS=4
LIVE_BUY_CASH_SOURCE=settled_cash
LIVE_BUY_CASH_FALLBACKS=    # Optional; blank means do not fall back from SettledCash
MAX_ORDER_NOTIONAL=         # Optional hard cap per order, blank disables
MAX_ORDER_QUANTITY=         # Optional hard share cap per order, blank disables
EXT_HOURS_MAX_ORDER_NOTIONAL=  # Required, or use another cap, for Web API extended-hours orders
MAX_BUYS_PER_DAY=4
MAX_ORDERS_PER_DAY=8
MIN_SECONDS_BETWEEN_ORDERS=0
DAILY_LOSS_STOP_PCT=
PREMARKET_MIN_COMPLETED_BARS=3
TELEGRAM_ENABLED=False
TELEGRAM_BOT_TOKEN=         # Put the real token only in local .env
TELEGRAM_CHAT_ID=           # Example: -1001234567890 or your group id
TELEGRAM_NOTIFY_ORDER_FILLED=True
```

In `slot_percent` mode, slot size is based on the bot's daily/session starting
Net Liquidation snapshot and the live slot ledger. `LIVE_BUY_ALLOCATION_PCT` is
used only by `LIVE_ORDER_SIZING_MODE=allocation_percent`.

For cash-account settlement safety, keep `LIVE_BUY_CASH_SOURCE=settled_cash`
and leave `LIVE_BUY_CASH_FALLBACKS` blank. When Web API execution data is
available, the bot also estimates unsettled proceeds from recent stock SELL
fills and caps fallback/total-cash sizing until the estimated T+1 business-day
settlement date. IBKR `SettledCash` remains the primary authority.

To use the main V4B fee-aware RSI/Bollinger strategy, set:

```
STRATEGY=RSI_BB_FEE_AWARE_V4B
FEE_PER_TRADE=2.5
FEE_AWARE_ESTIMATED_TRADE_NOTIONAL=1000
FEE_AWARE_MIN_REWARD_PCT=0.004
FEE_AWARE_REWARD_FEE_MULTIPLE=2.0
FEE_AWARE_MIN_BB_WIDTH_PCT=0.006
FEE_AWARE_REQUIRE_CONFIRMATION=True
FEE_AWARE_EXIT_RSI=55
FEE_AWARE_V4_SCALE_IN_ENABLED=True
FEE_AWARE_V4_SCALE_IN_INITIAL_FRACTION=0.4
FEE_AWARE_V4_SCALE_IN_FRACTION=0.6
FEE_AWARE_V4_SCALE_IN_DROP_PCT=0.0125
```

This version keeps the RSI + Bollinger setup, skips trades unless the expected
move can cover fees, and allows one DCA scale-in when price moves far enough
below the first entry.

> **Safety:** `IS_LIVE_ACTIVE=False` by default. The bot will not place real orders
> until you click "Turn ON Trading" on the dashboard **and** the data feed is fresh.

---

## 2. Docker Commands

### Linux

All commands run from `src/bot/`:

```bash
# Build everything
docker compose -f docker-compose.yml -f docker-compose.linux.yml build

# Start all services (gateway + bot)
docker compose -f docker-compose.yml -f docker-compose.linux.yml up -d

# Stop all services
docker compose -f docker-compose.yml -f docker-compose.linux.yml down

# Restart just the bot (after code changes)
docker compose -f docker-compose.yml -f docker-compose.linux.yml build bot
docker compose -f docker-compose.yml -f docker-compose.linux.yml up -d bot

# Restart just the gateway
docker compose -f docker-compose.yml -f docker-compose.linux.yml up -d ib_web_gateway

# View all logs (live stream)
docker compose -f docker-compose.yml -f docker-compose.linux.yml logs -f

# View only gateway logs
docker compose -f docker-compose.yml -f docker-compose.linux.yml logs -f ib_web_gateway

# View only bot logs
docker compose -f docker-compose.yml -f docker-compose.linux.yml logs -f bot

# View last 50 lines of bot logs
docker compose -f docker-compose.yml -f docker-compose.linux.yml logs --tail 50 bot

# Clean up orphan containers
docker compose -f docker-compose.yml -f docker-compose.linux.yml up -d --remove-orphans
```

**Linux networking:** Uses `network_mode: host`. The bot and gateway both bind directly
to the host, so the dashboard is at `http://localhost:5050` and the gateway at
`https://localhost:5000`.

---

### Windows

All commands run from `src\bot\`:

```powershell
# Build everything
docker compose -f docker-compose.yml -f docker-compose.local.yml build

# Start all services (gateway + bot)
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d

# Stop all services
docker compose -f docker-compose.yml -f docker-compose.local.yml down

# Restart just the bot (after code changes)
docker compose -f docker-compose.yml -f docker-compose.local.yml build bot
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d bot

# Restart just the gateway
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d ib_web_gateway

# View all logs (live stream)
docker compose -f docker-compose.yml -f docker-compose.local.yml logs -f

# View only gateway logs
docker compose -f docker-compose.yml -f docker-compose.local.yml logs -f ib_web_gateway

# View only bot logs
docker compose -f docker-compose.yml -f docker-compose.local.yml logs -f bot

# View last 50 lines of bot logs
docker compose -f docker-compose.yml -f docker-compose.local.yml logs --tail 50 bot
```

**Windows networking:** Uses Docker Compose bridge networking with port mapping.
Containers communicate via the internal hostname `ib_web_gateway`. Ports `5050` and
`5000` are mapped to `localhost`.

---

## 3. Login Flow

After `up -d`, IBeam will automatically:

1. Start the IBKR Client Portal Gateway
2. Fill in your username and password
3. Select "IB Key" from the 2FA dropdown
4. Send a **push notification to your phone**

**You need to approve the notification on your phone within 5 minutes.**

Watch the gateway logs to see when the notification is sent:

```bash
# Linux
docker compose -f docker-compose.yml -f docker-compose.linux.yml logs -f ib_web_gateway

# Windows
docker compose -f docker-compose.yml -f docker-compose.local.yml logs -f ib_web_gateway
```

You'll see this sequence:

```
Submitting the form
Required to select a 2FA method.
Available 2FA methods: ['Mobile Authenticator App', 'IB Key']
2FA method "IB Key" selected successfully.        <-- approve on your phone NOW
Webpage displayed "Client login succeeds"
AUTHENTICATED Status(... authenticated=True ...)
```

Once authenticated, the bot detects it automatically within 30 seconds.

If the dashboard shows **BROKER: DISCONNECTED** or **WAITING FOR LOGIN**, open
the Live page and click **Try Broker Auth** in the Broker Connection card. This
manually asks the IBKR Gateway/IBeam session to reauthenticate. You may still
need to approve the IBKR Mobile push notification; the button only triggers the
gateway flow and then refreshes the bot status.

---

## 4. Dashboard

Open `http://localhost:5050` (or `http://<linux-ip>:5050`).

### Status Bar Pills

| Pill | Meaning |
|------|---------|
| **MODE: LIVE / PAPER** | Trading mode from `.env` |
| **SYMBOL: TSLA** | Active trading symbol |
| **DATA: YFINANCE** | Active market data source |
| **FEED: FRESH / STALE** | Whether market data is current |
| **BROKER: CONNECTED / DISCONNECTED** | Gateway authentication status |
| **SESSION: OPEN / CLOSED** | Configured US Eastern trading window for live bar processing and orders |
| **BOT: ACTIVE / PAUSED / INACTIVE** | Order submission state (hover for pause reason) |

### Bot Pause Reasons

The bot can be paused for one or more reasons:

| Reason | What to do |
|--------|------------|
| **Trading is manually turned OFF** | Click "Turn ON Trading" on the dashboard |
| **Outside configured trading hours** | The gateway can stay online, but live candles/signals/orders are paused until the next configured ET session |
| **Primary feed stale** | Market is closed, or yfinance is down. Resumes automatically when fresh data arrives |
| **Broker disconnected** | Gateway session lost. IBeam will retry login automatically; approve the phone notification |

The Live page also shows a **Ready To Trade** panel with the active pause
reasons, feed lag, last data error, indicator warm-up state, pending retry
signal, broker heartbeat, and account/position summaries. Use this panel before
turning trading on, especially after a broker reconnect.

When `TRADING_MODE=LIVE`, the dashboard requires typing `LIVE` before enabling
order submission. Optional `MAX_ORDER_NOTIONAL` and `MAX_ORDER_QUANTITY` caps
block oversized orders before they are sent to IBKR.

### Telegram Notifications

The bot can send Telegram messages to a group when broker orders are confirmed
filled. Configure these values in local `.env` only:

```
TELEGRAM_ENABLED=True
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_group_or_chat_id
TELEGRAM_PARSE_MODE=
TELEGRAM_TIMEOUT_SECONDS=10
TELEGRAM_NOTIFY_ORDER_SENT=False
TELEGRAM_NOTIFY_ORDER_FILLED=True
TELEGRAM_NOTIFY_TEST_BUTTON=True
```

Use **Send Telegram Test** on the Live dashboard to send a status report and
verify the bot can post to the group. If it fails, check that the bot is a
member of the group, the chat id is correct, and the token has not been revoked.
If a token was copied into chat or logs, rotate it in BotFather and update local
`.env`.

Default live trading sessions are configured in `.env` with `TRADING_SESSIONS` in
US Eastern time: overnight `20:00-03:50`, pre-market `04:00-09:30`,
regular `09:30-16:00`, and after-hours `16:00-20:00`. At session boundaries the
bot waits for a completed live bar before evaluating a signal, so the first
5-minute pre-market decision happens after the `04:00-04:05` bar is available.
The gateway heartbeat remains active outside trading hours where possible to
reduce repeated 2FA approvals.

`PREMARKET_MIN_COMPLETED_BARS=3` adds an extra pre-market safety delay. With
5-minute bars, the bot skips the first two completed pre-market bars and the
first possible pre-market signal/order evaluation is after the `04:10-04:15`
bar closes.

`LIVE_STARTUP_MIN_BARS=6` is a short live-feed stability gate. Full indicator
maturity is tracked separately from startup readiness, so the dashboard can show
when trading is operational while longer-lookback indicators continue warming.
Broker history warm-up is requested with an adaptive day window based on the
strategy's required bars, clamped by `BROKER_HISTORY_MIN_DAYS` and
`BROKER_HISTORY_MAX_DAYS`.

For RSI+Bollinger strategies, extended-hours BUY entries use stricter defaults
(`EXT_HOURS_OVERSOLD=20`, `EXT_HOURS_BB_STD=3.0`) **and** a multi-bar
persistence check: the BUY setup (RSI oversold + price at lower band) must hold
for `EXT_HOURS_BUY_CONFIRMATION_BARS` consecutive completed bars before firing.
SELL exits use the same persistence concept against the regular Bollinger
upper band (`EXT_HOURS_SELL_CONFIRMATION_BARS`) so risk-management exits stay
reachable. The fee-aware mean-reversion exit (price back at the midline plus
`FEE_AWARE_EXIT_RSI`) remains a single-bar hard trigger so positions can always
be closed. Regular-hours signals always use a single-bar trigger.

`EXT_HOURS_RESET_INDICATORS_ON_SESSION_CHANGE=True` isolates indicator data to
the active session so prior-session drift does not drag RSI/BB calculations.

The legacy `EXT_HOURS_VOLUME_FILTER_ENABLED` is now off by default. yfinance
reports `volume=0` for pre/after-hours bars, which makes a volume threshold
useless; only re-enable the filter when the broker feed is the active source
during extended hours and you have verified non-zero volumes in the bar
stream.

Live dashboard state is persisted to JSON (`LIVE_STATE_PATH`, default
`simulation/results/live_state.json`) so recent candles, logs, account data,
positions, orders, executions, commissions, and net amounts can be restored
after a restart.

The live readiness panel shows any execution-derived unsettled sale proceeds
and the next estimated settlement time. This is a secondary visibility guard;
IBKR account balances still decide the final buyable cash.

---

## 5. Architecture

```
┌──────────────────────────────────────────┐
│              Your Machine                │
│                                          │
│  ┌─────────────┐    ┌────────────────┐   │
│  │  ib_bot     │    │ ib_web_gateway │   │
│  │  (Python)   │───>│ (IBeam+Java)   │───>  IBKR Servers
│  │  :5050      │    │ :5000          │   │
│  │  Dashboard  │    │ Client Portal  │   │
│  └─────────────┘    └────────────────┘   │
│        │                                 │
│        ▼                                 │
│   yfinance (market data)                 │
└──────────────────────────────────────────┘
```

- **ib_web_gateway** — IBeam container that manages the IBKR Client Portal Gateway.
  Handles login, session keep-alive, and reauthentication.
- **ib_bot** — The trading bot. Fetches market data, runs strategy signals,
  submits orders via the gateway, and serves the dashboard.

---

## 6. Files Overview

| File | Purpose |
|------|---------|
| `.env` | All configuration (credentials, strategy, thresholds) |
| `docker-compose.yml` | Base service definitions |
| `docker-compose.linux.yml` | Linux override (host networking) |
| `docker-compose.local.yml` | Windows override (bridge networking + port mapping) |
| `inputs/conf.yaml` | IBKR Gateway IP whitelist (allows Docker subnets) |
| `inputs/custom_two_fa_handler.py` | TOTP 2FA handler (for future use if you enable TOTP) |
| `core/ib_web_client.py` | IBKR Web API client with reconnection logic |
| `main.py` | Bot orchestrator (strategy loop, data feeds, trade execution) |
| `web/dashboard.py` | Flask dashboard API and server |
| `web/templates/live.html` | Live trading dashboard page |

---

## 7. Troubleshooting

### "BROKER: DISCONNECTED" after startup
The gateway hasn't authenticated yet. Watch the gateway logs and approve the
phone notification. The bot retries every 30 seconds. You can also use
**Try Broker Auth** on the Live dashboard to manually trigger the gateway
reauthentication flow.

### "FEED: STALE"
Market data is too old (> 900 seconds by default). This is normal when the
US stock market is closed. It auto-resolves when the market opens.

### Gateway shows "Login timeout counted as a failed login attempt"
You didn't approve the phone notification in time. IBeam will retry after 60
seconds. Keep your phone nearby.

### Bot says "PAUSED" even though broker is connected
Check the pause reason on the dashboard. Most likely the data feed is stale
(market closed) or trading is manually turned off.

### SSL errors in bot logs
If you see `SSLV3_ALERT_ILLEGAL_PARAMETER`, the bot's TLS adapter isn't stripping
the SNI hostname. This only affects Windows (bridge networking). The fix is already
built into `ib_web_client.py`.
