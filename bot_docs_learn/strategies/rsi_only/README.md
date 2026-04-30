# RSI_ONLY Strategy

Source:

```text
src/bot/strategies/rsi_only.py
```

Class:

```text
RsiOnlyStrategy
```

Factory name:

```text
RSI_ONLY
```

## Purpose

`RSI_ONLY` is the simplest registered strategy.

It buys when RSI is oversold and sells when RSI is overbought. It does not use Bollinger Bands, trend filters, fee filters, or confirmation.

## Indicators

The strategy calculates:

- RSI on close.

Defaults:

```text
RSI_PERIOD=14
OVERBOUGHT=70
OVERSOLD=30 in strategy factory
```

Note: the orchestrator shared config default for `OVERSOLD` is `33`, but the factory's direct default for `RSI_ONLY` is `30` if no config value is supplied.

## Warmup

The strategy calculates RSI only after:

```text
len(df) >= rsi_period + 1
```

Unlike the other strategies, `RsiOnlyStrategy` does not define `min_bars_required`. The live orchestrator falls back to `50` bars for dashboard indicator readiness when a strategy lacks that property.

## BUY Logic

A BUY signal requires:

```text
RSI < oversold
```

## SELL Logic

A SELL signal requires:

```text
RSI > overbought
```

## Trading Interpretation

This strategy treats RSI extremes as enough by themselves.

It is useful as a baseline simulation or debugging strategy because it is easy to reason about. It is usually less selective than the Bollinger strategies.

## Simulation Variant

Current simulation config includes:

```text
RSI_ONLY_5M
```

with:

```text
rsi_period=14
overbought=70
oversold=30
```

## Risks

- RSI can remain oversold during a strong downtrend.
- RSI can remain overbought during a strong uptrend.
- There is no price-location filter, so signals can fire away from volatility extremes.
- There is no fee or spread awareness.
- This is best treated as a simple baseline rather than the most production-ready live strategy.

