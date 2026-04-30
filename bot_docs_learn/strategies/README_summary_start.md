# Strategies

The bot registers strategies in `src/bot/strategies/strategy_factory.py`.

The factory recognizes:

- `BB_SMI`
- `RSI_BB`
- `RSI_BB_FEE_AWARE`
- `RSI_ONLY`

Any unknown strategy name falls back to `BB_SMI`.

## Quick Comparison

| Strategy | Entry Idea | Exit Idea | Selectivity |
| --- | --- | --- | --- |
| `RSI_ONLY` | RSI below oversold | RSI above overbought | Low |
| `RSI_BB` | RSI oversold and price at lower Bollinger Band | RSI overbought and price at upper Bollinger Band | Medium |
| `BB_SMI` | SMI crosses up near lower Bollinger Band | SMI crosses down near upper Bollinger Band | Medium |
| `RSI_BB_FEE_AWARE` | RSI/BB setup plus reward, volatility, fee, trend, and confirmation filters | Upper-band overbought exit or midline mean-reversion exit | High |

## Shared Interface

Each strategy exposes the same basic methods:

```text
add_bar(...)
update_indicators()
get_latest_signal()
```

`get_latest_signal()` returns:

```text
("BUY", price)
("SELL", price)
("HOLD", price_or_none)
```

The strategy does not place orders. The live orchestrator decides whether to act.

## Shared Data

Each strategy keeps a pandas dataframe of bars with these base columns:

```text
open
high
low
close
volume
barCount
wap
```

Indicator columns are added by each strategy as needed.

## Shared Live Rules Outside The Strategy

No matter which strategy is selected:

- The bot is long-only and can scale into additional BUY slots while a broker-reported long position exists.
- SELL exits the broker-reported position for the configured symbol.
- Live BUY sizing uses the configured `LIVE_ORDER_SIZING_MODE`; the safer default is ledger-backed slot sizing from the daily/session starting NetLiq snapshot.
- Live SELL sizing sells the full current position for the configured symbol.
- Trading-hours gating happens before strategy processing.
- Stale-data, broker, live-switch, and order-cap checks happen before order submission.

## Current Live Strategy

The local `.env` selects:

```text
RSI_BB_FEE_AWARE
```

That strategy is documented in:

```text
strategies/rsi_bb_fee_aware/README.md
```

