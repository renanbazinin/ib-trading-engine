import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

import requests


logger = logging.getLogger(__name__)


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    parse_mode: str = ""
    timeout_seconds: float = 10.0
    notify_order_sent: bool = False
    notify_order_filled: bool = True
    notify_test_button: bool = True


def _fmt_money(value: Any, digits: int = 2) -> str:
    try:
        return f"${float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "$--"


def _safe_abs_float(value: Any) -> float:
    try:
        return abs(float(value))
    except (TypeError, ValueError):
        return 0.0


def _fmt_quantity(value: Any) -> str:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return "--"
    return f"{parsed:.6f}".rstrip("0").rstrip(".")


def _fmt_rsi(value: Any) -> str:
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "--"


def _fmt_pct(value: Any) -> str:
    try:
        return f"{float(value):.3f}%"
    except (TypeError, ValueError):
        return "--"


def format_buy_executed(payload: Dict[str, Any]) -> str:
    symbol = payload.get("symbol", "--")
    price = payload.get("price")
    qty = payload.get("quantity")
    total = payload.get("total")
    rsi = payload.get("rsi")
    order_id = payload.get("order_id", "--")
    status = str(payload.get("status", "FILLED")).upper()
    return "\n".join(
        [
            "🛒 BUY Executed",
            "━━━━━━━━━━━━━━━━━━",
            f"📊 {symbol}",
            f"💰 Price: {_fmt_money(price)}",
            f"📦 Qty: {_fmt_quantity(qty)}",
            f"💵 Total: {_fmt_money(total)}",
            f"📈 RSI: {_fmt_rsi(rsi)}",
            f"🆔 Order: {order_id}",
            f"✅ Status: {status}",
        ]
    )


def format_sell_executed(payload: Dict[str, Any]) -> str:
    symbol = payload.get("symbol", "--")
    price = payload.get("price")
    qty = payload.get("quantity")
    total = payload.get("total")
    rsi = payload.get("rsi")
    order_id = payload.get("order_id", "--")
    status = str(payload.get("status", "FILLED")).upper()
    gross = payload.get("gross_pnl")
    fees = payload.get("fees")
    net = payload.get("net_pnl")
    net_pct = payload.get("net_pnl_pct")
    return "\n".join(
        [
            "💸 SELL Executed",
            "━━━━━━━━━━━━━━━━━━",
            f"📊 {symbol}",
            f"💰 Price: {_fmt_money(price)}",
            f"📦 Qty: {_fmt_quantity(qty)}",
            f"💵 Total: {_fmt_money(total)}",
            f"📈 RSI: {_fmt_rsi(rsi)}",
            "━━━━━━━━━━━━━━━━━━",
            "🟢 P&L",
            f"   Gross: {_fmt_money(gross, 4)}",
            f"   Fees:  -{_fmt_money(_safe_abs_float(fees), 4)}",
            f"   Net:   {_fmt_money(net, 4)} ({_fmt_pct(net_pct)})",
            "━━━━━━━━━━━━━━━━━━",
            f"🆔 Order: {order_id}",
            f"✅ Status: {status}",
        ]
    )


def format_status_report(payload: Dict[str, Any]) -> str:
    timestamp = payload.get("timestamp")
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return "\n".join(
        [
            "🤖 Bot Status Report",
            "━━━━━━━━━━━━━━━━━━",
            f"⏱ Uptime: {payload.get('uptime', '--')}",
            f"📊 Symbol: {payload.get('symbol', '--')}",
            f"📈 Mode: {payload.get('mode', '--')}",
            f"💰 Last Price: {_fmt_money(payload.get('last_price'))}",
            f"📐 RSI: {_fmt_rsi(payload.get('rsi'))}",
            f"📊 Position: {payload.get('position', '--')}",
            "━━━━━━━━━━━━━━━━━━",
            "🟢 Trading Summary",
            f"   Buys:  {int(payload.get('buy_count', 0) or 0)}",
            f"   Sells: {int(payload.get('sell_count', 0) or 0)}",
            f"   Fees:  -{_fmt_money(_safe_abs_float(payload.get('fees', 0)), 4)}",
            f"   Net P&L: {_fmt_money(payload.get('net_pnl', 0), 4)}",
            "━━━━━━━━━━━━━━━━━━",
            f"⏰ {timestamp}",
        ]
    )


class TelegramNotifier:
    def __init__(self, config: TelegramConfig, post_func: Optional[Callable] = None):
        self.config = config
        self._post = post_func or requests.post

    @property
    def is_configured(self) -> bool:
        return bool(self.config.enabled and self.config.bot_token and self.config.chat_id)

    def send_message(self, text: str) -> Dict[str, Any]:
        if not self.config.enabled:
            return {"ok": False, "message": "Telegram notifications are disabled."}
        if not self.config.bot_token:
            return {"ok": False, "message": "TELEGRAM_BOT_TOKEN is not configured."}
        if not self.config.chat_id:
            return {"ok": False, "message": "TELEGRAM_CHAT_ID is not configured."}

        payload = {
            "chat_id": self.config.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if self.config.parse_mode:
            payload["parse_mode"] = self.config.parse_mode

        try:
            response = self._post(
                f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage",
                json=payload,
                timeout=self.config.timeout_seconds,
            )
        except Exception as exc:
            logger.warning(f"Telegram send failed: {exc}")
            return {"ok": False, "message": str(exc)}

        status_code = getattr(response, "status_code", None)
        try:
            body = response.json()
        except Exception:
            body = {}

        if status_code == 200 and body.get("ok", True):
            return {
                "ok": True,
                "message": "Telegram message sent.",
                "status_code": status_code,
                "response": body,
            }

        description = body.get("description") or getattr(response, "text", "")[:300] or "Telegram send failed."
        return {
            "ok": False,
            "message": description,
            "status_code": status_code,
            "response": body,
        }

    def send_message_async(self, text: str) -> Dict[str, Any]:
        if not self.is_configured:
            return self.send_message(text)

        def _send():
            self.send_message(text)

        thread = threading.Thread(target=_send, daemon=True)
        thread.start()
        return {"ok": True, "message": "Telegram message queued."}
