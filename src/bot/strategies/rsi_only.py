import pandas as pd
import pandas_ta as ta
import logging

logger = logging.getLogger(__name__)


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

class RsiOnlyStrategy:
    """
    Pure RSI Mean-Reversion Strategy.
    Ported from Binance bot.
    
    BUY:  RSI <= oversold when flat
    SELL: RSI >= overbought when long, or stop loss when long
    """
    def __init__(self, rsi_period=14, overbought=70, oversold=30, stop_loss_pct=0.03):
        self.base_columns = ['open', 'high', 'low', 'close', 'volume', 'barCount', 'wap']
        self.df = pd.DataFrame(columns=self.base_columns)

        self.rsi_period = rsi_period
        self.overbought = overbought
        self.oversold = oversold
        self.stop_loss_pct = max(0.0, _as_float(stop_loss_pct, 0.03))
        self.min_bars_required = self.rsi_period + 1
        self.last_signal_context = {}

    def add_bar(self, date_str, open_price, high, low, close, volume, barCount=0, wap=0.0):
        """Append a new bar or update an existing one"""
        try:
            dt = pd.to_datetime(date_str)
        except Exception:
            dt = date_str

        self.df.at[dt, 'open'] = _as_float(open_price)
        self.df.at[dt, 'high'] = _as_float(high)
        self.df.at[dt, 'low'] = _as_float(low)
        self.df.at[dt, 'close'] = _as_float(close)
        self.df.at[dt, 'volume'] = _as_float(volume)
        self.df.at[dt, 'barCount'] = _as_int(barCount)
        self.df.at[dt, 'wap'] = _as_float(wap)

    def update_indicators(self):
        close_series = pd.to_numeric(self.df['close'], errors='coerce')
        
        if len(self.df) < self.rsi_period + 1:
            return
            
        # RSI
        self.df['RSI'] = ta.rsi(close_series, length=self.rsi_period)
            
    def get_latest_signal(self, planned_trade_notional=None, session_name=None, position_qty=0, avg_cost=0.0):
        """
        Returns BUY, SELL, or HOLD.
        """
        if 'RSI' not in self.df.columns:
            return "HOLD", None
            
        if len(self.df) < 1:
            return "HOLD", None

        latest = self.df.iloc[-1]
        
        if pd.isna(latest['RSI']):
            return "HOLD", None

        close_price = _as_float(latest['close'])
        rsi = _as_float(latest['RSI'])
        position_qty = _as_float(position_qty)
        avg_cost = _as_float(avg_cost)
        is_long = position_qty > 0
        stop_price = avg_cost * (1.0 - self.stop_loss_pct) if is_long and avg_cost > 0 else None

        self.last_signal_context = {
            "strategy": "RSI_ONLY",
            "session_name": session_name,
            "session_profile": "extended" if session_name in {"pre_market", "after_hours", "overnight"} else "regular",
            "rsi": rsi,
            "close": close_price,
            "oversold": self.oversold,
            "overbought": self.overbought,
            "effective_oversold": self.oversold,
            "position_qty": position_qty,
            "avg_cost": avg_cost,
            "stop_loss_pct": self.stop_loss_pct,
            "stop_price": stop_price,
            "signal_reason": "",
        }

        if is_long and stop_price is not None and close_price <= stop_price:
            self.last_signal_context["signal_reason"] = "stop_loss_exit"
            self.last_signal_context["exit_reason"] = "stop_loss_exit"
            logger.info(
                f"!!! RSI STOP LOSS SELL SIGNAL !!! at {close_price} "
                f"(avg_cost: {avg_cost:.4f}, stop: {stop_price:.4f})"
            )
            return "SELL", close_price
        
        # BUY
        if not is_long and rsi <= self.oversold:
            self.last_signal_context["signal_reason"] = "rsi_oversold_entry"
            logger.info(f"!!! RSI BUY SIGNAL !!! at {close_price} (RSI: {rsi:.2f})")
            return "BUY", close_price
                
        # SELL
        if is_long and rsi >= self.overbought:
            self.last_signal_context["signal_reason"] = "rsi_overbought_exit"
            self.last_signal_context["exit_reason"] = "rsi_overbought_exit"
            logger.info(f"!!! RSI SELL SIGNAL !!! at {close_price} (RSI: {rsi:.2f})")
            return "SELL", close_price
            
        return "HOLD", close_price
