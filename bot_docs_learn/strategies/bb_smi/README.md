# BB_SMI Strategy

Source:

```text
src/bot/strategies/bb_smi.py
```

Class:

```text
BBSmiStrategy
```

Factory name:

```text
BB_SMI
```

This is also the fallback strategy if an unknown strategy name is passed to `create_strategy()`.

## Purpose

`BB_SMI` is a Bollinger Band plus Stochastic Momentum Index mean-reversion strategy.

It looks for momentum turning up while price is near the lower Bollinger Band, and momentum turning down while price is near the upper Bollinger Band.

## Indicators

The strategy calculates:

- Bollinger Bands on close.
- SMI on close.
- SMI signal line.

Default parameters:

```text
BB_LENGTH=20
BB_STD=2.0 in strategy factory fallback
SMI_FAST=10
SMI_SLOW=3
SMI_SIG=3
NEAR_BAND_PCT=0.001
```

The orchestrator default for `BB_STD` is `2.2`, but the factory fallback default for `BB_SMI` is `2.0` if not supplied.

## Warmup

Minimum bars required:

```text
bb_length + smi_fast + smi_slow
```

With defaults:

```text
20 + 10 + 3 = 33 bars
```

On 5-minute bars, that is about 165 minutes of bars, though backfill normally preloads enough history.

## BUY Logic

A BUY signal requires:

1. Previous SMI below previous SMI signal.
2. Latest SMI above latest SMI signal.
3. Latest close at or below the lower-band threshold.

Threshold:

```text
buy_threshold = BB_LOWER * (1 + near_band_pct)
```

So with `near_band_pct=0.001`, price can be up to about 0.1% above the lower band and still count as near.

## SELL Logic

A SELL signal requires:

1. Previous SMI above previous SMI signal.
2. Latest SMI below latest SMI signal.
3. Latest close at or above the upper-band threshold.

Threshold:

```text
sell_threshold = BB_UPPER * (1 - near_band_pct)
```

So with `near_band_pct=0.001`, price can be up to about 0.1% below the upper band and still count as near.

## Trading Interpretation

This strategy wants confirmation from momentum, not just a band touch.

It is trying to avoid buying a falling lower-band touch until SMI crosses up, and avoid selling an upper-band touch until SMI crosses down.

## Simulation Variants

Current simulation config uses two `BB_SMI` variants:

```text
SCALPER_10_5
SWING_30_20
```

`SCALPER_10_5`:

```text
bb_length=10
bb_std=1.5
smi_fast=5
smi_slow=3
smi_sig=3
near_band_pct=0.0005
```

This is faster and more sensitive.

`SWING_30_20`:

```text
bb_length=30
bb_std=2.0
smi_fast=20
smi_slow=5
smi_sig=5
near_band_pct=0.001
```

This is slower and more stable.

## Risks

- It can miss reversals that do not produce a clean SMI cross near the band.
- It can still enter during strong trend continuation if SMI briefly crosses.
- It does not know about fees, spread, slippage, or account exposure.
- Risk is controlled outside the strategy by the orchestrator.

