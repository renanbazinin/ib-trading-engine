import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from core.trading_hours import TradingHours, interval_to_minutes


NY = ZoneInfo("America/New_York")


def dt(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=NY)


class TradingHoursTests(unittest.TestCase):
    def setUp(self):
        self.hours = TradingHours.from_config()

    def assert_session(self, value, is_open, name):
        status = self.hours.status(value)
        self.assertEqual(status.is_open, is_open, status)
        self.assertEqual(status.session_name, name, status)

    def test_overnight_boundaries(self):
        self.assert_session(dt(2026, 4, 21, 3, 49), True, "overnight")
        self.assert_session(dt(2026, 4, 21, 3, 50), False, "closed")
        self.assert_session(dt(2026, 4, 21, 3, 59), False, "closed")

    def test_pre_market_regular_and_after_hours_boundaries(self):
        self.assert_session(dt(2026, 4, 21, 4, 0), True, "pre_market")
        self.assert_session(dt(2026, 4, 21, 9, 29), True, "pre_market")
        self.assert_session(dt(2026, 4, 21, 9, 30), True, "regular")
        self.assert_session(dt(2026, 4, 21, 16, 0), True, "after_hours")
        self.assert_session(dt(2026, 4, 21, 19, 59), True, "after_hours")
        self.assert_session(dt(2026, 4, 21, 20, 0), True, "overnight")

    def test_friday_close_and_weekend(self):
        self.assert_session(dt(2026, 4, 24, 19, 59), True, "after_hours")
        self.assert_session(dt(2026, 4, 24, 20, 0), False, "closed")
        self.assert_session(dt(2026, 4, 25, 10, 0), False, "closed")
        self.assert_session(dt(2026, 4, 26, 19, 59), False, "closed")
        self.assert_session(dt(2026, 4, 26, 20, 0), True, "overnight")

    def test_midnight_overnight(self):
        self.assert_session(dt(2026, 4, 22, 0, 0), True, "overnight")

    def test_disabled_hours_are_always_open(self):
        hours = TradingHours.from_config(enabled=False)
        status = hours.status(dt(2026, 4, 25, 10, 0))
        self.assertTrue(status.is_open)
        self.assertEqual(status.reason, "trading_hours_disabled")

    def test_interval_to_minutes(self):
        self.assertEqual(interval_to_minutes("5m"), 5)
        self.assertEqual(interval_to_minutes("5 mins"), 5)
        self.assertEqual(interval_to_minutes("1h"), 60)
        self.assertEqual(interval_to_minutes("", default=3), 3)


if __name__ == "__main__":
    unittest.main()
