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


class RsiBB5mV3Strategy:
    """
    Regime-aware RSI + Bollinger Band strategy for MSTR-style volatility.

    BUY: flat, RSI capitulation, and lower Bollinger Band tag.
    SELL: long and either hard stop, RSI profit exit, or upper Bollinger Band tag.
    """

    def __init__(
        self,
        rsi_period=14,
        bb_length=20,
        bb_std=2.5,
        bb_tolerance=0.002,
        bull_oversold=33,
        bear_oversold=25,
        overbought=70,
        trend_timeframe="1h",
        trend_ema_period=200,
        stop_loss_pct=0.05,
    ):
        self.base_columns = ["open", "high", "low", "close", "volume", "barCount", "wap"]
        self.df = pd.DataFrame(columns=self.base_columns)

        self.rsi_period = _as_int(rsi_period, 14)
        self.bb_length = _as_int(bb_length, 20)
        self.bb_std = _as_float(bb_std, 2.5)
        self.bb_tolerance = max(0.0, _as_float(bb_tolerance, 0.002))
        self.bull_oversold = _as_float(bull_oversold, 33.0)
        self.bear_oversold = _as_float(bear_oversold, 25.0)
        self.overbought = _as_float(overbought, 70.0)
        self.trend_timeframe = str(trend_timeframe or "1h").lower()
        self.trend_ema_period = _as_int(trend_ema_period, 200)
        self.stop_loss_pct = max(0.0, _as_float(stop_loss_pct, 0.05))
        self.min_bars_required = max(
            self.rsi_period + 1,
            self.bb_length + 1,
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

        if len(self.df) >= self.rsi_period + 1:
            self.df["RSI"] = ta.rsi(close_series, length=self.rsi_period)

        if len(self.df) >= self.bb_length:
            bbands = ta.bbands(close_series, length=self.bb_length, std=self.bb_std)
            if bbands is not None:
                self.df["BB_LOWER"] = bbands.iloc[:, 0]
                self.df["BB_MID"] = bbands.iloc[:, 1]
                self.df["BB_UPPER"] = bbands.iloc[:, 2]

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

    def _record_context(self, **updates):
        self.last_signal_context.update(updates)

    def get_latest_signal(self, planned_trade_notional=None, session_name=None, position_qty=0, avg_cost=0.0):
        _ = planned_trade_notional
        required_columns = ["RSI", "BB_LOWER", "BB_MID", "BB_UPPER", "TREND_EMA_1H"]
        if len(self.df) < 1 or any(col not in self.df.columns for col in required_columns):
            return "HOLD", None

        latest = self.df.iloc[-1]
        close_price = _as_float(latest.get("close"))
        rsi_raw = latest.get("RSI")
        bb_lower_raw = latest.get("BB_LOWER")
        bb_mid_raw = latest.get("BB_MID")
        bb_upper_raw = latest.get("BB_UPPER")
        trend_close_raw = latest.get("TREND_CLOSE_1H")
        trend_ema_raw = latest.get("TREND_EMA_1H")
        position_qty = _as_float(position_qty)
        avg_cost = _as_float(avg_cost)
        is_long = position_qty > 0

        values_ready = not any(
            pd.isna(value)
            for value in (rsi_raw, bb_lower_raw, bb_mid_raw, bb_upper_raw, trend_ema_raw)
        )
        rsi = _as_float(rsi_raw)
        bb_lower = _as_float(bb_lower_raw)
        bb_mid = _as_float(bb_mid_raw)
        bb_upper = _as_float(bb_upper_raw)
        trend_close = _as_float(trend_close_raw)
        trend_ema = _as_float(trend_ema_raw)
        regime = "bull" if values_ready and close_price > trend_ema else "bear"
        effective_oversold = self.bull_oversold if regime == "bull" else self.bear_oversold
        lower_threshold = bb_lower * (1.0 + self.bb_tolerance)
        upper_threshold = bb_upper * (1.0 - self.bb_tolerance)
        price_at_lower = values_ready and close_price <= lower_threshold
        price_at_upper = values_ready and close_price >= upper_threshold
        stop_price = avg_cost * (1.0 - self.stop_loss_pct) if is_long and avg_cost > 0 else None

        self.last_signal_context = {
            "strategy": "RSI_BB_5M_V3",
            "session_name": session_name,
            "session_profile": "extended" if session_name in {"pre_market", "after_hours", "overnight"} else "regular",
            "rsi": rsi,
            "close": close_price,
            "bb_lower": bb_lower,
            "bb_mid": bb_mid,
            "bb_upper": bb_upper,
            "bb_std": self.bb_std,
            "bb_tolerance": self.bb_tolerance,
            "effective_bb_tolerance": self.bb_tolerance,
            "effective_bb_lower": bb_lower,
            "effective_bb_mid": bb_mid,
            "effective_bb_upper": bb_upper,
            "lower_band_threshold": lower_threshold,
            "upper_band_threshold": upper_threshold,
            "price_at_lower_band": price_at_lower,
            "price_at_upper_band": price_at_upper,
            "trend_close_1h": trend_close,
            "trend_ema_1h": trend_ema,
            "regime": regime,
            "bull_oversold": self.bull_oversold,
            "bear_oversold": self.bear_oversold,
            "oversold": effective_oversold,
            "effective_oversold": effective_oversold,
            "overbought": self.overbought,
            "position_qty": position_qty,
            "avg_cost": avg_cost,
            "stop_loss_pct": self.stop_loss_pct,
            "stop_price": stop_price,
            "signal_reason": "",
            "buy_block_reason": "",
            "sell_block_reason": "",
            "exit_reason": "",
        }

        if not values_ready:
            self._record_context(buy_block_reason="trend_not_ready")
            return "HOLD", close_price

        if is_long and stop_price is not None and close_price <= stop_price:
            self._record_context(signal_reason="hard_stop_exit", exit_reason="hard_stop_exit")
            logger.info(
                f"!!! RSI BB V3 HARD STOP SELL SIGNAL !!! at {close_price} "
                f"(avg_cost: {avg_cost:.4f}, stop: {stop_price:.4f})"
            )
            return "SELL", close_price

        if is_long and (rsi >= self.overbought or price_at_upper):
            exit_reason = "rsi_profit_exit" if rsi >= self.overbought else "upper_band_profit_exit"
            self._record_context(signal_reason=exit_reason, exit_reason=exit_reason)
            logger.info(
                f"!!! RSI BB V3 SELL SIGNAL !!! at {close_price} "
                f"(RSI: {rsi:.2f}, upper_band={bb_upper:.4f})"
            )
            return "SELL", close_price

        if not is_long:
            if rsi > effective_oversold:
                self._record_context(buy_block_reason="rsi_not_oversold")
            elif not price_at_lower:
                self._record_context(buy_block_reason="price_not_at_lower_band")
            else:
                self._record_context(signal_reason=f"{regime}_rsi_bb_entry")
                logger.info(
                    f"!!! RSI BB V3 BUY SIGNAL !!! at {close_price} "
                    f"(regime={regime}, RSI: {rsi:.2f}, lower_band={bb_lower:.4f})"
                )
                return "BUY", close_price

        return "HOLD", close_price
