# Overall Strategy Model

## Big Picture

The bot is built around mean-reversion strategies on 5-minute equity bars.

The common idea is:

1. Wait for price to become stretched.
2. Use an indicator to decide whether the stretch is actionable.
3. Enter long only.
4. Exit when the market reverts or becomes overbought.
5. Let the orchestrator handle timing, broker safety, sizing, and order routing.

The strategies are deliberately signal-only. They do not directly know about account balance, broker connection, trading hours, stale feeds, order types, or dashboard switches.

That separation is important:

- Strategy files answer: should this bar be BUY, SELL, or HOLD?
- `main.py` answers: are we allowed to act on that signal?
- Broker clients answer: how do we submit the order?
- Simulation manager answers: what would have happened if this strategy traded the same stream?

## Strategy Families

There are four registered live/simulation strategies:

- `BB_SMI`: Bollinger Bands plus SMI cross.
- `RSI_BB`: RSI plus Bollinger Bands plus trend filter.
- `RSI_BB_FEE_AWARE`: RSI plus Bollinger Bands with fee/reward, volatility, confirmation, and mean-reversion exit filters.
- `RSI_ONLY`: pure RSI thresholds.

The current local live strategy is:

```text
RSI_BB_FEE_AWARE
```

## Common Trading Style

All registered strategies are long-only mean-reversion systems.

They generally look for one of these entry ideas:

- Price near or below the lower Bollinger Band.
- RSI oversold.
- SMI crossing up after weakness.
- Enough expected rebound to justify fees.
- Optional confirmation that momentum is turning up.

They generally look for one of these exit ideas:

- RSI overbought.
- Price near or above the upper Bollinger Band.
- Price reverting to Bollinger midline with RSI strong enough.
- SMI crossing down near the upper band.

## Live Execution Model

The strategy emits:

```text
BUY
SELL
HOLD
```

The live bot only acts when:

- Backfill phase is finished.
- Trade-ready time has arrived.
- Trading hours allow the current bar.
- Live switch is active.
- Broker is connected and authenticated.
- Stale-data guard is clear.
- Current position state allows the signal.

Position state:

```text
NONE -> BUY can open LONG
LONG -> BUY can scale in if slots remain
LONG -> SELL can close the full symbol position
```

The system does not intentionally short. Scale-in capacity is bounded by broker-reported symbol exposure, slot settings, cash, and order caps.

## Risk And Sizing

The strategy does not size orders.

Live BUY sizing uses:

```text
default slot_percent mode:
one slot = daily/session starting NetLiquidation * LIVE_SLOT_ALLOCATION_PCT
max slots = LIVE_MAX_POSITION_SLOTS
BUY size = min(one slot, remaining symbol slot capacity, cash, caps)
```

Live SELL sizing uses:

```text
all currently reported shares for the configured symbol
```

Optional caps can block orders:

```text
MAX_ORDER_QUANTITY
MAX_ORDER_NOTIONAL
```

The bot persists a live slot ledger for BUY lots and uses broker-reported exposure as a backstop. This keeps a single BUY from deploying nearly the whole account and allows controlled scaling only while symbol slot capacity remains.

## Current Live Strategy Interpretation

With `STRATEGY=RSI_BB_FEE_AWARE` and `DEFAULT_SYMBOL=TSLA`, the live bot is trying to trade TSLA mean reversion on 5-minute bars.

The selected strategy is more conservative than plain `RSI_BB` because a BUY needs all of these:

- RSI below oversold threshold.
- Price near the lower Bollinger Band.
- Bear-market filter passed.
- Bollinger Band width wide enough.
- Expected move back to Bollinger midline large enough to cover fees with margin.
- Confirmation of reversal if enabled.

The current local fee-aware settings are:

```text
FEE_PER_TRADE=2.5
FEE_AWARE_ESTIMATED_TRADE_NOTIONAL=1000
FEE_AWARE_MIN_REWARD_PCT=0.006
FEE_AWARE_REWARD_FEE_MULTIPLE=3.0
FEE_AWARE_MIN_BB_WIDTH_PCT=0.008
FEE_AWARE_REQUIRE_CONFIRMATION=True
FEE_AWARE_EXIT_RSI=55
```

Given those values, the required reward is the larger of:

```text
0.6%
round-trip-fee-percent * 3
```

With a $2.50 fee per side and a $1000 estimated trade notional:

```text
round trip fees = $5
round-trip-fee-percent = 0.5%
fee multiple requirement = 1.5%
required reward = max(0.6%, 1.5%) = 1.5%
```

So a BUY needs the distance from current price to Bollinger midline to be at least about 1.5%, in addition to the other filters.

The fee-aware strategy uses the planned live BUY notional when the orchestrator can calculate one. `FEE_AWARE_ESTIMATED_TRADE_NOTIONAL=1000` remains the fallback for simulations and paths where planned notional is unavailable.

## Simulations

Simulations are configured in:

```text
src/bot/simulation/config/simulation_config.json
```

Currently configured simulations:

- `RSI_BB_DEFAULT`
- `RSI_ONLY_5M`
- `SCALPER_10_5`
- `SWING_30_20`

Simulations use the same `create_strategy()` factory and process the same normalized bars. Their BUY sizing also uses about 95% of simulated cash, not the `trade_quantity` field, even though `trade_quantity` is loaded.

## Practical Reading Of The Strategy Set

The strategies form a spectrum:

- `RSI_ONLY`: simplest and noisiest. It only cares if RSI is extreme.
- `RSI_BB`: adds price location and trend context.
- `BB_SMI`: focuses on momentum crossover at Bollinger extremes.
- `RSI_BB_FEE_AWARE`: most selective. It tries to avoid trades where the expected mean reversion is too small after fees.

For live TSLA, `RSI_BB_FEE_AWARE` is the best aligned with avoiding churn because TSLA can generate many intraday oversold/overbought touches that are not worth trading after costs and slippage.

