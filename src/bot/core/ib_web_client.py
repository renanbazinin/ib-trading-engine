import logging
import os
import ssl
import threading
import time
import requests
import urllib3
from requests.adapters import HTTPAdapter
from typing import Callable, Dict, List, Any, Optional
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


def _us_equity_wants_outside_rth_flag(extended_hours_config: bool) -> bool:
    """Mon–Fri only, True when local NY time is outside 9:30–16:00 ET (pre/post/overnight)."""
    if not extended_hours_config:
        return False
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    rth_open, rth_close = 9 * 60 + 30, 16 * 60
    return not (rth_open <= mins < rth_close)


def _float_field(val) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning(f"Invalid float value for {name}='{raw}'. Using default {default}.")
        return default


def is_us_equity_outside_regular_hours(extended_hours_config: bool) -> bool:
    return _us_equity_wants_outside_rth_flag(extended_hours_config)


class _NoSNIAdapter(HTTPAdapter):
    """IBKR Client Portal Gateway's Java TLS rejects SNI hostnames that don't
    match its self-signed certificate (issued for 'localhost'). This adapter
    patches the SSL context so wrap_socket never sends the container hostname
    (e.g. 'ib_web_gateway') as the SNI value."""

    def __init__(self, *args, **kwargs):
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._ctx
        kwargs["assert_hostname"] = False
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        proxy_kwargs["ssl_context"] = self._ctx
        proxy_kwargs["assert_hostname"] = False
        return super().proxy_manager_for(proxy, **proxy_kwargs)

    def send(self, request, *args, **kwargs):
        from urllib3.util.ssl_ import create_urllib3_context
        orig_wrap = self._ctx.wrap_socket

        def _patched_wrap(sock, server_side=False, do_handshake_on_connect=True,
                          suppress_ragged_eofs=True, server_hostname=None):
            return orig_wrap(
                sock,
                server_side=server_side,
                do_handshake_on_connect=do_handshake_on_connect,
                suppress_ragged_eofs=suppress_ragged_eofs,
                server_hostname=None,
            )

        self._ctx.wrap_socket = _patched_wrap
        try:
            return super().send(request, *args, **kwargs)
        finally:
            self._ctx.wrap_socket = orig_wrap

_DEFAULT_HEALTH_CHECK_INTERVAL = 30
_DEFAULT_RECONNECT_INTERVAL = 60
_DEFAULT_RECONNECT_MAX_ATTEMPTS = 10
_TICKLE_INTERVAL = 45

# IBKR may surface confirmation prompts before placing an order. Most are benign
# (e.g. "outside regular trading hours", "no market data subscription"), but a
# subset signal real risk and must NEVER be auto-confirmed by the bot. If any of
# the substrings below appears in a confirmation message, we refuse to reply
# 'confirmed' and the order is rejected so a human can review it manually.
_MAJOR_ORDER_WARNING_KEYWORDS = (
    "margin",
    "buying power",
    "free riding",
    "free-riding",
    "good faith",
    "good-faith",
    "wash sale",
    "pattern day",
    "day trader",
    "day-trader",
    "pdt",
    "exceeds",
    "exceed your",
    "exceed the",
    "loss",
    "large in size",
    "large order",
    "size limit",
    "trading limit",
    "circuit breaker",
    "halted",
    "restricted",
    "violation",
    "insufficient",
)


def _detect_major_order_warnings(messages):
    """Return the list of message strings that contain a major-warning keyword."""
    flagged = []
    for raw in messages or []:
        if not isinstance(raw, str):
            continue
        lower = raw.lower()
        for keyword in _MAJOR_ORDER_WARNING_KEYWORDS:
            if keyword in lower:
                flagged.append(raw)
                break
    return flagged


class IBWebClient:
    def __init__(self, base_url: str = "https://127.0.0.1:5000/v1/api"):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.verify = False
        self.session.mount("https://", _NoSNIAdapter())

        self.account_id: Optional[str] = None
        self.is_connected = False
        self.next_order_id = 1

        self._consecutive_tickle_failures = 0
        self._max_tickle_failures = 3
        self._last_tickle_error: Optional[str] = None
        self._last_successful_tickle: float = 0.0
        self._last_order_response: Optional[dict] = None
        self._last_order_preview: Optional[dict] = None
        self._last_order_id: Optional[str] = None
        self._order_preview_enabled = os.getenv("WEB_API_ORDER_PREVIEW_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
        self._ext_hours_buy_limit_pad_pct = _env_float("EXT_HOURS_BUY_LIMIT_PAD_PCT", 0.001)
        self._ext_hours_sell_limit_pad_pct = _env_float("EXT_HOURS_SELL_LIMIT_PAD_PCT", 0.001)
        self._ext_hours_last_price_pad_pct = _env_float("EXT_HOURS_LAST_PRICE_PAD_PCT", 0.001)
        self._was_connected = False
        self._reconnect_enabled = True
        self._reconnect_max_attempts = _DEFAULT_RECONNECT_MAX_ATTEMPTS
        self._reconnect_interval = _DEFAULT_RECONNECT_INTERVAL
        self._health_check_interval = _DEFAULT_HEALTH_CHECK_INTERVAL

        self.on_connection_lost: Optional[Callable] = None
        self.on_connection_restored: Optional[Callable] = None

        self.callbacks: Dict[str, List[Callable]] = {
            "historicalData": [],
            "historicalDataEnd": [],
            "historicalDataUpdate": [],
            "position": [],
            "positionEnd": [],
            "updateAccountValue": [],
        }

        self._poll_thread = None
        self._stop_event = threading.Event()
        self._conid_cache = {}

    def configure_reconnect(
        self,
        enabled: bool = True,
        max_attempts: int = _DEFAULT_RECONNECT_MAX_ATTEMPTS,
        interval: int = _DEFAULT_RECONNECT_INTERVAL,
        health_check_interval: int = _DEFAULT_HEALTH_CHECK_INTERVAL,
    ):
        self._reconnect_enabled = enabled
        self._reconnect_max_attempts = max_attempts
        self._reconnect_interval = interval
        self._health_check_interval = health_check_interval

    def register_callback(self, event_name: str, callback: Callable):
        if event_name in self.callbacks:
            self.callbacks[event_name].append(callback)
        else:
            logger.warning(f"Unsupported event for routing: {event_name}")

    def connect(self, host: str, port: int, clientId: int):
        """Check if the Gateway is authenticated and start the heartbeat/polling loop."""
        logger.info(f"Checking IBKR Web API authentication at {self.base_url}...")
        try:
            status = self._check_auth_status()
            if status and status.get("authenticated", False):
                self._mark_connected()
                self._fetch_initial_data()
            elif status is not None:
                logger.warning("Web API reachable but NOT authenticated. Please log in via the Gateway UI.")
            else:
                logger.warning("Failed to reach Web API. Will keep retrying in background...")
        except Exception as e:
            logger.warning(f"Initial Web API connect error: {e}. Will keep retrying in background...")
        self._start_polling()

    def _mark_connected(self):
        was = self.is_connected
        self.is_connected = True
        self._was_connected = True
        self._consecutive_tickle_failures = 0
        if not was:
            logger.info("Connected and Authenticated to IBKR Web API")
            if self.on_connection_restored:
                try:
                    self.on_connection_restored()
                except Exception as e:
                    logger.error(f"Error in on_connection_restored callback: {e}")

    def _mark_disconnected(self):
        was = self.is_connected
        self.is_connected = False
        if was:
            logger.warning("IBKR Web API session lost")
            if self.on_connection_lost:
                try:
                    self.on_connection_lost()
                except Exception as e:
                    logger.error(f"Error in on_connection_lost callback: {e}")

    def _check_auth_status(self) -> Optional[dict]:
        """Call /iserver/auth/status and return the parsed JSON, or None on error."""
        try:
            resp = self.session.get(f"{self.base_url}/iserver/auth/status", timeout=10)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401:
                return {"authenticated": False, "competing": False, "connected": False}
            logger.warning(f"Auth status returned HTTP {resp.status_code}")
            return {"authenticated": False, "connected": False, "http_status": resp.status_code}
        except Exception as e:
            logger.warning(f"Auth status check failed: {e}")
        return None

    def _try_reauthenticate(self) -> bool:
        """Ask IBeam/Gateway to reauthenticate. Returns True if session is restored."""
        logger.info("Attempting reauthentication via /iserver/reauthenticate...")
        try:
            resp = self.session.post(
                f"{self.base_url}/iserver/reauthenticate?force=true", timeout=15
            )
            logger.info(f"Reauthenticate response: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"Reauthenticate request failed: {e}")
            return False

        for attempt in range(15):
            time.sleep(2)
            status = self._check_auth_status()
            if status and status.get("authenticated", False):
                logger.info("Reauthentication succeeded")
                return True
            if self._stop_event.is_set():
                return False
        logger.warning("Reauthentication did not succeed within 30s")
        return False

    def request_reauthentication(self) -> dict:
        """Manually trigger a gateway reauthentication attempt for dashboard use."""
        try:
            status = self._check_auth_status()
            if status and status.get("authenticated", False):
                self._mark_connected()
                self._fetch_initial_data()
                return {
                    "ok": True,
                    "authenticated": True,
                    "message": "Gateway is already authenticated.",
                    "account_id": self.account_id,
                }

            restored = self._try_reauthenticate()
            if restored:
                self._mark_connected()
                self._fetch_initial_data()
                return {
                    "ok": True,
                    "authenticated": True,
                    "message": "Gateway reauthentication succeeded.",
                    "account_id": self.account_id,
                }

            final_status = self._check_auth_status()
            authenticated = bool(final_status and final_status.get("authenticated", False))
            if authenticated:
                self._mark_connected()
                self._fetch_initial_data()
                return {
                    "ok": True,
                    "authenticated": True,
                    "message": "Gateway authentication recovered.",
                    "account_id": self.account_id,
                }

            self._mark_disconnected()
            self._last_tickle_error = "manual_reauth_failed"
            return {
                "ok": False,
                "authenticated": False,
                "message": "Gateway reauthentication did not complete. Approve the IBKR Mobile prompt or use the Gateway UI.",
                "auth_status": final_status or {},
            }
        except Exception as exc:
            self._last_tickle_error = str(exc)[:200]
            logger.warning(f"Manual reauthentication failed: {exc}")
            return {
                "ok": False,
                "authenticated": False,
                "message": f"Gateway reauthentication request failed: {exc}",
            }

    def _fetch_initial_data(self):
        try:
            accounts_res = self.session.get(f"{self.base_url}/portfolio/accounts", timeout=10)
            if accounts_res.status_code == 200:
                accounts = accounts_res.json()
                if accounts:
                    self.account_id = accounts[0]["id"]
                    logger.info(f"Using account: {self.account_id}")
        except Exception as e:
            logger.error(f"Error fetching initial data: {e}")

    def _start_polling(self):
        if self._poll_thread is None or not self._poll_thread.is_alive():
            self._stop_event.clear()
            self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._poll_thread.start()

    def stop(self):
        self._stop_event.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=10)

    def _poll_loop(self):
        """Main loop: tickle for keepalive, monitor health, reconnect, poll data."""
        last_health_check = 0.0

        while not self._stop_event.is_set():
            try:
                now = time.time()

                self._tickle()

                if now - last_health_check >= self._health_check_interval:
                    self._health_check()
                    last_health_check = now

                if self.is_connected:
                    self._poll_positions()
                    self._poll_account_summary()

            except Exception as e:
                logger.error(f"Error in Web API poll loop: {e}")

            self._stop_event.wait(timeout=_TICKLE_INTERVAL)

    def ensure_authenticated(self, max_age: float = 60.0) -> bool:
        """Return True if we're confident the session is alive.

        Skips the network call if a background tickle succeeded within
        *max_age* seconds -- avoids an extra round-trip on every trade.
        """
        if not self.is_connected:
            return False

        if self._consecutive_tickle_failures == 0 and (time.time() - self._last_successful_tickle) < max_age:
            return True

        try:
            resp = self.session.get(f"{self.base_url}/tickle", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                authenticated = data.get("iserver", {}).get("authStatus", {}).get("authenticated", False)
                if authenticated:
                    self._consecutive_tickle_failures = 0
                    self._last_tickle_error = None
                    self._last_successful_tickle = time.time()
                    return True
                else:
                    logger.warning("Pre-trade auth check: gateway not authenticated")
                    self._last_tickle_error = "gateway_not_authenticated"
                    return False
            return False
        except Exception as e:
            logger.warning(f"Pre-trade auth check failed: {e}")
            return False

    def _tickle(self):
        """Keep the session alive and track failures."""
        try:
            resp = self.session.get(f"{self.base_url}/tickle", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                self._consecutive_tickle_failures = 0
                self._last_tickle_error = None
                authenticated = data.get("iserver", {}).get("authStatus", {}).get("authenticated", False)
                if authenticated:
                    self._last_successful_tickle = time.time()
                if not authenticated:
                    session_info = data.get("session", "")
                    logger.warning(f"Tickle OK but not authenticated (session={session_info})")
                    self._last_tickle_error = "gateway_not_authenticated"
                    if self.is_connected:
                        self._mark_disconnected()
            elif resp.status_code in (401, 404):
                self._last_tickle_error = f"gateway_not_authenticated (HTTP {resp.status_code})"
                if self._consecutive_tickle_failures == 0:
                    logger.info(f"Gateway not yet authenticated (HTTP {resp.status_code})")
            else:
                self._consecutive_tickle_failures += 1
                self._last_tickle_error = f"HTTP {resp.status_code}"
                logger.warning(f"Tickle HTTP {resp.status_code} (failure {self._consecutive_tickle_failures}/{self._max_tickle_failures})")
        except Exception as e:
            self._consecutive_tickle_failures += 1
            self._last_tickle_error = str(e)[:200]
            logger.warning(f"Tickle failed: {e} (failure {self._consecutive_tickle_failures}/{self._max_tickle_failures})")

        if self._consecutive_tickle_failures >= self._max_tickle_failures and self.is_connected:
            self._mark_disconnected()

    def _health_check(self):
        """Periodic deep health check. Triggers reconnect if session is lost."""
        status = self._check_auth_status()
        if status and status.get("authenticated", False):
            if not self.is_connected:
                self._mark_connected()
                self._fetch_initial_data()
            return

        if self.is_connected:
            self._mark_disconnected()

        if not self._reconnect_enabled or not self._was_connected:
            return

        self._attempt_reconnect()

    def _attempt_reconnect(self):
        """Try to restore the session via reauthentication."""
        logger.info("Starting reconnection sequence...")
        for attempt in range(1, self._reconnect_max_attempts + 1):
            if self._stop_event.is_set():
                return

            logger.info(f"Reconnect attempt {attempt}/{self._reconnect_max_attempts}")

            status = self._check_auth_status()
            if status and status.get("authenticated", False):
                self._mark_connected()
                self._fetch_initial_data()
                return

            if self._try_reauthenticate():
                self._mark_connected()
                self._fetch_initial_data()
                return

            if attempt < self._reconnect_max_attempts:
                logger.info(f"Waiting {self._reconnect_interval}s before next reconnect attempt...")
                self._stop_event.wait(timeout=self._reconnect_interval)

        logger.error(
            f"Failed to reconnect after {self._reconnect_max_attempts} attempts. "
            "IBeam should handle re-authentication; the bot will resume when the session recovers."
        )

    def _poll_positions(self):
        if not self.account_id:
            return
        try:
            res = self.session.get(
                f"{self.base_url}/portfolio/{self.account_id}/positions", timeout=10
            )
        except Exception as e:
            logger.debug(f"Position poll failed: {e}")
            return

        if res.status_code == 200:
            positions = res.json()
            for pos in positions:
                symbol = pos.get("symbol")

                class MockContract:
                    def __init__(self, s):
                        self.symbol = s
                        self.secType = "STK"

                contract = MockContract(symbol)
                for cb in self.callbacks["position"]:
                    cb(
                        self.account_id,
                        contract,
                        float(pos.get("position", 0)),
                        float(pos.get("mktPrice", 0)),
                    )

            for cb in self.callbacks["positionEnd"]:
                cb()

    def _poll_account_summary(self):
        if not self.account_id:
            return
        try:
            res = self.session.get(
                f"{self.base_url}/portfolio/{self.account_id}/ledger", timeout=10
            )
        except Exception as e:
            logger.debug(f"Account summary poll failed: {e}")
            return

        if res.status_code == 200:
            ledger = res.json()
            base = ledger.get("BASE", {})
            for key, val in base.items():
                for cb in self.callbacks["updateAccountValue"]:
                    cb(key, str(val), "USD", self.account_id)

    def get_conid(self, symbol: str) -> Optional[int]:
        if symbol in self._conid_cache:
            return self._conid_cache[symbol]

        logger.info(f"Resolving conid for {symbol}...")
        try:
            res = self.session.post(
                f"{self.base_url}/iserver/secdef/search",
                json={"symbol": symbol, "name": False, "secType": "STK"},
                timeout=10,
            )
        except Exception as e:
            logger.error(f"Failed to resolve conid for {symbol}: {e}")
            return None

        if res.status_code == 200:
            results = res.json()
            if results and isinstance(results, list):
                for entry in results:
                    if entry.get("symbol") == symbol:
                        conid = entry.get("conid")
                        self._conid_cache[symbol] = conid
                        logger.info(f"Resolved {symbol} to conid: {conid}")
                        return conid
            logger.error(f"Symbol {symbol} not found in search results.")
        else:
            logger.error(f"Failed to search for symbol {symbol}: {res.text}")
        return None

    def placeOrder(self, orderId: int, contract: Any, order: Any) -> bool:
        if not self.is_connected:
            logger.error("Cannot place order: Web API session is not connected")
            return False
        if not self.account_id:
            logger.error("No account ID for placing order")
            return False

        conid = self.get_conid(contract.symbol)
        if not conid:
            return False

        conid_int = int(conid)
        qty = int(order.totalQuantity)
        action = order.action
        extended_cfg = bool(getattr(order, "outsideRth", False))
        want_orth = _us_equity_wants_outside_rth_flag(extended_cfg)

        if want_orth and getattr(order, "orderType", "MKT") == "MKT":
            snap = self.get_snapshot(contract.symbol)
            last = _float_field(snap.get("31") if snap else None)
            ask = _float_field(snap.get("86") if snap else None)
            bid = _float_field(snap.get("84") if snap else None)
            if last is None:
                logger.error("Cannot place extended-hours order: no snapshot price. Enable market data or wait for a quote.")
                self._last_order_response = {"error": "no snapshot for extended hours LMT"}
                return False
            if action == "BUY":
                reference_price = ask if ask is not None else last
                pad_pct = self._ext_hours_buy_limit_pad_pct if ask is not None else self._ext_hours_last_price_pad_pct
                limit_price = round(reference_price * (1.0 + pad_pct), 2)
            else:
                reference_price = bid if bid is not None else last
                pad_pct = self._ext_hours_sell_limit_pad_pct if bid is not None else self._ext_hours_last_price_pad_pct
                limit_price = round(reference_price * (1.0 - pad_pct), 2)
            order_dict = {
                "acctId": self.account_id,
                "conid": conid_int,
                "secType": f"{conid_int}:STK",
                "orderType": "LMT",
                "price": limit_price,
                "listingExchange": "SMART",
                "outsideRTH": True,
                "side": action,
                "ticker": contract.symbol,
                "tif": "DAY",
                "quantity": qty,
            }
            logger.info(
                f"Extended hours: submitting marketable LMT {action} {qty} @ {limit_price} "
                f"(outsideRTH=True, reference={reference_price}, pad={pad_pct:.4%})"
            )
        else:
            order_dict = {
                "acctId": self.account_id,
                "conid": conid_int,
                "secType": f"{conid_int}:STK",
                "orderType": order.orderType,
                "listingExchange": "SMART",
                "outsideRTH": False,
                "side": action,
                "ticker": contract.symbol,
                "tif": "DAY",
                "quantity": qty,
            }
        payload = {"orders": [order_dict]}

        if self._order_preview_enabled:
            self._last_order_preview = self.preview_order(payload)

        logger.info(f"Placing Web API order: {payload}")
        try:
            self._last_order_id = None
            res = self.session.post(
                f"{self.base_url}/iserver/account/{self.account_id}/orders",
                json=payload,
                timeout=15,
            )
            response_data = res.json() if res.status_code == 200 else None
            logger.info(f"Order response (HTTP {res.status_code}): {response_data or res.text}")

            if res.status_code != 200:
                logger.error(f"Order request failed (HTTP {res.status_code}): {res.text}")
                self._last_order_response = {"error": res.text, "status_code": res.status_code}
                return False

            self._last_order_response = response_data
            return self._handle_order_response(response_data)

        except Exception as e:
            logger.error(f"Failed to place order (exception): {e}")
            return False

    def _handle_order_response(self, response_data, depth: int = 0) -> bool:
        """Process IBKR order response, handling confirmation prompts up to 3 levels deep."""
        if depth > 3:
            logger.error("Order confirmation loop exceeded 3 rounds, aborting")
            return False

        if not response_data:
            logger.error("Empty order response from IBKR")
            return False

        items = response_data if isinstance(response_data, list) else [response_data]

        for item in items:
            if not isinstance(item, dict):
                continue

            if "order_id" in item or "order_status" in item:
                oid = item.get("order_id", "?")
                ost = item.get("order_status", "?")
                if oid not in (None, "?"):
                    self._last_order_id = str(oid)
                logger.info(f"Order confirmed by IBKR: order_id={oid}, status={ost}")
                return True

            if "error" in item:
                logger.error(f"IBKR order error: {item['error']}")
                return False

        reply_id = None
        messages = []
        for item in items:
            if isinstance(item, dict) and "id" in item:
                reply_id = item["id"]
                messages = item.get("message", [])
                break

        if not reply_id:
            logger.warning(f"Unexpected order response format (no reply id, no order_id): {response_data}")
            for item in items:
                if isinstance(item, dict) and item.get("order_id"):
                    return True
            return False

        msg_text = "; ".join(messages) if messages else "(no message)"
        logger.info(f"IBKR order confirmation required (reply_id={reply_id}): {msg_text}")

        flagged = _detect_major_order_warnings(messages)
        if flagged:
            logger.error(
                f"IBKR confirmation prompt contains MAJOR warnings; refusing to auto-confirm. "
                f"reply_id={reply_id} flagged={flagged}"
            )
            self._last_order_response = {
                "error": "major_warning_refused",
                "reply_id": reply_id,
                "messages": list(messages),
                "flagged": flagged,
            }
            return False

        try:
            confirm_res = self.session.post(
                f"{self.base_url}/iserver/reply/{reply_id}",
                json={"confirmed": True},
                timeout=15,
            )
            logger.info(f"Confirmation reply (HTTP {confirm_res.status_code}): {confirm_res.text[:500]}")

            if confirm_res.status_code == 200:
                confirm_data = confirm_res.json()
                self._last_order_response = confirm_data
                return self._handle_order_response(confirm_data, depth + 1)
            else:
                logger.error(f"Confirmation failed (HTTP {confirm_res.status_code}): {confirm_res.text}")
                return False
        except Exception as e:
            logger.error(f"Order confirmation request failed: {e}")
            return False

    def reqHistoricalData(self, reqId: int, contract: Any, endDateTime: str, durationStr: str, barSizeSetting: str, whatToShow: str, useRTH: int, formatDate: int, keepUpToDate: bool, chartOptions: List[Any]):
        conid = self.get_conid(contract.symbol)
        if not conid:
            return

        period = (
            barSizeSetting.replace(" mins", "min")
            .replace(" min", "min")
            .replace(" hours", "h")
            .replace(" hour", "h")
            .replace(" day", "d")
            .replace(" ", "")
        )

        duration_parts = str(durationStr or "").strip().upper().split()
        if len(duration_parts) == 2 and duration_parts[0].isdigit() and duration_parts[1] in {"D", "W", "M", "Y"}:
            api_period = f"{int(duration_parts[0])}{duration_parts[1].lower()}"
        else:
            duration_map = {"2 D": "2d", "1 D": "1d", "1 W": "1w", "1 M": "1m"}
            api_period = duration_map.get(str(durationStr or "").strip().upper(), "2d")

        outside_rth = "true" if useRTH == 0 else "false"
        url = f"{self.base_url}/iserver/marketdata/history?conid={conid}&period={api_period}&bar={period}&outsideRth={outside_rth}"
        logger.info(f"Requesting history for {contract.symbol} ({conid}): {url}")

        try:
            res = self.session.get(url, timeout=30)
        except Exception as e:
            logger.error(f"Failed to fetch historical data: {e}")
            return

        if res.status_code == 200:
            data = res.json()
            bars = data.get("data", [])
            for bar_data in bars:

                class MockBar:
                    def __init__(self, d):
                        ts_ms = d.get("t")
                        if isinstance(ts_ms, (int, float)):
                            dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                            self.date = dt.strftime("%Y%m%d  %H:%M:%S")
                        else:
                            self.date = str(ts_ms)

                        self.open = float(d.get("o", 0))
                        self.high = float(d.get("h", 0))
                        self.low = float(d.get("l", 0))
                        self.close = float(d.get("c", 0))
                        self.volume = float(d.get("v", 0))
                        self.barCount = 0
                        self.wap = self.close

                mock_bar = MockBar(bar_data)
                for cb in self.callbacks["historicalData"]:
                    cb(reqId, mock_bar)

            for cb in self.callbacks["historicalDataEnd"]:
                cb(reqId, "", "")
        else:
            logger.error(f"Failed to fetch historical data (HTTP {res.status_code}): {res.text}")

    @staticmethod
    def get_contract(symbol: str, **kwargs) -> Any:
        class MockContract:
            def __init__(self, s):
                self.symbol = s
                self.secType = "STK"
        return MockContract(symbol)

    def get_live_orders(self) -> List[dict]:
        """Fetch current live/recent orders from the Web API."""
        if not self.is_connected:
            return []
        try:
            res = self.session.get(f"{self.base_url}/iserver/account/orders", timeout=10)
            if res.status_code == 200:
                data = res.json()
                return data.get("orders", []) if isinstance(data, dict) else data if isinstance(data, list) else []
        except Exception as e:
            logger.debug(f"Failed to fetch live orders: {e}")
        return []

    def get_snapshot(self, symbol: str) -> Optional[dict]:
        """Fetch a live market data snapshot (bid/ask/last) for a symbol."""
        conid = self.get_conid(symbol)
        if not conid:
            return None

        url = f"{self.base_url}/iserver/marketdata/snapshot?conids={conid}&fields=31,84,86"
        try:
            res = self.session.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, list) and data:
                    return data[0]
        except Exception as e:
            logger.debug(f"Snapshot request failed: {e}")
        return None

    def preview_order(self, payload: dict) -> Optional[dict]:
        """Preview an order with IBKR what-if to estimate commission and impact."""
        if not self.is_connected or not self.account_id:
            return None
        try:
            res = self.session.post(
                f"{self.base_url}/iserver/account/{self.account_id}/orders/whatif",
                json=payload,
                timeout=15,
            )
            if res.status_code == 200:
                data = res.json()
                logger.info(f"Order preview response: {data}")
                return data if isinstance(data, dict) else {"response": data}
            logger.debug(f"Order preview failed (HTTP {res.status_code}): {res.text[:200]}")
        except Exception as e:
            logger.debug(f"Failed to preview order: {e}")
        return None

    def get_accounts(self) -> List[str]:
        """Return list of brokerage account IDs."""
        if not self.is_connected:
            return []
        try:
            res = self.session.get(f"{self.base_url}/portfolio/accounts", timeout=10)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, list):
                    return [a.get("id", a.get("accountId", "")) for a in data if isinstance(a, dict)]
        except Exception as e:
            logger.debug(f"Failed to fetch accounts: {e}")
        return []

    def get_account_summary(self, account_id: str) -> dict:
        """Fetch account ledger (balances) for a given account."""
        if not self.is_connected or not account_id:
            return {}
        try:
            res = self.session.get(
                f"{self.base_url}/portfolio/{account_id}/ledger", timeout=10
            )
            if res.status_code == 200:
                return res.json()
        except Exception as e:
            logger.debug(f"Failed to fetch account summary: {e}")
        return {}

    def get_portfolio_summary(self, account_id: str) -> dict:
        """Fetch detailed account equity/margin summary."""
        if not self.is_connected or not account_id:
            return {}
        try:
            res = self.session.get(
                f"{self.base_url}/portfolio/{account_id}/summary", timeout=10
            )
            if res.status_code == 200:
                data = res.json()
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.debug(f"Failed to fetch portfolio summary: {e}")
        return {}

    def get_positions(self, account_id: str) -> List[dict]:
        """Fetch open positions for a given account."""
        if not self.is_connected or not account_id:
            return []
        try:
            res = self.session.get(
                f"{self.base_url}/portfolio/{account_id}/positions/0", timeout=10
            )
            if res.status_code == 200:
                data = res.json()
                return data if isinstance(data, list) else []
        except Exception as e:
            logger.debug(f"Failed to fetch positions: {e}")
        return []

    def get_recent_trades(self, days: int = 3) -> List[dict]:
        """Fetch recent executions/trades, including commission where IBKR returns it."""
        if not self.is_connected:
            return []
        safe_days = max(1, min(int(days or 1), 7))
        try:
            res = self.session.get(
                f"{self.base_url}/iserver/account/trades?days={safe_days}",
                timeout=10,
            )
            if res.status_code == 200:
                data = res.json()
                return data if isinstance(data, list) else []
            logger.debug(f"Recent trades request failed (HTTP {res.status_code}): {res.text[:200]}")
        except Exception as e:
            logger.debug(f"Failed to fetch recent trades: {e}")
        return []

    @staticmethod
    def create_market_order(action: str, quantity: float, outside_rth: bool = False) -> Any:
        class MockOrder:
            def __init__(self, a, q, orth):
                self.action = a
                self.totalQuantity = q
                self.orderType = "MKT"
                self.outsideRth = orth
        return MockOrder(action, quantity, outside_rth)
