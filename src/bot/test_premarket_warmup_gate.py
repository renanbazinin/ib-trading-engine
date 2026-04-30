import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from core.market_data_provider import NormalizedBar
from core.trading_hours import TradingHours
from main import TWSBotOrchestrator
import web.dashboard as dash


NY = ZoneInfo("America/New_York")


def bar_at_et(year, month, day, hour, minute):
    ts = datetime(year, month, day, hour, minute, tzinfo=NY).astimezone(timezone.utc)
    return NormalizedBar(
        date=ts.isoformat(),
        open=100,
        high=101,
        low=99,
        close=100,
        volume=1000,
        source="test",
        timestamp=ts,
    )


class PremarketWarmupGateTests(unittest.TestCase):
    def setUp(self):
        base_hours = TradingHours.from_config()
        fixed_now = datetime(2026, 4, 21, 4, 16, tzinfo=NY)

        class FixedNowTradingHours:
            sessions = base_hours.sessions
            timezone_name = base_hours.timezone_name

            @staticmethod
            def status(value=None):
                return base_hours.status(value or fixed_now)

            @staticmethod
            def localize(value=None):
                return base_hours.localize(value or fixed_now)

        self.bot = TWSBotOrchestrator.__new__(TWSBotOrchestrator)
        self.bot.live_symbol = "TSLA"
        self.bot.live_interval = "5m"
        self.bot.live_interval_minutes = 5
        self.bot.trading_hours_enabled = True
        self.bot.trading_hours = FixedNowTradingHours()
        self.bot.premarket_min_completed_bars = 3

    def test_first_two_premarket_bars_are_blocked(self):
        allowed, retry_later, reason, payload = self.bot._live_bar_gate(bar_at_et(2026, 4, 21, 4, 0))

        self.assertFalse(allowed)
        self.assertFalse(retry_later)
        self.assertEqual(reason, "waiting_for_session_warmup_bars")
        self.assertEqual(payload["completed_session_bars"], 1)
        self.assertEqual(payload["required_session_bars"], 3)

        allowed, retry_later, reason, payload = self.bot._live_bar_gate(bar_at_et(2026, 4, 21, 4, 5))

        self.assertFalse(allowed)
        self.assertFalse(retry_later)
        self.assertEqual(reason, "waiting_for_session_warmup_bars")
        self.assertEqual(payload["completed_session_bars"], 2)

    def test_third_premarket_bar_is_allowed(self):
        allowed, retry_later, reason, payload = self.bot._live_bar_gate(bar_at_et(2026, 4, 21, 4, 10))

        self.assertTrue(allowed)
        self.assertFalse(retry_later)
        self.assertEqual(reason, "open")


class DummySessionStrategy:
    def __init__(self, df):
        self.base_columns = ["open", "high", "low", "close", "volume", "barCount", "wap"]
        self.df = df
        self.session_name = None

    def set_session_context(self, session_name):
        self.session_name = session_name


class DummyWarmupStrategy:
    def __init__(self, rows=0, min_bars_required=50):
        self.min_bars_required = min_bars_required
        self.df = pd.DataFrame(
            [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000.0} for _ in range(rows)]
        )


class RecordingBrokerClient:
    is_connected = True

    def __init__(self):
        self.requests = []

    @staticmethod
    def get_contract(symbol):
        return {"symbol": symbol}

    def reqHistoricalData(self, **kwargs):
        self.requests.append(kwargs)

    def get_snapshot(self, symbol):
        return None


def make_history_bot(min_bars_required=2424, extended_hours=True, min_days=2, max_days=45):
    bot = TWSBotOrchestrator.__new__(TWSBotOrchestrator)
    bot.live_symbol = "TSLA"
    bot.live_interval_minutes = 5
    bot.broker_bar_size = "5 mins"
    bot.broker_req_id = 91
    bot.live_strategy = DummyWarmupStrategy(rows=0, min_bars_required=min_bars_required)
    bot.live_startup_min_bars = 6
    bot.broker_history_min_days = min_days
    bot.broker_history_max_days = max_days
    bot.extended_hours = extended_hours
    bot.trading_hours_enabled = True
    bot.trading_hours = TradingHours.from_config()
    bot.client = RecordingBrokerClient()
    bot.web_api_enabled = False
    bot.broker_poll_seconds = 0
    bot._last_broker_poll_time = 0
    return bot


class SessionIndicatorResetTests(unittest.TestCase):
    def test_first_session_context_does_not_prune_backfill(self):
        hours = TradingHours.from_config()
        regular_ts = datetime(2026, 4, 21, 15, 55, tzinfo=NY).astimezone(timezone.utc)
        after_hours_ts = datetime(2026, 4, 21, 16, 0, tzinfo=NY).astimezone(timezone.utc)
        df = pd.DataFrame(
            [
                {"close": 100.0, "volume": 1000.0, "RSI": 45.0},
                {"close": 101.0, "volume": 2000.0, "RSI": 48.0},
            ],
            index=pd.to_datetime([regular_ts.isoformat(), after_hours_ts.isoformat()]),
        )

        bot = TWSBotOrchestrator.__new__(TWSBotOrchestrator)
        bot.trading_hours = hours
        bot.trading_hours_enabled = True
        bot.ext_hours_reset_indicators_on_session_change = True
        bot._active_strategy_session = None
        bot._indicators_warmed_up = True
        bot.live_strategy = DummySessionStrategy(df)
        dash.bot_state["indicators_ready"] = True

        bot._prepare_live_session_context("after_hours", bar_at_et(2026, 4, 21, 16, 0))

        self.assertEqual(bot.live_strategy.session_name, "after_hours")
        self.assertEqual(bot._active_strategy_session, "after_hours")
        self.assertEqual(len(bot.live_strategy.df), 2)
        self.assertTrue(bot._indicators_warmed_up)
        self.assertTrue(dash.bot_state["indicators_ready"])

    def test_session_change_prunes_prior_session_indicator_rows(self):
        hours = TradingHours.from_config()
        regular_ts = datetime(2026, 4, 21, 15, 55, tzinfo=NY).astimezone(timezone.utc)
        after_hours_ts = datetime(2026, 4, 21, 16, 0, tzinfo=NY).astimezone(timezone.utc)
        df = pd.DataFrame(
            [
                {"close": 100.0, "volume": 1000.0, "RSI": 45.0, "BB_LOWER": 95.0, "BB_UPPER": 105.0},
                {"close": 101.0, "volume": 2000.0, "RSI": 48.0, "BB_LOWER": 96.0, "BB_UPPER": 106.0},
            ],
            index=pd.to_datetime([regular_ts.isoformat(), after_hours_ts.isoformat()]),
        )

        bot = TWSBotOrchestrator.__new__(TWSBotOrchestrator)
        bot.trading_hours = hours
        bot.trading_hours_enabled = True
        bot.ext_hours_reset_indicators_on_session_change = True
        bot._active_strategy_session = "regular"
        bot._indicators_warmed_up = True
        bot.live_strategy = DummySessionStrategy(df)

        bot._prepare_live_session_context("after_hours", bar_at_et(2026, 4, 21, 16, 0))

        self.assertEqual(bot.live_strategy.session_name, "after_hours")
        self.assertEqual(len(bot.live_strategy.df), 1)
        self.assertEqual(float(bot.live_strategy.df.iloc[0]["close"]), 101.0)
        self.assertTrue(pd.isna(bot.live_strategy.df.iloc[0]["RSI"]))
        self.assertTrue(pd.isna(bot.live_strategy.df.iloc[0]["BB_LOWER"]))
        self.assertTrue(pd.isna(bot.live_strategy.df.iloc[0]["BB_UPPER"]))
        self.assertFalse(dash.bot_state["startup_ready"])
        self.assertFalse(dash.bot_state["indicators_fully_ready"])


class HybridWarmupTests(unittest.TestCase):
    def test_startup_ready_can_be_true_before_full_indicator_warmup(self):
        bot = TWSBotOrchestrator.__new__(TWSBotOrchestrator)
        bot.live_strategy = DummyWarmupStrategy(rows=6, min_bars_required=50)
        bot.live_startup_min_bars = 6

        state = bot._update_warmup_state()

        self.assertTrue(state["startup_ready"])
        self.assertFalse(state["full_ready"])
        self.assertTrue(dash.bot_state["startup_ready"])
        self.assertTrue(dash.bot_state["indicators_ready"])
        self.assertFalse(dash.bot_state["indicators_fully_ready"])

    def test_adaptive_history_uses_extended_hours_session_capacity(self):
        bot = make_history_bot(extended_hours=True)

        plan = bot._broker_history_request_plan()

        self.assertEqual(plan["bars_per_day"], 286)
        self.assertEqual(plan["duration"], "11 D")
        self.assertEqual(plan["use_rth"], 0)

    def test_adaptive_history_uses_rth_capacity(self):
        bot = make_history_bot(extended_hours=False)

        plan = bot._broker_history_request_plan()

        self.assertEqual(plan["bars_per_day"], 78)
        self.assertEqual(plan["duration"], "34 D")
        self.assertEqual(plan["use_rth"], 1)

    def test_adaptive_history_respects_min_and_max_day_caps(self):
        min_capped = make_history_bot(min_bars_required=10, extended_hours=True, min_days=7, max_days=45)
        max_capped = make_history_bot(min_bars_required=2424, extended_hours=True, min_days=2, max_days=5)

        self.assertEqual(min_capped._broker_history_request_plan()["duration"], "7 D")
        self.assertEqual(max_capped._broker_history_request_plan()["duration"], "5 D")

    def test_broker_history_request_uses_computed_duration(self):
        bot = make_history_bot(extended_hours=True)

        bot._request_broker_history(keep_up_to_date=True, reason="test")

        self.assertEqual(bot.client.requests[-1]["durationStr"], "11 D")
        self.assertEqual(bot.client.requests[-1]["useRTH"], 0)


if __name__ == "__main__":
    unittest.main()
