import logging

import pandas as pd
import pandas_ta as ta

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


class Rsi5mV2Strategy:
    """
    Balanced 5m RSI dip strategy for volatile stocks.

    BUY: flat, RSI <= oversold, and latest completed 1H close > 1H EMA.
    SELL: long and either RSI >= overbought or price hits ATR-based stop.
    """

    def __init__(
        self,
        rsi_period=14,
        overbought=75,
        oversold=25,
        trend_timeframe="1h",
        trend_ema_period=200,
        atr_period=14,
        atr_stop_multiple=2.0,
    ):
        self.base_columns = ["open", "high", "low", "close", "volume", "barCount", "wap"]
        self.df = pd.DataFrame(columns=self.base_columns)

        self.rsi_period = _as_int(rsi_period, 14)
        self.overbought = _as_float(overbought, 75.0)
        self.oversold = _as_float(oversold, 25.0)
        self.trend_timeframe = str(trend_timeframe or "1h").lower()
        self.trend_ema_period = _as_int(trend_ema_period, 200)
        self.atr_period = _as_int(atr_period, 14)
        self.atr_stop_multiple = max(0.0, _as_float(atr_stop_multiple, 2.0))
        self.min_bars_required = max(
            self.rsi_period + 1,
            self.atr_period + 1,
            (self.trend_ema_period + 2) * 12,
        )
        self.last_signal_context = {}

    def add_bar(self, date_str, open_price, high, low, close, volume, barCount=0, wap=0.0):
        """Append a new bar or update an existing one."""
        try:
            dt = pd.to_datetime(date_str)
        except Exception:
            dt = date_str

        self.df.at[dt, "open"] = _as_float(open_price)
        self.df.at[dt, "high"] = _as_float(high)
        self.df.at[dt, "low"] = _as_float(low)
        self.df.at[dt, "close"] = _as_float(close)
        self.df.at[dt, "volume"] = _as_float(volume)
        self.df.at[dt, "barCount"] = _as_int(barCount)
        self.df.at[dt, "wap"] = _as_float(wap, _as_float(close))

    def update_indicators(self):
        if len(self.df) == 0:
            return

        self.df = self.df.sort_index()
        close_series = pd.to_numeric(self.df["close"], errors="coerce")
        high_series = pd.to_numeric(self.df["high"], errors="coerce")
        low_series = pd.to_numeric(self.df["low"], errors="coerce")

        if len(self.df) >= self.rsi_period + 1:
            self.df["RSI"] = ta.rsi(close_series, length=self.rsi_period)

        if len(self.df) >= self.atr_period + 1:
            self.df["ATR"] = ta.atr(
                high=high_series,
                low=low_series,
                close=close_series,
                length=self.atr_period,
            )

        self._update_trend_indicators(close_series)

    def _update_trend_indicators(self, close_series):
        if self.trend_ema_period <= 0 or len(close_series.dropna()) == 0:
            return

        try:
            hourly_close = close_series.resample(self.trend_timeframe).last().dropna()
        except (TypeError, ValueError):
            return

        if len(hourly_close) < self.trend_ema_period:
            return

        hourly_ema = ta.ema(hourly_close, length=self.trend_ema_period)
        trend_frame = pd.DataFrame(
            {
                "TREND_CLOSE_1H": hourly_close,
                "TREND_EMA_1H": hourly_ema,
            }
        ).shift(1)
        aligned = trend_frame.reindex(self.df.index, method="ffill")
        self.df["TREND_CLOSE_1H"] = aligned["TREND_CLOSE_1H"]
        self.df["TREND_EMA_1H"] = aligned["TREND_EMA_1H"]

    def get_latest_signal(self, planned_trade_notional=None, session_name=None, position_qty=0, avg_cost=0.0):
        _ = planned_trade_notional
        required_columns = ["RSI", "ATR", "TREND_CLOSE_1H", "TREND_EMA_1H"]
        if len(self.df) < 1 or any(col not in self.df.columns for col in required_columns):
            return "HOLD", None

        latest = self.df.iloc[-1]
        close_price = _as_float(latest.get("close"))
        rsi = latest.get("RSI")
        atr = latest.get("ATR")
        trend_close = latest.get("TREND_CLOSE_1H")
        trend_ema = latest.get("TREND_EMA_1H")
        position_qty = _as_float(position_qty)
        avg_cost = _as_float(avg_cost)
        is_long = position_qty > 0

        values_ready = not any(pd.isna(value) for value in (rsi, atr, trend_close, trend_ema))
        rsi = _as_float(rsi)
        atr = _as_float(atr)
        trend_close = _as_float(trend_close)
        trend_ema = _as_float(trend_ema)
        trend_passed = values_ready and trend_close > trend_ema
        atr_stop_price = avg_cost - (atr * self.atr_stop_multiple) if is_long and avg_cost > 0 and atr > 0 else None

        self.last_signal_context = {
            "strategy": "RSI_5M_V2",
            "session_name": session_name,
            "session_profile": "extended" if session_name in {"pre_market", "after_hours", "overnight"} else "regular",
            "rsi": rsi,
            "close": close_price,
            "oversold": self.oversold,
            "overbought": self.overbought,
            "effective_oversold": self.oversold,
            "atr": atr,
            "atr_stop_multiple": self.atr_stop_multiple,
            "atr_stop_price": atr_stop_price,
            "trend_close_1h": trend_close,
            "trend_ema_1h": trend_ema,
            "trend_passed": trend_passed,
            "position_qty": position_qty,
            "avg_cost": avg_cost,
            "signal_reason": "",
            "buy_block_reason": "",
            "exit_reason": "",
        }

        if not values_ready:
            self.last_signal_context["buy_block_reason"] = "trend_not_ready"
            return "HOLD", close_price

        if is_long and atr_stop_price is not None and close_price <= atr_stop_price:
            self.last_signal_context["signal_reason"] = "atr_stop_exit"
            self.last_signal_context["exit_reason"] = "atr_stop_exit"
            logger.info(
                f"!!! RSI V2 ATR STOP SELL SIGNAL !!! at {close_price} "
                f"(avg_cost: {avg_cost:.4f}, stop: {atr_stop_price:.4f})"
            )
            return "SELL", close_price

        if not is_long and rsi <= self.oversold:
            if not trend_passed:
                self.last_signal_context["buy_block_reason"] = "below_trend_ema"
                return "HOLD", close_price

            self.last_signal_context["signal_reason"] = "rsi_oversold_trend_entry"
            logger.info(
                f"!!! RSI V2 BUY SIGNAL !!! at {close_price} "
                f"(RSI: {rsi:.2f}, 1H close: {trend_close:.4f}, EMA{self.trend_ema_period}: {trend_ema:.4f})"
            )
            return "BUY", close_price

        if is_long and rsi >= self.overbought:
            self.last_signal_context["signal_reason"] = "rsi_overbought_exit"
            self.last_signal_context["exit_reason"] = "rsi_overbought_exit"
            logger.info(f"!!! RSI V2 SELL SIGNAL !!! at {close_price} (RSI: {rsi:.2f})")
            return "SELL", close_price

        return "HOLD", close_price
