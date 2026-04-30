import unittest
from datetime import datetime, timezone

from core.yfinance_provider import YFinancePollingProvider


class _Bar:
    date = "2026-04-21T04:00:00+00:00"
    timestamp = datetime(2026, 4, 21, 4, 0, tzinfo=timezone.utc)

    def signature(self):
        return (self.date, 1.0, 1.0, 1.0, 1.0, 100.0)


class YFinancePollingProviderTests(unittest.TestCase):
    def test_backfill_uses_configured_prepost_setting(self):
        observed_prepost = []
        provider = YFinancePollingProvider(symbol="TSLA", prepost=True)

        def fake_load_bars(period):
            observed_prepost.append(provider.prepost)
            return [_Bar()]

        provider._load_bars = fake_load_bars
        provider._emit_backfill()

        self.assertEqual(observed_prepost, [True])
        self.assertTrue(provider.prepost)


if __name__ == "__main__":
    unittest.main()
