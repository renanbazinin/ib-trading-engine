import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.ib_web_client import IBWebClient
from main import TWSBotOrchestrator, _ib_bar_size_from_interval
import web.dashboard as dashboard


class BrokerReauthClientTests(unittest.TestCase):
    def test_request_reauthentication_returns_success_when_already_authenticated(self):
        client = IBWebClient()
        client._check_auth_status = Mock(return_value={"authenticated": True})
        client._fetch_initial_data = Mock()
        client.account_id = "DU123"

        result = client.request_reauthentication()

        self.assertTrue(result["ok"])
        self.assertTrue(result["authenticated"])
        self.assertTrue(client.is_connected)
        self.assertEqual(result["account_id"], "DU123")
        client._fetch_initial_data.assert_called_once()

    def test_request_reauthentication_reports_failure(self):
        client = IBWebClient()
        client._check_auth_status = Mock(return_value={"authenticated": False})
        client._try_reauthenticate = Mock(return_value=False)

        result = client.request_reauthentication()

        self.assertFalse(result["ok"])
        self.assertFalse(result["authenticated"])
        self.assertFalse(client.is_connected)
        self.assertIn("did not complete", result["message"])


class DashboardRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = dashboard.app.test_client()
        dashboard.app.config["TESTING"] = True
        dashboard.configure_broker_reauth_handler(None)
        dashboard.bot_state["is_live_active"] = False
        dashboard.bot_state["trading_mode"] = "PAPER"

    def tearDown(self):
        dashboard.configure_broker_reauth_handler(None)
        dashboard.bot_state["is_live_active"] = False
        dashboard.bot_state["trading_mode"] = "PAPER"

    def test_broker_reauthenticate_uses_configured_handler(self):
        dashboard.configure_broker_reauth_handler(lambda: {"ok": True, "message": "done"})

        response = self.client.post("/api/broker/reauthenticate")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], "done")

    def test_live_toggle_requires_confirmation_in_live_mode(self):
        dashboard.bot_state["trading_mode"] = "LIVE"
        dashboard.bot_state["is_live_active"] = False

        response = self.client.post("/api/bot/toggle", json={})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(dashboard.bot_state["is_live_active"])

        ok_response = self.client.post("/api/bot/toggle", json={"confirmation": "LIVE"})
        self.assertEqual(ok_response.status_code, 200)
        self.assertTrue(dashboard.bot_state["is_live_active"])


class ExecuteLiveTradeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.previous_account = dict(dashboard.live_account_data)
        self.previous_positions = dict(dashboard.live_positions_data)
        self.previous_runtime = dict(dashboard.live_runtime_state)
        self.previous_trades = list(dashboard.live_trades_data)
        self.previous_live_state = dashboard.bot_state.get("is_live_active", False)
        dashboard.live_account_data.clear()
        dashboard.live_positions_data.clear()
        dashboard.live_trades_data.clear()
        dashboard.live_runtime_state.clear()
        dashboard.live_runtime_state.update({"slot_ledger": {}, "risk_counters": {}})
        dashboard.bot_state["is_live_active"] = True
        dashboard.live_account_data["NetLiquidation"] = {
            "value": 10000.0,
            "currency": "USD",
            "account": "DU123",
        }
        dashboard.live_account_data["SettledCash"] = {
            "value": 10000.0,
            "currency": "USD",
            "account": "DU123",
        }
        dashboard.bot_state["account_data_ready"] = True
        dashboard.bot_state["positions_data_ready"] = True

    def tearDown(self):
        dashboard.live_account_data.clear()
        dashboard.live_account_data.update(self.previous_account)
        dashboard.live_positions_data.clear()
        dashboard.live_positions_data.update(self.previous_positions)
        dashboard.live_trades_data.clear()
        dashboard.live_trades_data.extend(self.previous_trades)
        dashboard.live_runtime_state.clear()
        dashboard.live_runtime_state.update(self.previous_runtime)
        dashboard.bot_state["is_live_active"] = self.previous_live_state
        dashboard.bot_state["account_data_ready"] = False
        dashboard.bot_state["positions_data_ready"] = False

    def _bot(self, max_quantity=None, max_notional=None):
        bot = TWSBotOrchestrator.__new__(TWSBotOrchestrator)
        bot.trading_mode = "PAPER"
        bot.web_api_enabled = False
        bot.live_symbol = "TSLA"
        bot.max_order_quantity = max_quantity
        bot.max_order_notional = max_notional
        bot.ext_hours_max_order_notional = None
        bot.extended_hours = False
        bot.live_order_sizing_mode = "slot_percent"
        bot.live_slot_allocation_pct = 0.25
        bot.live_max_position_slots = 4
        bot.live_allocation_pct = 0.25
        bot.live_buy_cash_source = "settled_cash"
        bot.live_buy_cash_fallbacks = []
        bot.max_buys_per_day = 4
        bot.max_orders_per_day = 8
        bot.min_seconds_between_orders = 0.0
        bot.daily_loss_stop_pct = None
        bot.telegram_summary = {"net_pnl": 0.0}
        bot.trade_quantity = 100
        bot.client = SimpleNamespace(is_connected=True)
        bot.feed_health = SimpleNamespace(snapshot=lambda: SimpleNamespace(should_pause_live=False, active_source="yfinance"))
        bot.trading_hours = SimpleNamespace(status=lambda: SimpleNamespace(is_open=True, as_dict=lambda: {}))
        bot._sync_dashboard_status = lambda: None
        return bot

    @staticmethod
    def _trade_time_ms(days_ago=0):
        return int((datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp() * 1000)

    @staticmethod
    def _epoch_ms(value):
        return int(value.timestamp() * 1000)

    def test_buy_plan_caps_quantity(self):
        bot = self._bot(max_quantity=10)

        plan = bot._build_live_order_plan("BUY", 100.0)

        self.assertTrue(plan.allowed)
        self.assertEqual(plan.quantity, 10)
        self.assertEqual(plan.order_notional, 1000.0)

    def test_buy_plan_caps_notional(self):
        bot = self._bot(max_notional=1200.0)

        plan = bot._build_live_order_plan("BUY", 100.0)

        self.assertTrue(plan.allowed)
        self.assertEqual(plan.quantity, 12)
        self.assertEqual(plan.order_notional, 1200.0)

    def test_startup_position_counts_used_slots(self):
        bot = self._bot()
        dashboard.live_positions_data["DU123_TSLA_STK"] = {
            "account": "DU123",
            "symbol": "TSLA",
            "secType": "STK",
            "position": 25,
            "avgCost": 100.0,
        }

        plan = bot._build_live_order_plan("BUY", 100.0)

        self.assertTrue(plan.allowed)
        self.assertEqual(plan.slots_used, 1)
        self.assertEqual(plan.remaining_slots, 3)

    def test_partial_capped_exposure_does_not_consume_a_full_slot(self):
        bot = self._bot()
        dashboard.live_positions_data["DU123_TSLA_STK"] = {
            "account": "DU123",
            "symbol": "TSLA",
            "secType": "STK",
            "position": 20,
            "avgCost": 100.0,
        }

        plan = bot._build_live_order_plan("BUY", 100.0)

        self.assertTrue(plan.allowed)
        self.assertEqual(plan.slots_used, 0)
        self.assertEqual(plan.remaining_slots, 4)

    def test_order_ledger_partial_lot_fills_remaining_slot_budget(self):
        bot = self._bot(max_notional=1200.0)
        dashboard.live_runtime_state["slot_ledger"] = {
            "date": bot._ledger_day_key(),
            "base_net_liquidation": 10000.0,
            "symbols": {
                "TSLA": {
                    "buy_lots": [
                        {"order_id": "1", "notional": 1200.0, "quantity": 12, "status": "filled"}
                    ]
                }
            },
        }

        plan = bot._build_live_order_plan("BUY", 100.0)

        self.assertTrue(plan.allowed)
        self.assertEqual(plan.slots_used, 0)
        self.assertEqual(plan.quantity, 12)
        self.assertEqual(plan.order_notional, 1200.0)

    def test_order_ledger_completed_lot_counts_one_slot(self):
        bot = self._bot()
        dashboard.live_runtime_state["slot_ledger"] = {
            "date": bot._ledger_day_key(),
            "base_net_liquidation": 10000.0,
            "symbols": {
                "TSLA": {
                    "buy_lots": [
                        {"order_id": "1", "notional": 2500.0, "quantity": 25, "status": "filled"}
                    ]
                }
            },
        }

        plan = bot._build_live_order_plan("BUY", 100.0)

        self.assertTrue(plan.allowed)
        self.assertEqual(plan.slots_used, 1)
        self.assertEqual(plan.remaining_slots, 3)

    def test_buy_plan_blocks_when_slots_full(self):
        bot = self._bot()
        dashboard.live_positions_data["DU123_TSLA_STK"] = {
            "account": "DU123",
            "symbol": "TSLA",
            "secType": "STK",
            "position": 100,
            "avgCost": 100.0,
        }

        plan = bot._build_live_order_plan("BUY", 100.0)

        self.assertFalse(plan.allowed)
        self.assertEqual(plan.slots_used, 4)
        self.assertIn("slot capacity full", plan.reason)

    def test_slot_outlook_full_slots_points_to_sell_exit(self):
        bot = self._bot()
        dashboard.live_positions_data["DU123_TSLA_STK"] = {
            "account": "DU123",
            "symbol": "TSLA",
            "secType": "STK",
            "position": 100,
            "avgCost": 100.0,
        }

        bot._refresh_live_position_state(100.0)
        outlook = dashboard.bot_state["slot_outlook"]

        self.assertEqual(outlook["slots"], "4/4")
        self.assertEqual(outlook["next_action"], "SELL")
        self.assertFalse(outlook["buy"]["allowed"])
        self.assertTrue(outlook["sell"]["allowed"])

    def test_slot_outlook_partial_slots_allows_buy_or_sell(self):
        bot = self._bot()
        dashboard.live_positions_data["DU123_TSLA_STK"] = {
            "account": "DU123",
            "symbol": "TSLA",
            "secType": "STK",
            "position": 75,
            "avgCost": 100.0,
        }

        bot._refresh_live_position_state(100.0)
        outlook = dashboard.bot_state["slot_outlook"]

        self.assertEqual(outlook["slots"], "3/4")
        self.assertEqual(outlook["remaining_slots"], 1)
        self.assertEqual(outlook["next_action"], "BUY_OR_SELL")
        self.assertTrue(outlook["buy"]["allowed"])
        self.assertTrue(outlook["sell"]["allowed"])

    def test_slot_outlook_latest_buy_signal_prefers_buy_when_slot_open(self):
        bot = self._bot()
        dashboard.live_positions_data["DU123_TSLA_STK"] = {
            "account": "DU123",
            "symbol": "TSLA",
            "secType": "STK",
            "position": 75,
            "avgCost": 100.0,
        }
        bot._refresh_live_position_state(100.0)

        buy_plan = bot._build_live_order_plan("BUY", 100.0)
        sell_plan = bot._build_live_order_plan("SELL", 100.0)
        bot._publish_live_slot_outlook(
            current_price=100.0,
            signal_name="BUY",
            buy_plan=buy_plan,
            sell_plan=sell_plan,
        )
        outlook = dashboard.bot_state["slot_outlook"]

        self.assertEqual(outlook["slots"], "3/4")
        self.assertEqual(outlook["next_action"], "BUY")
        self.assertEqual(outlook["buy"]["quantity"], 25)

    def test_fixed_quantity_still_respects_slot_capacity(self):
        bot = self._bot()
        bot.live_order_sizing_mode = "fixed_quantity"
        bot.trade_quantity = 95
        dashboard.live_positions_data["DU123_TSLA_STK"] = {
            "account": "DU123",
            "symbol": "TSLA",
            "secType": "STK",
            "position": 100,
            "avgCost": 100.0,
        }

        plan = bot._build_live_order_plan("BUY", 100.0)

        self.assertFalse(plan.allowed)
        self.assertIn("slot capacity full", plan.reason)

    def test_sell_plan_exits_full_symbol_position(self):
        bot = self._bot()
        dashboard.live_positions_data["DU123_TSLA_STK"] = {
            "account": "DU123",
            "symbol": "TSLA",
            "secType": "STK",
            "position": 12,
            "avgCost": 100.0,
        }

        plan = bot._build_live_order_plan("SELL", 100.0)

        self.assertTrue(plan.allowed)
        self.assertEqual(plan.quantity, 12)

    def test_sell_plan_ignores_notional_and_quantity_caps(self):
        bot = self._bot(max_quantity=5, max_notional=500.0)
        bot.ext_hours_max_order_notional = 500.0
        dashboard.live_positions_data["DU123_TSLA_STK"] = {
            "account": "DU123",
            "symbol": "TSLA",
            "secType": "STK",
            "position": 12,
            "avgCost": 100.0,
        }

        plan = bot._build_live_order_plan("SELL", 100.0)

        self.assertTrue(plan.allowed)
        self.assertEqual(plan.quantity, 12)
        self.assertAlmostEqual(plan.order_notional, 1200.0)

    def test_sell_plan_works_in_extended_hours_without_cap_config(self):
        bot = self._bot()
        bot.web_api_enabled = True
        bot.extended_hours = True
        dashboard.live_positions_data["DU123_TSLA_STK"] = {
            "account": "DU123",
            "symbol": "TSLA",
            "secType": "STK",
            "position": 8,
            "avgCost": 100.0,
        }

        with patch("main.is_us_equity_outside_regular_hours", return_value=True):
            plan = bot._build_live_order_plan("SELL", 100.0)

        self.assertTrue(plan.allowed)
        self.assertEqual(plan.quantity, 8)

    def test_sell_plan_with_unloaded_positions_is_marked_transient(self):
        bot = self._bot()
        dashboard.bot_state["positions_data_ready"] = False

        plan = bot._build_live_order_plan("SELL", 100.0)

        self.assertFalse(plan.allowed)
        self.assertTrue(plan.transient)
        self.assertIn("position data is not ready", plan.reason)

    def test_buy_plan_with_unready_account_is_marked_transient(self):
        bot = self._bot()
        dashboard.bot_state["account_data_ready"] = False
        dashboard.live_account_data.pop("SettledCash")

        plan = bot._build_live_order_plan("BUY", 100.0)

        self.assertFalse(plan.allowed)
        self.assertTrue(plan.transient)
        self.assertIn("account data is not ready", plan.reason)

    def test_buy_cash_uses_configured_fallback_when_primary_missing(self):
        bot = self._bot()
        bot.live_buy_cash_fallbacks = ["available_funds"]
        dashboard.live_account_data.pop("SettledCash")
        dashboard.live_account_data["AvailableFunds"] = {
            "value": 900.0,
            "currency": "USD",
            "account": "DU123",
        }

        plan = bot._build_live_order_plan("BUY", 100.0)

        self.assertTrue(plan.allowed)
        self.assertEqual(plan.cash_source, "AvailableFunds")
        self.assertEqual(plan.quantity, 9)

    def test_recent_sell_caps_total_cash_until_estimated_settlement(self):
        bot = self._bot()
        bot.live_buy_cash_source = "total_cash"
        bot.live_order_sizing_mode = "fixed_quantity"
        bot.trade_quantity = 500
        bot.live_max_position_slots = 100
        dashboard.live_account_data["TotalCashValue"] = {
            "value": 35000.0,
            "currency": "USD",
            "account": "DU123",
        }
        dashboard.live_trades_data.append(
            {
                "execution_id": "sell-1",
                "order_id": 1,
                "side": "S",
                "sec_type": "STK",
                "symbol": "TSLA",
                "size": 100,
                "price": "250.00",
                "commission": "0",
                "net_amount": 25000.0,
                "trade_time_r": self._trade_time_ms(),
            }
        )

        plan = bot._build_live_order_plan("BUY", 100.0)

        self.assertTrue(plan.allowed)
        self.assertEqual(plan.cash_source, "TotalCashValue")
        self.assertEqual(plan.cash_available, 10000.0)
        self.assertEqual(plan.quantity, 100)
        self.assertEqual(dashboard.bot_state["unsettled_sale_proceeds"], 25000.0)
        self.assertEqual(len(dashboard.bot_state["unsettled_sell_fills"]), 1)

    def test_old_sell_is_ignored_after_estimated_settlement(self):
        bot = self._bot()
        bot.live_buy_cash_source = "total_cash"
        bot.live_order_sizing_mode = "fixed_quantity"
        bot.trade_quantity = 500
        bot.live_max_position_slots = 100
        dashboard.live_account_data["TotalCashValue"] = {
            "value": 35000.0,
            "currency": "USD",
            "account": "DU123",
        }
        dashboard.live_trades_data.append(
            {
                "execution_id": "sell-old",
                "side": "S",
                "sec_type": "STK",
                "symbol": "TSLA",
                "size": 100,
                "price": "250.00",
                "net_amount": 25000.0,
                "trade_time_r": self._trade_time_ms(days_ago=10),
            }
        )

        plan = bot._build_live_order_plan("BUY", 100.0)

        self.assertTrue(plan.allowed)
        self.assertEqual(plan.cash_available, 35000.0)
        self.assertEqual(plan.quantity, 350)
        self.assertEqual(dashboard.bot_state["unsettled_sale_proceeds"], 0.0)

    def test_settlement_guard_holds_through_settlement_day(self):
        bot = self._bot()
        trade_time = datetime(2026, 4, 27, 15, 30, tzinfo=timezone.utc)
        now_on_settlement_day = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
        dashboard.live_trades_data.append(
            {
                "execution_id": "sell-settlement-day",
                "side": "S",
                "sec_type": "STK",
                "symbol": "TSLA",
                "size": 100,
                "price": "250.00",
                "net_amount": 25000.0,
                "trade_time_r": self._epoch_ms(trade_time),
            }
        )

        snapshot = bot._unsettled_sale_proceeds(now=now_on_settlement_day)

        self.assertEqual(snapshot["total"], 25000.0)
        self.assertEqual(snapshot["next_estimated_settlement"], "2026-04-28")
        self.assertEqual(snapshot["next_release_at"], "2026-04-29T00:00:00+00:00")
        self.assertEqual(snapshot["fills"][0]["settlement_date"], "2026-04-28")
        self.assertEqual(snapshot["fills"][0]["release_at"], "2026-04-29T00:00:00+00:00")

    def test_settlement_guard_dedupes_duplicate_executions(self):
        bot = self._bot()
        trade = {
            "execution_id": "sell-duplicate",
            "side": "S",
            "sec_type": "STK",
            "symbol": "TSLA",
            "size": 100,
            "price": "250.00",
            "net_amount": 25000.0,
            "trade_time_r": self._trade_time_ms(),
        }
        dashboard.live_trades_data.extend([dict(trade), dict(trade)])

        snapshot = bot._unsettled_sale_proceeds()

        self.assertEqual(snapshot["total"], 25000.0)
        self.assertEqual(len(snapshot["fills"]), 1)
        self.assertEqual(dashboard.bot_state["unsettled_sale_proceeds"], 25000.0)

    def test_settlement_guard_ignores_non_sell_sides_starting_with_s(self):
        bot = self._bot()
        dashboard.live_trades_data.append(
            {
                "execution_id": "not-a-sell",
                "side": "SUBMITTED",
                "sec_type": "STK",
                "symbol": "TSLA",
                "size": 100,
                "price": "250.00",
                "net_amount": 25000.0,
                "trade_time_r": self._trade_time_ms(),
            }
        )

        snapshot = bot._unsettled_sale_proceeds()

        self.assertEqual(snapshot["total"], 0.0)
        self.assertEqual(snapshot["fills"], [])
        self.assertEqual(dashboard.bot_state["unsettled_sale_proceeds"], 0.0)

    def test_buy_execution_does_not_create_unsettled_sale_proceeds(self):
        bot = self._bot()
        bot.live_buy_cash_source = "total_cash"
        bot.live_order_sizing_mode = "fixed_quantity"
        bot.trade_quantity = 500
        bot.live_max_position_slots = 100
        dashboard.live_account_data["TotalCashValue"] = {
            "value": 35000.0,
            "currency": "USD",
            "account": "DU123",
        }
        dashboard.live_trades_data.append(
            {
                "execution_id": "buy-1",
                "side": "B",
                "sec_type": "STK",
                "symbol": "TSLA",
                "size": 100,
                "price": "250.00",
                "net_amount": 25000.0,
                "trade_time_r": self._trade_time_ms(),
            }
        )

        plan = bot._build_live_order_plan("BUY", 100.0)

        self.assertTrue(plan.allowed)
        self.assertEqual(plan.cash_available, 35000.0)
        self.assertEqual(plan.quantity, 350)
        self.assertEqual(dashboard.bot_state["unsettled_sale_proceeds"], 0.0)

    def test_missing_settled_cash_without_fallback_still_blocks_even_with_total_cash(self):
        bot = self._bot()
        dashboard.live_account_data.pop("SettledCash")
        dashboard.live_account_data["TotalCashValue"] = {
            "value": 35000.0,
            "currency": "USD",
            "account": "DU123",
        }
        dashboard.live_trades_data.append(
            {
                "execution_id": "sell-1",
                "side": "S",
                "sec_type": "STK",
                "symbol": "TSLA",
                "size": 100,
                "price": "250.00",
                "net_amount": 25000.0,
                "trade_time_r": self._trade_time_ms(),
            }
        )

        plan = bot._build_live_order_plan("BUY", 100.0)

        self.assertFalse(plan.allowed)
        self.assertTrue(plan.transient)
        self.assertIn("account data is not ready", plan.reason)

    def test_configured_fallback_cash_is_capped_by_unsettled_sell(self):
        bot = self._bot()
        bot.live_buy_cash_fallbacks = ["available_funds"]
        bot.live_order_sizing_mode = "fixed_quantity"
        bot.trade_quantity = 500
        bot.live_max_position_slots = 100
        dashboard.live_account_data.pop("SettledCash")
        dashboard.live_account_data["AvailableFunds"] = {
            "value": 35000.0,
            "currency": "USD",
            "account": "DU123",
        }
        dashboard.live_trades_data.append(
            {
                "execution_id": "sell-1",
                "side": "S",
                "sec_type": "STK",
                "symbol": "TSLA",
                "size": 100,
                "price": "250.00",
                "net_amount": 25000.0,
                "trade_time_r": self._trade_time_ms(),
            }
        )

        plan = bot._build_live_order_plan("BUY", 100.0)

        self.assertTrue(plan.allowed)
        self.assertEqual(plan.cash_source, "AvailableFunds")
        self.assertEqual(plan.cash_available, 10000.0)
        self.assertEqual(plan.quantity, 100)

    def test_daily_buy_limit_blocks_new_buys(self):
        bot = self._bot()
        dashboard.live_runtime_state["risk_counters"] = {
            "date": bot._ledger_day_key(),
            "buy_orders": 4,
            "total_orders": 4,
            "last_order_ts": 0.0,
        }

        plan = bot._build_live_order_plan("BUY", 100.0)

        self.assertFalse(plan.allowed)
        self.assertIn("daily BUY limit reached", plan.reason)

    def test_daily_buy_limit_does_not_block_sell_exit(self):
        bot = self._bot()
        dashboard.live_runtime_state["risk_counters"] = {
            "date": bot._ledger_day_key(),
            "buy_orders": 4,
            "total_orders": 8,
            "last_order_ts": 0.0,
        }
        dashboard.live_positions_data["DU123_TSLA_STK"] = {
            "account": "DU123",
            "symbol": "TSLA",
            "secType": "STK",
            "position": 8,
            "avgCost": 100.0,
        }

        plan = bot._build_live_order_plan("SELL", 100.0)

        self.assertTrue(plan.allowed)
        self.assertEqual(plan.quantity, 8)

    def test_extended_hours_web_buy_requires_a_cap(self):
        bot = self._bot()
        bot.web_api_enabled = True
        bot.extended_hours = True

        with patch("main.is_us_equity_outside_regular_hours", return_value=True):
            plan = bot._build_live_order_plan("BUY", 100.0)

        self.assertFalse(plan.allowed)
        self.assertIn("extended-hours Web API orders require", plan.reason)

    def test_web_positions_prefer_ticker_over_contract_desc(self):
        bot = self._bot()
        bot.web_api_enabled = True
        bot._last_account_poll_time = 0
        bot.client = SimpleNamespace(
            is_connected=True,
            account_id="DU123",
            get_account_summary=Mock(return_value={}),
            get_portfolio_summary=Mock(return_value={}),
            get_positions=Mock(
                return_value=[
                    {
                        "ticker": "TSLA",
                        "contractDesc": "TESLA INC",
                        "assetClass": "STK",
                        "position": 3,
                        "avgCost": 100.0,
                    }
                ]
            ),
        )

        bot._poll_account_data()

        self.assertEqual(bot._symbol_position_quantity(), 3)

    def test_broker_use_rth_follows_extended_hours_config(self):
        bot = self._bot()
        bot.extended_hours = False
        self.assertEqual(bot._broker_use_rth(), 1)
        bot.extended_hours = True
        self.assertEqual(bot._broker_use_rth(), 0)


class BrokerBarSizeTests(unittest.TestCase):
    def test_live_interval_maps_to_ib_bar_size(self):
        self.assertEqual(_ib_bar_size_from_interval("10m"), "10 mins")
        self.assertEqual(_ib_bar_size_from_interval("1h"), "1 hour")


if __name__ == "__main__":
    unittest.main()
