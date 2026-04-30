import unittest

import pandas as pd

from core.sim_config_loader import parse_and_validate_simulation_config
from strategies.rsi_bb_5m_v3 import RsiBB5mV3Strategy
from strategies.strategy_factory import create_strategy


def seed_v3_df(
    strategy,
    latest_close=100.0,
    latest_rsi=33.0,
    bb_lower=100.0,
    bb_mid=105.0,
    bb_upper=110.0,
    trend_ema=95.0,
    trend_close=100.0,
):
    strategy.df = pd.DataFrame(
        [
            {
                "open": latest_close,
                "high": latest_close + 1.0,
                "low": latest_close - 1.0,
                "close": latest_close,
                "volume": 1000.0,
                "barCount": 0,
                "wap": latest_close,
                "RSI": latest_rsi,
                "BB_LOWER": bb_lower,
                "BB_MID": bb_mid,
                "BB_UPPER": bb_upper,
                "TREND_CLOSE_1H": trend_close,
                "TREND_EMA_1H": trend_ema,
            }
        ],
        index=pd.to_datetime(["2026-04-21 09:40"]),
    )


class RsiBB5mV3StrategyTests(unittest.TestCase):
    def test_factory_creates_v3_strategy(self):
        strategy = create_strategy("RSI_BB_5M_V3", {"bb_std": 2.5})

        self.assertIsInstance(strategy, RsiBB5mV3Strategy)
        self.assertEqual(strategy.bb_std, 2.5)

    def test_buy_in_bull_regime_with_rsi_and_lower_band_tag(self):
        strategy = RsiBB5mV3Strategy()
        seed_v3_df(strategy, latest_close=100.0, latest_rsi=33.0, bb_lower=100.0, trend_ema=95.0)

        signal, price = strategy.get_latest_signal(position_qty=0, avg_cost=0.0)

        self.assertEqual(signal, "BUY")
        self.assertEqual(price, 100.0)
        self.assertEqual(strategy.last_signal_context["regime"], "bull")
        self.assertEqual(strategy.last_signal_context["signal_reason"], "bull_rsi_bb_entry")

    def test_buy_in_bear_regime_only_with_deeper_rsi_and_lower_band_tag(self):
        strategy = RsiBB5mV3Strategy()
        seed_v3_df(strategy, latest_close=100.0, latest_rsi=25.0, bb_lower=100.0, trend_ema=101.0)

        signal, price = strategy.get_latest_signal(position_qty=0, avg_cost=0.0)

        self.assertEqual(signal, "BUY")
        self.assertEqual(price, 100.0)
        self.assertEqual(strategy.last_signal_context["regime"], "bear")
        self.assertEqual(strategy.last_signal_context["signal_reason"], "bear_rsi_bb_entry")

    def test_hold_in_bear_regime_when_only_bull_oversold(self):
        strategy = RsiBB5mV3Strategy()
        seed_v3_df(strategy, latest_close=100.0, latest_rsi=30.0, bb_lower=100.0, trend_ema=101.0)

        signal, price = strategy.get_latest_signal(position_qty=0, avg_cost=0.0)

        self.assertEqual(signal, "HOLD")
        self.assertEqual(price, 100.0)
        self.assertEqual(strategy.last_signal_context["buy_block_reason"], "rsi_not_oversold")

    def test_hold_when_price_is_not_at_lower_band(self):
        strategy = RsiBB5mV3Strategy(bb_tolerance=0.002)
        seed_v3_df(strategy, latest_close=101.0, latest_rsi=20.0, bb_lower=100.0, trend_ema=95.0)

        signal, price = strategy.get_latest_signal(position_qty=0, avg_cost=0.0)

        self.assertEqual(signal, "HOLD")
        self.assertEqual(price, 101.0)
        self.assertEqual(strategy.last_signal_context["buy_block_reason"], "price_not_at_lower_band")

    def test_sell_on_rsi_overbought(self):
        strategy = RsiBB5mV3Strategy()
        seed_v3_df(strategy, latest_close=105.0, latest_rsi=70.0, bb_lower=95.0, bb_upper=110.0)

        signal, price = strategy.get_latest_signal(position_qty=10, avg_cost=100.0)

        self.assertEqual(signal, "SELL")
        self.assertEqual(price, 105.0)
        self.assertEqual(strategy.last_signal_context["exit_reason"], "rsi_profit_exit")

    def test_sell_on_upper_band_tag_even_when_rsi_below_overbought(self):
        strategy = RsiBB5mV3Strategy(bb_tolerance=0.002)
        seed_v3_df(strategy, latest_close=109.9, latest_rsi=55.0, bb_lower=95.0, bb_upper=110.0)

        signal, price = strategy.get_latest_signal(position_qty=10, avg_cost=100.0)

        self.assertEqual(signal, "SELL")
        self.assertEqual(price, 109.9)
        self.assertEqual(strategy.last_signal_context["exit_reason"], "upper_band_profit_exit")

    def test_sell_on_hard_five_percent_stop(self):
        strategy = RsiBB5mV3Strategy(stop_loss_pct=0.05)
        seed_v3_df(strategy, latest_close=95.0, latest_rsi=45.0, bb_lower=90.0, bb_upper=110.0)

        signal, price = strategy.get_latest_signal(position_qty=10, avg_cost=100.0)

        self.assertEqual(signal, "SELL")
        self.assertEqual(price, 95.0)
        self.assertEqual(strategy.last_signal_context["exit_reason"], "hard_stop_exit")

    def test_hold_while_trend_ema_is_not_warmed_up(self):
        strategy = RsiBB5mV3Strategy()
        seed_v3_df(strategy, latest_close=100.0, latest_rsi=20.0, bb_lower=100.0, trend_ema=float("nan"))

        signal, price = strategy.get_latest_signal(position_qty=0, avg_cost=0.0)

        self.assertEqual(signal, "HOLD")
        self.assertEqual(price, 100.0)
        self.assertEqual(strategy.last_signal_context["buy_block_reason"], "trend_not_ready")


class RsiBB5mV3ConfigTests(unittest.TestCase):
    def test_sim_config_preserves_v3_fields(self):
        parsed = parse_and_validate_simulation_config(
            {
                "defaults": {"starting_balance": 10000},
                "export_policy": {"summary_filename": "latest_sims.json"},
                "simulations": [
                    {
                        "id": "RSI_BB_5M_V3",
                        "strategy": "RSI_BB_5M_V3",
                        "trade_allocation_pct": 0.25,
                        "bb_std": 2.5,
                        "bb_tolerance": 0.002,
                        "bull_oversold": 33,
                        "bear_oversold": 25,
                        "stop_loss_pct": 0.05,
                    }
                ],
            }
        )

        sim_config = parsed["simulations"][0]
        self.assertEqual(sim_config["strategy"], "RSI_BB_5M_V3")
        self.assertEqual(sim_config["trade_allocation_pct"], 0.25)
        self.assertEqual(sim_config["bb_std"], 2.5)
        self.assertEqual(sim_config["bb_tolerance"], 0.002)
        self.assertEqual(sim_config["bull_oversold"], 33)
        self.assertEqual(sim_config["bear_oversold"], 25)
        self.assertEqual(sim_config["stop_loss_pct"], 0.05)


if __name__ == "__main__":
    unittest.main()
