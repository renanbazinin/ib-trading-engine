import json
import os
import tempfile
import unittest

import pandas as pd

from core.market_data_provider import NormalizedBar
from core.sim_config_loader import load_simulation_config, parse_and_validate_simulation_config
from historical_backtester import HistoricalBacktester
from main import LiveOrderPlan, TWSBotOrchestrator
from simulation.sim_manager import VirtualPortfolio
from strategies.rsi_bb_fee_aware_v3 import RsiBBFeeAwareV3Strategy
from strategies.rsi_bb_fee_aware_v4 import RsiBBFeeAwareV4AStrategy, RsiBBFeeAwareV4BStrategy
from strategies.strategy_factory import create_strategy


def seed_v3_df(strategy, latest_volume=1600.0, latest_close=95.0, latest_rsi=30.0):
    strategy.df = pd.DataFrame(
        [
            {
                "open": 96.0,
                "high": 97.0,
                "low": 94.0,
                "close": 96.0,
                "volume": 1000.0,
                "barCount": 0,
                "wap": 96.0,
                "RSI": 25.0,
                "BB_LOWER": 96.5,
                "BB_MID": 100.0,
                "BB_UPPER": 104.0,
                "EMA_FAST": 96.0,
                "VOLUME_SMA": 1000.0,
                "TREND_SMA": 90.0,
                "DYNAMIC_BB_STD": 2.2,
                "ATR": 2.0,
                "ATR_SMA": 2.0,
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
                "BB_MID": 100.0,
                "BB_UPPER": 105.0,
                "EMA_FAST": 96.0,
                "VOLUME_SMA": 1000.0,
                "TREND_SMA": 90.0,
                "DYNAMIC_BB_STD": 2.2,
                "ATR": 2.0,
                "ATR_SMA": 2.0,
            },
        ],
        index=pd.to_datetime(["2026-04-21 09:35", "2026-04-21 09:40"]),
    )


class FakeStrategy:
    def __init__(self, signals):
        self.signals = list(signals)

    def add_bar(self, **_kwargs):
        pass

    def update_indicators(self):
        pass

    def get_latest_signal(self, **_kwargs):
        return self.signals.pop(0)


class RsiBBFeeAwareV3Tests(unittest.TestCase):
    def test_factory_creates_v3_strategy(self):
        strategy = create_strategy("RSI_BB_FEE_AWARE_V3", {})

        self.assertIsInstance(strategy, RsiBBFeeAwareV3Strategy)

    def test_simulation_commission_is_charged_per_trade_leg(self):
        portfolio = VirtualPortfolio(
            sim_id="commission_test",
            config={"commission_per_trade": 2.5},
            starting_balance=1000.0,
        )
        portfolio.strategy = FakeStrategy([("BUY", 100.0), ("SELL", 110.0)])

        bar = NormalizedBar(
            date="2026-04-21T13:30:00Z",
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=1000,
        )
        portfolio.process_bar(bar)
        portfolio.process_bar(bar)

        self.assertEqual(portfolio.total_trades, 2)
        self.assertEqual(portfolio.total_commissions, 5.0)
        self.assertEqual(portfolio.position, 0)
        self.assertAlmostEqual(portfolio.realized_pnl, 85.0)
        self.assertAlmostEqual(portfolio.balance, 1085.0)

    def test_v3_volume_gate_blocks_low_volume_buy(self):
        strategy = RsiBBFeeAwareV3Strategy(
            min_reward_pct=0.004,
            fee_reward_multiple=2.0,
            min_bb_width_pct=0.006,
            volume_multiplier=1.5,
        )
        seed_v3_df(strategy, latest_volume=1200.0)

        signal, _price = strategy.get_latest_signal(position_qty=0, avg_cost=0.0)

        self.assertEqual(signal, "HOLD")
        self.assertEqual(strategy.last_signal_context["buy_block_reason"], "low_entry_volume")

    def test_v3_volume_gate_allows_high_volume_buy(self):
        strategy = RsiBBFeeAwareV3Strategy(
            min_reward_pct=0.004,
            fee_reward_multiple=2.0,
            min_bb_width_pct=0.006,
            volume_multiplier=1.5,
        )
        seed_v3_df(strategy, latest_volume=1600.0)

        signal, price = strategy.get_latest_signal(position_qty=0, avg_cost=0.0)

        self.assertEqual(signal, "BUY")
        self.assertEqual(price, 95.0)
        self.assertGreaterEqual(strategy.last_signal_context["volume_ratio"], 1.5)

    def test_v3_rsi_profit_exit_when_long(self):
        strategy = RsiBBFeeAwareV3Strategy(profit_rsi=70)
        seed_v3_df(strategy, latest_close=110.0, latest_rsi=71.0)
        strategy.df.iloc[-1, strategy.df.columns.get_loc("EMA_FAST")] = 108.0

        signal, price = strategy.get_latest_signal(position_qty=10, avg_cost=100.0)

        self.assertEqual(signal, "SELL")
        self.assertEqual(price, 110.0)
        self.assertEqual(strategy.last_signal_context["exit_reason"], "rsi_profit_exit")

    def test_v3_ema_profit_exit_when_long(self):
        strategy = RsiBBFeeAwareV3Strategy(profit_rsi=70)
        seed_v3_df(strategy, latest_close=105.0, latest_rsi=55.0)
        strategy.df.iloc[-1, strategy.df.columns.get_loc("EMA_FAST")] = 106.0

        signal, price = strategy.get_latest_signal(position_qty=10, avg_cost=100.0)

        self.assertEqual(signal, "SELL")
        self.assertEqual(price, 105.0)
        self.assertEqual(strategy.last_signal_context["exit_reason"], "ema_profit_exit")

    def test_v3_dynamic_bb_std_stays_within_bounds(self):
        strategy = RsiBBFeeAwareV3Strategy(
            dynamic_bb_min_std=2.0,
            dynamic_bb_max_std=2.5,
            atr_baseline_period=20,
        )
        for i in range(90):
            close = 100.0 + (i * 0.1) + ((i % 5) * 0.2)
            strategy.add_bar(
                date_str=f"2026-04-21T13:{i % 60:02d}:00Z",
                open_price=close - 0.2,
                high=close + 1.0,
                low=close - 1.0,
                close=close,
                volume=1000 + i,
            )

        strategy.update_indicators()
        latest_std = float(strategy.df["DYNAMIC_BB_STD"].dropna().iloc[-1])

        self.assertGreaterEqual(latest_std, 2.0)
        self.assertLessEqual(latest_std, 2.5)

    def test_factory_creates_v4_variants(self):
        v4a = create_strategy("RSI_BB_FEE_AWARE_V4A", {})
        v4b = create_strategy("RSI_BB_FEE_AWARE_V4B", {})

        self.assertIsInstance(v4a, RsiBBFeeAwareV4AStrategy)
        self.assertIsInstance(v4b, RsiBBFeeAwareV4BStrategy)

    def test_sim_config_loader_preserves_v4b_fields(self):
        parsed = parse_and_validate_simulation_config(
            {
                "defaults": {"starting_balance": 10000, "near_band_pct": 0.001},
                "export_policy": {"summary_filename": "latest_sims.json"},
                "simulations": [
                    {
                        "id": "RSI_BB_FEE_AWARE_V4B",
                        "strategy": "RSI_BB_FEE_AWARE_V4B",
                        "commission_per_trade": 2.5,
                        "scale_in_enabled": True,
                        "scale_in_initial_fraction": 0.4,
                        "scale_in_fraction": 0.6,
                        "scale_in_drop_pct": 0.0125,
                    }
                ],
            }
        )

        sim_config = parsed["simulations"][0]
        self.assertEqual(parsed["defaults"]["near_band_pct"], 0.001)
        self.assertEqual(sim_config["strategy"], "RSI_BB_FEE_AWARE_V4B")
        self.assertEqual(sim_config["commission_per_trade"], 2.5)
        self.assertTrue(sim_config["scale_in_enabled"])
        self.assertEqual(sim_config["scale_in_initial_fraction"], 0.4)

    def test_default_simulation_config_loads_rsi_only(self):
        loaded = load_simulation_config()
        enabled = [sim for sim in loaded["simulations"] if sim["enabled"]]

        self.assertEqual(len(enabled), 3)
        by_id = {sim["id"]: sim for sim in enabled}
        self.assertEqual(by_id["RSI_ONLY_V1"]["strategy"], "RSI_ONLY")
        self.assertEqual(by_id["RSI_ONLY_V1"]["stop_loss_pct"], 0.03)
        self.assertEqual(by_id["RSI_ONLY_V1"]["trade_allocation_pct"], 0.9)
        self.assertEqual(by_id["RSI_5M_V2"]["strategy"], "RSI_5M_V2")
        self.assertEqual(by_id["RSI_5M_V2"]["trade_allocation_pct"], 0.25)
        self.assertEqual(by_id["RSI_BB_5M_V3"]["strategy"], "RSI_BB_5M_V3")
        self.assertEqual(by_id["RSI_BB_5M_V3"]["trade_allocation_pct"], 0.25)
        self.assertEqual(by_id["RSI_BB_5M_V3"]["stop_loss_pct"], 0.05)

    def test_v4a_volume_window_allows_prior_spike(self):
        strategy = RsiBBFeeAwareV4AStrategy(
            min_reward_pct=0.004,
            fee_reward_multiple=2.0,
            min_bb_width_pct=0.006,
            volume_multiplier=1.25,
            volume_spike_window_bars=2,
        )
        seed_v3_df(strategy, latest_volume=1000.0)
        strategy.df.iloc[-2, strategy.df.columns.get_loc("volume")] = 1400.0
        strategy.df.iloc[-2, strategy.df.columns.get_loc("VOLUME_SMA")] = 1000.0

        signal, price = strategy.get_latest_signal(position_qty=0, avg_cost=0.0)

        self.assertEqual(signal, "BUY")
        self.assertEqual(price, 95.0)
        self.assertGreaterEqual(strategy.last_signal_context["volume_ratio"], 1.25)

    def test_v4a_ema_exit_requires_reclaim_before_loss(self):
        strategy = RsiBBFeeAwareV4AStrategy(profit_rsi=70)
        seed_v3_df(strategy, latest_close=105.0, latest_rsi=55.0)
        strategy.df.iloc[-1, strategy.df.columns.get_loc("EMA_FAST")] = 104.0

        signal, _price = strategy.get_latest_signal(position_qty=10, avg_cost=100.0)
        self.assertEqual(signal, "HOLD")
        self.assertTrue(strategy.last_signal_context["ema_reclaimed"])

        next_row = strategy.df.iloc[-1].copy()
        next_row["close"] = 103.0
        next_row["EMA_FAST"] = 104.0
        strategy.df.loc[pd.Timestamp("2026-04-21 09:45")] = next_row

        signal, price = strategy.get_latest_signal(position_qty=10, avg_cost=100.0)

        self.assertEqual(signal, "SELL")
        self.assertEqual(price, 103.0)
        self.assertEqual(strategy.last_signal_context["exit_reason"], "ema_reclaim_lost_exit")

    def test_v4a_stagnation_exit_after_max_bars(self):
        strategy = RsiBBFeeAwareV4AStrategy(max_stagnation_bars=24)
        seed_v3_df(strategy, latest_close=100.0, latest_rsi=50.0)
        strategy.df.iloc[-1, strategy.df.columns.get_loc("EMA_FAST")] = 99.0
        strategy._position_active = True
        strategy._entry_bar_index = -23

        signal, price = strategy.get_latest_signal(position_qty=10, avg_cost=100.0)

        self.assertEqual(signal, "SELL")
        self.assertEqual(price, 100.0)
        self.assertEqual(strategy.last_signal_context["exit_reason"], "stagnation_exit")

    def test_v4b_initial_and_scale_in_buy_fractions(self):
        strategy = RsiBBFeeAwareV4BStrategy(
            min_reward_pct=0.004,
            fee_reward_multiple=2.0,
            min_bb_width_pct=0.006,
            volume_multiplier=1.25,
            volume_spike_window_bars=2,
        )
        seed_v3_df(strategy, latest_volume=1600.0)

        signal, _price = strategy.get_latest_signal(position_qty=0, avg_cost=0.0)
        self.assertEqual(signal, "BUY")
        self.assertEqual(strategy.last_signal_context["buy_fraction"], 0.4)

        strategy.df.iloc[-1, strategy.df.columns.get_loc("close")] = 98.0
        strategy.df.iloc[-1, strategy.df.columns.get_loc("RSI")] = 25.0
        signal, price = strategy.get_latest_signal(position_qty=10, avg_cost=100.0)

        self.assertEqual(signal, "BUY")
        self.assertEqual(price, 98.0)
        self.assertTrue(strategy.last_signal_context["scale_in_signal"])
        self.assertEqual(strategy.last_signal_context["buy_fraction"], 0.6)

    def test_live_v4b_buy_fraction_uses_full_slot_allocation(self):
        bot = TWSBotOrchestrator.__new__(TWSBotOrchestrator)
        bot.live_strategy = type("StrategyStub", (), {"last_signal_context": {"buy_fraction": 0.4}})()
        bot._slot_ledger_notional = lambda: 0.0
        bot._effective_max_order_notional = lambda: None
        bot.max_order_quantity = None
        plan = LiveOrderPlan(
            action="BUY",
            quantity=23,
            order_notional=2375.0,
            allowed=True,
            reason="ok",
            sizing_mode="slot_percent",
            cash_available=10000.0,
            symbol_exposure=0.0,
            slot_notional=2375.0,
            max_slots=4,
            remaining_slots=4,
        )

        adjusted = bot._apply_strategy_buy_fraction(plan, current_price=100.0)

        self.assertTrue(adjusted.allowed)
        self.assertEqual(adjusted.quantity, 38)
        self.assertEqual(adjusted.order_notional, 3800.0)

    def test_live_v4b_scale_in_fraction_respects_remaining_capacity(self):
        bot = TWSBotOrchestrator.__new__(TWSBotOrchestrator)
        bot.live_strategy = type("StrategyStub", (), {"last_signal_context": {"buy_fraction": 0.6}})()
        bot._slot_ledger_notional = lambda: 3800.0
        bot._effective_max_order_notional = lambda: None
        bot.max_order_quantity = None
        plan = LiveOrderPlan(
            action="BUY",
            quantity=24,
            order_notional=2400.0,
            allowed=True,
            reason="ok",
            sizing_mode="slot_percent",
            cash_available=10000.0,
            symbol_exposure=3800.0,
            slot_notional=2375.0,
            max_slots=4,
            remaining_slots=3,
        )

        adjusted = bot._apply_strategy_buy_fraction(plan, current_price=100.0)

        self.assertTrue(adjusted.allowed)
        self.assertEqual(adjusted.quantity, 57)
        self.assertEqual(adjusted.order_notional, 5700.0)

    def test_historical_artifacts_write_summary_and_trades(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backtester = HistoricalBacktester(
                csv_path="unused.csv",
                config_path="unused.json",
                json_results_dir=temp_dir,
            )
            backtester._export_json_artifacts(
                {
                    "timestamp": "2026-04-27T19:01:30+00:00",
                    "simulations": [
                        {
                            "id": "ARTIFACT_TEST",
                            "stats": {"trades_total": 1},
                            "all_trades": [{"side": "BUY", "qty": 1}],
                        }
                    ],
                }
            )

            summary_path = os.path.join(temp_dir, "ARTIFACT_TEST_summary.json")
            trades_path = os.path.join(temp_dir, "ARTIFACT_TEST_trades.json")
            with open(summary_path, "r", encoding="utf-8") as file_handle:
                summary = json.load(file_handle)
            with open(trades_path, "r", encoding="utf-8") as file_handle:
                trades = json.load(file_handle)

        self.assertEqual(summary["id"], "ARTIFACT_TEST")
        self.assertEqual(trades[0]["side"], "BUY")


if __name__ == "__main__":
    unittest.main()
