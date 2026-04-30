import unittest

import pandas as pd

from core.sim_config_loader import parse_and_validate_simulation_config
from strategies.rsi_5m_v2 import Rsi5mV2Strategy
from strategies.strategy_factory import create_strategy


def seed_v2_df(
    strategy,
    latest_close=100.0,
    latest_rsi=25.0,
    latest_atr=2.0,
    trend_close=110.0,
    trend_ema=100.0,
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
                "ATR": latest_atr,
                "TREND_CLOSE_1H": trend_close,
                "TREND_EMA_1H": trend_ema,
            }
        ],
        index=pd.to_datetime(["2026-04-21 09:40"]),
    )


class Rsi5mV2StrategyTests(unittest.TestCase):
    def test_factory_creates_v2_strategy(self):
        strategy = create_strategy("RSI_5M_V2", {"oversold": 25, "overbought": 75})

        self.assertIsInstance(strategy, Rsi5mV2Strategy)
        self.assertEqual(strategy.oversold, 25)
        self.assertEqual(strategy.overbought, 75)

    def test_no_buy_when_oversold_but_below_trend_ema(self):
        strategy = Rsi5mV2Strategy()
        seed_v2_df(strategy, latest_rsi=25.0, trend_close=99.0, trend_ema=100.0)

        signal, price = strategy.get_latest_signal(position_qty=0, avg_cost=0.0)

        self.assertEqual(signal, "HOLD")
        self.assertEqual(price, 100.0)
        self.assertEqual(strategy.last_signal_context["buy_block_reason"], "below_trend_ema")

    def test_buy_when_oversold_and_trend_filter_passes(self):
        strategy = Rsi5mV2Strategy()
        seed_v2_df(strategy, latest_rsi=25.0, trend_close=110.0, trend_ema=100.0)

        signal, price = strategy.get_latest_signal(position_qty=0, avg_cost=0.0)

        self.assertEqual(signal, "BUY")
        self.assertEqual(price, 100.0)
        self.assertEqual(strategy.last_signal_context["signal_reason"], "rsi_oversold_trend_entry")

    def test_sell_when_overbought_while_long(self):
        strategy = Rsi5mV2Strategy()
        seed_v2_df(strategy, latest_close=105.0, latest_rsi=75.0)

        signal, price = strategy.get_latest_signal(position_qty=10, avg_cost=100.0)

        self.assertEqual(signal, "SELL")
        self.assertEqual(price, 105.0)
        self.assertEqual(strategy.last_signal_context["exit_reason"], "rsi_overbought_exit")

    def test_sell_when_atr_stop_is_hit(self):
        strategy = Rsi5mV2Strategy(atr_stop_multiple=2.0)
        seed_v2_df(strategy, latest_close=96.0, latest_rsi=50.0, latest_atr=2.0)

        signal, price = strategy.get_latest_signal(position_qty=10, avg_cost=100.0)

        self.assertEqual(signal, "SELL")
        self.assertEqual(price, 96.0)
        self.assertEqual(strategy.last_signal_context["exit_reason"], "atr_stop_exit")

    def test_hold_while_trend_ema_is_not_warmed_up(self):
        strategy = Rsi5mV2Strategy()
        seed_v2_df(strategy, latest_rsi=25.0, trend_close=float("nan"), trend_ema=float("nan"))

        signal, price = strategy.get_latest_signal(position_qty=0, avg_cost=0.0)

        self.assertEqual(signal, "HOLD")
        self.assertEqual(price, 100.0)
        self.assertEqual(strategy.last_signal_context["buy_block_reason"], "trend_not_ready")


class Rsi5mV2ConfigTests(unittest.TestCase):
    def test_sim_config_preserves_v2_fields(self):
        parsed = parse_and_validate_simulation_config(
            {
                "defaults": {"starting_balance": 10000},
                "export_policy": {"summary_filename": "latest_sims.json"},
                "simulations": [
                    {
                        "id": "RSI_5M_V2",
                        "strategy": "RSI_5M_V2",
                        "trade_allocation_pct": 0.25,
                        "trend_ema_period": 200,
                        "atr_period": 14,
                        "atr_stop_multiple": 2.0,
                    }
                ],
            }
        )

        sim_config = parsed["simulations"][0]
        self.assertEqual(sim_config["strategy"], "RSI_5M_V2")
        self.assertEqual(sim_config["trade_allocation_pct"], 0.25)
        self.assertEqual(sim_config["trend_ema_period"], 200)
        self.assertEqual(sim_config["atr_period"], 14)
        self.assertEqual(sim_config["atr_stop_multiple"], 2.0)


if __name__ == "__main__":
    unittest.main()
