# RSI_BB_FEE_AWARE Strategy

Source:

```text
src/bot/strategies/rsi_bb_fee_aware.py
```

Class:

```text
RsiBBFeeAwareStrategy
```

Factory name:

```text
RSI_BB_FEE_AWARE
```

Current local live strategy:

```text
STRATEGY=RSI_BB_FEE_AWARE
DEFAULT_SYMBOL=TSLA
```

## Purpose

`RSI_BB_FEE_AWARE` is a stricter version of `RSI_BB`.

It still looks for mean reversion from an oversold lower-band condition, but it only buys when the expected move back to the Bollinger midline is large enough to justify fees and when the band is wide enough to imply usable volatility.

It also has an earlier mean-reversion exit: price back to Bollinger midline with RSI above an exit threshold.

## Indicators

The strategy calculates:

- RSI on close.
- Bollinger lower, mid, and upper bands.
- Trend SMA when `trend_sma_period > 0`.

Defaults:

```text
RSI_PERIOD=14
BB_LENGTH=20
BB_STD=2.2
OVERBOUGHT=70
OVERSOLD=33
TREND_SMA_PERIOD=50
BEAR_DIP_RSI=28
BB_TOLERANCE=0.0015
FEE_PER_TRADE=2.5
FEE_AWARE_ESTIMATED_TRADE_NOTIONAL=1000.0
FEE_AWARE_MIN_REWARD_PCT=0.006
FEE_AWARE_REWARD_FEE_MULTIPLE=3.0
FEE_AWARE_MIN_BB_WIDTH_PCT=0.008
FEE_AWARE_REQUIRE_CONFIRMATION=True
FEE_AWARE_EXIT_RSI=55
```

## Warmup

Minimum bars required:

```text
max(rsi_period, bb_length, trend_sma_period) + 1
```

With defaults:

```text
51 bars
```

The extra bar is needed because confirmation compares the latest bar with the previous bar.

## Fee Math

Round-trip fee percent:

```text
round_trip_fee_pct = (fee_per_trade * 2) / estimated_trade_notional
```

Required reward percent:

```text
required_reward_pct = max(min_reward_pct, round_trip_fee_pct * fee_reward_multiple)
```

Current local settings:

```text
fee_per_trade = 2.5
estimated_trade_notional = 1000
min_reward_pct = 0.006
fee_reward_multiple = 3.0
```

That means:

```text
round_trip_fee_pct = 5 / 1000 = 0.005 = 0.5%
fee multiple requirement = 0.5% * 3 = 1.5%
required_reward_pct = max(0.6%, 1.5%) = 1.5%
```

So the strategy needs at least about 1.5% expected reward from entry price back to Bollinger midline.

## BUY Logic

A BUY signal requires all of these:

1. RSI below oversold threshold.
2. Close near the lower Bollinger Band.
3. Bear trend filter passes.
4. Bollinger Band width is large enough.
5. Expected reward back to Bollinger midline is large enough.
6. Reversal confirmation passes, if enabled.

Detailed formulas:

```text
tolerance = (BB_UPPER - BB_LOWER) * bb_tolerance
price_at_lower = close <= BB_LOWER + tolerance
bb_width_pct = (BB_UPPER - BB_LOWER) / close
expected_reward_pct = (BB_MID - close) / close
volatility_ok = bb_width_pct >= min_bb_width_pct
reward_ok = expected_reward_pct >= required_reward_pct
```

Bear trend filter:

```text
trend != BEAR
or BEAR_DIP_RSI == -1
or RSI < BEAR_DIP_RSI
```

## Reversal Confirmation

When `FEE_AWARE_REQUIRE_CONFIRMATION=True`, the BUY also needs:

```text
latest RSI > previous RSI
and
(latest candle is green or price reclaimed the lower band)
```

In plain language, the strategy wants the dip to show at least a small turn before entering.

## SELL Logic

A SELL signal happens when either classic overbought/upper-band exit is true:

```text
RSI > OVERBOUGHT
and
close >= BB_UPPER - tolerance
```

or mean-reversion exit is true:

```text
close >= BB_MID
and
RSI >= FEE_AWARE_EXIT_RSI
```

The mean-reversion exit makes this strategy more practical for fee-aware scalping because it does not require waiting all the way to the upper band.

## Trading Interpretation For TSLA

With the current local live setup, the bot is trying to buy TSLA only when:

- TSLA is oversold on 5-minute RSI.
- TSLA is near the lower Bollinger Band.
- The band is wide enough, so the move is not too tiny.
- The move back to the midline is expected to be worth at least about 1.5%.
- There is some reversal evidence.

This is the most selective registered strategy and best matches a goal of avoiding low-quality churn.

## Important Mismatch To Know

The fee/reward calculation uses the planned live BUY notional when the orchestrator can calculate it.

Current local value:

```text
1000
```

Fallback value when planned notional is unavailable:

```text
FEE_AWARE_ESTIMATED_TRADE_NOTIONAL=1000
```

This keeps live fee filtering aligned with actual slot/fixed sizing while preserving a deterministic fallback for simulations and manual strategy use.

## Risks

- It may skip valid bounces if required reward or confirmation is too strict.
- It still assumes Bollinger midline is a reasonable reversion target.
- It does not directly account for spread/slippage beyond the fee/reward filter.
- It does not size positions; the orchestrator can still allocate nearly the whole account unless caps are configured.

