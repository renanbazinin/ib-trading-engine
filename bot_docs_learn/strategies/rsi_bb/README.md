# RSI_BB Strategy

Source:

```text
src/bot/strategies/rsi_bb.py
```

Class:

```text
RsiBBStrategy
```

Factory name:

```text
RSI_BB
```

## Purpose

`RSI_BB` is an RSI plus Bollinger Band mean-reversion strategy with an optional SMA trend filter.

It buys when RSI is oversold and price is near the lower Bollinger Band. It sells when RSI is overbought and price is near the upper Bollinger Band.

## Indicators

The strategy calculates:

- RSI on close.
- Bollinger Bands on close.
- Trend SMA on close when `trend_sma_period > 0`.

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
```

## Warmup

Minimum bars required:

```text
max(rsi_period, bb_length, trend_sma_period)
```

With defaults:

```text
50 bars
```

## Trend Filter

If `trend_sma_period > 0`, the strategy labels trend as:

```text
BULL if close > TREND_SMA
BEAR if close <= TREND_SMA
```

If trend is `BEAR`, a BUY needs a deeper RSI dip:

```text
RSI < BEAR_DIP_RSI
```

Unless:

```text
BEAR_DIP_RSI=-1
```

which disables the bear-market extra dip requirement.

## BUY Logic

A BUY signal requires:

1. RSI below oversold threshold.
2. Close near the lower Bollinger Band.
3. If trend is bearish, RSI must also be below `bear_dip_rsi`.

Band tolerance:

```text
tolerance = (BB_UPPER - BB_LOWER) * bb_tolerance
price_at_lower = close <= BB_LOWER + tolerance
```

## SELL Logic

A SELL signal requires:

1. RSI above overbought threshold.
2. Close near the upper Bollinger Band.

Band tolerance:

```text
tolerance = (BB_UPPER - BB_LOWER) * bb_tolerance
price_at_upper = close >= BB_UPPER - tolerance
```

## Trading Interpretation

This is a classic mean-reversion setup:

- RSI says momentum is stretched.
- Bollinger position says price is stretched relative to recent volatility.
- SMA trend filter makes bearish entries stricter.

Compared with `RSI_ONLY`, this strategy avoids buying every oversold RSI reading. It requires price to also be near the lower band.

## Simulation Variant

Current simulation config includes:

```text
RSI_BB_DEFAULT
```

with the standard defaults.

## Risks

- In strong trends, RSI can stay oversold or overbought longer than expected.
- The strategy does not check whether expected reward covers fees.
- The exit waits for upper-band overbought conditions, which can delay exits after a partial mean reversion.
- Sizing and broker risk are handled outside the strategy.

