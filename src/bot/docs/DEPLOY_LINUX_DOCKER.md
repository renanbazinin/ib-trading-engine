# Linux Docker Deployment Guide (IBKR + Bot)

This guide deploys the bot on Linux with Docker, while keeping Interactive Brokers connectivity reliable.

## 1) Recommended Architecture

Recommended production topology:

- IB Gateway runs on the Linux host (or another stable machine).
- Bot runs in Docker using the project compose file.
- Bot connects to IB Gateway socket port.
- Dashboard is served on port 5000.

Why this is recommended:

- IB Gateway login and 2FA are easier to manage outside the bot container.
- Bot remains isolated and easy to redeploy.
- This matches your current `docker-compose.yml` using host networking.

## 2) Server Requirements

Minimum practical baseline:

- Ubuntu 22.04 or 24.04
- 2 vCPU
- 4 GB RAM
- 20 GB disk
- Stable outbound internet
- Correct system time (NTP enabled)

Ports:

- Dashboard: 5000 (optional public access, preferably protected)
- IB socket (host-local preferred): 4001 or 4002 or 7496 or 7497

## 3) Install Docker and Compose Plugin

Run on Linux host:

```bash
sudo apt update
sudo apt install -y ca-certificates curl gnupg lsb-release

sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

sudo usermod -aG docker $USER
newgrp docker

docker --version
docker compose version
```

## 4) Copy Project to Server

Place the repository on server, then go to bot directory:

```bash
cd /path/to/TWS\ API/src/bot
```

(Use your real path.)

## 5) Prepare Environment File

Create env file from template:

```bash
cp .env.example .env
```

Edit `.env` with at least:

- `TRADING_MODE=PAPER` for first deployment
- `DEFAULT_SYMBOL=TSLA` (or your symbol)
- `TWS_HOST=127.0.0.1` if IB Gateway is on same host
- `TWS_PORT=4001` (live gateway) or `4002` (paper gateway)
- `CLIENT_ID=1`
- `MARKET_DATA_PRIMARY=yfinance`
- `MARKET_DATA_FALLBACK_ENABLED=True`
- `IS_LIVE_ACTIVE=False` initially

Optional but useful:

- `LIVE_CANDLE_BUFFER_MAX=500`
- `LIVE_LOG_BUFFER_MAX=1000`
- `NEAR_BAND_PCT=0.003`
- `NEAR_SMI_GAP=4.0`

## 6) Configure IB Gateway / TWS API Access

In IB Gateway or TWS settings:

- Enable API socket clients.
- Set socket port (must match `.env` `TWS_PORT`).
- Disable Read-Only API if you want real order submission.
- Add trusted IPs (at least `127.0.0.1` if local).
- Keep only one process using the same `CLIENT_ID`.

Quick socket test from Linux host:

```bash
nc -zv 127.0.0.1 4001
```

(Use your real host/port.)

## 7) Build and Start with Docker Compose

From `src/bot`:

```bash
docker compose build
docker compose up -d
```

Check status:

```bash
docker compose ps
docker compose logs -f bot
```

Expected early log lines include:

- Loaded config
- Connecting to TWS IBKR
- Connected! Next valid order ID
- Primary market data provider started: yfinance

## 8) Validate Dashboard and APIs

From host:

```bash
curl -I http://127.0.0.1:5000/live
curl http://127.0.0.1:5000/api/bot/status
curl "http://127.0.0.1:5000/api/live/candles?limit=3"
curl "http://127.0.0.1:5000/api/live/logs?limit=5"
```

Open in browser:

- `http://SERVER_IP:5000/live`

Confirm:

- Live chart draws candles.
- Live price and HH:MM:SS update show.
- Candle and near buy/sell logs populate.

## 9) Safe Go-Live Procedure

1. Start with `TRADING_MODE=PAPER` and `IS_LIVE_ACTIVE=False`.
2. Validate data flow, signals, and logs for at least one market session.
3. Set `IS_LIVE_ACTIVE=True` only when ready.
4. For real account, set `TRADING_MODE=LIVE` and verify IB port is correct.

## 10) Daily Operations

Restart bot:

```bash
docker compose restart bot
```

Stop bot:

```bash
docker compose stop bot
```

Update after code changes:

```bash
docker compose build
docker compose up -d
```

Watch logs:

```bash
docker compose logs -f bot
```

Results storage path:

- `src/bot/simulation/results` is mounted and persisted.

## 11) Auto-Start on Reboot

Current compose already has:

- `restart: unless-stopped`

That usually handles reboot recovery after Docker daemon starts.

If you want strict boot ordering, add a systemd wrapper (optional).

## 12) Security Hardening

Recommended:

- Do not expose IB socket ports publicly.
- Restrict dashboard port with firewall and reverse proxy auth.
- Allow only your IP to access 5000.
- Keep `.env` private.

Example UFW approach:

```bash
sudo ufw allow OpenSSH
sudo ufw allow from YOUR_IP to any port 5000 proto tcp
sudo ufw enable
sudo ufw status
```

## 13) Troubleshooting

### A) Bot cannot connect to IB

Check:

- IB Gateway/TWS running and logged in.
- API socket enabled.
- `TWS_HOST` and `TWS_PORT` match runtime.
- Trusted IP list includes bot source host.
- Another app is not conflicting with same `CLIENT_ID`.

### B) Dashboard loads but chart/logs empty

Check:

- `docker compose logs -f bot` for market-data lines.
- `/api/live/candles` and `/api/live/logs` endpoint output.
- yfinance connectivity from host/container.

### C) Live orders not firing

Check:

- `IS_LIVE_ACTIVE=True`
- `TRADING_MODE=LIVE`
- stale-data guard status in `/api/bot/status`

### D) Immediate source switching at startup

This can happen before first primary bars arrive. It should self-recover after yfinance backfill.

## 14) Alternative Topology Notes

If IB Gateway runs on another machine:

- Set `TWS_HOST` to that machine IP.
- Ensure firewall allows that socket inbound.
- Keep dashboard exposure locked down.

If you later want fully containerized IB Gateway, use a dedicated IB Gateway container stack and point this bot to it via network alias and port, but host-managed Gateway remains the simplest stable production path.
