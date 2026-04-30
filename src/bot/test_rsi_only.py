import unittest

import pandas as pd

from core.market_data_provider import NormalizedBar
from core.sim_config_loader import parse_and_validate_simulation_config
from simulation.sim_manager import VirtualPortfolio
from strategies.rsi_only import RsiOnlyStrategy
from strategies.strategy_factory import create_strategy


def seed_rsi_only_df(strategy, latest_close=100.0, latest_rsi=50.0):
    strategy.df = pd.DataFrame(
        [
            {
                "open": latest_close,
                "high": latest_close,
                "low": latest_close,
                "close": latest_close,
                "volume": 1000.0,
                "barCount": 0,
                "wap": latest_close,
                "RSI": latest_rsi,
            }
        ],
        index=pd.to_datetime(["2026-04-21 09:40"]),
    )


class FakeStrategy:
    def __init__(self, signals):
        self.signals = list(signals)
        self.df = pd.DataFrame()
        self.last_signal_context = {}

    def add_bar(self, **_kwargs):
        pass

    def update_indicators(self):
        pass

    def get_latest_signal(self, **_kwargs):
        return self.signals.pop(0)


class RsiOnlyStrategyTests(unittest.TestCase):
    def test_factory_creates_rsi_only_with_stop_loss(self):
        strategy = create_strategy("RSI_ONLY", {"stop_loss_pct": 0.03})

        self.assertIsInstance(strategy, RsiOnlyStrategy)
        self.assertEqual(strategy.stop_loss_pct, 0.03)

    def test_buy_signal_includes_oversold_threshold(self):
        strategy = RsiOnlyStrategy(oversold=30)
        seed_rsi_only_df(strategy, latest_close=100.0, latest_rsi=30.0)

        signal, price = strategy.get_latest_signal(position_qty=0, avg_cost=0.0)

        self.assertEqual(signal, "BUY")
        self.assertEqual(price, 100.0)
        self.assertEqual(strategy.last_signal_context["signal_reason"], "rsi_oversold_entry")

    def test_sell_signal_includes_overbought_threshold_when_long(self):
        strategy = RsiOnlyStrategy(overbought=70)
        seed_rsi_only_df(strategy, latest_close=105.0, latest_rsi=70.0)

        signal, price = strategy.get_latest_signal(position_qty=10, avg_cost=100.0)

        self.assertEqual(signal, "SELL")
        self.assertEqual(price, 105.0)
        self.assertEqual(strategy.last_signal_context["exit_reason"], "rsi_overbought_exit")

    def test_stop_loss_sells_before_rsi_exit_when_long(self):
        strategy = RsiOnlyStrategy(stop_loss_pct=0.03)
        seed_rsi_only_df(strategy, latest_close=97.0, latest_rsi=50.0)

        signal, price = strategy.get_latest_signal(position_qty=10, avg_cost=100.0)

        self.assertEqual(signal, "SELL")
        self.assertEqual(price, 97.0)
        self.assertEqual(strategy.last_signal_context["exit_reason"], "stop_loss_exit")

    def test_does_not_repeat_buy_while_long(self):
        strategy = RsiOnlyStrategy(oversold=30)
        seed_rsi_only_df(strategy, latest_close=100.0, latest_rsi=30.0)

        signal, _price = strategy.get_latest_signal(position_qty=10, avg_cost=100.0)

        self.assertEqual(signal, "HOLD")


class RsiOnlySimulationTests(unittest.TestCase):
    def test_trade_allocation_pct_controls_default_sim_buy_size(self):
        portfolio = VirtualPortfolio(
            sim_id="rsi_only_alloc",
            config={"strategy": "RSI_ONLY", "trade_allocation_pct": 0.9},
            starting_balance=1000.0,
        )
        portfolio.strategy = FakeStrategy([("BUY", 100.0)])

        bar = NormalizedBar(
            date="2026-04-21T13:30:00Z",
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=1000,
        )
        _signal_event, trade_event = portfolio.process_bar(bar)

        self.assertEqual(trade_event["qty"], 9)
        self.assertEqual(portfolio.position, 9)

    def test_sim_config_preserves_rsi_only_fields(self):
        parsed = parse_and_validate_simulation_config(
            {
                "defaults": {"starting_balance": 10000},
                "export_policy": {"summary_filename": "latest_sims.json"},
                "simulations": [
                    {
                        "id": "RSI_ONLY",
                        "strategy": "RSI_ONLY",
                        "stop_loss_pct": 0.03,
                        "trade_allocation_pct": 0.9,
                    }
                ],
            }
        )

        sim_config = parsed["simulations"][0]
        self.assertEqual(sim_config["strategy"], "RSI_ONLY")
        self.assertEqual(sim_config["stop_loss_pct"], 0.03)
        self.assertEqual(sim_config["trade_allocation_pct"], 0.9)


if __name__ == "__main__":
    unittest.main()
