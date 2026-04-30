import logging
import math
import os
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

from core.feed_health import FeedHealthMonitor
from core.ib_client import EventRouterClient
from core.ib_web_client import IBWebClient, is_us_equity_outside_regular_hours
from core.market_data_provider import NormalizedBar, from_ib_bar, normalize_timestamp
from core.sim_config_loader import SimulationConfigError, get_simulation_config_path, load_simulation_config
from core.telegram_notifier import (
    TelegramConfig,
    TelegramNotifier,
    format_buy_executed,
    format_sell_executed,
    format_status_report,
)
from core.trading_hours import TradingHours, interval_to_minutes
from core.yfinance_provider import YFinancePollingProvider
from simulation.sim_manager import SimManager
from strategies.strategy_factory import create_strategy
import web.dashboard as dash

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

load_dotenv()


@dataclass
class LiveOrderPlan:
    action: str
    quantity: int = 0
    order_notional: float = 0.0
    allowed: bool = False
    reason: str = ""
    sizing_mode: str = ""
    cash_source: str = ""
    cash_available: float = 0.0
    net_liquidation: float = 0.0
    base_net_liquidation: float = 0.0
    symbol_exposure: float = 0.0
    slot_notional: float = 0.0
    slots_used: int = 0
    max_slots: int = 0
    remaining_slots: int = 0
    transient: bool = False

    def as_payload(self) -> dict:
        return {
            "action": self.action,
            "quantity": self.quantity,
            "notional": round(self.order_notional, 2),
            "allowed": self.allowed,
            "reason": self.reason,
            "sizing_mode": self.sizing_mode,
            "cash_source": self.cash_source,
            "cash_available": round(self.cash_available, 2),
            "net_liquidation": round(self.net_liquidation, 2),
            "base_net_liquidation": round(self.base_net_liquidation, 2),
            "symbol_exposure": round(self.symbol_exposure, 2),
            "slot_notional": round(self.slot_notional, 2),
            "slots_used": self.slots_used,
            "max_slots": self.max_slots,
            "remaining_slots": self.remaining_slots,
            "transient": self.transient,
        }


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"Invalid integer value for {name}='{raw}'. Using default {default}.")
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"Invalid float value for {name}='{raw}'. Using default {default}.")
        return default


def _env_optional_int(name: str):
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"Invalid integer value for {name}='{raw}'. Ignoring this cap.")
        return None
    return value if value > 0 else None


def _env_optional_float(name: str):
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning(f"Invalid float value for {name}='{raw}'. Ignoring this cap.")
        return None
    return value if value > 0 else None


def _env_list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        raw = default
    return [token.strip().lower() for token in raw.split(",") if token.strip()]


def _epoch_to_iso(ts: float):
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _ib_bar_size_from_interval(interval: str) -> str:
    value = (interval or "5m").strip().lower()
    aliases = {
        "1m": "1 min",
        "2m": "2 mins",
        "3m": "3 mins",
        "5m": "5 mins",
        "10m": "10 mins",
        "15m": "15 mins",
        "20m": "20 mins",
        "30m": "30 mins",
        "1h": "1 hour",
        "2h": "2 hours",
        "3h": "3 hours",
        "4h": "4 hours",
        "1d": "1 day",
    }
    mapped = aliases.get(value)
    if mapped:
        return mapped

    logger.warning(f"Unsupported LIVE_BAR_INTERVAL='{interval}' for IBKR history. Falling back to 5 mins.")
    return "5 mins"


class TWSBotOrchestrator:
    def __init__(self):
        self.started_at = time.time()
        self.web_api_base_url = None
        self.web_api_enabled = _env_bool("IBKR_WEB_API_ENABLED", False)
        if self.web_api_enabled:
            self.web_api_base_url = os.getenv("IBKR_WEB_API_BASE_URL", "https://127.0.0.1:5000/v1/api")
            self.client = IBWebClient(base_url=self.web_api_base_url)
            self.client.configure_reconnect(
                enabled=_env_bool("WEB_API_RECONNECT_ENABLED", True),
                max_attempts=_env_int("WEB_API_RECONNECT_MAX_ATTEMPTS", 10),
                interval=_env_int("WEB_API_RECONNECT_INTERVAL_SECONDS", 60),
                health_check_interval=_env_int("WEB_API_HEALTH_CHECK_INTERVAL_SECONDS", 30),
            )
            self.client.on_connection_lost = self._on_broker_connection_lost
            self.client.on_connection_restored = self._on_broker_connection_restored
            logger.info(f"Bot configured to use IBKR Client Portal API (Web API) at {self.web_api_base_url}")
        else:
            self.client = EventRouterClient()
            logger.info("Bot configured to use TWS Socket API")

        self.shutdown_event = threading.Event()

        self._bar_lock = threading.Lock()
        self._last_processed_signature = None
        self._last_processed_ts = None
        self._last_logged_bar_ts = None
        self._last_guard_state = False
        self._last_near_signal_key = None
        self._broker_was_connected = False
        self._last_market_session_open = None
        self._last_market_session_name = None
        self._last_bar_gate_log_key = None
        self._last_pending_session_wait_log = 0.0
        self._broker_reauth_lock = threading.Lock()
        self._telegram_notified_orders = set()
        self._telegram_position_basis = {}
        self.telegram_summary = {
            "buy_count": 0,
            "sell_count": 0,
            "fees": 0.0,
            "net_pnl": 0.0,
        }

        strategy_name = os.getenv("STRATEGY", "RSI_BB_FEE_AWARE_V4B").upper()
        strategy_config = {
            "rsi_period": _env_int("RSI_PERIOD", 14),
            "bb_length": _env_int("BB_LENGTH", 20),
            "bb_std": _env_float("RSI_BB_V3_BB_STD", _env_float("BB_STD", 2.2)),
            "overbought": _env_int("OVERBOUGHT", 70),
            "oversold": _env_int("OVERSOLD", 35),
            "stop_loss_pct": _env_float(
                "RSI_BB_V3_STOP_LOSS_PCT",
                _env_float("RSI_ONLY_STOP_LOSS_PCT", _env_float("STOP_LOSS_PCT", 0.03)),
            ),
            "trend_timeframe": os.getenv("RSI_BB_V3_TREND_TIMEFRAME", os.getenv("RSI_V2_TREND_TIMEFRAME", "1h")),
            "trend_ema_period": _env_int("RSI_BB_V3_TREND_EMA_PERIOD", _env_int("RSI_V2_TREND_EMA_PERIOD", 200)),
            "atr_stop_multiple": _env_float("RSI_V2_ATR_STOP_MULTIPLE", 2.0),
            "bull_oversold": _env_float("RSI_BB_V3_BULL_OVERSOLD", 33.0),
            "bear_oversold": _env_float("RSI_BB_V3_BEAR_OVERSOLD", 25.0),
            "trend_sma_period": _env_int("TREND_SMA_PERIOD", 50),
            "bear_dip_rsi": _env_int("BEAR_DIP_RSI", 30),
            "bb_tolerance": _env_float("RSI_BB_V3_BB_TOLERANCE", _env_float("BB_TOLERANCE", 0.003)),
            "smi_fast": _env_int("SMI_FAST", 10),
            "smi_slow": _env_int("SMI_SLOW", 3),
            "smi_sig": _env_int("SMI_SIG", 3),
            "near_band_pct": _env_float("NEAR_BAND_PCT", 0.001),
            "fee_per_trade": _env_float("FEE_PER_TRADE", 2.5),
            "estimated_trade_notional": _env_float("FEE_AWARE_ESTIMATED_TRADE_NOTIONAL", 1000.0),
            "min_reward_pct": _env_float("FEE_AWARE_MIN_REWARD_PCT", 0.004),
            "fee_reward_multiple": _env_float("FEE_AWARE_REWARD_FEE_MULTIPLE", 2.0),
            "min_bb_width_pct": _env_float("FEE_AWARE_MIN_BB_WIDTH_PCT", 0.006),
            "require_confirmation": _env_bool("FEE_AWARE_REQUIRE_CONFIRMATION", True),
            "exit_rsi": _env_float("FEE_AWARE_EXIT_RSI", 55.0),
            "ext_hours_oversold": _env_int("EXT_HOURS_OVERSOLD", 20),
            "ext_hours_bb_std": _env_float("EXT_HOURS_BB_STD", 3.0),
            "ext_hours_bb_tolerance": _env_optional_float("EXT_HOURS_BB_TOLERANCE"),
            "ext_hours_volume_filter_enabled": _env_bool("EXT_HOURS_VOLUME_FILTER_ENABLED", False),
            "ext_hours_volume_lookback": _env_int("EXT_HOURS_VOLUME_LOOKBACK", 50),
            "ext_hours_volume_multiplier": _env_float("EXT_HOURS_VOLUME_MULTIPLIER", 2.0),
            "ext_hours_buy_confirmation_bars": _env_int("EXT_HOURS_BUY_CONFIRMATION_BARS", 4),
            "ext_hours_sell_confirmation_bars": _env_int("EXT_HOURS_SELL_CONFIRMATION_BARS", 4),
            "ema_fast_period": _env_int("FEE_AWARE_V3_EMA_FAST_PERIOD", 9),
            "atr_period": _env_int("FEE_AWARE_V3_ATR_PERIOD", 14),
            "atr_baseline_period": _env_int("FEE_AWARE_V3_ATR_BASELINE_PERIOD", 50),
            "volume_lookback": _env_int("FEE_AWARE_V3_VOLUME_LOOKBACK", 20),
            "volume_multiplier": _env_float("FEE_AWARE_V3_VOLUME_MULTIPLIER", 1.25),
            "profit_rsi": _env_float("FEE_AWARE_V3_PROFIT_RSI", 70.0),
            "dynamic_bb_min_std": _env_float("FEE_AWARE_V3_DYNAMIC_BB_MIN_STD", 2.1),
            "dynamic_bb_max_std": _env_float("FEE_AWARE_V3_DYNAMIC_BB_MAX_STD", 2.3),
            "volume_spike_window_bars": _env_int("FEE_AWARE_V4_VOLUME_SPIKE_WINDOW_BARS", 3),
            "max_stagnation_bars": _env_int("FEE_AWARE_V4_MAX_STAGNATION_BARS", 24),
            "stagnation_min_pnl_pct": _env_float("FEE_AWARE_V4_STAGNATION_MIN_PNL_PCT", 0.0),
            "scale_in_enabled": _env_bool("FEE_AWARE_V4_SCALE_IN_ENABLED", True),
            "scale_in_initial_fraction": _env_float("FEE_AWARE_V4_SCALE_IN_INITIAL_FRACTION", 0.4),
            "scale_in_fraction": _env_float("FEE_AWARE_V4_SCALE_IN_FRACTION", 0.6),
            "scale_in_drop_pct": _env_float("FEE_AWARE_V4_SCALE_IN_DROP_PCT", 0.0125),
        }
        self.live_strategy = create_strategy(strategy_name, strategy_config)

        self.live_position = "NONE"

        self.live_symbol = os.getenv("DEFAULT_SYMBOL", "SPY")
        self.trading_mode = os.getenv("TRADING_MODE", "PAPER").upper()
        self.live_interval = os.getenv("LIVE_BAR_INTERVAL", os.getenv("YF_INTERVAL", "5m"))
        self.live_interval_minutes = interval_to_minutes(self.live_interval, default=5)
        self.broker_bar_size = _ib_bar_size_from_interval(self.live_interval)
        self.session_min_completed_bars = max(0, _env_int("SESSION_MIN_COMPLETED_BARS", 0))
        self.premarket_min_completed_bars = max(0, _env_int("PREMARKET_MIN_COMPLETED_BARS", 3))
        self.live_startup_min_bars = max(
            1,
            _env_int("LIVE_STARTUP_MIN_BARS", 6),
            self.session_min_completed_bars,
            self.premarket_min_completed_bars,
        )

        self.market_data_primary = os.getenv("MARKET_DATA_PRIMARY", "yfinance").strip().lower()
        self.fallback_enabled = _env_bool("MARKET_DATA_FALLBACK_ENABLED", True)
        self.stale_after_seconds = _env_int("DATA_STALE_AFTER_SECONDS", 900)
        self.auto_pause_live_on_stale = _env_bool("AUTO_PAUSE_LIVE_ON_STALE", True)
        self.near_band_pct = _env_float("NEAR_BAND_PCT", 0.003)
        self.near_smi_gap = _env_float("NEAR_SMI_GAP", 4.0)
        self.broker_req_id = _env_int("BROKER_FALLBACK_REQ_ID", 91)
        self.extended_hours = _env_bool("EXTENDED_HOURS_TRADING", False)
        self.trading_hours_enabled = _env_bool("TRADING_HOURS_ENABLED", True)
        self.trading_hours = TradingHours.from_config(
            sessions_raw=os.getenv("TRADING_SESSIONS"),
            timezone_name=os.getenv("TRADING_HOURS_TIMEZONE", "America/New_York"),
            enabled=self.trading_hours_enabled,
        )
        self.broker_poll_seconds = _env_int("BROKER_POLL_SECONDS", 60)
        self.broker_history_min_days = max(1, _env_int("BROKER_HISTORY_MIN_DAYS", 2))
        self.broker_history_max_days = max(
            self.broker_history_min_days,
            _env_int("BROKER_HISTORY_MAX_DAYS", 45),
        )
        self.live_trades_lookback_days = max(1, min(_env_int("LIVE_TRADES_LOOKBACK_DAYS", 3), 7))
        self.live_trades_poll_seconds = _env_int("LIVE_TRADES_POLL_SECONDS", 30)
        self.live_orders_poll_seconds = _env_int("LIVE_ORDERS_POLL_SECONDS", 5)
        self.broker_backfill_complete = False
        self._backfill_bars_applied = 0
        self._broker_history_received_bars = 0
        self._last_broker_poll_time = 0.0
        self._last_order_poll_time = 0.0
        self._last_trades_poll_time = 0.0
        self.signal_retry_ttl_seconds = _env_int("SIGNAL_RETRY_TTL_SECONDS", 300)
        self._pending_signal = None
        self._indicators_warmed_up = False
        self._last_warmup_log_key = None
        self._last_warmup_log_time = 0.0
        self._warmup_log_interval_seconds = 300.0
        self._backfill_phase = True
        self._trade_ready_time = None
        self.ext_hours_reset_indicators_on_session_change = _env_bool(
            "EXT_HOURS_RESET_INDICATORS_ON_SESSION_CHANGE",
            True,
        )
        self._active_strategy_session = None
        self.yf_provider = None

        self.feed_health = FeedHealthMonitor(
            primary_source=self.market_data_primary,
            fallback_source="broker",
            stale_after_seconds=self.stale_after_seconds,
            auto_pause_live_on_stale=self.auto_pause_live_on_stale,
        )

        dash.bot_state["is_live_active"] = _env_bool("IS_LIVE_ACTIVE", False)
        dash.bot_state["trading_mode"] = self.trading_mode
        dash.bot_state["symbol"] = self.live_symbol
        dash.bot_state["strategy"] = strategy_name
        dash.bot_state["extended_hours"] = self.extended_hours
        dash.bot_state["trading_hours_enabled"] = self.trading_hours_enabled
        dash.bot_state["trading_hours_timezone"] = self.trading_hours.timezone_name
        dash.bot_state["premarket_min_completed_bars"] = self.premarket_min_completed_bars
        dash.bot_state["live_startup_min_bars"] = self.live_startup_min_bars
        dash.bot_state["startup_ready"] = False
        dash.bot_state["indicators_ready"] = False
        dash.bot_state["indicators_fully_ready"] = False
        dash.bot_state["indicator_bars_loaded"] = 0
        dash.bot_state["indicator_min_bars_required"] = getattr(self.live_strategy, "min_bars_required", 50)
        dash.bot_state["market_session"] = "unknown"
        dash.bot_state["market_session_open"] = False
        dash.bot_state["market_session_reason"] = "initializing"
        dash.bot_state["market_session_now_et"] = None
        dash.bot_state["next_session_open"] = None
        dash.bot_state["next_session_close"] = None
        dash.bot_state["minutes_to_open"] = None
        dash.bot_state["data_primary_source"] = self.market_data_primary
        dash.bot_state["data_active_source"] = self.market_data_primary
        dash.bot_state["data_fallback_source"] = "broker"
        dash.bot_state["data_primary_lag_seconds"] = None
        dash.bot_state["data_fallback_lag_seconds"] = None
        dash.bot_state["data_primary_is_stale"] = False
        dash.bot_state["data_primary_last_bar_ts"] = None
        dash.bot_state["data_fallback_last_bar_ts"] = None
        dash.bot_state["data_last_error"] = ""
        dash.bot_state["live_pause_guard"] = False
        dash.bot_state["effective_live_active"] = dash.bot_state["is_live_active"]
        dash.bot_state["last_price"] = None
        dash.bot_state["last_price_time"] = None
        dash.bot_state["last_price_source"] = None
        dash.bot_state["last_signal"] = "HOLD"
        dash.bot_state["last_signal_time"] = None
        dash.bot_state["near_buy"] = False
        dash.bot_state["near_sell"] = False
        dash.bot_state["broker_connected"] = False
        dash.bot_state["pause_reasons"] = []
        dash.bot_state["web_api_enabled"] = self.web_api_enabled
        dash.bot_state["web_api_url"] = self.web_api_base_url or ""
        dash.bot_state["broker_account_id"] = ""
        dash.bot_state["last_tickle_ok"] = None
        dash.bot_state["consecutive_tickle_failures"] = 0

        self.trade_quantity = max(1, _env_int("LIVE_DEFAULT_QUANTITY", _env_int("DEFAULT_QUANTITY", 100)))
        self.live_order_sizing_mode = os.getenv("LIVE_ORDER_SIZING_MODE", "slot_percent").strip().lower()
        if self.live_order_sizing_mode not in {"slot_percent", "fixed_quantity", "allocation_percent"}:
            logger.warning(
                f"Invalid LIVE_ORDER_SIZING_MODE='{self.live_order_sizing_mode}'. Falling back to slot_percent."
            )
            self.live_order_sizing_mode = "slot_percent"
        self.live_slot_allocation_pct = max(0.0, _env_float("LIVE_SLOT_ALLOCATION_PCT", 0.25))
        self.live_max_position_slots = max(1, _env_int("LIVE_MAX_POSITION_SLOTS", 4))
        self.live_allocation_pct = max(0.0, _env_float("LIVE_BUY_ALLOCATION_PCT", 0.25))
        self.live_buy_cash_source = os.getenv("LIVE_BUY_CASH_SOURCE", "settled_cash").strip().lower()
        if self.live_buy_cash_source not in {"settled_cash", "available_funds", "total_cash"}:
            logger.warning(
                f"Invalid LIVE_BUY_CASH_SOURCE='{self.live_buy_cash_source}'. Falling back to settled_cash."
            )
            self.live_buy_cash_source = "settled_cash"
        self.live_buy_cash_fallbacks = [
            source
            for source in _env_list("LIVE_BUY_CASH_FALLBACKS")
            if source in {"settled_cash", "available_funds", "total_cash"} and source != self.live_buy_cash_source
        ]
        self.max_order_notional = _env_optional_float("MAX_ORDER_NOTIONAL")
        self.max_order_quantity = _env_optional_int("MAX_ORDER_QUANTITY")
        self.ext_hours_max_order_notional = _env_optional_float("EXT_HOURS_MAX_ORDER_NOTIONAL")
        self.max_buys_per_day = _env_optional_int("MAX_BUYS_PER_DAY")
        if self.max_buys_per_day is None:
            self.max_buys_per_day = 4
        self.max_orders_per_day = _env_optional_int("MAX_ORDERS_PER_DAY")
        if self.max_orders_per_day is None:
            self.max_orders_per_day = 8
        self.min_seconds_between_orders = max(0.0, _env_float("MIN_SECONDS_BETWEEN_ORDERS", 0.0))
        self.daily_loss_stop_pct = _env_optional_float("DAILY_LOSS_STOP_PCT")
        self.live_strategy_df_max_bars = max(
            getattr(self.live_strategy, "min_bars_required", 50) * 4,
            _env_int("LIVE_STRATEGY_DF_MAX_BARS", _env_int("LIVE_CANDLE_BUFFER_MAX", 500)),
            500,
        )
        self.broker_use_rth = self._broker_use_rth()
        history_plan = self._broker_history_request_plan()
        dash.bot_state["max_order_notional"] = self.max_order_notional
        dash.bot_state["max_order_quantity"] = self.max_order_quantity
        dash.bot_state["ext_hours_max_order_notional"] = self.ext_hours_max_order_notional
        dash.bot_state["live_order_sizing_mode"] = self.live_order_sizing_mode
        dash.bot_state["live_default_quantity"] = self.trade_quantity
        dash.bot_state["live_slot_allocation_pct"] = self.live_slot_allocation_pct
        dash.bot_state["live_max_position_slots"] = self.live_max_position_slots
        dash.bot_state["live_buy_cash_source"] = self.live_buy_cash_source
        dash.bot_state["live_buy_cash_fallbacks"] = self.live_buy_cash_fallbacks
        dash.bot_state["max_buys_per_day"] = self.max_buys_per_day
        dash.bot_state["max_orders_per_day"] = self.max_orders_per_day
        dash.bot_state["min_seconds_between_orders"] = self.min_seconds_between_orders
        dash.bot_state["daily_loss_stop_pct"] = self.daily_loss_stop_pct
        dash.bot_state["live_strategy_df_max_bars"] = self.live_strategy_df_max_bars
        dash.bot_state["broker_use_rth"] = self.broker_use_rth
        dash.bot_state["broker_history_duration"] = history_plan["duration"]
        dash.bot_state["broker_history_days"] = history_plan["days"]
        dash.bot_state["broker_history_min_days"] = self.broker_history_min_days
        dash.bot_state["broker_history_max_days"] = self.broker_history_max_days
        dash.bot_state["broker_history_required_bars"] = history_plan["required_bars"]
        dash.bot_state["broker_history_bars_per_day"] = history_plan["bars_per_day"]
        dash.bot_state["position_state"] = "NONE"
        dash.bot_state["live_symbol_position"] = 0.0
        dash.bot_state["live_position_slots"] = f"0/{self.live_max_position_slots}"
        dash.bot_state["live_symbol_exposure"] = 0.0
        dash.bot_state["live_buy_plan"] = None
        dash.bot_state["live_sell_plan"] = None
        dash.bot_state["unsettled_sale_proceeds"] = 0.0
        dash.bot_state["next_estimated_settlement"] = None
        dash.bot_state["next_release_at"] = None
        dash.bot_state["unsettled_sell_fills"] = []
        dash.bot_state["slot_outlook"] = {}
        dash.bot_state["account_data_ready"] = False
        dash.bot_state["positions_data_ready"] = False
        self.telegram_notifier = TelegramNotifier(
            TelegramConfig(
                enabled=_env_bool("TELEGRAM_ENABLED", False),
                bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
                chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
                parse_mode=os.getenv("TELEGRAM_PARSE_MODE", "").strip(),
                timeout_seconds=_env_float("TELEGRAM_TIMEOUT_SECONDS", 10.0),
                notify_order_sent=_env_bool("TELEGRAM_NOTIFY_ORDER_SENT", False),
                notify_order_filled=_env_bool("TELEGRAM_NOTIFY_ORDER_FILLED", True),
                notify_test_button=_env_bool("TELEGRAM_NOTIFY_TEST_BUTTON", True),
            )
        )
        dash.bot_state["telegram_enabled"] = self.telegram_notifier.config.enabled
        dash.bot_state["telegram_configured"] = self.telegram_notifier.is_configured
        dash.bot_state["telegram_chat_id"] = self.telegram_notifier.config.chat_id

        results_dir = os.path.join(os.path.dirname(__file__), "simulation", "results")
        live_state_path = os.getenv("LIVE_STATE_PATH") or os.path.join(results_dir, "live_state.json")
        dash.configure_live_state_store(live_state_path)
        dash.configure_broker_reauth_handler(self.request_broker_reauth)
        dash.configure_telegram_test_handler(self.send_telegram_status_report)
        sim_config_raw = os.getenv("SIM_CONFIG_PATH", "").strip()
        sim_config_path = get_simulation_config_path()
        if sim_config_raw:
            logger.info(f"Using explicit simulation config path: {sim_config_path}")
        else:
            logger.info(f"SIM_CONFIG_PATH is blank; using default simulation config: {sim_config_path}")
        try:
            sim_config_bundle = load_simulation_config()
        except SimulationConfigError as exc:
            logger.error(f"Simulation config error: {exc}")
            raise

        dash.bot_state["sim_config_path"] = sim_config_bundle["config_path"]

        self.sim_manager = SimManager(
            results_dir=results_dir,
            export_policy=sim_config_bundle["export_policy"],
        )
        self.sim_manager.initialize_simulations(
            configs=sim_config_bundle["simulations"],
            defaults=sim_config_bundle["defaults"],
        )

        if self.market_data_primary == "yfinance":
            self.yf_provider = YFinancePollingProvider(
                symbol=self.live_symbol,
                interval=os.getenv("YF_INTERVAL", "5m"),
                backfill_period=os.getenv("YF_BACKFILL_PERIOD", "5d"),
                lookback_period=os.getenv("YF_LOOKBACK_PERIOD", "2d"),
                poll_seconds=_env_int("YF_POLL_SECONDS", 20),
                max_backfill_bars=_env_int("YF_MAX_BACKFILL_BARS", 500),
                on_backfill_bar=self.on_primary_backfill_bar,
                on_backfill_complete=self.on_primary_backfill_complete,
                on_live_bar=self.on_primary_live_bar,
                on_status=self.on_primary_status,
                prepost=self.extended_hours,
            )
        elif self.market_data_primary == "broker":
            self.feed_health.set_active_source("broker")
        else:
            logger.warning(
                f"Unsupported MARKET_DATA_PRIMARY='{self.market_data_primary}'. Falling back to broker stream only."
            )
            self.market_data_primary = "broker"
            self.feed_health.primary_source = "broker"
            self.feed_health.set_active_source("broker")

        # Broker historical stream is used for fallback data and account visibility.
        self.client.register_callback("historicalData", self.on_broker_historical_data)
        self.client.register_callback("historicalDataEnd", self.on_broker_historical_data_end)
        self.client.register_callback("historicalDataUpdate", self.on_broker_historical_data_update)

        self.client.register_callback("position", self._on_live_position_update)
        self.client.register_callback("positionEnd", self._on_live_position_end)
        self.client.register_callback("updateAccountValue", self._on_live_account_update)

        self._sync_dashboard_status()

    def _on_broker_connection_lost(self):
        logger.warning("Broker connection lost -- live trading will be paused until session recovers")
        dash.bot_state["broker_connected"] = False
        dash.append_live_log(
            level="ERROR",
            event_type="broker",
            message="Broker session lost. Waiting for reconnection...",
            payload={},
        )

    def _on_broker_connection_restored(self):
        logger.info("Broker connection restored -- re-subscribing to data")
        dash.bot_state["broker_connected"] = True
        dash.append_live_log(
            level="SUCCESS",
            event_type="broker",
            message="Broker session restored",
            payload={},
        )
        self._resubscribe_broker_data()

    def _broker_history_bars_per_day(self, use_rth: int) -> int:
        if use_rth:
            return max(1, int(390 // max(1, self.live_interval_minutes)))

        if getattr(self, "trading_hours_enabled", True):
            session_minutes = 0
            for session in getattr(self.trading_hours, "sessions", []):
                if session.crosses_midnight:
                    session_minutes += (24 * 60 - session.start_minute) + session.end_minute
                else:
                    session_minutes += max(0, session.end_minute - session.start_minute)
            if session_minutes > 0:
                return max(1, int(session_minutes // max(1, self.live_interval_minutes)))

        return max(1, int((24 * 60) // max(1, self.live_interval_minutes)))

    def _broker_history_request_plan(self) -> dict:
        use_rth = self._broker_use_rth()
        bars_per_day = self._broker_history_bars_per_day(use_rth)
        full_required = max(1, int(getattr(self.live_strategy, "min_bars_required", 50)))
        startup_required = max(1, int(getattr(self, "live_startup_min_bars", 6)))
        startup_buffer = max(6, startup_required)
        required_bars = max(full_required, startup_required + startup_buffer)
        raw_days = math.ceil(required_bars / bars_per_day) + 2
        days = max(self.broker_history_min_days, min(raw_days, self.broker_history_max_days))
        return {
            "duration": f"{days} D",
            "days": days,
            "raw_days": raw_days,
            "required_bars": required_bars,
            "full_required_bars": full_required,
            "startup_required_bars": startup_required,
            "bars_per_day": bars_per_day,
            "use_rth": use_rth,
            "capped": days != raw_days,
        }

    def _request_broker_history(self, keep_up_to_date: bool, reason: str):
        contract = self.client.get_contract(self.live_symbol)
        history_plan = self._broker_history_request_plan()
        duration = history_plan["duration"]
        self._broker_history_received_bars = 0
        dash.bot_state["broker_history_duration"] = duration
        dash.bot_state["broker_history_days"] = history_plan["days"]
        dash.bot_state["broker_history_required_bars"] = history_plan["required_bars"]
        dash.bot_state["broker_history_bars_per_day"] = history_plan["bars_per_day"]

        logger.info(
            f"Requesting broker history: {self.live_symbol}, {duration}/{self.broker_bar_size}, "
            f"useRTH={history_plan['use_rth']} ({self._broker_history_label()}), "
            f"required_bars={history_plan['required_bars']}, keepUpToDate={keep_up_to_date}"
        )
        dash.append_live_log(
            level="INFO",
            event_type="provider",
            message=(
                f"Requesting broker historical data: {self.live_symbol} "
                f"{duration}/{self.broker_bar_size} ({self._broker_history_label()})"
            ),
            payload={
                "useRTH": history_plan["use_rth"],
                "keepUpToDate": keep_up_to_date,
                "bar_size": self.broker_bar_size,
                "duration": duration,
                "required_bars": history_plan["required_bars"],
                "bars_per_day": history_plan["bars_per_day"],
                "raw_days": history_plan["raw_days"],
                "capped": history_plan["capped"],
                "reason": reason,
            },
        )
        self.client.reqHistoricalData(
            reqId=self.broker_req_id,
            contract=contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=self.broker_bar_size,
            whatToShow="TRADES",
            useRTH=history_plan["use_rth"],
            formatDate=1,
            keepUpToDate=keep_up_to_date,
            chartOptions=[],
        )

    def _resubscribe_broker_data(self):
        """Re-request historical data and positions after reconnection."""
        try:
            self._request_broker_history(keep_up_to_date=not self.web_api_enabled, reason="reconnect")
            logger.info(
                f"Re-subscribed to broker historical data after reconnection "
                f"({self.broker_bar_size}, {self._broker_history_label()})"
            )
        except Exception as exc:
            logger.error(f"Failed to re-subscribe broker data after reconnection: {exc}")

    def _on_live_position_update(self, account: str, contract, position: float, avg_cost: float):
        dash.update_live_position(account, contract, position, avg_cost)
        self._refresh_live_position_state()

    def _on_live_position_end(self):
        dash.bot_state["positions_data_ready"] = True
        self._refresh_live_position_state()

    def _on_live_account_update(self, key: str, val: str, currency: str, account_name: str):
        dash.update_live_account(key, val, currency, account_name)
        if key in {"NetLiquidation", "AvailableFunds", "TotalCashValue", "BuyingPower", "GrossPositionValue", "SettledCash"}:
            self._update_account_data_ready()

    def _update_market_session_status(self, log_changes: bool = False):
        status = self.trading_hours.status()
        dash.bot_state["trading_hours_enabled"] = status.enabled
        dash.bot_state["market_session"] = status.session_name
        dash.bot_state["market_session_open"] = status.is_open
        dash.bot_state["market_session_reason"] = status.reason
        dash.bot_state["market_session_now_et"] = status.now_et
        dash.bot_state["next_session_open"] = status.next_open
        dash.bot_state["next_session_close"] = status.next_close
        dash.bot_state["minutes_to_open"] = status.minutes_to_open

        changed = (
            self._last_market_session_open is None
            or self._last_market_session_open != status.is_open
            or self._last_market_session_name != status.session_name
        )
        if log_changes and changed:
            if status.is_open:
                dash.append_live_log(
                    level="SUCCESS",
                    event_type="trading_hours",
                    message=f"Trading session open: {status.session_name}",
                    payload=status.as_dict(),
                )
            else:
                dash.append_live_log(
                    level="WARN",
                    event_type="trading_hours",
                    message="Trading session closed; live bar processing and orders are paused",
                    payload=status.as_dict(),
                )
        self._last_market_session_open = status.is_open
        self._last_market_session_name = status.session_name
        return status

    def _sync_dashboard_status(self):
        snapshot = self.feed_health.snapshot()
        market_status = self._update_market_session_status()
        dash.bot_state["data_primary_source"] = snapshot.primary_source
        dash.bot_state["data_active_source"] = snapshot.active_source
        dash.bot_state["data_fallback_source"] = snapshot.fallback_source
        dash.bot_state["data_primary_lag_seconds"] = snapshot.primary_lag_seconds
        dash.bot_state["data_fallback_lag_seconds"] = snapshot.fallback_lag_seconds
        dash.bot_state["data_active_lag_seconds"] = snapshot.active_lag_seconds
        dash.bot_state["data_primary_is_stale"] = snapshot.primary_is_stale
        dash.bot_state["data_active_is_stale"] = snapshot.active_is_stale
        dash.bot_state["data_primary_last_bar_ts"] = snapshot.primary_last_bar_ts
        dash.bot_state["data_fallback_last_bar_ts"] = snapshot.fallback_last_bar_ts
        dash.bot_state["data_last_error"] = snapshot.last_primary_error
        dash.bot_state["live_pause_guard"] = snapshot.should_pause_live
        dash.bot_state["broker_connected"] = self.client.is_connected
        if self.web_api_enabled and hasattr(self.client, '_consecutive_tickle_failures'):
            dash.bot_state["consecutive_tickle_failures"] = self.client._consecutive_tickle_failures
            dash.bot_state["broker_account_id"] = self.client.account_id or ""
            dash.bot_state["last_tickle_ok"] = self.client._consecutive_tickle_failures == 0
            dash.bot_state["last_tickle_error"] = getattr(self.client, '_last_tickle_error', None)
            dash.bot_state["last_successful_tickle_ts"] = _epoch_to_iso(
                getattr(self.client, '_last_successful_tickle', 0.0)
            )
            dash.bot_state["gateway_authenticated"] = self.client.is_connected
        broker_down = self.web_api_enabled and not self.client.is_connected
        is_manual_on = bool(dash.bot_state.get("is_live_active", False))
        dash.bot_state["effective_live_active"] = (
            is_manual_on and market_status.is_open and not snapshot.should_pause_live and not broker_down
        )

        reasons = []
        if not is_manual_on:
            reasons.append("Trading is manually turned OFF")
        if not market_status.is_open:
            if market_status.minutes_to_open is None:
                reasons.append("Outside configured trading hours")
            else:
                reasons.append(f"Outside configured trading hours (next open in {market_status.minutes_to_open}m)")
        if snapshot.should_pause_live:
            lag = snapshot.active_lag_seconds
            lag_str = f"{int(lag)}s" if lag is not None else "unknown"
            reasons.append(f"Active feed ({snapshot.active_source}) stale ({lag_str} lag, threshold {self.feed_health.stale_after_seconds}s)")
        if broker_down:
            reasons.append("Broker disconnected")
        if not dash.bot_state.get("startup_ready", False):
            min_bars = dash.bot_state.get("live_startup_min_bars", self.live_startup_min_bars)
            loaded = dash.bot_state.get("indicator_bars_loaded", 0)
            reasons.append(f"Startup warm-up not complete ({loaded}/{min_bars} bars)")
        if not dash.bot_state.get("account_data_ready", False):
            reasons.append("Broker account data not ready")
        if not dash.bot_state.get("positions_data_ready", False):
            reasons.append("Broker position data not ready")
        dash.bot_state["pause_reasons"] = reasons

        dash.bot_state["pending_signal"] = self._pending_signal
        counters = self._risk_counter()
        dash.bot_state["daily_buy_orders"] = counters.get("buy_orders", 0)
        dash.bot_state["daily_total_orders"] = counters.get("total_orders", 0)
        startup_ok = dash.bot_state.get("startup_ready", False)
        account_ok = dash.bot_state.get("account_data_ready", False)
        positions_ok = dash.bot_state.get("positions_data_ready", False)
        feed_ok = not snapshot.should_pause_live
        broker_ok = not broker_down
        dash.bot_state["ready_to_trade"] = (
            is_manual_on and startup_ok and account_ok and positions_ok and feed_ok and broker_ok and market_status.is_open
        )

    def request_broker_reauth(self) -> dict:
        """Manual dashboard action to ask the IBKR gateway to authenticate again."""
        if not self.web_api_enabled or not hasattr(self.client, "request_reauthentication"):
            result = {
                "ok": False,
                "message": "Manual broker authentication is only available for the IBKR Web API gateway.",
            }
            dash.bot_state["last_broker_reauth_result"] = result
            return result

        if not self._broker_reauth_lock.acquire(blocking=False):
            return {
                "ok": False,
                "message": "Broker authentication is already in progress.",
            }

        dash.bot_state["broker_reauth_in_progress"] = True
        dash.append_live_log(
            level="INFO",
            event_type="broker",
            message="Manual broker authentication requested from dashboard",
            payload={"web_api_url": self.web_api_base_url},
        )
        try:
            result = self.client.request_reauthentication()
            dash.bot_state["last_broker_reauth_result"] = result
            dash.bot_state["broker_connected"] = self.client.is_connected
            if result.get("ok"):
                dash.bot_state["broker_account_id"] = self.client.account_id or ""
                dash.append_live_log(
                    level="SUCCESS",
                    event_type="broker",
                    message=result.get("message", "Broker authentication succeeded"),
                    payload={"account_id": self.client.account_id},
                )
                self._resubscribe_broker_data()
                self._poll_account_data()
            else:
                dash.append_live_log(
                    level="ERROR",
                    event_type="broker",
                    message=result.get("message", "Broker authentication failed"),
                    payload=result,
                )
            self._sync_dashboard_status()
            return result
        finally:
            dash.bot_state["broker_reauth_in_progress"] = False
            self._broker_reauth_lock.release()

    @staticmethod
    def _first_present(mapping: dict, keys: tuple):
        if not isinstance(mapping, dict):
            return None
        for key in keys:
            value = mapping.get(key)
            if value not in (None, ""):
                return value
        return None

    def _extract_fill_price(self, order_info: dict, tracked: dict):
        value = self._first_present(
            order_info,
            ("avgPrice", "avg_price", "price", "filledPrice", "fill_price", "lastExecutionPrice", "last_price"),
        )
        parsed = self._safe_float(value)
        if parsed is not None and parsed > 0:
            return parsed
        parsed = self._safe_float(tracked.get("price"))
        return parsed if parsed is not None else 0.0

    def _extract_fill_quantity(self, order_info: dict, tracked: dict):
        value = self._first_present(order_info, ("filledQuantity", "filled_quantity", "quantity", "qty", "size"))
        parsed = self._safe_float(value)
        if parsed is not None and parsed > 0:
            return parsed
        parsed = self._safe_float(tracked.get("qty"))
        return parsed if parsed is not None else 0.0

    def _trade_fees_for_order(self, order_id: str) -> float:
        total = 0.0
        for trade in list(dash.live_trades_data):
            if not isinstance(trade, dict):
                continue
            trade_order_id = self._first_present(trade, ("orderId", "order_id", "ib_order_id"))
            if trade_order_id is None or str(trade_order_id) != str(order_id):
                continue
            raw_fee = self._first_present(trade, ("commission", "commissions", "fee", "fees"))
            parsed = self._safe_float(raw_fee)
            if parsed is not None:
                total += abs(parsed)
        return total

    def _trade_matches_order(self, trade: dict, order_id: str) -> bool:
        trade_order_id = self._first_present(
            trade,
            (
                "orderId",
                "order_id",
                "ib_order_id",
                "orderID",
                "order_ref",
                "orderRef",
            ),
        )
        return trade_order_id is not None and str(trade_order_id) == str(order_id)

    def _recent_trade_order_info(self, trade: dict, tracked: dict) -> dict:
        order_info = dict(trade)
        order_info.setdefault("status", "filled")
        if self._first_present(order_info, ("avgPrice", "avg_price", "price")) is None:
            fill_price = self._first_present(order_info, ("executionPrice", "exec_price", "fill_price", "last_price"))
            if fill_price is not None:
                order_info["price"] = fill_price
        if self._first_present(order_info, ("filledQuantity", "filled_quantity", "quantity", "qty", "size")) is None:
            fill_qty = self._first_present(order_info, ("shares", "filled", "cumQty", "cum_qty"))
            if fill_qty is not None:
                order_info["quantity"] = fill_qty
        order_info.setdefault("symbol", tracked.get("symbol", self.live_symbol))
        return order_info

    def _reconcile_recent_trade_fills(self, trades: list):
        if not hasattr(self, "_tracked_orders") or not self._tracked_orders:
            return
        if not isinstance(trades, list):
            return

        resolved = []
        for oid, tracked in list(self._tracked_orders.items()):
            match = next(
                (
                    trade
                    for trade in trades
                    if isinstance(trade, dict) and self._trade_matches_order(trade, oid)
                ),
                None,
            )
            if match is None:
                continue

            order_info = self._recent_trade_order_info(match, tracked)
            fill_price = self._extract_fill_price(order_info, tracked)
            logger.info(f"Order {oid} FILLED from recent trades (price={fill_price})")
            dash.append_live_log(
                level="SUCCESS",
                event_type="order_filled",
                message=f"Order {oid} FILLED: {tracked['action']} {tracked['qty']} {tracked['symbol']} @ {fill_price}",
                payload={"order_id": oid, "action": tracked["action"], "fill_price": fill_price, "source": "recent_trades"},
            )
            self._notify_order_filled(oid, tracked, order_info)
            self._record_order_filled(oid, tracked, order_info)
            resolved.append(oid)

        for oid in resolved:
            self._tracked_orders.pop(oid, None)

    def _build_telegram_trade_payload(self, order_id: str, tracked: dict, order_info: dict) -> dict:
        action = str(tracked.get("action", "")).upper()
        price = self._extract_fill_price(order_info, tracked)
        quantity = self._extract_fill_quantity(order_info, tracked)
        total = price * quantity
        fees = self._trade_fees_for_order(order_id)
        metrics = self._extract_live_metrics()
        payload = {
            "symbol": tracked.get("symbol", self.live_symbol),
            "action": action,
            "price": price,
            "quantity": quantity,
            "total": total,
            "rsi": metrics.get("rsi"),
            "order_id": order_id,
            "status": self._first_present(order_info, ("status", "order_status")) or "FILLED",
            "fees": fees,
        }

        symbol = payload["symbol"]
        if action == "BUY" and price > 0 and quantity > 0:
            self._telegram_position_basis[symbol] = {
                "price": price,
                "quantity": quantity,
                "total": total,
                "fees": fees,
            }
            self.telegram_summary["buy_count"] += 1
            self.telegram_summary["fees"] += fees
        elif action == "SELL":
            basis = self._telegram_position_basis.get(symbol, {})
            basis_price = self._safe_float(basis.get("price"))
            basis_qty = self._safe_float(basis.get("quantity"))
            matched_qty = min(quantity, basis_qty) if basis_qty else quantity
            gross = (price - basis_price) * matched_qty if basis_price is not None else 0.0
            basis_fees = self._safe_float(basis.get("fees")) or 0.0
            total_fees = basis_fees + fees
            net = gross - total_fees
            basis_total = (basis_price or 0.0) * matched_qty
            net_pct = (net / basis_total * 100.0) if basis_total else 0.0
            payload.update(
                {
                    "gross_pnl": gross,
                    "fees": total_fees,
                    "net_pnl": net,
                    "net_pnl_pct": net_pct,
                }
            )
            self.telegram_summary["sell_count"] += 1
            self.telegram_summary["fees"] += fees
            self.telegram_summary["net_pnl"] += net
            self._telegram_position_basis.pop(symbol, None)

        return payload

    def _notify_order_filled(self, order_id: str, tracked: dict, order_info: dict):
        if not self.telegram_notifier.config.notify_order_filled:
            return
        if order_id in self._telegram_notified_orders:
            return

        payload = self._build_telegram_trade_payload(order_id, tracked, order_info)
        action = payload.get("action")
        if action == "BUY":
            message = format_buy_executed(payload)
        elif action == "SELL":
            message = format_sell_executed(payload)
        else:
            return

        self._telegram_notified_orders.add(order_id)
        result = self.telegram_notifier.send_message_async(message)
        dash.bot_state["last_telegram_result"] = result
        dash.append_live_log(
            level="INFO" if result.get("ok") else "WARN",
            event_type="telegram",
            message=f"Telegram {action} fill notification: {result.get('message')}",
            payload={"order_id": order_id, "ok": result.get("ok")},
        )

    def _build_telegram_status_payload(self) -> dict:
        metrics = self._extract_live_metrics()
        return {
            "uptime": _format_duration(time.time() - self.started_at),
            "symbol": self.live_symbol,
            "mode": "REAL" if self.trading_mode == "LIVE" else self.trading_mode,
            "last_price": dash.bot_state.get("last_price"),
            "rsi": metrics.get("rsi"),
            "position": self.live_position,
            "buy_count": self.telegram_summary.get("buy_count", 0),
            "sell_count": self.telegram_summary.get("sell_count", 0),
            "fees": self.telegram_summary.get("fees", 0.0),
            "net_pnl": self.telegram_summary.get("net_pnl", 0.0),
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        }

    def send_telegram_status_report(self) -> dict:
        if not self.telegram_notifier.config.notify_test_button:
            result = {"ok": False, "message": "Telegram test button notifications are disabled."}
        else:
            message = format_status_report(self._build_telegram_status_payload())
            result = self.telegram_notifier.send_message(message)

        dash.bot_state["last_telegram_result"] = result
        dash.append_live_log(
            level="SUCCESS" if result.get("ok") else "WARN",
            event_type="telegram",
            message=f"Telegram status test: {result.get('message')}",
            payload={"ok": result.get("ok"), "status_code": result.get("status_code")},
        )
        return result

    def _switch_active_source(self, source: str, reason: str):
        current = self.feed_health.active_source
        if source == current:
            return

        self.feed_health.set_active_source(source)
        logger.warning(f"Market data source switch: {current} -> {source}. Reason: {reason}")
        dash.append_live_log(
            level="WARN",
            event_type="source_switch",
            message=f"Market data source switched {current} -> {source}",
            payload={"from": current, "to": source, "reason": reason},
        )

    def _evaluate_data_health(self):
        snapshot = self.feed_health.snapshot()
        self._update_market_session_status(log_changes=True)

        if self.fallback_enabled:
            if snapshot.active_source == self.market_data_primary and snapshot.primary_is_stale and snapshot.fallback_available:
                self._switch_active_source("broker", "primary data stale")
                snapshot = self.feed_health.snapshot()
            elif snapshot.active_source == "broker" and self.market_data_primary != "broker" and not snapshot.primary_is_stale:
                self._switch_active_source(self.market_data_primary, "primary data recovered")
                snapshot = self.feed_health.snapshot()

        if snapshot.should_pause_live != self._last_guard_state:
            if snapshot.should_pause_live:
                logger.warning("Live trading paused by stale-data guard")
                dash.append_live_log(
                    level="WARN",
                    event_type="guard",
                    message="Live trading paused by stale-data guard",
                    payload={"active_source": snapshot.active_source, "primary_lag_seconds": snapshot.primary_lag_seconds},
                )
            else:
                logger.info("Live trading stale-data guard cleared")
                dash.append_live_log(
                    level="SUCCESS",
                    event_type="guard",
                    message="Live trading stale-data guard cleared",
                    payload={"active_source": snapshot.active_source, "primary_lag_seconds": snapshot.primary_lag_seconds},
                )
            self._last_guard_state = snapshot.should_pause_live

        self._sync_dashboard_status()

    def _poll_broker_data(self):
        """Re-fetch broker historical data and live snapshot (Web API has no streaming)."""
        if not self.web_api_enabled or not self.client.is_connected:
            return

        now = time.time()
        if now - self._last_broker_poll_time < self.broker_poll_seconds:
            return

        self._last_broker_poll_time = now

        try:
            self._request_broker_history(keep_up_to_date=False, reason="poll")
        except Exception as exc:
            logger.warning(f"Broker data poll failed: {exc}")

        self._poll_broker_snapshot()

    def _poll_broker_snapshot(self):
        """Fetch a live quote snapshot to keep the feed alive during sparse-bar periods."""
        try:
            snap = self.client.get_snapshot(self.live_symbol)
            if not snap:
                return

            last_price = snap.get("31")
            if last_price is not None:
                try:
                    price_val = float(last_price)
                except (TypeError, ValueError):
                    return

                now_utc = datetime.now(timezone.utc)
                if self.market_data_primary == "broker":
                    self.feed_health.on_primary_bar(now_utc)
                else:
                    self.feed_health.on_fallback_bar(now_utc)

                dash.bot_state["last_price"] = price_val
                dash.bot_state["last_price_time"] = now_utc.strftime("%H:%M:%S")
                dash.bot_state["last_price_source"] = "broker_snapshot"
                logger.debug(f"Broker snapshot: {self.live_symbol} last={price_val}")
        except Exception as exc:
            logger.debug(f"Broker snapshot poll failed: {exc}")

    def _poll_account_data(self):
        """Fetch account summary and positions via the Web API."""
        if not self.web_api_enabled or not self.client.is_connected:
            return

        now = time.time()
        if now - getattr(self, '_last_account_poll_time', 0) < 30:
            return
        self._last_account_poll_time = now

        try:
            account_id = self.client.account_id
            if not account_id:
                accounts = self.client.get_accounts()
                if accounts:
                    account_id = accounts[0]
                    self.client.account_id = account_id
                    dash.bot_state["broker_account_id"] = account_id
                else:
                    return

            ledger = self.client.get_account_summary(account_id)
            if ledger:
                base_currency = ledger.get("BASE", ledger.get("USD", {}))
                if isinstance(base_currency, dict):
                    with dash._live_buffers_lock:
                        dash.live_account_data["Ledger"] = ledger
                    mapping = {
                        "netliquidationvalue": "NetLiquidation",
                        "availablefunds": "AvailableFunds",
                        "totalcashvalue": "TotalCashValue",
                        "buyingpower": "BuyingPower",
                        "grosspositionvalue": "GrossPositionValue",
                        "settledcash": "SettledCash",
                    }
                    for api_key, display_key in mapping.items():
                        val = base_currency.get(api_key)
                        if val is not None:
                            dash.update_live_account(
                                display_key, str(val), base_currency.get("currency", "USD"), account_id
                            )
                    self._update_account_data_ready()

            if hasattr(self.client, "get_portfolio_summary"):
                summary = self.client.get_portfolio_summary(account_id)
                if summary:
                    with dash._live_buffers_lock:
                        dash.live_account_data["PortfolioSummary"] = summary
                    dash.persist_live_state()
                    self._update_account_data_ready()

            positions = self.client.get_positions(account_id)
            with dash._live_buffers_lock:
                dash.live_positions_data.clear()
            for pos in positions:
                qty = pos.get("position", 0)
                if qty == 0:
                    continue

                class _MockContract:
                    pass

                c = _MockContract()
                c.symbol = self._position_symbol_from_api(pos)
                c.secType = pos.get("assetClass", "STK")
                avg_cost = pos.get("avgCost", 0)
                dash.update_live_position(account_id, c, qty, avg_cost)
                key = f"{account_id}_{c.symbol}_{c.secType}"
                with dash._live_buffers_lock:
                    stored = dash.live_positions_data.get(key)
                    if stored is not None:
                        for src_key, dst_key in (
                            ("mktPrice", "marketPrice"),
                            ("marketPrice", "marketPrice"),
                            ("mktValue", "marketValue"),
                            ("marketValue", "marketValue"),
                        ):
                            if src_key in pos and pos.get(src_key) is not None:
                                stored[dst_key] = pos.get(src_key)
                        stored["avgCost"] = round(float(avg_cost), 2) if avg_cost else stored.get("avgCost", 0.0)
            dash.bot_state["positions_data_ready"] = True
            self._refresh_live_position_state()
            dash.persist_live_state()
        except Exception as exc:
            logger.debug(f"Account data poll failed: {exc}")

    def _poll_order_status(self):
        """Check status of recently placed orders via the Web API."""
        if not self.web_api_enabled or not self.client.is_connected:
            return
        if not hasattr(self, '_tracked_orders'):
            self._tracked_orders = {}

        now = time.time()
        if now - self._last_order_poll_time < self.live_orders_poll_seconds:
            return
        self._last_order_poll_time = now

        try:
            live_orders = self.client.get_live_orders()
        except Exception:
            return
        dash.update_live_orders(live_orders)

        order_map = {}
        for o in live_orders:
            oid = o.get("orderId") or o.get("order_id")
            if oid is not None:
                order_map[str(oid)] = o

        resolved = []
        for oid, tracked in self._tracked_orders.items():
            order_info = order_map.get(oid)
            status_raw = self._first_present(order_info or {}, ("status", "order_status"))
            status = str(status_raw or "").lower()

            if status in ("filled",):
                fill_price = self._extract_fill_price(order_info or {}, tracked)
                logger.info(f"Order {oid} FILLED (price={fill_price})")
                dash.append_live_log(
                    level="SUCCESS",
                    event_type="order_filled",
                    message=f"Order {oid} FILLED: {tracked['action']} {tracked['qty']} {tracked['symbol']} @ {fill_price}",
                    payload={"order_id": oid, "action": tracked["action"], "fill_price": fill_price},
                )
                self._notify_order_filled(oid, tracked, order_info or {})
                self._record_order_filled(oid, tracked, order_info or {})
                resolved.append(oid)
            elif status in ("cancelled", "inactive"):
                logger.warning(f"Order {oid} {status.upper()}")
                dash.append_live_log(
                    level="ERROR",
                    event_type="order_cancelled",
                    message=f"Order {oid} {status.upper()}: {tracked['action']} {tracked['qty']} {tracked['symbol']}",
                    payload={"order_id": oid, "action": tracked["action"], "status": status},
                )
                if str(tracked.get("action", "")).upper() == "BUY":
                    self._remove_reserved_buy_lot(oid)
                dash.bot_state["last_order_status"] = status
                dash.bot_state["last_order_id"] = oid
                resolved.append(oid)
            elif time.time() - tracked["ts"] > 300:
                logger.warning(f"Order {oid} tracking expired (not found in live orders after 5min)")
                resolved.append(oid)

        for oid in resolved:
            self._tracked_orders.pop(oid, None)

    def _poll_recent_trades(self):
        """Fetch recent executions/fills so commissions survive restarts."""
        if not self.web_api_enabled or not self.client.is_connected:
            return
        if not hasattr(self.client, "get_recent_trades"):
            return

        now = time.time()
        if now - self._last_trades_poll_time < self.live_trades_poll_seconds:
            return
        self._last_trades_poll_time = now

        try:
            trades = self.client.get_recent_trades(days=self.live_trades_lookback_days)
        except Exception:
            return
        dash.update_live_trades(trades)
        self._reconcile_recent_trade_fills(trades)

    def _check_broker_connection(self):
        """Track broker connection state transitions and log them."""
        connected = self.client.is_connected
        if connected != self._broker_was_connected:
            if connected:
                logger.info("Broker connection state: CONNECTED")
            else:
                logger.warning("Broker connection state: DISCONNECTED")
            self._broker_was_connected = connected
            self._sync_dashboard_status()

    def _shutdown(self, reason: str):
        if self.shutdown_event.is_set():
            return

        logger.info(f"Shutdown requested: {reason}")
        self.shutdown_event.set()

        if self.yf_provider is not None:
            try:
                self.yf_provider.stop()
            except Exception as exc:
                logger.warning(f"Failed to stop yfinance provider cleanly: {exc}")

        dash.persist_live_state(force=True)

        try:
            if hasattr(self.client, "disconnect"):
                self.client.disconnect()
            elif hasattr(self.client, "stop"):
                self.client.stop()
        except Exception as exc:
            logger.warning(f"Disconnect warning during shutdown: {exc}")

    def _handle_signal(self, signum, _frame):
        self._shutdown(f"signal {signum}")

    @staticmethod
    def _safe_float(value):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None

        if parsed != parsed:
            return None

        return parsed

    def _broker_use_rth(self) -> int:
        return 0 if getattr(self, "extended_hours", False) else 1

    def _broker_history_label(self) -> str:
        return "extended hours included" if self._broker_use_rth() == 0 else "regular hours only"

    @staticmethod
    def _cash_source_key(source: str) -> str:
        return {
            "settled_cash": "SettledCash",
            "available_funds": "AvailableFunds",
            "total_cash": "TotalCashValue",
        }.get(source, "SettledCash")

    def _configured_buy_cash_keys(self) -> list[str]:
        sources = [getattr(self, "live_buy_cash_source", "settled_cash")]
        sources.extend(getattr(self, "live_buy_cash_fallbacks", []))
        keys = []
        for source in sources:
            key = self._cash_source_key(source)
            if key not in keys:
                keys.append(key)
        return keys

    def _account_data_ready(self) -> bool:
        if self._account_metric("NetLiquidation") is None:
            return False
        return any(self._account_metric(key) is not None for key in self._configured_buy_cash_keys())

    def _update_account_data_ready(self):
        dash.bot_state["account_data_ready"] = self._account_data_ready()

    def _ledger_day_key(self) -> str:
        try:
            return self.trading_hours.localize().date().isoformat()
        except Exception:
            return datetime.now(timezone.utc).date().isoformat()

    def _runtime_bucket(self, key: str) -> dict:
        with dash._live_buffers_lock:
            bucket = dash.live_runtime_state.setdefault(key, {})
            if not isinstance(bucket, dict):
                bucket = {}
                dash.live_runtime_state[key] = bucket
            return bucket

    def _slot_ledger(self) -> dict:
        ledger = self._runtime_bucket("slot_ledger")
        day_key = self._ledger_day_key()
        if ledger.get("date") != day_key:
            ledger.clear()
            ledger.update({"date": day_key, "base_net_liquidation": 0.0, "symbols": {}})

        if not isinstance(ledger.get("symbols"), dict):
            ledger["symbols"] = {}

        base = self._safe_float(ledger.get("base_net_liquidation")) or 0.0
        current_net = self._account_metric("NetLiquidation") or 0.0
        if base <= 0 and current_net > 0:
            ledger["base_net_liquidation"] = current_net
            base = current_net
            dash.persist_live_state(force=True)

        dash.bot_state["slot_ledger_date"] = ledger.get("date")
        dash.bot_state["slot_base_net_liquidation"] = round(base, 2)
        return ledger

    def _slot_symbol_ledger(self) -> dict:
        ledger = self._slot_ledger()
        symbols = ledger.setdefault("symbols", {})
        symbol_key = str(self.live_symbol).upper()
        symbol_ledger = symbols.setdefault(symbol_key, {"buy_lots": []})
        if not isinstance(symbol_ledger.get("buy_lots"), list):
            symbol_ledger["buy_lots"] = []
        return symbol_ledger

    def _slot_base_net_liquidation(self) -> float:
        ledger = self._slot_ledger()
        return self._safe_float(ledger.get("base_net_liquidation")) or 0.0

    def _slot_ledger_notional(self) -> float:
        symbol_ledger = self._slot_symbol_ledger()
        total = 0.0
        for lot in symbol_ledger.get("buy_lots", []):
            if not isinstance(lot, dict):
                continue
            if str(lot.get("status", "")).lower() in {"cancelled", "inactive", "removed"}:
                continue
            parsed = self._safe_float(lot.get("notional"))
            if parsed is not None:
                total += max(0.0, parsed)
        return total

    def _slot_usage(self, symbol_exposure: float, slot_notional: float) -> tuple[int, float]:
        if slot_notional <= 0:
            return 0, 0.0
        ledger_notional = self._slot_ledger_notional()
        basis = ledger_notional if ledger_notional > 0 else max(0.0, symbol_exposure)
        return min(getattr(self, "live_max_position_slots", 4), int(basis // slot_notional)), basis

    def _risk_counter(self) -> dict:
        counters = self._runtime_bucket("risk_counters")
        day_key = self._ledger_day_key()
        if counters.get("date") != day_key:
            counters.clear()
            counters.update({"date": day_key, "buy_orders": 0, "total_orders": 0, "last_order_ts": 0.0})
        return counters

    def _daily_loss_stop_hit(self) -> bool:
        stop_pct = getattr(self, "daily_loss_stop_pct", None)
        if stop_pct is None:
            return False
        base_net = self._slot_base_net_liquidation() or self._account_metric("NetLiquidation") or 0.0
        if base_net <= 0:
            return False
        realized = self._safe_float(self.telegram_summary.get("net_pnl")) or 0.0
        return realized <= -(base_net * stop_pct)

    def _risk_limit_block_reason(self, action: str) -> Optional[str]:
        action = action.upper()
        if action != "BUY":
            return None
        counters = self._risk_counter()
        max_buys = getattr(self, "max_buys_per_day", None)
        if max_buys is not None and counters.get("buy_orders", 0) >= max_buys:
            return f"daily BUY limit reached ({counters.get('buy_orders', 0)}/{max_buys})"
        max_orders = getattr(self, "max_orders_per_day", None)
        if max_orders is not None and counters.get("total_orders", 0) >= max_orders:
            return f"daily order limit reached ({counters.get('total_orders', 0)}/{max_orders})"
        min_gap = getattr(self, "min_seconds_between_orders", 0.0)
        last_order_ts = self._safe_float(counters.get("last_order_ts")) or 0.0
        if min_gap > 0 and last_order_ts > 0 and time.time() - last_order_ts < min_gap:
            remaining = int(min_gap - (time.time() - last_order_ts))
            return f"minimum order delay active ({remaining}s remaining)"
        if self._daily_loss_stop_hit():
            return f"daily loss stop reached ({getattr(self, 'daily_loss_stop_pct', 0.0):.2%})"
        return None

    def _record_order_sent(self, action: str, order_id: int, quantity: int, price: float, notional: float):
        action = action.upper()
        now = time.time()
        counters = self._risk_counter()
        counters["total_orders"] = int(counters.get("total_orders", 0)) + 1
        counters["last_order_ts"] = now
        if action == "BUY":
            counters["buy_orders"] = int(counters.get("buy_orders", 0)) + 1
            symbol_ledger = self._slot_symbol_ledger()
            symbol_ledger.setdefault("buy_lots", []).append(
                {
                    "order_id": str(order_id),
                    "quantity": int(quantity),
                    "price": float(price),
                    "notional": float(notional),
                    "status": "sent",
                    "ts": now,
                }
            )
        dash.bot_state["daily_buy_orders"] = counters.get("buy_orders", 0)
        dash.bot_state["daily_total_orders"] = counters.get("total_orders", 0)
        dash.bot_state["last_order_action"] = action
        dash.bot_state["last_order_status"] = "sent"
        dash.bot_state["last_order_id"] = str(order_id)
        self._refresh_live_position_state(price)
        dash.persist_live_state(force=True)

    def _record_order_filled(self, order_id: str, tracked: dict, order_info: dict):
        action = str(tracked.get("action", "")).upper()
        if action == "BUY":
            for lot in self._slot_symbol_ledger().get("buy_lots", []):
                if isinstance(lot, dict) and str(lot.get("order_id")) == str(order_id):
                    lot["status"] = "filled"
                    lot["fill_price"] = self._extract_fill_price(order_info, tracked)
                    lot["filled_ts"] = time.time()
                    break
        elif action == "SELL":
            self._slot_symbol_ledger()["buy_lots"] = []
        dash.bot_state["last_order_status"] = "filled"
        dash.bot_state["last_order_id"] = str(order_id)
        self._refresh_live_position_state()
        dash.persist_live_state(force=True)

    def _remove_reserved_buy_lot(self, order_id: str):
        symbol_ledger = self._slot_symbol_ledger()
        lots = symbol_ledger.get("buy_lots", [])
        symbol_ledger["buy_lots"] = [
            lot for lot in lots if not (isinstance(lot, dict) and str(lot.get("order_id")) == str(order_id))
        ]
        dash.persist_live_state(force=True)

    @staticmethod
    def _normalize_symbol(value) -> str:
        return str(value or "").strip().upper()

    def _symbols_match(self, candidate) -> bool:
        return self._normalize_symbol(candidate) == self._normalize_symbol(self.live_symbol)

    def _position_symbol_from_api(self, pos: dict) -> str:
        for key in ("ticker", "symbol", "contractSymbol", "contractDesc"):
            value = self._normalize_symbol(pos.get(key))
            if value:
                return value
        return "?"

    def _account_metric(self, display_key: str) -> Optional[float]:
        direct = dash.live_account_data.get(display_key, {})
        if isinstance(direct, dict):
            parsed = self._safe_float(direct.get("value"))
            if parsed is not None:
                return parsed

        summary = dash.live_account_data.get("PortfolioSummary", {})
        if isinstance(summary, dict):
            entry = summary.get(display_key.lower()) or summary.get(display_key)
            if isinstance(entry, dict):
                parsed = self._safe_float(entry.get("amount"))
                if parsed is not None:
                    return parsed

        ledger = dash.live_account_data.get("Ledger", {})
        if isinstance(ledger, dict):
            base = ledger.get("BASE") or ledger.get("USD") or {}
            if isinstance(base, dict):
                parsed = self._safe_float(base.get(display_key.lower()))
                if parsed is not None:
                    return parsed

        return None

    def _local_datetime(self, value: Optional[datetime] = None) -> datetime:
        try:
            return self.trading_hours.localize(value)
        except (AttributeError, ValueError):
            if value is None:
                value = datetime.now(timezone.utc)
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            tz = getattr(getattr(self, "trading_hours", None), "tz", None) or timezone.utc
            return value.astimezone(tz).replace(second=0, microsecond=0)

    @staticmethod
    def _add_business_days(local_dt: datetime, days: int) -> datetime:
        settlement = local_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        remaining = max(0, int(days))
        while remaining > 0:
            settlement += timedelta(days=1)
            if settlement.weekday() < 5:
                remaining -= 1
        return settlement

    def _trade_local_datetime(self, trade: dict) -> Optional[datetime]:
        raw_epoch = self._first_present(trade, ("trade_time_r", "tradeTimeR", "trade_time_ms"))
        epoch = self._safe_float(raw_epoch)
        if epoch is not None and epoch > 0:
            if epoch > 10_000_000_000:
                epoch /= 1000.0
            return self._local_datetime(datetime.fromtimestamp(epoch, tz=timezone.utc))

        raw_time = self._first_present(trade, ("trade_time", "tradeTime", "time"))
        if not raw_time:
            return None
        for fmt in ("%Y%m%d-%H:%M:%S", "%Y%m%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(str(raw_time), fmt)
            except ValueError:
                continue
            return self._local_datetime(parsed.replace(tzinfo=timezone.utc))
        return None

    def _sell_trade_proceeds(self, trade: dict) -> Optional[float]:
        raw_net = self._first_present(trade, ("net_amount", "netAmount", "net_amount_in_base"))
        net_amount = self._safe_float(raw_net)
        if net_amount is not None and net_amount > 0:
            return abs(net_amount)

        price = self._safe_float(self._first_present(trade, ("price", "avg_price", "avgPrice")))
        size = self._safe_float(self._first_present(trade, ("size", "quantity", "qty")))
        if price is None or size is None or price <= 0 or size <= 0:
            return None
        fee = self._safe_float(self._first_present(trade, ("commission", "commissions", "fee", "fees"))) or 0.0
        return max(0.0, abs(price * size) - abs(fee))

    def _unsettled_sale_proceeds(self, now: Optional[datetime] = None) -> dict:
        now_local = self._local_datetime(now)
        fills = []
        seen_exec = set()
        seen_orders = set()
        total = 0.0

        for trade in list(dash.live_trades_data):
            if not isinstance(trade, dict):
                continue

            sec_type = str(self._first_present(trade, ("sec_type", "secType", "assetClass")) or "").strip().upper()
            if sec_type and sec_type != "STK":
                continue

            side = str(self._first_present(trade, ("side", "action")) or "").strip().upper()
            if side not in {"S", "SLD", "SOLD", "SELL"}:
                continue

            execution_id = self._first_present(trade, ("execution_id", "executionId", "execId"))
            order_id = self._first_present(trade, ("order_id", "orderId", "ib_order_id"))

            trade_local = self._trade_local_datetime(trade)
            if trade_local is None:
                continue

            settlement_local = self._add_business_days(trade_local, 1)
            release_at_local = self._add_business_days(trade_local, 2)
            if now_local >= release_at_local:
                continue

            proceeds = self._sell_trade_proceeds(trade)
            if proceeds is None or proceeds <= 0:
                continue

            quantity = self._safe_float(self._first_present(trade, ("size", "quantity", "qty"))) or 0.0
            if execution_id:
                if execution_id in seen_exec:
                    continue
                seen_exec.add(execution_id)
            elif order_id:
                fallback_key = (order_id, trade_local.isoformat(), quantity, round(proceeds, 2))
                if fallback_key in seen_orders:
                    continue
                seen_orders.add(fallback_key)

            total += proceeds
            fills.append(
                {
                    "execution_id": execution_id,
                    "order_id": order_id,
                    "symbol": self._first_present(trade, ("symbol", "contract_description_1", "ticker")),
                    "side": side,
                    "quantity": quantity,
                    "proceeds": round(proceeds, 2),
                    "trade_time": trade_local.isoformat(),
                    "estimated_settlement": settlement_local.date().isoformat(),
                    "settlement_date": settlement_local.date().isoformat(),
                    "release_at": release_at_local.isoformat(),
                }
            )

        fills.sort(key=lambda fill: fill.get("release_at") or "")
        next_settlement = fills[0]["settlement_date"] if fills else None
        next_release_at = fills[0]["release_at"] if fills else None
        snapshot = {
            "total": round(total, 2),
            "next_estimated_settlement": next_settlement,
            "next_release_at": next_release_at,
            "fills": fills[:20],
        }
        dash.bot_state["unsettled_sale_proceeds"] = snapshot["total"]
        dash.bot_state["next_estimated_settlement"] = next_settlement
        dash.bot_state["next_release_at"] = next_release_at
        dash.bot_state["unsettled_sell_fills"] = snapshot["fills"]
        return snapshot

    def _cash_after_settlement_guard(self, display_key: str, value: float, settlement_guard: dict) -> float:
        cash_value = max(0.0, value)
        unsettled = self._safe_float(settlement_guard.get("total")) or 0.0
        if unsettled <= 0:
            return cash_value

        if display_key == "SettledCash":
            total_cash = self._account_metric("TotalCashValue")
            if total_cash is None:
                return cash_value
            return max(0.0, min(cash_value, total_cash - unsettled))

        if display_key in {"AvailableFunds", "TotalCashValue"}:
            return max(0.0, cash_value - unsettled)

        return cash_value

    def _buy_cash_available(self) -> tuple[float, str]:
        settlement_guard = self._unsettled_sale_proceeds()
        for display_key in self._configured_buy_cash_keys():
            value = self._account_metric(display_key)
            if value is not None:
                return self._cash_after_settlement_guard(display_key, value, settlement_guard), display_key
        primary_key = self._cash_source_key(getattr(self, "live_buy_cash_source", "settled_cash"))
        return 0.0, primary_key

    def _symbol_position_quantity(self) -> float:
        total_held = 0.0
        for pos_data in dash.live_positions_data.values():
            if self._symbols_match(pos_data.get("symbol")):
                parsed = self._safe_float(pos_data.get("position"))
                if parsed is not None:
                    total_held += parsed
        return total_held

    def _symbol_exposure_notional(self, current_price: float) -> float:
        exposure = 0.0
        for pos_data in dash.live_positions_data.values():
            if not self._symbols_match(pos_data.get("symbol")):
                continue

            position = self._safe_float(pos_data.get("position")) or 0.0
            if position == 0:
                continue

            if current_price and current_price > 0:
                exposure += abs(position * current_price)
                continue

            market_value = self._safe_float(pos_data.get("marketValue"))
            if market_value is not None:
                exposure += abs(market_value)
                continue

            market_price = self._safe_float(pos_data.get("marketPrice"))
            if market_price is not None:
                exposure += abs(position * market_price)

        return exposure

    def _refresh_live_position_state(self, current_price: float = 0.0):
        quantity = self._symbol_position_quantity()
        self.live_position = "LONG" if quantity > 0 else "NONE"
        dash.bot_state["position_state"] = self.live_position
        dash.bot_state["live_symbol_position"] = round(quantity, 4)
        price = self._safe_float(current_price) or self._safe_float(dash.bot_state.get("last_price")) or 0.0
        self._publish_live_slot_outlook(price)

    def _build_slot_outlook(
        self,
        buy_plan: LiveOrderPlan,
        sell_plan: LiveOrderPlan,
        signal_name: str = "HOLD",
        near_buy: bool = False,
        near_sell: bool = False,
    ) -> dict:
        signal = str(signal_name or "HOLD").upper()
        slots_used = buy_plan.slots_used
        max_slots = buy_plan.max_slots
        remaining_slots = buy_plan.remaining_slots
        slots_label = f"{slots_used}/{max_slots}" if max_slots else "--"
        full = max_slots > 0 and remaining_slots <= 0
        has_position = self.live_position == "LONG" or sell_plan.allowed

        next_action = "HOLD"
        explanation = "Waiting for the strategy to produce a BUY or SELL setup."

        if signal == "BUY":
            if buy_plan.allowed:
                next_action = "BUY"
                explanation = "Latest strategy signal is BUY and the current slot plan can add exposure."
            elif has_position:
                next_action = "SELL"
                explanation = f"BUY is blocked ({buy_plan.reason}); the next tradable direction is an exit SELL."
            else:
                explanation = f"BUY is blocked ({buy_plan.reason})."
        elif signal == "SELL":
            if sell_plan.allowed:
                next_action = "SELL"
                explanation = "Latest strategy signal is SELL and the current position can be exited."
            elif buy_plan.allowed:
                next_action = "BUY"
                explanation = f"SELL is blocked ({sell_plan.reason}); a later BUY setup can still open or add a slot."
            else:
                explanation = f"SELL is blocked ({sell_plan.reason})."
        elif full:
            if sell_plan.allowed:
                next_action = "SELL"
                explanation = f"Slots are full ({slots_label}); no more BUY slots are available until a SELL exit clears them."
            else:
                explanation = f"Slots are full ({slots_label}), but no sellable position is currently available."
        elif near_buy and near_sell and buy_plan.allowed and sell_plan.allowed:
            next_action = "BUY_OR_SELL"
            explanation = "Both near BUY and near SELL flags are active; the next completed bar decides."
        elif near_buy and buy_plan.allowed:
            next_action = "BUY"
            explanation = "Near BUY setup is active and at least one slot remains."
        elif near_sell and sell_plan.allowed:
            next_action = "SELL"
            explanation = "Near SELL setup is active and the current position can be exited."
        elif buy_plan.allowed and sell_plan.allowed:
            next_action = "BUY_OR_SELL"
            explanation = (
                f"{remaining_slots} slot(s) remain; a later BUY can scale in, "
                "or a later SELL signal can exit the full position."
            )
        elif buy_plan.allowed:
            next_action = "BUY"
            explanation = f"{remaining_slots} slot(s) remain for a later BUY signal."
        elif sell_plan.allowed:
            next_action = "SELL"
            explanation = "BUY is blocked; a later SELL signal can exit the current position."
        elif buy_plan.reason:
            explanation = f"No actionable slot path now: {buy_plan.reason}."

        return {
            "slots": slots_label,
            "slots_used": slots_used,
            "max_slots": max_slots,
            "remaining_slots": remaining_slots,
            "slot_state": "FULL" if full else "OPEN",
            "position_state": self.live_position,
            "current_signal": signal,
            "near_buy": bool(near_buy),
            "near_sell": bool(near_sell),
            "next_action": next_action,
            "explanation": explanation,
            "buy": buy_plan.as_payload(),
            "sell": sell_plan.as_payload(),
        }

    def _publish_live_slot_outlook(
        self,
        current_price: float = 0.0,
        signal_name: str = "HOLD",
        near_buy: bool = False,
        near_sell: bool = False,
        buy_plan: Optional[LiveOrderPlan] = None,
        sell_plan: Optional[LiveOrderPlan] = None,
    ):
        price = self._safe_float(current_price) or 0.0
        buy_plan = buy_plan or self._build_live_order_plan("BUY", price)
        sell_plan = sell_plan or self._build_live_order_plan("SELL", price)
        dash.bot_state["live_position_slots"] = f"{buy_plan.slots_used}/{buy_plan.max_slots}"
        dash.bot_state["live_symbol_exposure"] = round(buy_plan.symbol_exposure, 2)
        dash.bot_state["live_buy_plan"] = buy_plan.as_payload()
        dash.bot_state["live_sell_plan"] = sell_plan.as_payload()
        dash.bot_state["slot_outlook"] = self._build_slot_outlook(
            buy_plan=buy_plan,
            sell_plan=sell_plan,
            signal_name=signal_name,
            near_buy=near_buy,
            near_sell=near_sell,
        )

    def _extended_hours_order_active(self) -> bool:
        return bool(
            getattr(self, "web_api_enabled", False)
            and is_us_equity_outside_regular_hours(getattr(self, "extended_hours", False))
        )

    def _effective_max_order_notional(self) -> Optional[float]:
        values = []
        max_order_notional = getattr(self, "max_order_notional", None)
        if max_order_notional is not None:
            values.append(max_order_notional)
        ext_hours_max = getattr(self, "ext_hours_max_order_notional", None)
        if self._extended_hours_order_active() and ext_hours_max is not None:
            values.append(ext_hours_max)
        if not values:
            return None
        return min(values)

    def _extended_hours_has_cap(self) -> bool:
        return any(
            cap is not None
            for cap in (
                getattr(self, "max_order_quantity", None),
                getattr(self, "max_order_notional", None),
                getattr(self, "ext_hours_max_order_notional", None),
            )
        )

    def _build_live_order_plan(self, action: str, current_price: float) -> LiveOrderPlan:
        action = action.upper()
        price = self._safe_float(current_price) or 0.0
        net_liquidation = self._account_metric("NetLiquidation") or 0.0
        symbol_exposure = self._symbol_exposure_notional(price)
        max_slots = getattr(self, "live_max_position_slots", 4)
        slot_pct = getattr(self, "live_slot_allocation_pct", 0.25)
        base_net_liquidation = self._slot_base_net_liquidation()
        if base_net_liquidation <= 0 and net_liquidation > 0:
            base_net_liquidation = net_liquidation
        slot_notional = base_net_liquidation * slot_pct if base_net_liquidation > 0 else 0.0
        slots_used, ledger_basis_notional = self._slot_usage(symbol_exposure, slot_notional)
        remaining_slots = max(0, max_slots - slots_used)

        plan = LiveOrderPlan(
            action=action,
            sizing_mode=getattr(self, "live_order_sizing_mode", "slot_percent"),
            net_liquidation=net_liquidation,
            base_net_liquidation=base_net_liquidation,
            symbol_exposure=symbol_exposure,
            slot_notional=slot_notional,
            slots_used=slots_used,
            max_slots=max_slots,
            remaining_slots=remaining_slots,
        )

        if price <= 0:
            plan.reason = "current price is unavailable"
            plan.transient = True
            return plan

        if action == "SELL":
            total_held = self._symbol_position_quantity()
            if total_held <= 0:
                if not dash.bot_state.get("positions_data_ready", False):
                    plan.reason = "broker position data is not ready"
                    plan.transient = True
                else:
                    plan.reason = f"no open {self.live_symbol} position"
                return plan

            quantity = int(total_held)
            if quantity <= 0:
                plan.reason = f"{self.live_symbol} position is below one whole share"
                return plan

            # Exits must always be allowed to close the full position. Notional/
            # quantity caps and the extended-hours cap requirement only bound new
            # exposure (BUYs) -- never an exit.
            plan.quantity = quantity
            plan.order_notional = abs(quantity * price)
            plan.allowed = True
            plan.reason = "ok"
            return plan

        if action != "BUY":
            plan.reason = f"unsupported action {action}"
            return plan

        if not self._account_data_ready():
            plan.reason = "broker account data is not ready"
            plan.transient = True
            return plan

        if not dash.bot_state.get("positions_data_ready", False):
            plan.reason = "broker position data is not ready"
            plan.transient = True
            return plan

        if self._extended_hours_order_active() and not self._extended_hours_has_cap():
            plan.reason = "extended-hours Web API orders require MAX_ORDER_QUANTITY, MAX_ORDER_NOTIONAL, or EXT_HOURS_MAX_ORDER_NOTIONAL"
            return plan

        risk_reason = self._risk_limit_block_reason("BUY")
        if risk_reason:
            plan.reason = risk_reason
            return plan

        cash_available, cash_source = self._buy_cash_available()
        plan.cash_available = cash_available
        plan.cash_source = cash_source

        if net_liquidation <= 0:
            plan.reason = "NetLiquidation is unavailable"
            plan.transient = True
            return plan

        if cash_available <= 0:
            plan.reason = f"{cash_source} is unavailable or zero"
            plan.transient = True
            return plan

        remaining_slot_notional = None
        if slot_notional > 0:
            max_position_notional = slot_notional * max_slots
            remaining_slot_notional = max(0.0, max_position_notional - max(symbol_exposure, ledger_basis_notional))
            if remaining_slot_notional <= 0:
                plan.reason = f"slot capacity full ({slots_used}/{max_slots})"
                return plan

        sizing_mode = getattr(self, "live_order_sizing_mode", "slot_percent")
        if sizing_mode == "fixed_quantity":
            desired_notional = getattr(self, "trade_quantity", 100) * price
        elif sizing_mode == "allocation_percent":
            desired_notional = net_liquidation * getattr(self, "live_allocation_pct", 0.25)
        else:
            if slot_notional <= 0:
                plan.reason = "slot notional is zero"
                plan.transient = True
                return plan
            if remaining_slots <= 0:
                plan.reason = f"slot capacity full ({slots_used}/{getattr(self, 'live_max_position_slots', 4)})"
                return plan
            remainder = ledger_basis_notional % slot_notional if slot_notional > 0 else 0.0
            desired_notional = slot_notional - remainder if remainder > 0 else slot_notional

        if remaining_slot_notional is not None:
            desired_notional = min(desired_notional, remaining_slot_notional)

        desired_notional = min(desired_notional, cash_available)

        max_notional = self._effective_max_order_notional()
        if max_notional is not None:
            desired_notional = min(desired_notional, max_notional)

        quantity = int(desired_notional // price)
        max_quantity = getattr(self, "max_order_quantity", None)
        if max_quantity is not None:
            quantity = min(quantity, max_quantity)

        if quantity <= 0:
            plan.reason = f"insufficient {cash_source} or caps to buy one share at {price:.2f}"
            return plan

        plan.quantity = quantity
        plan.order_notional = quantity * price
        plan.allowed = True
        plan.reason = "ok"
        return plan

    def _log_trade_plan_blocked(self, action: str, current_price: float, plan: LiveOrderPlan, level: str = "WARN"):
        logger.warning(f"*** [{self.trading_mode}] BLOCKED {action}: {plan.reason}. ***")
        dash.append_live_log(
            level=level,
            event_type="trade_blocked",
            message=f"Blocked {action}: {plan.reason}",
            payload={
                "mode": self.trading_mode,
                "symbol": self.live_symbol,
                "price": current_price,
                **plan.as_payload(),
            },
        )
        self._sync_dashboard_status()

    def _symbol_avg_cost(self) -> float:
        total_qty = 0.0
        weighted_cost = 0.0
        for pos_data in dash.live_positions_data.values():
            if not self._symbols_match(pos_data.get("symbol")):
                continue
            qty = self._safe_float(pos_data.get("position")) or 0.0
            avg_cost = self._safe_float(pos_data.get("avgCost")) or 0.0
            if qty <= 0 or avg_cost <= 0:
                continue
            total_qty += qty
            weighted_cost += qty * avg_cost
        return weighted_cost / total_qty if total_qty > 0 else 0.0

    def _get_latest_strategy_signal(self, planned_trade_notional: Optional[float], session_name: Optional[str] = None):
        position_qty = self._symbol_position_quantity()
        avg_cost = self._symbol_avg_cost()
        try:
            return self.live_strategy.get_latest_signal(
                planned_trade_notional=planned_trade_notional,
                session_name=session_name,
                position_qty=position_qty,
                avg_cost=avg_cost,
            )
        except TypeError:
            try:
                return self.live_strategy.get_latest_signal(planned_trade_notional=planned_trade_notional)
            except TypeError:
                try:
                    return self.live_strategy.get_latest_signal(session_name=session_name)
                except TypeError:
                    return self.live_strategy.get_latest_signal()

    def _apply_strategy_buy_fraction(self, buy_plan: LiveOrderPlan, current_price: float) -> LiveOrderPlan:
        context = getattr(self.live_strategy, "last_signal_context", {}) or {}
        raw_fraction = context.get("buy_fraction")
        if raw_fraction is None or not buy_plan.allowed:
            return buy_plan
        try:
            fraction = float(raw_fraction)
        except (TypeError, ValueError):
            return buy_plan
        fraction = min(1.0, max(0.0, fraction))
        if fraction <= 0:
            buy_plan.allowed = False
            buy_plan.reason = "strategy buy fraction is zero"
            buy_plan.quantity = 0
            buy_plan.order_notional = 0.0
            return buy_plan

        price = self._safe_float(current_price) or 0.0
        if price <= 0:
            return buy_plan

        if buy_plan.sizing_mode == "slot_percent" and buy_plan.slot_notional > 0 and buy_plan.max_slots > 0:
            max_strategy_notional = buy_plan.slot_notional * buy_plan.max_slots
            ledger_basis_notional = self._slot_ledger_notional()
            used_notional = max(buy_plan.symbol_exposure, ledger_basis_notional)
            remaining_notional = max(0.0, max_strategy_notional - used_notional)
            desired_notional = min(max_strategy_notional * fraction, remaining_notional)
        else:
            desired_notional = buy_plan.order_notional * fraction

        desired_notional = min(desired_notional, buy_plan.cash_available)
        max_notional = self._effective_max_order_notional()
        if max_notional is not None:
            desired_notional = min(desired_notional, max_notional)

        adjusted_quantity = int(desired_notional // price)
        max_quantity = getattr(self, "max_order_quantity", None)
        if max_quantity is not None:
            adjusted_quantity = min(adjusted_quantity, max_quantity)

        if adjusted_quantity <= 0:
            buy_plan.allowed = False
            buy_plan.reason = "strategy buy fraction is below one share after cash/caps"
            buy_plan.quantity = 0
            buy_plan.order_notional = 0.0
            return buy_plan

        buy_plan.quantity = adjusted_quantity
        buy_plan.order_notional = adjusted_quantity * price
        return buy_plan

    def _extract_live_metrics(self) -> dict:
        if len(self.live_strategy.df) == 0:
            return {}

        latest = self.live_strategy.df.iloc[-1]

        close_value = self._safe_float(latest.get("close"))
        bb_lower = self._safe_float(latest.get("BB_LOWER"))
        bb_mid = self._safe_float(latest.get("BB_MID"))
        bb_upper = self._safe_float(latest.get("BB_UPPER"))
        rsi = self._safe_float(latest.get("RSI"))
        smi = self._safe_float(latest.get("SMI"))
        smi_signal = self._safe_float(latest.get("SMI_SIGNAL"))
        trend_sma = self._safe_float(latest.get("TREND_SMA"))
        signal_context = getattr(self.live_strategy, "last_signal_context", {}) or {}

        distance_to_lower_pct = None
        distance_to_upper_pct = None
        smi_gap = None

        if close_value and bb_lower is not None:
            distance_to_lower_pct = max(0.0, (close_value - bb_lower) / close_value)

        if close_value and bb_upper is not None:
            distance_to_upper_pct = max(0.0, (bb_upper - close_value) / close_value)

        if smi is not None and smi_signal is not None:
            smi_gap = abs(smi - smi_signal)

        return {
            "close": close_value,
            "bb_lower": bb_lower,
            "bb_mid": bb_mid,
            "bb_upper": bb_upper,
            "rsi": rsi,
            "smi": smi,
            "smi_signal": smi_signal,
            "trend_sma": trend_sma,
            "distance_to_lower_pct": distance_to_lower_pct,
            "distance_to_upper_pct": distance_to_upper_pct,
            "smi_gap": smi_gap,
            "session_name": signal_context.get("session_name"),
            "session_profile": signal_context.get("session_profile"),
            "effective_oversold": self._safe_float(signal_context.get("effective_oversold")),
            "effective_bb_std": self._safe_float(signal_context.get("effective_bb_std")),
            "effective_bb_tolerance": self._safe_float(signal_context.get("effective_bb_tolerance")),
            "effective_bb_lower": self._safe_float(signal_context.get("effective_bb_lower")),
            "effective_bb_mid": self._safe_float(signal_context.get("effective_bb_mid")),
            "effective_bb_upper": self._safe_float(signal_context.get("effective_bb_upper")),
            "volume_sma": self._safe_float(signal_context.get("volume_sma")),
            "volume_ratio": self._safe_float(signal_context.get("volume_ratio")),
            "volume_filter_passed": signal_context.get("volume_filter_passed"),
            "buy_block_reason": signal_context.get("buy_block_reason"),
            "sell_block_reason": signal_context.get("sell_block_reason"),
            "buy_setup_bars": signal_context.get("buy_setup_bars"),
            "required_buy_setup_bars": signal_context.get("required_buy_setup_bars"),
            "sell_setup_bars": signal_context.get("sell_setup_bars"),
            "required_sell_setup_bars": signal_context.get("required_sell_setup_bars"),
        }

    def _detect_near_signals(self, metrics: dict) -> tuple[bool, bool, str, str]:
        near_buy = False
        near_sell = False
        near_buy_reason = ""
        near_sell_reason = ""

        distance_to_lower_pct = metrics.get("distance_to_lower_pct")
        distance_to_upper_pct = metrics.get("distance_to_upper_pct")
        
        # Original BB_SMI near signals
        smi = metrics.get("smi")
        smi_signal = metrics.get("smi_signal")
        smi_gap = metrics.get("smi_gap")

        if (
            smi is not None
            and smi_signal is not None
            and smi_gap is not None
            and distance_to_lower_pct is not None
            and distance_to_lower_pct <= self.near_band_pct
            and smi <= smi_signal
            and smi_gap <= self.near_smi_gap
        ):
            near_buy = True
            near_buy_reason = "price_near_lower_bb_and_smi_near_cross_up"

        if (
            smi is not None
            and smi_signal is not None
            and smi_gap is not None
            and distance_to_upper_pct is not None
            and distance_to_upper_pct <= self.near_band_pct
            and smi >= smi_signal
            and smi_gap <= self.near_smi_gap
        ):
            near_sell = True
            near_sell_reason = "price_near_upper_bb_and_smi_near_cross_down"
            
        # Generic RSI/BB near signals
        rsi = metrics.get("rsi")
        if rsi is not None and not (near_buy or near_sell):
            # If RSI is within 2 points of threshold and price is near band
            if distance_to_lower_pct is not None and distance_to_lower_pct <= self.near_band_pct and rsi < 40:
                near_buy = True
                near_buy_reason = f"price_near_lower_bb_rsi_{rsi:.1f}"
            if distance_to_upper_pct is not None and distance_to_upper_pct <= self.near_band_pct and rsi > 60:
                near_sell = True
                near_sell_reason = f"price_near_upper_bb_rsi_{rsi:.1f}"

        return near_buy, near_sell, near_buy_reason, near_sell_reason

    def _format_signal_diag_message(
        self,
        signal_name: str,
        near_buy: bool,
        near_sell: bool,
        metrics: dict,
    ) -> str:
        signal = str(signal_name or "HOLD").upper()
        dist_buy_pct = self._safe_float(metrics.get("distance_to_lower_pct"))
        dist_sell_pct = self._safe_float(metrics.get("distance_to_upper_pct"))
        rsi = self._safe_float(metrics.get("rsi"))
        oversold = self._safe_float(metrics.get("effective_oversold"))
        overbought = self._safe_float(getattr(self.live_strategy, "overbought", None))
        buy_block_reason = metrics.get("buy_block_reason")
        sell_block_reason = metrics.get("sell_block_reason")

        parts = [f"Signal {signal}", f"near BUY/SELL {('yes' if near_buy else 'no')}/{('yes' if near_sell else 'no')}"]
        if dist_buy_pct is not None:
            parts.append(f"buy_dist={dist_buy_pct * 100:.2f}%")
        if dist_sell_pct is not None:
            parts.append(f"sell_dist={dist_sell_pct * 100:.2f}%")
        if rsi is not None:
            parts.append(f"rsi={rsi:.1f}")
        if rsi is not None and oversold is not None:
            parts.append(f"rsi_to_buy={rsi - oversold:+.1f}")
        if rsi is not None and overbought is not None:
            parts.append(f"rsi_to_sell={overbought - rsi:+.1f}")
        if buy_block_reason:
            parts.append(f"buy_block={buy_block_reason}")
        if sell_block_reason:
            parts.append(f"sell_block={sell_block_reason}")
        return " | ".join(parts)

    def _log_bar_signal_diagnostics(
        self,
        bar: NormalizedBar,
        signal_name: str,
        near_buy: bool,
        near_sell: bool,
        near_buy_reason: str,
        near_sell_reason: str,
        metrics: dict,
    ):
        dist_buy_pct = self._safe_float(metrics.get("distance_to_lower_pct"))
        dist_sell_pct = self._safe_float(metrics.get("distance_to_upper_pct"))
        rsi = self._safe_float(metrics.get("rsi"))
        oversold = self._safe_float(metrics.get("effective_oversold"))
        overbought = self._safe_float(getattr(self.live_strategy, "overbought", None))
        payload = {
            "symbol": self.live_symbol,
            "time": bar.date,
            "source": bar.source,
            "signal": str(signal_name or "HOLD").upper(),
            "near_buy": near_buy,
            "near_sell": near_sell,
            "near_buy_reason": near_buy_reason,
            "near_sell_reason": near_sell_reason,
            "distance_to_buy_pct": round(dist_buy_pct * 100, 4) if dist_buy_pct is not None else None,
            "distance_to_sell_pct": round(dist_sell_pct * 100, 4) if dist_sell_pct is not None else None,
            "rsi": round(rsi, 4) if rsi is not None else None,
            "effective_oversold": round(oversold, 4) if oversold is not None else None,
            "overbought": round(overbought, 4) if overbought is not None else None,
            "rsi_to_buy": round(rsi - oversold, 4) if rsi is not None and oversold is not None else None,
            "rsi_to_sell": round(overbought - rsi, 4) if rsi is not None and overbought is not None else None,
            "buy_block_reason": metrics.get("buy_block_reason"),
            "sell_block_reason": metrics.get("sell_block_reason"),
            "buy_setup_bars": metrics.get("buy_setup_bars"),
            "required_buy_setup_bars": metrics.get("required_buy_setup_bars"),
            "sell_setup_bars": metrics.get("sell_setup_bars"),
            "required_sell_setup_bars": metrics.get("required_sell_setup_bars"),
            "session_name": metrics.get("session_name"),
            "session_profile": metrics.get("session_profile"),
        }
        dash.append_live_log(
            level="INFO",
            event_type="signal_diag",
            message=self._format_signal_diag_message(signal_name, near_buy, near_sell, metrics),
            payload=payload,
        )

    def _update_live_state(
        self,
        bar: NormalizedBar,
        signal_name: str,
        near_buy: bool,
        near_sell: bool,
        buy_plan: Optional[LiveOrderPlan] = None,
        sell_plan: Optional[LiveOrderPlan] = None,
    ):
        dash.bot_state["last_price"] = round(float(bar.close), 4)
        dash.bot_state["last_price_time"] = bar.date
        dash.bot_state["last_price_source"] = bar.source
        dash.bot_state["last_signal"] = signal_name
        dash.bot_state["last_signal_time"] = bar.date
        signal_context = getattr(self.live_strategy, "last_signal_context", {}) or {}
        dash.bot_state["last_signal_session_profile"] = signal_context.get("session_profile")
        dash.bot_state["last_buy_block_reason"] = signal_context.get("buy_block_reason")
        dash.bot_state["last_sell_block_reason"] = signal_context.get("sell_block_reason")
        dash.bot_state["last_volume_ratio"] = signal_context.get("volume_ratio")
        dash.bot_state["last_buy_setup_bars"] = signal_context.get("buy_setup_bars")
        dash.bot_state["last_required_buy_setup_bars"] = signal_context.get("required_buy_setup_bars")
        dash.bot_state["last_sell_setup_bars"] = signal_context.get("sell_setup_bars")
        dash.bot_state["last_required_sell_setup_bars"] = signal_context.get("required_sell_setup_bars")
        dash.bot_state["near_buy"] = near_buy
        dash.bot_state["near_sell"] = near_sell
        self._publish_live_slot_outlook(
            current_price=bar.close,
            signal_name=signal_name,
            near_buy=near_buy,
            near_sell=near_sell,
            buy_plan=buy_plan,
            sell_plan=sell_plan,
        )

    def _append_live_candle(self, bar: NormalizedBar, signal_name: str, metrics: dict, near_buy: bool, near_sell: bool):
        dash.append_live_candle(
            symbol=self.live_symbol,
            interval=self.live_interval,
            source=bar.source,
            bar_time=bar.date,
            open_price=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            indicators={
                "signal": signal_name,
                "near_buy": near_buy,
                "near_sell": near_sell,
                "bb_lower": metrics.get("bb_lower"),
                "bb_mid": metrics.get("bb_mid"),
                "bb_upper": metrics.get("bb_upper"),
                "rsi": metrics.get("rsi"),
                "smi": metrics.get("smi"),
                "smi_signal": metrics.get("smi_signal"),
                "trend_sma": metrics.get("trend_sma"),
                "distance_to_lower_pct": metrics.get("distance_to_lower_pct"),
                "distance_to_upper_pct": metrics.get("distance_to_upper_pct"),
                "smi_gap": metrics.get("smi_gap"),
                "session_name": metrics.get("session_name"),
                "session_profile": metrics.get("session_profile"),
                "effective_oversold": metrics.get("effective_oversold"),
                "effective_bb_std": metrics.get("effective_bb_std"),
                "effective_bb_tolerance": metrics.get("effective_bb_tolerance"),
                "effective_bb_lower": metrics.get("effective_bb_lower"),
                "effective_bb_mid": metrics.get("effective_bb_mid"),
                "effective_bb_upper": metrics.get("effective_bb_upper"),
                "volume_sma": metrics.get("volume_sma"),
                "volume_ratio": metrics.get("volume_ratio"),
                "volume_filter_passed": metrics.get("volume_filter_passed"),
                "buy_block_reason": metrics.get("buy_block_reason"),
                "sell_block_reason": metrics.get("sell_block_reason"),
                "buy_setup_bars": metrics.get("buy_setup_bars"),
                "required_buy_setup_bars": metrics.get("required_buy_setup_bars"),
                "sell_setup_bars": metrics.get("sell_setup_bars"),
                "required_sell_setup_bars": metrics.get("required_sell_setup_bars"),
            },
        )

    def _trim_live_strategy_df(self):
        df = getattr(self.live_strategy, "df", None)
        max_bars = getattr(self, "live_strategy_df_max_bars", 500)
        if df is None or len(df) <= max_bars:
            return
        self.live_strategy.df = df.tail(max_bars).copy()
        logger.debug(f"Trimmed live strategy dataframe to {max_bars} rows")


    def _apply_backfill_bar(self, bar: NormalizedBar):
        with self._bar_lock:
            signature = bar.signature()
            if self._last_processed_signature == signature:
                return

            try:
                bar_index = pd.to_datetime(bar.date)
                existing = getattr(self.live_strategy, "df", None)
                if existing is not None and bar_index in existing.index:
                    existing_row = existing.loc[bar_index]
                    if isinstance(existing_row, pd.DataFrame):
                        existing_row = existing_row.iloc[-1]
                    same_bar = (
                        float(existing_row.get("open", 0.0)) == float(bar.open)
                        and float(existing_row.get("high", 0.0)) == float(bar.high)
                        and float(existing_row.get("low", 0.0)) == float(bar.low)
                        and float(existing_row.get("close", 0.0)) == float(bar.close)
                        and float(existing_row.get("volume", 0.0)) == float(bar.volume)
                    )
                    if same_bar:
                        self._last_processed_signature = signature
                        self._last_processed_ts = bar.timestamp
                        return
            except Exception:
                pass

            self.live_strategy.add_bar(
                date_str=bar.date,
                open_price=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                barCount=bar.barCount,
                wap=bar.wap,
            )
            self._trim_live_strategy_df()
            self.sim_manager.on_backfill_bar(bar)

            dash.bot_state["last_price"] = round(float(bar.close), 4)
            dash.bot_state["last_price_time"] = bar.date

            self._last_processed_signature = signature
            self._last_processed_ts = bar.timestamp
            self._backfill_bars_applied += 1

    def _session_start_et(self, local_dt: datetime, session_name: str):
        session = next((s for s in self.trading_hours.sessions if s.name == session_name), None)
        if session is None:
            return None

        start_hour, start_minute = divmod(session.start_minute, 60)
        session_day = local_dt
        if session.crosses_midnight:
            local_minute = local_dt.hour * 60 + local_dt.minute
            if local_minute < session.end_minute:
                session_day = local_dt - timedelta(days=1)

        return session_day.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)

    def _min_completed_bars_for_session(self, session_name: str) -> int:
        general_min_bars = getattr(self, "session_min_completed_bars", 0)
        if general_min_bars > 0:
            return general_min_bars
        if session_name == "pre_market":
            return self.premarket_min_completed_bars
        return 0

    def _prune_live_strategy_to_session(self, session_name: str, reference_ts: datetime) -> int:
        df = getattr(self.live_strategy, "df", None)
        if df is None or len(df) == 0:
            return 0

        session_start_et = self._session_start_et(self.trading_hours.localize(reference_ts), session_name)
        if session_start_et is None:
            return 0

        session_start_utc = pd.Timestamp(session_start_et.astimezone(timezone.utc))
        index_utc = pd.to_datetime(df.index, errors="coerce", utc=True)
        keep_mask = index_utc >= session_start_utc
        removed = int(len(df) - keep_mask.sum())
        if removed > 0:
            pruned = df.loc[keep_mask].copy()
            base_columns = set(getattr(self.live_strategy, "base_columns", []))
            if not base_columns:
                base_columns = {"open", "high", "low", "close", "volume", "barCount", "wap"}
            indicator_columns = [col for col in pruned.columns if col not in base_columns]
            if indicator_columns:
                pruned.loc[:, indicator_columns] = float("nan")
            self.live_strategy.df = pruned
        return removed

    def _prepare_live_session_context(self, session_name: str, bar: NormalizedBar):
        session_name = session_name or "regular"
        if hasattr(self.live_strategy, "set_session_context"):
            self.live_strategy.set_session_context(session_name)

        previous_session = getattr(self, "_active_strategy_session", None)
        if previous_session is None:
            self._active_strategy_session = session_name
            dash.bot_state["market_session_profile"] = (
                "extended" if session_name in {"pre_market", "after_hours", "overnight"} else "regular"
            )
            return
        if previous_session == session_name:
            return

        removed = 0
        if getattr(self, "trading_hours_enabled", True) and getattr(self, "ext_hours_reset_indicators_on_session_change", True):
            removed = self._prune_live_strategy_to_session(session_name, bar.timestamp)
            dash.bot_state["indicators_fully_ready"] = False
            self._indicators_warmed_up = False
            self._update_warmup_state()

        self._active_strategy_session = session_name
        dash.bot_state["market_session_profile"] = "extended" if session_name in {"pre_market", "after_hours", "overnight"} else "regular"

        if previous_session is not None or removed > 0:
            dash.append_live_log(
                level="INFO",
                event_type="trading_hours",
                message=f"Session context changed to {session_name}; indicator data isolated to current session",
                payload={
                    "previous_session": previous_session,
                    "session": session_name,
                    "removed_bars": removed,
                    "bar_time": bar.date,
                },
            )

    def _live_bar_gate(self, bar: NormalizedBar) -> tuple[bool, bool, str, dict]:
        if not self.trading_hours_enabled:
            return True, False, "trading_hours_disabled", {}

        now_status = self.trading_hours.status()
        bar_status = self.trading_hours.status(bar.timestamp)
        payload = {
            "symbol": self.live_symbol,
            "bar_time": bar.date,
            "bar_session": bar_status.session_name,
            "current_session": now_status.session_name,
            "now_et": now_status.now_et,
            "next_open": now_status.next_open,
            "next_close": now_status.next_close,
        }

        if not now_status.is_open:
            return False, False, "outside_trading_hours", payload
        if not bar_status.is_open:
            return False, False, "bar_outside_trading_hours", payload
        if bar_status.session_name != now_status.session_name:
            return False, False, "stale_bar_from_previous_session", payload

        bar_close_et = self.trading_hours.localize(bar.timestamp) + timedelta(minutes=self.live_interval_minutes)
        now_et = self.trading_hours.localize()
        if now_et < bar_close_et:
            payload["bar_close_et"] = bar_close_et.isoformat()
            return False, True, "waiting_for_completed_bar", payload

        min_completed_bars = self._min_completed_bars_for_session(now_status.session_name)
        if min_completed_bars > 0:
            session_start_et = self._session_start_et(self.trading_hours.localize(bar.timestamp), bar_status.session_name)
            if session_start_et is not None:
                completed_bars = int(
                    max(0.0, (bar_close_et - session_start_et).total_seconds())
                    // (self.live_interval_minutes * 60)
                )
                payload["session_open_et"] = session_start_et.isoformat()
                payload["completed_session_bars"] = completed_bars
                payload["required_session_bars"] = min_completed_bars
                payload["first_allowed_bar_close_et"] = (
                    session_start_et + timedelta(minutes=self.live_interval_minutes * min_completed_bars)
                ).isoformat()
                if completed_bars < min_completed_bars:
                    return False, False, "waiting_for_session_warmup_bars", payload

        return True, False, "open", payload

    def _apply_live_bar(self, bar: NormalizedBar):
        allowed, retry_later, gate_reason, gate_payload = self._live_bar_gate(bar)
        if not allowed:
            gate_key = (gate_reason, gate_payload.get("bar_time"), gate_payload.get("current_session"))
            if gate_key != self._last_bar_gate_log_key:
                level = "INFO" if retry_later else "WARN"
                message = (
                    f"Waiting for completed {self.live_interval} bar before processing"
                    if retry_later
                    else f"Skipped live bar: {gate_reason.replace('_', ' ')}"
                )
                dash.append_live_log(
                    level=level,
                    event_type="trading_hours",
                    message=message,
                    payload=gate_payload,
                )
                self._last_bar_gate_log_key = gate_key
            self._sync_dashboard_status()
            return False if retry_later else True

        with self._bar_lock:
            if self._last_processed_ts is not None and bar.timestamp < self._last_processed_ts:
                return True

            signature = bar.signature()
            if self._last_processed_signature == signature:
                return True

            session_name = gate_payload.get("bar_session") or gate_payload.get("current_session") or "regular"
            self._prepare_live_session_context(session_name, bar)

            self.live_strategy.add_bar(
                date_str=bar.date,
                open_price=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                barCount=bar.barCount,
                wap=bar.wap,
            )
            self._trim_live_strategy_df()
            self.live_strategy.update_indicators()

            self._check_indicator_warmup("live bar")

            buy_plan = self._build_live_order_plan("BUY", bar.close)
            planned_trade_notional = buy_plan.order_notional if buy_plan.allowed else None
            signal_name, _price = self._get_latest_strategy_signal(planned_trade_notional, session_name=session_name)
            if signal_name == "BUY":
                buy_plan = self._apply_strategy_buy_fraction(buy_plan, bar.close)
            sell_plan = self._build_live_order_plan("SELL", bar.close)

            if not self._indicators_warmed_up and signal_name != "HOLD":
                self._indicators_warmed_up = True
                logger.info(f"Indicators warmed up -- first signal: {signal_name} at {bar.close}")
                dash.append_live_log(
                    level="SUCCESS",
                    event_type="warmup",
                    message=f"Indicators warmed up -- first signal: {signal_name} at {bar.close}",
                    payload={"signal": signal_name, "price": bar.close, "bars": len(self.live_strategy.df)},
                )

            metrics = self._extract_live_metrics()
            if not metrics.get("session_name"):
                metrics["session_name"] = session_name
            if not metrics.get("session_profile"):
                metrics["session_profile"] = (
                    "extended" if session_name in {"pre_market", "after_hours", "overnight"} else "regular"
                )
            near_buy, near_sell, near_buy_reason, near_sell_reason = self._detect_near_signals(metrics)
            self._log_bar_signal_diagnostics(
                bar=bar,
                signal_name=signal_name,
                near_buy=near_buy,
                near_sell=near_sell,
                near_buy_reason=near_buy_reason,
                near_sell_reason=near_sell_reason,
                metrics=metrics,
            )

            self._update_live_state(
                bar=bar,
                signal_name=signal_name,
                near_buy=near_buy,
                near_sell=near_sell,
                buy_plan=buy_plan,
                sell_plan=sell_plan,
            )
            self._append_live_candle(bar=bar, signal_name=signal_name, metrics=metrics, near_buy=near_buy, near_sell=near_sell)

            trade_allowed = (
                not self._backfill_phase
                and self._trade_ready_time is not None
                and time.time() >= self._trade_ready_time
                and dash.bot_state.get("startup_ready", False)
            )

            executed = False
            if trade_allowed and signal_name == "BUY":
                if not buy_plan.allowed:
                    self._log_trade_plan_blocked("BUY", bar.close, buy_plan, level="INFO")
                elif self.execute_live_trade("BUY", bar.close, sizing_plan=buy_plan):
                    executed = True
                    self._pending_signal = None
                else:
                    self._save_pending_signal("BUY", bar.close, "broker_issue")
            elif trade_allowed and signal_name == "SELL":
                if not sell_plan.allowed:
                    self._log_trade_plan_blocked("SELL", bar.close, sell_plan, level="INFO")
                elif self.execute_live_trade("SELL", bar.close, sizing_plan=sell_plan):
                    executed = True
                    self._pending_signal = None
                else:
                    self._save_pending_signal("SELL", bar.close, "broker_issue")

            if trade_allowed and signal_name in ("BUY", "SELL"):
                dash.append_live_log(
                    level="TRADE",
                    event_type="signal",
                    message=f"Signal {signal_name} detected on {self.live_symbol}",
                    payload={
                        "symbol": self.live_symbol,
                        "signal": signal_name,
                        "price": bar.close,
                        "time": bar.date,
                        "source": bar.source,
                        "executed": executed,
                        "position_state": self.live_position,
                    },
                )

            self.sim_manager.on_live_bar(bar)

            self._last_processed_signature = signature
            self._last_processed_ts = bar.timestamp
            return True

    def on_primary_backfill_bar(self, bar: NormalizedBar):
        self.feed_health.on_primary_bar(bar.timestamp)
        self._apply_backfill_bar(bar)
        self._evaluate_data_health()

    def on_primary_backfill_complete(self):
        snap = self.feed_health.snapshot()
        lag_str = f"{snap.primary_lag_seconds:.0f}s" if snap.primary_lag_seconds is not None else "unknown"
        fresh_str = "FRESH" if not snap.primary_is_stale else "STALE"
        logger.info(f"Primary yfinance backfill complete. Newest bar lag: {lag_str} ({fresh_str})")
        dash.append_live_log(
            level="SUCCESS",
            event_type="provider",
            message=f"Primary yfinance backfill complete (newest bar {lag_str} ago, {fresh_str})",
            payload={"provider": "yfinance", "symbol": self.live_symbol, "lag": lag_str, "stale": snap.primary_is_stale},
        )
        self.live_strategy.update_indicators()
        self._check_indicator_warmup("yfinance backfill")
        self.sim_manager.export_results()

        self._backfill_phase = False
        self._trade_ready_time = time.time() + 5
        logger.info("Backfill phase complete. Trade execution enabled in 5s.")

        self._evaluate_data_health()

    def on_primary_live_bar(self, bar: NormalizedBar):
        self.feed_health.on_primary_bar(bar.timestamp)
        processed = True
        if self.feed_health.active_source == self.market_data_primary:
            processed = self._apply_live_bar(bar)
        self._evaluate_data_health()
        return processed

    def on_primary_status(self, payload: dict):
        last_bar_ts = payload.get("last_bar_ts")
        if last_bar_ts:
            try:
                self.feed_health.on_primary_bar(normalize_timestamp(last_bar_ts))
            except Exception:
                pass

        try:
            consecutive_errors = int(payload.get("consecutive_errors", 0))
        except (TypeError, ValueError):
            consecutive_errors = 0

        if consecutive_errors > 0:
            error_text = payload.get("last_error") or "yfinance polling error"
            self.feed_health.on_primary_error(error_text)
            diag = payload.get("diagnostics")
            hint = ""
            if isinstance(diag, dict):
                stage = diag.get("stage")
                if stage:
                    hint += f" stage={stage}"
                if "raw_row_count" in diag:
                    hint += f" yf_rows={diag.get('raw_row_count')}"
                if "final_row_count" in diag:
                    hint += f" after_clean={diag.get('final_row_count')}"
                if "missing" in diag:
                    hint += f" missing_cols={diag.get('missing')}"
            phase = payload.get("phase")
            level = "ERROR" if phase == "poll_error" else "WARN"
            dash.append_live_log(
                level=level,
                event_type="provider_error",
                message=f"Primary provider: {error_text}{hint}",
                payload={
                    "provider": "yfinance",
                    "consecutive_errors": consecutive_errors,
                    "diagnostics": diag,
                    "last_error": error_text,
                    "phase": phase,
                },
            )

        self._evaluate_data_health()

    def on_broker_historical_data(self, req_id: int, bar):
        if req_id != self.broker_req_id:
            return

        normalized = from_ib_bar(bar, source="broker")
        if self.market_data_primary == "broker":
            self.feed_health.on_primary_bar(normalized.timestamp)
        else:
            self.feed_health.on_fallback_bar(normalized.timestamp)

        self._broker_history_received_bars += 1
        if self.web_api_enabled or not self.broker_backfill_complete:
            self._apply_backfill_bar(normalized)

        self._evaluate_data_health()

    def on_broker_historical_data_end(self, req_id: int, start: str, end: str):
        if req_id != self.broker_req_id:
            return

        snap = self.feed_health.snapshot()
        fallback_lag_str = f"{snap.fallback_lag_seconds:.0f}s" if snap.fallback_lag_seconds is not None else "unknown"
        history_plan = self._broker_history_request_plan()
        received = self._broker_history_received_bars
        logger.info(
            f"Broker fallback backfill complete. {start} to {end} "
            f"(lag: {fallback_lag_str}, received={received})"
        )
        dash.append_live_log(
            level="INFO",
            event_type="provider",
            message=f"Broker fallback backfill complete ({start} to {end}, lag: {fallback_lag_str})",
            payload={
                "provider": "broker",
                "start": start,
                "end": end,
                "lag": fallback_lag_str,
                "received_bars": received,
                "required_bars": history_plan["required_bars"],
                "duration": history_plan["duration"],
            },
        )
        self.broker_backfill_complete = True

        self.live_strategy.update_indicators()
        self._check_indicator_warmup("broker backfill")
        if received < self.live_startup_min_bars:
            dash.append_live_log(
                level="WARN",
                event_type="warmup",
                message=(
                    f"Broker history returned too few bars for startup: "
                    f"{received}/{self.live_startup_min_bars}; will retry on next broker poll"
                ),
                payload={
                    "received_bars": received,
                    "startup_required": self.live_startup_min_bars,
                    "duration": history_plan["duration"],
                    "required_bars": history_plan["required_bars"],
                },
            )
        self.sim_manager.export_results()

        self._evaluate_data_health()

    def on_broker_historical_data_update(self, req_id: int, bar):
        if req_id != self.broker_req_id:
            return

        normalized = from_ib_bar(bar, source="broker")
        if self.market_data_primary == "broker":
            self.feed_health.on_primary_bar(normalized.timestamp)
        else:
            self.feed_health.on_fallback_bar(normalized.timestamp)

        if self.feed_health.active_source == "broker":
            self._apply_live_bar(normalized)

        self._evaluate_data_health()

    def _update_warmup_state(self) -> dict:
        """Refresh startup/full indicator readiness without changing strategy formulas."""
        df = getattr(self.live_strategy, "df", None)
        bar_count = len(df) if df is not None else 0
        startup_required = max(1, int(getattr(self, "live_startup_min_bars", 6)))
        full_required = max(1, int(getattr(self.live_strategy, "min_bars_required", 50)))
        startup_ready = bar_count >= startup_required
        full_ready = bar_count >= full_required

        dash.bot_state["startup_ready"] = startup_ready
        dash.bot_state["indicators_ready"] = startup_ready
        dash.bot_state["indicators_fully_ready"] = full_ready
        dash.bot_state["indicator_bars_loaded"] = bar_count
        dash.bot_state["live_startup_min_bars"] = startup_required
        dash.bot_state["indicator_min_bars_required"] = full_required
        return {
            "bars": bar_count,
            "startup_required": startup_required,
            "full_required": full_required,
            "startup_ready": startup_ready,
            "full_ready": full_ready,
        }

    def _warmup_log_due(self, key: tuple) -> bool:
        now = time.time()
        if key != self._last_warmup_log_key:
            self._last_warmup_log_key = key
            self._last_warmup_log_time = now
            return True
        if now - self._last_warmup_log_time >= self._warmup_log_interval_seconds:
            self._last_warmup_log_time = now
            return True
        return False

    def _check_indicator_warmup(self, source: str):
        """Validate startup readiness separately from full indicator maturity."""
        previous_startup_ready = bool(dash.bot_state.get("startup_ready", False))
        previous_full_ready = bool(dash.bot_state.get("indicators_fully_ready", False))
        state = self._update_warmup_state()
        applied = self._backfill_bars_applied
        payload = {
            "bars": state["bars"],
            "startup_required": state["startup_required"],
            "required": state["full_required"],
            "source": source,
            "applied": applied,
            "startup_ready": state["startup_ready"],
            "full_ready": state["full_ready"],
        }

        if state["full_ready"]:
            if not previous_full_ready:
                logger.info(
                    f"Indicator warm-up OK: {state['bars']}/{state['full_required']} bars after {source} "
                    f"(applied={applied})"
                )
                dash.append_live_log(
                    level="SUCCESS",
                    event_type="warmup",
                    message=f"Indicators fully ready: {state['bars']} bars loaded (need {state['full_required']})",
                    payload=payload,
                )
            return

        if state["startup_ready"]:
            if (not previous_startup_ready) or self._warmup_log_due(("partial", source)):
                logger.info(
                    f"Startup warm-up OK; full indicators still warming: "
                    f"{state['bars']}/{state['full_required']} bars after {source} (applied={applied})"
                )
                dash.append_live_log(
                    level="INFO",
                    event_type="warmup",
                    message=(
                        f"Startup warm-up ready: {state['bars']}/{state['startup_required']} bars; "
                        f"full indicators {state['bars']}/{state['full_required']}"
                    ),
                    payload=payload,
                )
            return

        if self._warmup_log_due(("startup_wait", source)):
            logger.warning(
                f"Startup warm-up incomplete after {source}: "
                f"{state['bars']}/{state['startup_required']} bars "
                f"(full indicators {state['bars']}/{state['full_required']}, applied={applied})"
            )
            dash.append_live_log(
                level="WARN",
                event_type="warmup",
                message=(
                    f"Startup warm-up incomplete: {state['bars']}/{state['startup_required']} bars "
                    f"after {source} (full indicators need {state['full_required']}, applied={applied})"
                ),
                payload=payload,
            )

    def _save_pending_signal(self, action: str, price: float, reason: str):
        """Save a failed signal for retry when broker reconnects."""
        if not dash.bot_state.get("is_live_active", False):
            return
        if not self.client.is_connected:
            self._pending_signal = {
                "action": action,
                "price": price,
                "ts": time.time(),
                "retries": 0,
                "reason": reason,
            }
            logger.warning(f"Signal {action} @ {price} saved for retry (reason: {reason})")
            dash.append_live_log(
                level="WARN",
                event_type="signal_queued",
                message=f"Signal {action} queued for retry when broker reconnects",
                payload={"action": action, "price": price, "reason": reason, "ttl": self.signal_retry_ttl_seconds},
            )

    def _retry_pending_signal(self):
        """Retry a pending signal if broker is back and TTL hasn't expired."""
        if self._pending_signal is None:
            return
        if not self.client.is_connected:
            return

        market_status = self.trading_hours.status()
        if not market_status.is_open:
            now = time.time()
            if now - self._last_pending_session_wait_log >= 60:
                dash.append_live_log(
                    level="INFO",
                    event_type="signal_retry",
                    message=f"Queued {self._pending_signal['action']} signal waiting for trading session",
                    payload=market_status.as_dict(),
                )
                self._last_pending_session_wait_log = now
            self._sync_dashboard_status()
            return

        sig = self._pending_signal
        age = time.time() - sig["ts"]

        if age > self.signal_retry_ttl_seconds:
            logger.warning(f"Pending signal {sig['action']} expired after {age:.0f}s (TTL={self.signal_retry_ttl_seconds}s)")
            dash.append_live_log(
                level="WARN",
                event_type="signal_expired",
                message=f"Queued {sig['action']} signal expired ({age:.0f}s > {self.signal_retry_ttl_seconds}s TTL)",
                payload={"action": sig["action"], "price": sig["price"], "age": round(age)},
            )
            self._pending_signal = None
            return

        sig["retries"] += 1
        logger.info(f"Retrying pending signal: {sig['action']} @ {sig['price']} (attempt {sig['retries']}, age {age:.0f}s)")
        dash.append_live_log(
            level="INFO",
            event_type="signal_retry",
            message=f"Retrying {sig['action']} signal (attempt {sig['retries']}, {age:.0f}s old)",
            payload={"action": sig["action"], "price": sig["price"], "attempt": sig["retries"]},
        )

        action = sig["action"]
        sizing_plan = self._build_live_order_plan(action, sig["price"])
        if not sizing_plan.allowed:
            if sizing_plan.transient:
                logger.info(
                    f"Pending signal {action} waiting on transient condition: {sizing_plan.reason} "
                    f"(retry in <={self.signal_retry_ttl_seconds - int(age)}s)"
                )
                dash.append_live_log(
                    level="INFO",
                    event_type="signal_retry_waiting",
                    message=f"Queued {action} signal waiting: {sizing_plan.reason}",
                    payload={"action": action, "price": sig["price"], **sizing_plan.as_payload()},
                )
                self._sync_dashboard_status()
                return

            logger.warning(f"Pending signal {action} abandoned: {sizing_plan.reason}")
            dash.append_live_log(
                level="WARN",
                event_type="signal_retry_abandoned",
                message=f"Queued {action} signal abandoned: {sizing_plan.reason}",
                payload={"action": action, "price": sig["price"], **sizing_plan.as_payload()},
            )
            self._pending_signal = None
            self._sync_dashboard_status()
            return

        if self.execute_live_trade(action, sig["price"], sizing_plan=sizing_plan):
            logger.info(f"Pending signal {action} executed successfully on retry")
            dash.append_live_log(
                level="SUCCESS",
                event_type="signal_retry_ok",
                message=f"Queued {action} signal executed on retry (attempt {sig['retries']})",
                payload={"action": action, "price": sig["price"], "attempt": sig["retries"]},
            )
            self._pending_signal = None
        elif sig["retries"] >= 5:
            logger.error(f"Pending signal {action} failed after {sig['retries']} retries, giving up")
            dash.append_live_log(
                level="ERROR",
                event_type="signal_retry_failed",
                message=f"Queued {action} signal abandoned after {sig['retries']} retries",
                payload={"action": action, "price": sig["price"], "retries": sig["retries"]},
            )
            self._pending_signal = None

    def execute_live_trade(self, action: str, current_price: float, sizing_plan: Optional[LiveOrderPlan] = None) -> bool:
        mode = self.trading_mode
        snapshot = self.feed_health.snapshot()
        market_status = self.trading_hours.status()

        cooldown = getattr(self, '_order_cooldown_until', 0)
        if time.time() < cooldown:
            remaining = int(cooldown - time.time())
            logger.info(f"Order cooldown active ({remaining}s remaining), skipping {action}")
            return False

        if not market_status.is_open:
            logger.warning(f"*** [{mode}] BLOCKED {action} at {current_price}. Outside configured trading hours. ***")
            dash.append_live_log(
                level="WARN",
                event_type="trade_blocked",
                message=f"Blocked {action}: outside configured trading hours",
                payload={"mode": mode, "action": action, **market_status.as_dict()},
            )
            self._sync_dashboard_status()
            return False

        if not self.client.is_connected:
            logger.warning(f"*** [{mode}] BLOCKED {action} at {current_price}. Broker session is disconnected. ***")
            dash.append_live_log(
                level="ERROR",
                event_type="trade_blocked",
                message=f"Blocked {action}: broker session disconnected",
                payload={"mode": mode, "action": action},
            )
            return False

        if self.web_api_enabled and hasattr(self.client, 'ensure_authenticated'):
            if not self.client.ensure_authenticated():
                logger.warning(f"*** [{mode}] BLOCKED {action} at {current_price}. Pre-trade auth check failed. ***")
                dash.append_live_log(
                    level="ERROR",
                    event_type="trade_blocked",
                    message=f"Blocked {action}: pre-trade auth check failed (gateway not authenticated)",
                    payload={"mode": mode, "action": action},
                )
                return False

        if snapshot.should_pause_live:
            logger.warning(f"*** [{mode}] BLOCKED {action} at {current_price}. Stale-data guard is active. ***")
            dash.append_live_log(
                level="WARN",
                event_type="trade_blocked",
                message=f"Blocked {action} due to stale-data guard",
                payload={"mode": mode, "action": action, "active_source": snapshot.active_source},
            )
            self._sync_dashboard_status()
            return False

        if not dash.bot_state.get("is_live_active", False):
            logger.info(f"*** [{mode}] IGNORED {action} at {current_price}. Bot status is INACTIVE. ***")
            dash.append_live_log(
                level="INFO",
                event_type="trade_ignored",
                message=f"Ignored {action}; live trading is inactive",
                payload={"mode": mode, "action": action},
            )
            return False

        plan = sizing_plan or self._build_live_order_plan(action, current_price)
        if not plan.allowed:
            self._log_trade_plan_blocked(action, current_price, plan)
            return False

        quantity = plan.quantity
        order_notional = plan.order_notional
        logger.warning(f"*** [{mode}] EXECUTING LIVE {action} for {quantity} @ {current_price} ***")

        contract = self.client.get_contract(self.live_symbol)
        order = self.client.create_market_order(action, float(quantity), outside_rth=self.extended_hours)

        if self.client.next_order_id is None:
            logger.error("No valid order ID available from broker session; order not sent")
            dash.append_live_log(
                level="ERROR",
                event_type="trade_error",
                message=f"Failed to send {action} {quantity}; missing broker order id",
                payload={"mode": mode, "action": action, "quantity": quantity},
            )
            return False

        order_id = self.client.next_order_id
        order_accepted = self.client.placeOrder(order_id, contract, order)
        self.client.next_order_id = order_id + 1
        order_preview = getattr(self.client, "_last_order_preview", None)

        if order_accepted:
            broker_order_id = getattr(self.client, "_last_order_id", None)
            tracked_order_id = str(broker_order_id or order_id)
            logger.info(f"--> PLACE_ORDER {action} {quantity} on {contract.symbol} (order_id={tracked_order_id})")
            dash.append_live_log(
                level="SUCCESS",
                event_type="trade_sent",
                message=f"Sent {action} {quantity} {contract.symbol} (order_id={tracked_order_id})",
                payload={
                    "mode": mode,
                    "action": action,
                    "quantity": quantity,
                    "symbol": contract.symbol,
                    "order_id": tracked_order_id,
                    "local_order_id": order_id,
                    "notional": round(order_notional, 2),
                    "sizing": plan.as_payload(),
                    "order_preview": order_preview,
                },
            )
            if not hasattr(self, '_tracked_orders'):
                self._tracked_orders = {}
            self._tracked_orders[tracked_order_id] = {
                "action": action,
                "qty": quantity,
                "symbol": self.live_symbol,
                "price": current_price,
                "notional": order_notional,
                "sizing": plan.as_payload(),
                "local_order_id": order_id,
                "ts": time.time(),
                "order_preview": order_preview,
            }
            self._record_order_sent(action, tracked_order_id, quantity, current_price, order_notional)
            return True
        else:
            self._order_cooldown_until = time.time() + 30
            last_resp = getattr(self.client, '_last_order_response', None)
            resp_summary = str(last_resp)[:300] if last_resp else "no response"
            logger.error(
                f"--> PLACE_ORDER FAILED {action} {quantity} on {contract.symbol}. "
                f"IBKR response: {last_resp}. Cooldown 30s."
            )
            dash.append_live_log(
                level="ERROR",
                event_type="trade_error",
                message=f"Order FAILED: {action} {quantity} {contract.symbol} | IBKR: {resp_summary}",
                payload={
                    "mode": mode, "action": action, "quantity": quantity,
                    "symbol": contract.symbol, "order_id": order_id,
                    "ibkr_response": resp_summary,
                    "order_preview": order_preview,
                },
            )
            return False


    def start(self):
        signal.signal(signal.SIGINT, self._handle_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, self._handle_signal)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, self._handle_signal)

        dash.append_live_log(
            level="INFO",
            event_type="startup",
            message="Bot startup sequence started",
            payload={
                "symbol": self.live_symbol,
                "mode": self.trading_mode,
                "extended_hours": self.extended_hours,
                "trading_hours_enabled": self.trading_hours_enabled,
                "trading_hours_timezone": self.trading_hours.timezone_name,
                "primary_source": self.market_data_primary,
                "stale_threshold": self.stale_after_seconds,
            },
        )
        logger.info(
            f"Config: symbol={self.live_symbol}, mode={self.trading_mode}, "
            f"extended_hours={self.extended_hours}, primary={self.market_data_primary}, "
            f"stale_threshold={self.stale_after_seconds}s"
        )

        dash_thread = threading.Thread(target=dash.start_server, daemon=True)
        dash_thread.start()

        if self.web_api_enabled:
            logger.info(f"Connecting to IBKR Web API at {self.web_api_base_url}...")
            dash.append_live_log(
                level="INFO",
                event_type="broker",
                message=f"Connecting to IBKR Web API at {self.web_api_base_url}...",
                payload={"web_api_url": self.web_api_base_url},
            )
            self.client.connect(host="", port=0, clientId=0)
            connect_payload = {"type": "web", "endpoint": self.web_api_base_url}
        else:
            host = os.getenv("TWS_HOST", "127.0.0.1")
            port = int(os.getenv("TWS_PORT", "4001"))
            client_id = int(os.getenv("CLIENT_ID", "1"))
            logger.info(f"Connecting to TWS IBKR {host}:{port} Client {client_id}")
            self.client.connect(host, port, clientId=client_id)
            api_thread = threading.Thread(target=self.client.run, daemon=True)
            api_thread.start()
            connect_payload = {"type": "tws", "host": host, "port": port, "client_id": client_id}

        initial_timeout = 60.0 if self.web_api_enabled else 5.0
        timeout = initial_timeout
        while not self.client.is_connected and timeout > 0:
            time.sleep(1.0)
            timeout -= 1.0
            if self.web_api_enabled and int(timeout) % 10 == 0 and timeout < initial_timeout:
                logger.info(f"Waiting for gateway authentication... ({int(initial_timeout - timeout)}s elapsed)")
                dash.append_live_log(
                    level="INFO",
                    event_type="broker",
                    message=f"Waiting for gateway authentication... ({int(initial_timeout - timeout)}s elapsed)",
                    payload={},
                )

        if self.client.is_connected:
            self._broker_was_connected = True
            dash.bot_state["broker_connected"] = True
            logger.info("Broker connected successfully. Starting subscriptions...")
            dash.append_live_log(
                level="SUCCESS",
                event_type="broker",
                message="Connected to broker session",
                payload=connect_payload,
            )
            
            if not self.web_api_enabled:
                self.client.reqPositions()
                self.client.reqAccountUpdates(True, "")

            self._request_broker_history(keep_up_to_date=not self.web_api_enabled, reason="startup")
            self._last_broker_poll_time = time.time()
        else:
            logger.error("Could not connect to broker. Broker fallback and order execution are unavailable.")
            dash.append_live_log(
                level="ERROR",
                event_type="broker",
                message="Could not connect to broker session",
                payload=connect_payload,
            )

        if self.yf_provider is not None:
            self.yf_provider.start()
            ext_label = " (extended hours)" if self.extended_hours else " (regular hours only)"
            logger.info(f"Primary market data provider started: yfinance{ext_label}")
            dash.append_live_log(
                level="INFO",
                event_type="provider",
                message=f"Primary market data provider started: yfinance{ext_label}",
                payload={"provider": "yfinance", "symbol": self.live_symbol, "interval": self.live_interval, "prepost": self.extended_hours},
            )

        try:
            while not self.shutdown_event.is_set():
                self._evaluate_data_health()
                self._check_broker_connection()
                self._poll_broker_data()
                self._poll_account_data()
                self._retry_pending_signal()
                self._poll_order_status()
                self._poll_recent_trades()
                time.sleep(1.0)
        except KeyboardInterrupt:
            self._shutdown("KeyboardInterrupt")

        logger.info("Process stopping...")
        os._exit(0)


if __name__ == "__main__":
    bot = TWSBotOrchestrator()
    bot.start()
