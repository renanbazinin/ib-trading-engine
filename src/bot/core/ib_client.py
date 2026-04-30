import logging
from typing import Callable, Dict, List

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.common import BarData, TickerId

logger = logging.getLogger(__name__)

class EventRouterClient(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        
        self.next_order_id = None
        self.is_connected = False
        
        # Callbacks for specific events. Format: self.callbacks["historicalDataUpdate"].append(func)
        self.callbacks: Dict[str, List[Callable]] = {
            "historicalData": [],
            "historicalDataEnd": [],
            "historicalDataUpdate": [],
            "position": [],
            "positionEnd": [],
            "updateAccountValue": []
        }

    def register_callback(self, event_name: str, callback: Callable):
        if event_name in self.callbacks:
            self.callbacks[event_name].append(callback)
        else:
            logger.warning(f"Unsupported event for routing: {event_name}")

    def error(self, reqId: TickerId, *args):
        """Handle ibapi error callbacks across multiple client versions."""
        error_time = None
        error_code = None
        error_string = ""

        if len(args) == 2:
            # Signature: (errorCode, errorString)
            error_code, error_string = args
        elif len(args) == 3:
            # Either (errorCode, errorString, advancedOrderRejectJson)
            # or (errorTime, errorCode, errorString)
            if isinstance(args[1], int):
                error_time, error_code, error_string = args
            else:
                error_code, error_string, _advanced_order_reject_json = args
        elif len(args) >= 4:
            # Signature: (errorTime, errorCode, errorString, advancedOrderRejectJson)
            error_time, error_code, error_string, _advanced_order_reject_json = args[:4]
        else:
            logger.error(f"Unexpected error callback payload for reqId {reqId}: {args}")
            return

        try:
            error_code = int(error_code)
        except (TypeError, ValueError):
            logger.error(f"Invalid error code in callback for reqId {reqId}: {args}")
            return

        if error_code not in [2104, 2106, 2107, 2108, 2158, 2176]:  # Ignore common non-fatal warnings
            if error_time is None:
                logger.error(f"Error {reqId} [{error_code}]: {error_string}")
            else:
                logger.error(f"Error {reqId} @ {error_time} [{error_code}]: {error_string}")
        
    def nextValidId(self, orderId: int):
        super().nextValidId(orderId)
        self.next_order_id = orderId
        self.is_connected = True
        logger.info(f"Connected! Next valid order ID: {orderId}")

    def connectionClosed(self):
        logger.warning("Connection closed by TWS/IBGateway.")
        self.is_connected = False

    # =============== Routed Event Handlers ===============
    def historicalData(self, reqId: int, bar: BarData):
        for cb in self.callbacks["historicalData"]:
            cb(reqId, bar)

    def historicalDataEnd(self, reqId: int, start: str, end: str):
        for cb in self.callbacks["historicalDataEnd"]:
            cb(reqId, start, end)

    def historicalDataUpdate(self, reqId: int, bar: BarData):
        for cb in self.callbacks["historicalDataUpdate"]:
            cb(reqId, bar)

    def position(self, account: str, contract: Contract, position: float, avgCost: float):
        for cb in self.callbacks["position"]:
            cb(account, contract, position, avgCost)
            
    def positionEnd(self):
        for cb in self.callbacks["positionEnd"]:
            cb()

    def updateAccountValue(self, key: str, val: str, currency: str, accountName: str):
        for cb in self.callbacks["updateAccountValue"]:
            cb(key, val, currency, accountName)

    # =============== Order Methods ===============
    def placeOrder(self, orderId, contract, order) -> bool:
        """Wraps EClient.placeOrder and returns True if the socket send succeeds."""
        try:
            super().placeOrder(orderId, contract, order)
            return True
        except Exception as exc:
            logger.error(f"Failed to send order via TWS socket: {exc}")
            return False

    # =============== Utility Methods ===============
    @staticmethod
    def get_contract(symbol: str, secType: str="STK", currency: str="USD", exchange: str="SMART") -> Contract:
        contract = Contract()
        contract.symbol = symbol
        contract.secType = secType
        contract.currency = currency
        contract.exchange = exchange
        return contract

    @staticmethod
    def create_market_order(action: str, quantity: float, outside_rth: bool = False) -> Order:
        order = Order()
        order.action = action
        order.orderType = "MKT"
        order.totalQuantity = quantity
        order.outsideRth = outside_rth
        return order
