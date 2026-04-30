import unittest

import pandas as pd

from strategies.rsi_bb_fee_aware import RsiBBFeeAwareStrategy
from strategies.strategy_factory import create_strategy


def make_strategy(**overrides):
    config = {
        "fee_per_trade": 2.5,
        "estimated_trade_notional": 1000.0,
        "min_reward_pct": 0.006,
        "fee_reward_multiple": 3.0,
        "min_bb_width_pct": 0.008,
        "require_confirmation": True,
    }
    config.update(overrides)
    return RsiBBFeeAwareStrategy(**config)


def seed_df(strategy, latest_mid=100.0, latest_close=95.0, latest_rsi=27.0, latest_volume=1200.0):
    strategy.df = pd.DataFrame(
        [
            {
                "open": 97.0,
                "high": 98.0,
                "low": 94.0,
                "close": 96.0,
                "volume": 1000.0,
                "barCount": 0,
                "wap": 96.0,
                "RSI": 25.0,
                "BB_LOWER": 96.5,
                "BB_MID": 100.0,
                "BB_UPPER": 104.0,
                "TREND_SMA": 90.0,
            },
            {
                "open": 94.0,
                "high": 96.0,
                "low": 93.0,
                "close": latest_close,
                "volume": latest_volume,
                "barCount": 0,
                "wap": latest_close,
                "RSI": latest_rsi,
                "BB_LOWER": 95.5,
                "BB_MID": latest_mid,
                "BB_UPPER": 105.0,
                "TREND_SMA": 90.0,
            },
        ],
        index=pd.to_datetime(["2026-04-21 04:05", "2026-04-21 04:10"]),
    )


def seed_extended_streak_df(
    strategy,
    setup_bars,
    rsi_value=25.0,
    bb_lower=95.5,
    bb_mid=100.0,
    bb_upper=105.0,
    ext_bb_lower=94.5,
    ext_bb_upper=106.0,
    close_value=94.0,
    volume_value=1200.0,
    failing_bar_close=98.0,
    failing_bar_rsi=40.0,
):
    """Seed a DataFrame with `setup_bars` consecutive BUY-setup bars at the tail.

    A neutral 'failing' bar is placed before the streak so we exercise the
    streak-detection logic instead of relying on uniformly-true rows.
    """
    rows = []
    timestamps = []
    base_minute = 0

    rows.append(
        {
            "open": failing_bar_close - 1.0,
            "high": failing_bar_close + 1.0,
            "low": failing_bar_close - 2.0,
            "close": failing_bar_close,
            "volume": volume_value,
            "barCount": 0,
            "wap": failing_bar_close,
            "RSI": failing_bar_rsi,
            "BB_LOWER": bb_lower,
            "BB_MID": bb_mid,
            "BB_UPPER": bb_upper,
            "EXT_BB_LOWER": ext_bb_lower,
            "EXT_BB_MID": bb_mid,
            "EXT_BB_UPPER": ext_bb_upper,
            "TREND_SMA": 90.0,
        }
    )
    timestamps.append(f"2026-04-21 04:{base_minute:02d}")

    for _ in range(setup_bars):
        base_minute += 5
        rows.append(
            {
                "open": close_value + 0.5,
                "high": close_value + 1.0,
                "low": close_value - 0.5,
                "close": close_value,
                "volume": volume_value,
                "barCount": 0,
                "wap": close_value,
                "RSI": rsi_value,
                "BB_LOWER": bb_lower,
                "BB_MID": bb_mid,
                "BB_UPPER": bb_upper,
                "EXT_BB_LOWER": ext_bb_lower,
                "EXT_BB_MID": bb_mid,
                "EXT_BB_UPPER": ext_bb_upper,
                "TREND_SMA": 90.0,
            }
        )
        timestamps.append(f"2026-04-21 04:{base_minute:02d}")

    strategy.df = pd.DataFrame(rows, index=pd.to_datetime(timestamps))


class RsiBBFeeAwareStrategyTests(unittest.TestCase):
    def test_factory_creates_fee_aware_strategy(self):
        strategy = create_strategy("RSI_BB_FEE_AWARE", {})

        self.assertIsInstance(strategy, RsiBBFeeAwareStrategy)

    def test_buy_requires_reward_above_fee_threshold(self):
        strategy = make_strategy()
        seed_df(strategy, latest_mid=95.2)

        signal, _price = strategy.get_latest_signal()

        self.assertEqual(signal, "HOLD")

    def test_planned_notional_reduces_fee_reward_threshold(self):
        strategy = make_strategy(min_reward_pct=0.001)

        fallback_required = strategy.required_reward_pct_for_notional()
        large_trade_required = strategy.required_reward_pct_for_notional(10000.0)

        self.assertGreater(fallback_required, large_trade_required)
        self.assertEqual(large_trade_required, 0.0015)

    def test_buy_when_reward_and_confirmation_are_present(self):
        strategy = make_strategy()
        seed_df(strategy, latest_mid=100.0)

        signal, price = strategy.get_latest_signal()

        self.assertEqual(signal, "BUY")
        self.assertEqual(price, 95.0)

    def test_confirmation_blocks_falling_candle(self):
        strategy = make_strategy()
        seed_df(strategy, latest_mid=100.0, latest_close=95.0, latest_rsi=24.0)
        strategy.df.iloc[-1, strategy.df.columns.get_loc("open")] = 96.0

        signal, _price = strategy.get_latest_signal()

        self.assertEqual(signal, "HOLD")

    def test_mean_reversion_exit_at_midline(self):
        strategy = make_strategy()
        seed_df(strategy, latest_mid=95.0, latest_close=96.0, latest_rsi=56.0)

        signal, price = strategy.get_latest_signal()

        self.assertEqual(signal, "SELL")
        self.assertEqual(price, 96.0)

    def test_extended_hours_buy_requires_volume_spike(self):
        strategy = make_strategy(
            require_confirmation=False,
            ext_hours_oversold=33,
            ext_hours_buy_confirmation_bars=1,
            ext_hours_volume_filter_enabled=True,
            ext_hours_volume_lookback=1,
            ext_hours_volume_multiplier=2.0,
        )
        seed_df(strategy, latest_mid=100.0, latest_rsi=27.0, latest_volume=1200.0)

        signal, _price = strategy.get_latest_signal(session_name="pre_market")

        self.assertEqual(signal, "HOLD")
        self.assertEqual(strategy.last_signal_context["session_profile"], "extended")
        self.assertEqual(strategy.last_signal_context["buy_block_reason"], "low_extended_hours_volume")
        self.assertAlmostEqual(strategy.last_signal_context["volume_ratio"], 1.2)

    def test_extended_hours_buy_allows_high_volume_spike(self):
        strategy = make_strategy(
            require_confirmation=False,
            ext_hours_oversold=33,
            ext_hours_buy_confirmation_bars=1,
            ext_hours_volume_filter_enabled=True,
            ext_hours_volume_lookback=1,
            ext_hours_volume_multiplier=2.0,
        )
        seed_df(strategy, latest_mid=100.0, latest_rsi=27.0, latest_volume=2500.0)

        signal, price = strategy.get_latest_signal(session_name="pre_market")

        self.assertEqual(signal, "BUY")
        self.assertEqual(price, 95.0)
        self.assertTrue(strategy.last_signal_context["volume_filter_passed"])
        self.assertAlmostEqual(strategy.last_signal_context["volume_ratio"], 2.5)

    def test_regular_hours_buy_ignores_extended_volume_filter(self):
        strategy = make_strategy(
            require_confirmation=False,
            ext_hours_oversold=20,
            ext_hours_volume_lookback=1,
            ext_hours_volume_multiplier=10.0,
        )
        seed_df(strategy, latest_mid=100.0, latest_rsi=27.0, latest_volume=1.0)

        signal, price = strategy.get_latest_signal(session_name="regular")

        self.assertEqual(signal, "BUY")
        self.assertEqual(price, 95.0)
        self.assertEqual(strategy.last_signal_context["session_profile"], "regular")
        self.assertTrue(strategy.last_signal_context["volume_filter_passed"])

    def test_extended_hours_buy_blocked_until_persistence_reached(self):
        strategy = make_strategy(
            require_confirmation=False,
            ext_hours_oversold=33,
            ext_hours_buy_confirmation_bars=4,
        )
        seed_extended_streak_df(strategy, setup_bars=3)

        signal, _price = strategy.get_latest_signal(session_name="pre_market")

        self.assertEqual(signal, "HOLD")
        self.assertEqual(strategy.last_signal_context["session_profile"], "extended")
        self.assertEqual(strategy.last_signal_context["buy_block_reason"], "awaiting_extended_hours_buy_persistence")
        self.assertEqual(strategy.last_signal_context["buy_setup_bars"], 3)
        self.assertEqual(strategy.last_signal_context["required_buy_setup_bars"], 4)

    def test_extended_hours_buy_fires_after_persistence_reached(self):
        strategy = make_strategy(
            require_confirmation=False,
            ext_hours_oversold=33,
            ext_hours_buy_confirmation_bars=4,
        )
        seed_extended_streak_df(strategy, setup_bars=4)

        signal, price = strategy.get_latest_signal(session_name="pre_market")

        self.assertEqual(signal, "BUY")
        self.assertEqual(price, 94.0)
        self.assertEqual(strategy.last_signal_context["buy_setup_bars"], 4)
        self.assertEqual(strategy.last_signal_context["required_buy_setup_bars"], 4)

    def test_extended_hours_buy_streak_resets_on_failing_bar(self):
        strategy = make_strategy(
            require_confirmation=False,
            ext_hours_oversold=33,
            ext_hours_buy_confirmation_bars=4,
            exit_rsi=999,  # disable mean-reversion exit so we isolate the BUY streak
        )
        seed_extended_streak_df(strategy, setup_bars=4)
        # Inject a non-setup bar at the tail (price popped above the lower band,
        # RSI no longer oversold). This should reset the BUY streak to 0.
        failing_ts = pd.Timestamp("2026-04-21 04:55")
        new_row = strategy.df.iloc[-1].copy()
        new_row["close"] = 98.0
        new_row["RSI"] = 45.0
        strategy.df.loc[failing_ts] = new_row

        signal, _price = strategy.get_latest_signal(session_name="pre_market")

        self.assertEqual(signal, "HOLD")
        self.assertEqual(strategy.last_signal_context["buy_setup_bars"], 0)

    def test_extended_hours_sell_requires_persistence(self):
        strategy = make_strategy(
            require_confirmation=False,
            ext_hours_sell_confirmation_bars=4,
            overbought=70,
            exit_rsi=999,  # disable mean-reversion exit so we isolate the band path
        )
        # Build a tail of 3 SELL-setup bars (rsi>70 + close>=upper-tol) preceded by
        # a neutral bar.
        rows = []
        timestamps = []
        rows.append(
            {
                "open": 90.0, "high": 91.0, "low": 89.0, "close": 90.0,
                "volume": 1000.0, "barCount": 0, "wap": 90.0,
                "RSI": 50.0, "BB_LOWER": 90.0, "BB_MID": 95.0, "BB_UPPER": 100.0,
                "TREND_SMA": 90.0,
            }
        )
        timestamps.append("2026-04-21 04:00")
        for offset, minute in enumerate([5, 10, 15]):
            rows.append(
                {
                    "open": 99.5, "high": 101.0, "low": 99.0, "close": 100.0,
                    "volume": 1500.0, "barCount": 0, "wap": 100.0,
                    "RSI": 75.0, "BB_LOWER": 90.0, "BB_MID": 95.0, "BB_UPPER": 100.0,
                    "TREND_SMA": 90.0,
                }
            )
            timestamps.append(f"2026-04-21 04:{minute:02d}")
        strategy.df = pd.DataFrame(rows, index=pd.to_datetime(timestamps))

        signal, _price = strategy.get_latest_signal(session_name="after_hours")

        self.assertEqual(signal, "HOLD")
        self.assertEqual(
            strategy.last_signal_context["sell_block_reason"],
            "awaiting_extended_hours_sell_persistence",
        )
        self.assertEqual(strategy.last_signal_context["sell_setup_bars"], 3)
        self.assertEqual(strategy.last_signal_context["required_sell_setup_bars"], 4)

    def test_extended_hours_sell_fires_after_persistence(self):
        strategy = make_strategy(
            require_confirmation=False,
            ext_hours_sell_confirmation_bars=4,
            overbought=70,
            exit_rsi=999,
        )
        rows = []
        timestamps = []
        rows.append(
            {
                "open": 90.0, "high": 91.0, "low": 89.0, "close": 90.0,
                "volume": 1000.0, "barCount": 0, "wap": 90.0,
                "RSI": 50.0, "BB_LOWER": 90.0, "BB_MID": 95.0, "BB_UPPER": 100.0,
                "TREND_SMA": 90.0,
            }
        )
        timestamps.append("2026-04-21 04:00")
        for minute in [5, 10, 15, 20]:
            rows.append(
                {
                    "open": 99.5, "high": 101.0, "low": 99.0, "close": 100.0,
                    "volume": 1500.0, "barCount": 0, "wap": 100.0,
                    "RSI": 75.0, "BB_LOWER": 90.0, "BB_MID": 95.0, "BB_UPPER": 100.0,
                    "TREND_SMA": 90.0,
                }
            )
            timestamps.append(f"2026-04-21 04:{minute:02d}")
        strategy.df = pd.DataFrame(rows, index=pd.to_datetime(timestamps))

        signal, price = strategy.get_latest_signal(session_name="after_hours")

        self.assertEqual(signal, "SELL")
        self.assertEqual(price, 100.0)
        self.assertEqual(strategy.last_signal_context["sell_setup_bars"], 4)
        self.assertEqual(strategy.last_signal_context["required_sell_setup_bars"], 4)

    def test_mean_reversion_exit_does_not_require_persistence(self):
        strategy = make_strategy(
            require_confirmation=False,
            ext_hours_sell_confirmation_bars=4,
        )
        seed_df(strategy, latest_mid=95.0, latest_close=96.0, latest_rsi=56.0)

        signal, price = strategy.get_latest_signal(session_name="after_hours")

        self.assertEqual(signal, "SELL")
        self.assertEqual(price, 96.0)


if __name__ == "__main__":
    unittest.main()
