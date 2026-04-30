import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.ib_web_client import IBWebClient, _detect_major_order_warnings


class _Response:
    status_code = 200
    text = "ok"

    @staticmethod
    def json():
        return [{"order_id": "1", "order_status": "Submitted"}]


class IBWebClientOrderTests(unittest.TestCase):
    def test_extended_hours_buy_uses_configured_tight_limit_padding(self):
        with patch.dict(
            "os.environ",
            {
                "EXT_HOURS_BUY_LIMIT_PAD_PCT": "0.0005",
                "EXT_HOURS_SELL_LIMIT_PAD_PCT": "0.0005",
                "EXT_HOURS_LAST_PRICE_PAD_PCT": "0.001",
            },
        ):
            client = IBWebClient()

        client.is_connected = True
        client.account_id = "DU123"
        client.get_conid = Mock(return_value=123)
        client.get_snapshot = Mock(return_value={"31": "100.00", "86": "100.10", "84": "99.90"})
        client.session = SimpleNamespace(post=Mock(return_value=_Response()))

        contract = client.get_contract("TSLA")
        order = client.create_market_order("BUY", 10, outside_rth=True)

        with patch("core.ib_web_client._us_equity_wants_outside_rth_flag", return_value=True):
            accepted = client.placeOrder(1, contract, order)

        self.assertTrue(accepted)
        payload = client.session.post.call_args.kwargs["json"]
        order_payload = payload["orders"][0]
        self.assertEqual(order_payload["orderType"], "LMT")
        self.assertTrue(order_payload["outsideRTH"])
        self.assertEqual(order_payload["price"], 100.15)

    def test_order_response_stores_broker_order_id(self):
        client = IBWebClient()

        accepted = client._handle_order_response([{"order_id": "abc-123", "order_status": "Submitted"}])

        self.assertTrue(accepted)
        self.assertEqual(client._last_order_id, "abc-123")


class MajorWarningDetectorTests(unittest.TestCase):
    def test_flags_margin_warning(self):
        flagged = _detect_major_order_warnings(
            ["This order would result in a margin call"]
        )
        self.assertEqual(len(flagged), 1)

    def test_flags_free_riding_warning(self):
        flagged = _detect_major_order_warnings(
            ["Potential Free Riding violation detected"]
        )
        self.assertEqual(len(flagged), 1)

    def test_flags_size_and_loss_warnings(self):
        flagged = _detect_major_order_warnings(
            [
                "This order is large in size and may have a market impact",
                "Stop loss will be triggered",
            ]
        )
        self.assertEqual(len(flagged), 2)

    def test_ignores_benign_messages(self):
        flagged = _detect_major_order_warnings(
            [
                "Order will execute outside of regular trading hours.",
                "You do not have a market data subscription for this exchange.",
            ]
        )
        self.assertEqual(flagged, [])


class HandleOrderResponseTests(unittest.TestCase):
    def setUp(self):
        self.client = IBWebClient()

    def test_major_warning_refuses_confirmation(self):
        confirmation_payload = [
            {
                "id": "abc-123",
                "message": [
                    "This order would exceed your buying power.",
                ],
            }
        ]
        confirm_post = Mock()
        self.client.session = SimpleNamespace(post=confirm_post)

        accepted = self.client._handle_order_response(confirmation_payload)

        self.assertFalse(accepted)
        confirm_post.assert_not_called()
        self.assertEqual(self.client._last_order_response.get("error"), "major_warning_refused")
        self.assertEqual(self.client._last_order_response.get("reply_id"), "abc-123")

    def test_benign_warning_is_auto_confirmed(self):
        confirmation_payload = [
            {
                "id": "xyz-9",
                "message": [
                    "Order will execute outside of regular trading hours.",
                ],
            }
        ]
        success_response = SimpleNamespace(
            status_code=200,
            text="ok",
            json=lambda: [{"order_id": "1", "order_status": "Submitted"}],
        )
        confirm_post = Mock(return_value=success_response)
        self.client.session = SimpleNamespace(post=confirm_post)
        self.client.base_url = "https://example/v1/api"

        accepted = self.client._handle_order_response(confirmation_payload)

        self.assertTrue(accepted)
        confirm_post.assert_called_once()
        called_url = confirm_post.call_args.args[0]
        self.assertIn("/iserver/reply/xyz-9", called_url)


if __name__ == "__main__":
    unittest.main()
