import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from core.telegram_notifier import (
    TelegramConfig,
    TelegramNotifier,
    format_buy_executed,
    format_sell_executed,
    format_status_report,
)
from main import TWSBotOrchestrator
import web.dashboard as dashboard


class _FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body if body is not None else {"ok": True, "result": {"message_id": 1}}
        self.text = text

    def json(self):
        return self._body


class TelegramNotifierTests(unittest.TestCase):
    def test_send_message_success(self):
        post = Mock(return_value=_FakeResponse())
        notifier = TelegramNotifier(
            TelegramConfig(enabled=True, bot_token="token", chat_id="-123", timeout_seconds=3),
            post_func=post,
        )

        result = notifier.send_message("hello")

        self.assertTrue(result["ok"])
        post.assert_called_once()
        url = post.call_args.args[0]
        payload = post.call_args.kwargs["json"]
        self.assertEqual(url, "https://api.telegram.org/bottoken/sendMessage")
        self.assertEqual(payload["chat_id"], "-123")
        self.assertEqual(payload["text"], "hello")
        self.assertEqual(post.call_args.kwargs["timeout"], 3)

    def test_send_message_failure(self):
        post = Mock(return_value=_FakeResponse(400, {"ok": False, "description": "Bad Request"}))
        notifier = TelegramNotifier(TelegramConfig(enabled=True, bot_token="token", chat_id="-123"), post_func=post)

        result = notifier.send_message("hello")

        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "Bad Request")

    def test_message_formatters_include_expected_fields(self):
        buy = format_buy_executed(
            {"symbol": "TSLA", "price": 100.5, "quantity": 2, "total": 201, "rsi": 26.5, "order_id": "42"}
        )
        sell = format_sell_executed(
            {
                "symbol": "TSLA",
                "price": 105,
                "quantity": 2,
                "total": 210,
                "rsi": 70.3,
                "order_id": "43",
                "gross_pnl": 9,
                "fees": 1,
                "net_pnl": 8,
                "net_pnl_pct": 3.98,
            }
        )
        status = format_status_report(
            {
                "uptime": "1m 2s",
                "symbol": "TSLA",
                "mode": "REAL",
                "last_price": 105,
                "rsi": 58.3,
                "position": "LONG",
                "timestamp": "2026-04-11 16:37:47 UTC",
            }
        )

        self.assertIn("BUY Executed", buy)
        self.assertIn("Order: 42", buy)
        self.assertIn("SELL Executed", sell)
        self.assertIn("Net:   $8.0000", sell)
        self.assertIn("Bot Status Report", status)
        self.assertIn("2026-04-11 16:37:47 UTC", status)


class TelegramDashboardRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = dashboard.app.test_client()
        dashboard.app.config["TESTING"] = True
        dashboard.configure_telegram_test_handler(None)

    def tearDown(self):
        dashboard.configure_telegram_test_handler(None)

    def test_telegram_test_route_uses_configured_handler(self):
        dashboard.configure_telegram_test_handler(lambda: {"ok": True, "message": "sent"})

        response = self.client.post("/api/notifications/telegram/test")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], "sent")

    def test_telegram_test_route_unavailable_without_handler(self):
        response = self.client.post("/api/notifications/telegram/test")

        self.assertEqual(response.status_code, 503)


class TelegramOrderNotificationTests(unittest.TestCase):
    def setUp(self):
        dashboard.live_trades_data.clear()
        dashboard.live_logs_buffer.clear()

    def test_order_filled_notification_sends_once_per_order(self):
        bot = TWSBotOrchestrator.__new__(TWSBotOrchestrator)
        bot.live_symbol = "TSLA"
        bot._telegram_notified_orders = set()
        bot._telegram_position_basis = {}
        bot.telegram_summary = {"buy_count": 0, "sell_count": 0, "fees": 0.0, "net_pnl": 0.0}
        bot._extract_live_metrics = lambda: {"rsi": 26.5}
        bot.telegram_notifier = SimpleNamespace(
            config=SimpleNamespace(notify_order_filled=True),
            send_message_async=Mock(return_value={"ok": True, "message": "queued"}),
        )

        tracked = {"action": "BUY", "qty": 2, "symbol": "TSLA", "price": 100.0}
        order_info = {"status": "filled", "avgPrice": 100.0, "filledQuantity": 2}

        bot._notify_order_filled("42", tracked, order_info)
        bot._notify_order_filled("42", tracked, order_info)

        bot.telegram_notifier.send_message_async.assert_called_once()
        self.assertEqual(bot.telegram_summary["buy_count"], 1)

    def test_recent_trade_reconciliation_sends_fill_notification(self):
        bot = TWSBotOrchestrator.__new__(TWSBotOrchestrator)
        bot.live_symbol = "MSTR"
        bot.live_position = "FLAT"
        bot._telegram_notified_orders = set()
        bot._telegram_position_basis = {}
        bot.telegram_summary = {"buy_count": 0, "sell_count": 0, "fees": 0.0, "net_pnl": 0.0}
        bot._tracked_orders = {
            "987": {
                "action": "BUY",
                "qty": 3,
                "symbol": "MSTR",
                "price": 420.0,
                "notional": 1260.0,
                "ts": 1,
            }
        }
        bot._extract_live_metrics = lambda: {"rsi": 24.5}
        bot._record_order_filled = Mock()
        bot.telegram_notifier = SimpleNamespace(
            config=SimpleNamespace(notify_order_filled=True),
            send_message_async=Mock(return_value={"ok": True, "message": "queued"}),
        )
        trades = [{"order_id": "987", "executionPrice": 421.25, "shares": 3, "commission": 1.0}]

        bot._reconcile_recent_trade_fills(trades)

        bot.telegram_notifier.send_message_async.assert_called_once()
        bot._record_order_filled.assert_called_once()
        self.assertNotIn("987", bot._tracked_orders)
        self.assertEqual(bot.telegram_summary["buy_count"], 1)


if __name__ == "__main__":
    unittest.main()
