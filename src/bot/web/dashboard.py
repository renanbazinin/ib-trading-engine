import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Deque, Dict, Optional

from flask import Flask, jsonify, redirect, render_template, request, url_for

from core.sim_config_loader import (
    SimulationConfigError,
    get_simulation_config_path,
    parse_and_validate_simulation_config,
)
from core.live_state_store import LiveStateStore

app = Flask(__name__)
logger = logging.getLogger(__name__)
_broker_reauth_handler = None
_telegram_test_handler = None

# Global dictionary representing bot state.
bot_state: Dict[str, Any] = {
    "is_live_active": False,
    "effective_live_active": False,
    "live_pause_guard": False,
    "trading_mode": "PAPER",
    "symbol": "",
    "strategy": "",
    "sim_config_path": "",
    "data_primary_source": "yfinance",
    "data_active_source": "yfinance",
    "data_fallback_source": "broker",
    "data_primary_lag_seconds": None,
    "data_fallback_lag_seconds": None,
    "data_active_lag_seconds": None,
    "data_primary_is_stale": False,
    "data_active_is_stale": False,
    "data_primary_last_bar_ts": None,
    "data_fallback_last_bar_ts": None,
    "data_last_error": "",
    "startup_ready": False,
    "indicators_ready": False,
    "indicators_fully_ready": False,
    "indicator_bars_loaded": 0,
    "indicator_min_bars_required": 50,
    "live_startup_min_bars": 6,
    "pending_signal": None,
    "unsettled_sale_proceeds": 0.0,
    "next_estimated_settlement": None,
    "next_release_at": None,
    "unsettled_sell_fills": [],
}

# These dicts act as in-memory cache updated asynchronously by main.py.
live_account_data = {}
live_positions_data = {}
live_orders_data = []
live_trades_data = []
live_runtime_state = {
    "slot_ledger": {},
    "risk_counters": {},
}

LIVE_CANDLE_BUFFER_MAX = max(50, int(os.getenv("LIVE_CANDLE_BUFFER_MAX", "500")))
LIVE_LOG_BUFFER_MAX = max(100, int(os.getenv("LIVE_LOG_BUFFER_MAX", "1000")))

live_candles_buffer: Deque[Dict[str, Any]] = deque(maxlen=LIVE_CANDLE_BUFFER_MAX)
live_logs_buffer: Deque[Dict[str, Any]] = deque(maxlen=LIVE_LOG_BUFFER_MAX)
_live_buffers_lock = RLock()
_live_state_store: Optional[LiveStateStore] = None
_last_live_state_save_ts = 0.0
LIVE_STATE_SAVE_SECONDS = max(0.25, float(os.getenv("LIVE_STATE_SAVE_SECONDS", "2.0")))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _results_path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "simulation", "results", "latest_sims.json")


def _read_sim_results() -> Dict[str, Any]:
    path = _results_path()
    if not os.path.exists(path):
        return {
            "timestamp": None,
            "summary": {},
            "simulations": [],
            "recent_signals": [],
            "recent_trades": [],
        }

    try:
        with open(path, "r", encoding="utf-8") as file_handle:
            return json.load(file_handle)
    except Exception as exc:
        logger.error(f"Error reading simulation summary: {exc}")
        return {
            "timestamp": None,
            "summary": {},
            "simulations": [],
            "recent_signals": [],
            "recent_trades": [],
            "error": str(exc),
        }


def _parse_limit(raw_value: str, default: int = 30, max_value: int = 200) -> int:
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, max_value))


def _snapshot_live_state() -> Dict[str, Any]:
    with _live_buffers_lock:
        return {
            "bot_state": {
                key: bot_state.get(key)
                for key in (
                    "trading_mode",
                    "symbol",
                    "strategy",
                    "extended_hours",
                    "trading_hours_enabled",
                    "trading_hours_timezone",
                    "market_session",
                    "market_session_open",
                    "market_session_reason",
                    "market_session_now_et",
                    "next_session_open",
                    "next_session_close",
                    "minutes_to_open",
                    "last_price",
                    "last_price_time",
                    "last_price_source",
                    "last_signal",
                    "last_signal_time",
                    "near_buy",
                    "near_sell",
                    "broker_account_id",
                    "ready_to_trade",
                    "startup_ready",
                    "indicators_ready",
                    "indicators_fully_ready",
                    "indicator_bars_loaded",
                    "indicator_min_bars_required",
                    "live_startup_min_bars",
                    "pending_signal",
                    "position_state",
                    "live_symbol_position",
                    "live_position_slots",
                    "live_symbol_exposure",
                    "live_buy_plan",
                    "live_sell_plan",
                    "slot_outlook",
                    "slot_ledger_date",
                    "slot_base_net_liquidation",
                    "daily_buy_orders",
                    "daily_total_orders",
                    "last_order_action",
                    "last_order_status",
                    "last_order_id",
                    "unsettled_sale_proceeds",
                    "next_estimated_settlement",
                    "next_release_at",
                    "unsettled_sell_fills",
                )
                if key in bot_state
            },
            "account": dict(live_account_data),
            "positions": dict(live_positions_data),
            "orders": list(live_orders_data),
            "trades": list(live_trades_data),
            "runtime": dict(live_runtime_state),
            "candles": list(live_candles_buffer),
            "logs": list(live_logs_buffer),
        }


def configure_live_state_store(path: str):
    global _live_state_store
    _live_state_store = LiveStateStore(path)
    bot_state["live_state_path"] = _live_state_store.path
    restore_live_state()


def configure_broker_reauth_handler(handler):
    global _broker_reauth_handler
    _broker_reauth_handler = handler


def configure_telegram_test_handler(handler):
    global _telegram_test_handler
    _telegram_test_handler = handler


def restore_live_state():
    if _live_state_store is None:
        return

    payload = _live_state_store.read()
    if not payload:
        return

    with _live_buffers_lock:
        restored_bot_state = payload.get("bot_state", {})
        if isinstance(restored_bot_state, dict):
            skip_keys = {
                "is_live_active",
                "effective_live_active",
                "trading_mode",
                "symbol",
                "strategy",
                "extended_hours",
                "trading_hours_enabled",
                "trading_hours_timezone",
            }
            bot_state.update({k: v for k, v in restored_bot_state.items() if k not in skip_keys})

        live_account_data.clear()
        if isinstance(payload.get("account"), dict):
            live_account_data.update(payload["account"])

        live_positions_data.clear()
        if isinstance(payload.get("positions"), dict):
            live_positions_data.update(payload["positions"])

        live_orders_data.clear()
        if isinstance(payload.get("orders"), list):
            live_orders_data.extend(payload["orders"][-200:])

        live_trades_data.clear()
        if isinstance(payload.get("trades"), list):
            live_trades_data.extend(payload["trades"][-200:])

        live_runtime_state.clear()
        restored_runtime = payload.get("runtime", {})
        if isinstance(restored_runtime, dict):
            live_runtime_state.update(restored_runtime)
        live_runtime_state.setdefault("slot_ledger", {})
        live_runtime_state.setdefault("risk_counters", {})

        live_candles_buffer.clear()
        for candle in payload.get("candles", [])[-LIVE_CANDLE_BUFFER_MAX:]:
            if isinstance(candle, dict):
                live_candles_buffer.append(candle)

        live_logs_buffer.clear()
        for log in payload.get("logs", [])[-LIVE_LOG_BUFFER_MAX:]:
            if isinstance(log, dict):
                live_logs_buffer.append(log)

    bot_state["live_state_loaded_at"] = _utc_now_iso()


def persist_live_state(force: bool = False):
    global _last_live_state_save_ts
    if _live_state_store is None:
        return
    now = time.time()
    if not force and now - _last_live_state_save_ts < LIVE_STATE_SAVE_SECONDS:
        return
    _last_live_state_save_ts = now
    try:
        _live_state_store.write(_snapshot_live_state())
    except Exception as exc:
        logger.debug(f"Failed to persist live state: {exc}")


def append_live_candle(
    symbol: str,
    interval: str,
    source: str,
    bar_time: str,
    open_price: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    indicators: Optional[Dict[str, Any]] = None,
):
    candle_payload = {
        "symbol": symbol,
        "interval": interval,
        "source": source,
        "time": bar_time,
        "open": float(open_price),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "volume": float(volume),
        "indicators": indicators or {},
    }

    with _live_buffers_lock:
        if live_candles_buffer and live_candles_buffer[-1].get("time") == bar_time and live_candles_buffer[-1].get("source") == source:
            live_candles_buffer[-1] = candle_payload
        else:
            live_candles_buffer.append(candle_payload)
    persist_live_state()


def append_live_log(level: str, event_type: str, message: str, payload: Optional[Dict[str, Any]] = None):
    with _live_buffers_lock:
        live_logs_buffer.append(
            {
                "time": _utc_now_iso(),
                "level": str(level or "INFO").upper(),
                "event_type": str(event_type or "info"),
                "message": str(message or ""),
                "payload": payload or {},
            }
        )
    persist_live_state()


def update_live_orders(orders):
    with _live_buffers_lock:
        live_orders_data.clear()
        if isinstance(orders, list):
            live_orders_data.extend(orders[-200:])
    persist_live_state(force=True)


def update_live_trades(trades):
    with _live_buffers_lock:
        live_trades_data.clear()
        if isinstance(trades, list):
            live_trades_data.extend(trades[-200:])
    persist_live_state(force=True)


@app.route("/")
def index():
    return redirect(url_for("live_page"))


@app.route("/live")
def live_page():
    return render_template("live.html", page="live")


@app.route("/simulations")
def simulations_page():
    return render_template("simulations.html", page="simulations")


@app.route("/config")
def config_page():
    return render_template("config.html", page="config")


@app.route("/api/bot/status")
def api_bot_status():
    with _live_buffers_lock:
        return jsonify(dict(bot_state))


@app.route("/api/broker/status")
def api_broker_status():
    with _live_buffers_lock:
        return jsonify({
            "connected": bot_state.get("broker_connected", False),
            "web_api_enabled": bot_state.get("web_api_enabled", False),
            "web_api_url": bot_state.get("web_api_url", ""),
            "account_id": bot_state.get("broker_account_id", ""),
            "last_tickle_ok": bot_state.get("last_tickle_ok", None),
            "last_tickle_error": bot_state.get("last_tickle_error", None),
            "last_successful_tickle_ts": bot_state.get("last_successful_tickle_ts", None),
            "consecutive_tickle_failures": bot_state.get("consecutive_tickle_failures", 0),
            "gateway_authenticated": bot_state.get("gateway_authenticated", False),
            "reauth_in_progress": bot_state.get("broker_reauth_in_progress", False),
            "last_reauth_result": bot_state.get("last_broker_reauth_result", None),
        })


@app.route("/api/broker/reauthenticate", methods=["POST"])
def api_broker_reauthenticate():
    if _broker_reauth_handler is None:
        return jsonify({
            "ok": False,
            "message": "Broker reauthentication is not available until the bot is fully initialized.",
        }), 503

    try:
        result = _broker_reauth_handler()
    except Exception as exc:
        logger.exception("Broker reauthentication handler failed")
        return jsonify({"ok": False, "message": str(exc)}), 500

    status_code = 200 if result.get("ok") else 409
    return jsonify(result), status_code


@app.route("/api/notifications/telegram/test", methods=["POST"])
def api_telegram_test():
    if _telegram_test_handler is None:
        return jsonify({
            "ok": False,
            "message": "Telegram test notification is not available until the bot is fully initialized.",
        }), 503

    try:
        result = _telegram_test_handler()
    except Exception as exc:
        logger.exception("Telegram test notification handler failed")
        return jsonify({"ok": False, "message": str(exc)}), 500

    status_code = 200 if result.get("ok") else 409
    return jsonify(result), status_code


@app.route("/api/bot/toggle", methods=["POST"])
def api_bot_toggle():
    payload = request.get_json(silent=True) or {}
    turning_on = not bot_state["is_live_active"]
    if turning_on and str(bot_state.get("trading_mode", "")).upper() == "LIVE":
        if payload.get("confirmation") != "LIVE":
            return jsonify({
                "ok": False,
                "error": "LIVE confirmation is required to enable trading.",
            }), 400

    with _live_buffers_lock:
        bot_state["is_live_active"] = not bot_state["is_live_active"]
        state = "ACTIVE" if bot_state["is_live_active"] else "INACTIVE"
        response = dict(bot_state)
    logger.warning(f"*** Dashboard user manually toggled Live Trading: {state} ***")
    return jsonify(response)


@app.route("/api/live/positions")
def api_live_positions():
    with _live_buffers_lock:
        return jsonify(list(live_positions_data.values()))


@app.route("/api/live/account")
def api_live_account():
    with _live_buffers_lock:
        return jsonify(dict(live_account_data))


@app.route("/api/live/orders")
def api_live_orders():
    with _live_buffers_lock:
        return jsonify({"orders": list(live_orders_data)})


@app.route("/api/live/trades")
def api_live_trades():
    with _live_buffers_lock:
        return jsonify({"trades": list(live_trades_data)})


@app.route("/api/live/state")
def api_live_state():
    return jsonify(_snapshot_live_state())


@app.route("/api/live/candles")
def api_live_candles():
    limit = _parse_limit(request.args.get("limit", "240"), default=240, max_value=LIVE_CANDLE_BUFFER_MAX)
    with _live_buffers_lock:
        candles = list(live_candles_buffer)[-limit:]
    return jsonify({"candles": candles, "limit": limit, "buffer_max": LIVE_CANDLE_BUFFER_MAX})


@app.route("/api/live/logs")
def api_live_logs():
    limit = _parse_limit(request.args.get("limit", "300"), default=300, max_value=LIVE_LOG_BUFFER_MAX)
    level_raw = str(request.args.get("level", "")).strip()
    event_type = str(request.args.get("event_type", "")).strip().lower()
    query = str(request.args.get("q", "")).strip().lower()
    with _live_buffers_lock:
        all_logs = list(live_logs_buffer)

    logs = list(all_logs)

    levels = {
        token.strip().upper()
        for token in level_raw.split(",")
        if token.strip()
    }
    if levels:
        logs = [entry for entry in logs if str(entry.get("level", "INFO")).upper() in levels]

    if event_type:
        logs = [entry for entry in logs if str(entry.get("event_type", "")).lower() == event_type]

    if query:
        logs = [entry for entry in logs if query in str(entry.get("message", "")).lower()]

    filtered_logs = logs[-limit:]
    event_types = sorted(
        {
            str(entry.get("event_type", "")).strip()
            for entry in all_logs
            if str(entry.get("event_type", "")).strip()
        }
    )

    return jsonify(
        {
            "logs": filtered_logs,
            "limit": limit,
            "buffer_max": LIVE_LOG_BUFFER_MAX,
            "event_type": event_type or None,
            "levels": sorted(levels) if levels else None,
            "q": query or None,
            "event_types": event_types,
        }
    )


@app.route("/api/simulations")
def api_simulations():
    return jsonify(_read_sim_results())


@app.route("/api/simulations/recent-signals")
def api_sim_recent_signals():
    limit = _parse_limit(request.args.get("limit", "30"))
    payload = _read_sim_results()
    return jsonify({"events": payload.get("recent_signals", [])[:limit], "limit": limit})


@app.route("/api/simulations/recent-trades")
def api_sim_recent_trades():
    limit = _parse_limit(request.args.get("limit", "30"))
    payload = _read_sim_results()
    return jsonify({"events": payload.get("recent_trades", [])[:limit], "limit": limit})


@app.route("/api/config/simulations", methods=["GET"])
def api_get_simulation_config():
    config_path = bot_state.get("sim_config_path") or get_simulation_config_path()
    if not os.path.exists(config_path):
        return jsonify({"error": f"Config file not found: {config_path}"}), 404

    try:
        with open(config_path, "r", encoding="utf-8") as file_handle:
            text = file_handle.read()
            parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return jsonify({"error": f"Config file JSON is invalid: {exc}", "path": config_path}), 500

    return jsonify({
        "path": config_path,
        "config": parsed,
        "config_text": text,
    })


@app.route("/api/config/simulations", methods=["POST"])
def api_save_simulation_config():
    payload = request.get_json(silent=True) or {}
    config_text = str(payload.get("config_text", "")).strip()
    if not config_text:
        return jsonify({"error": "config_text is required"}), 400

    try:
        parsed = json.loads(config_text)
    except json.JSONDecodeError as exc:
        return jsonify({"error": f"Invalid JSON: {exc}"}), 400

    try:
        parse_and_validate_simulation_config(parsed)
    except SimulationConfigError as exc:
        return jsonify({"error": str(exc)}), 400

    config_path = bot_state.get("sim_config_path") or get_simulation_config_path()
    os.makedirs(os.path.dirname(config_path), exist_ok=True)

    try:
        with open(config_path, "w", encoding="utf-8") as file_handle:
            json.dump(parsed, file_handle, indent=2)
            file_handle.write("\n")
    except Exception as exc:
        logger.error(f"Failed to save simulation config: {exc}")
        return jsonify({"error": "Failed to save config"}), 500

    return jsonify({
        "ok": True,
        "path": config_path,
        "message": "Simulation config saved. Restart the bot process to apply changes.",
    })


def update_live_account(key: str, val: str, currency: str, account_name: str):
    relevant_keys = ["NetLiquidation", "AvailableFunds", "TotalCashValue", "BuyingPower", "GrossPositionValue", "SettledCash"]
    if key in relevant_keys:
        with _live_buffers_lock:
            live_account_data[key] = {
                "value": round(float(val), 2) if val else 0.0,
                "currency": currency,
                "account": account_name,
            }
        persist_live_state()


def update_live_position(account: str, contract, position: float, avg_cost: float):
    with _live_buffers_lock:
        if position != 0:
            key = f"{account}_{contract.symbol}_{contract.secType}"
            live_positions_data[key] = {
                "account": account,
                "symbol": contract.symbol,
                "secType": contract.secType,
                "position": position,
                "avgCost": round(avg_cost, 2) if avg_cost else 0.0,
            }
        else:
            key = f"{account}_{contract.symbol}_{contract.secType}"
            live_positions_data.pop(key, None)
    persist_live_state()


def start_server(host: str = "0.0.0.0", port: Optional[int] = None):
    """Launch Flask in a background thread from main.py."""
    if port is None:
        raw_port = os.getenv("DASHBOARD_PORT", "5050")
        try:
            port = int(raw_port)
        except ValueError:
            logger.warning(f"Invalid DASHBOARD_PORT='{raw_port}'. Falling back to 5050.")
            port = 5050

    app.run(host=host, port=port, debug=False, use_reloader=False)
