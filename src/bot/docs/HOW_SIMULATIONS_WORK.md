# How Simulations Work

## Overview
Simulations run in parallel with the live bot and receive the same normalized market bars as live strategy logic.
They are virtual portfolios and do not place real orders.

Main file: src/bot/simulation/sim_manager.py

## Configuration Source
Simulations are loaded from JSON:
- simulation/config/simulation_config.json

Loader and validation:
- core/sim_config_loader.py

Config areas:
- defaults: starting_balance, trade_quantity, max_recent_events
- export_policy: summary filename, summary frequency, recent limit
- simulations: per-simulation params and enabled flag

## Runtime Model
The simulation manager creates one VirtualPortfolio per enabled simulation config.
Each VirtualPortfolio has:
- Own strategy instance (BBSmiStrategy).
- Own balance/equity/position state.
- Own compact recent signal and trade buffers.

Source behavior:
- Primary bars come from yfinance.
- If the primary feed is stale, orchestration can switch to broker fallback bars.
- Simulations continue processing whichever source is currently active.

## Processing Flow
For each incoming bar:
1. Portfolio adds bar to its strategy dataframe.
2. Indicators are recalculated.
3. Strategy returns BUY/SELL/HOLD.
4. BUY/SELL creates a signal event.
5. If order condition is valid, a virtual trade event is created and portfolio state updates.

Decision rules in current implementation:
- BUY acts only when portfolio is flat and has enough virtual balance.
- SELL acts only when portfolio currently has a virtual position.
- HOLD creates no persisted event.

## Data Saved
Summary JSON (dashboard-friendly):
- simulation/results/latest_sims.json

Event logs (compact, append-only JSONL):
- simulation/results/signal_events_YYYYMMDD.jsonl
- simulation/results/trade_events_YYYYMMDD.jsonl

Summary includes:
- Per-simulation config snapshot.
- Balance, equity, position, avg cost.
- Aggregate stats: total signals, acted signals, total trades, realized pnl.
- Recent events (bounded deque size).

## What Is Intentionally Not Logged
To avoid oversized files:
- No full bar-by-bar dump.
- No full indicator history snapshots.
- No unlimited in-memory event accumulation.

## Backfill vs Live Updates
- Historical backfill updates simulation state but does not persist event files.
- Live updates persist compact signal/trade events and refresh summary output.
- Backfill/live distinction is source-agnostic (yfinance or broker fallback).

## Changing Simulation Parameters
1. Open Config page: /config
2. Edit JSON.
3. Save config.
4. Restart bot process to apply changes.

The current behavior requires restart (no hot reload yet).

## Relationship To Live Trading Switch
- Simulations run regardless of live trading toggle.
- Turning live trading OFF only prevents real order placement.
- Simulations continue learning and exporting results.
- Stale-data guard can pause real order placement automatically; simulations still continue.

## Common Issues
If simulation output is empty:
- Ensure at least one simulation is enabled in config.
- Ensure primary or fallback feed is producing bars (check dashboard source/lag pills).
- Check file write permissions for simulation/results.

If config save fails:
- JSON format error or invalid field type/range.
- Review validation error message from /api/config/simulations.
