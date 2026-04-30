import logging

import pandas as pd
import pandas_ta as ta

from strategies.rsi_bb_fee_aware import RsiBBFeeAwareStrategy, _as_float, _as_int


logger = logging.getLogger(__name__)


class RsiBBFeeAwareV3Strategy(RsiBBFeeAwareStrategy):
    """V3 fee-aware RSI+BB strategy with volume entries and dynamic exits."""

    def __init__(
        self,
        *args,
        ema_fast_period=9,
        atr_period=14,
        atr_baseline_period=50,
        volume_lookback=20,
        volume_multiplier=1.5,
        profit_rsi=70,
        dynamic_bb_min_std=2.0,
        dynamic_bb_max_std=2.5,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.ema_fast_period = max(1, _as_int(ema_fast_period, 9))
        self.atr_period = max(1, _as_int(atr_period, 14))
        self.atr_baseline_period = max(1, _as_int(atr_baseline_period, 50))
        self.volume_lookback = max(1, _as_int(volume_lookback, 20))
        self.volume_multiplier = max(0.0, _as_float(volume_multiplier, 1.5))
        self.profit_rsi = _as_float(profit_rsi, 70.0)
        min_std = max(0.1, _as_float(dynamic_bb_min_std, 2.0))
        max_std = max(min_std, _as_float(dynamic_bb_max_std, 2.5))
        self.dynamic_bb_min_std = min_std
        self.dynamic_bb_max_std = max_std

    @property
    def min_bars_required(self) -> int:
        return max(
            self.rsi_period,
            self.bb_length,
            self.trend_sma_period,
            self.ema_fast_period,
            self.atr_period + self.atr_baseline_period,
            self.volume_lookback + 1,
        ) + 1

    def update_indicators(self):
        close_series = pd.to_numeric(self.df["close"], errors="coerce")
        high_series = pd.to_numeric(self.df["high"], errors="coerce")
        low_series = pd.to_numeric(self.df["low"], errors="coerce")
        volume_series = pd.to_numeric(self.df["volume"], errors="coerce")

        if len(self.df) < self.min_bars_required - 1:
            return

        self.df["RSI"] = ta.rsi(close_series, length=self.rsi_period)
        self.df["EMA_FAST"] = ta.ema(close_series, length=self.ema_fast_period)
        self.df["ATR"] = ta.atr(
            high=high_series,
            low=low_series,
            close=close_series,
            length=self.atr_period,
        )
        self.df["ATR_SMA"] = ta.sma(self.df["ATR"], length=self.atr_baseline_period)
        self.df["VOLUME_SMA"] = volume_series.shift(1).rolling(self.volume_lookback).mean()

        bb_mid = close_series.rolling(self.bb_length).mean()
        bb_std = close_series.rolling(self.bb_length).std(ddof=0)
        atr_ratio = self.df["ATR"] / self.df["ATR_SMA"]
        dynamic_std = (self.bb_std * atr_ratio).clip(
            lower=self.dynamic_bb_min_std,
            upper=self.dynamic_bb_max_std,
        )
        dynamic_std = dynamic_std.fillna(self.bb_std)

        self.df["DYNAMIC_BB_STD"] = dynamic_std
        self.df["BB_MID"] = bb_mid
        self.df["BB_LOWER"] = bb_mid - (bb_std * dynamic_std)
        self.df["BB_UPPER"] = bb_mid + (bb_std * dynamic_std)

        ext_bbands = ta.bbands(close_series, length=self.bb_length, std=self.ext_hours_bb_std)
        if ext_bbands is not None:
            self.df["EXT_BB_LOWER"] = ext_bbands.iloc[:, 0]
            self.df["EXT_BB_MID"] = ext_bbands.iloc[:, 1]
            self.df["EXT_BB_UPPER"] = ext_bbands.iloc[:, 2]

        if self.trend_sma_period > 0:
            self.df["TREND_SMA"] = ta.sma(close_series, length=self.trend_sma_period)

    def _entry_volume_status(self, latest):
        current_volume = _as_float(latest.get("volume"), 0.0)
        volume_sma = latest.get("VOLUME_SMA")
        if volume_sma is None or pd.isna(volume_sma):
            return {
                "passed": False,
                "volume_sma": None,
                "volume_ratio": None,
                "reason": "insufficient_entry_volume_history",
            }

        volume_sma = float(volume_sma)
        if volume_sma <= 0:
            return {
                "passed": False,
                "volume_sma": volume_sma,
                "volume_ratio": None,
                "reason": "invalid_entry_volume_baseline",
            }

        volume_ratio = current_volume / volume_sma
        passed = volume_ratio >= self.volume_multiplier
        return {
            "passed": passed,
            "volume_sma": volume_sma,
            "volume_ratio": volume_ratio,
            "reason": "" if passed else "low_entry_volume",
        }

    def _position_context(self, position_qty, avg_cost):
        qty = _as_float(position_qty, 0.0)
        cost = _as_float(avg_cost, 0.0)
        return qty, cost

    def _profit_after_estimated_commissions(self, close_price, position_qty, avg_cost):
        if position_qty <= 0 or avg_cost <= 0:
            return False
        gross_pnl = (close_price - avg_cost) * position_qty
        return gross_pnl > (self.fee_per_trade * 2.0)

    def get_latest_signal(
        self,
        planned_trade_notional=None,
        session_name=None,
        position_qty=0,
        avg_cost=0.0,
    ):
        self.set_planned_trade_notional(planned_trade_notional)
        params = self._buy_params(session_name)

        required_cols = ["RSI", "BB_LOWER", "BB_MID", "BB_UPPER", "EMA_FAST", "VOLUME_SMA"]
        if self.trend_sma_period > 0:
            required_cols.append("TREND_SMA")

        for col in required_cols:
            if col not in self.df.columns:
                self._record_signal_context(buy_block_reason="indicators_not_ready")
                return "HOLD", None

        if len(self.df) < 2:
            self._record_signal_context(buy_block_reason="insufficient_bars")
            return "HOLD", None

        latest = self.df.iloc[-1]
        previous = self.df.iloc[-2]

        if pd.isna(latest["RSI"]) or pd.isna(latest["BB_LOWER"]) or pd.isna(latest["BB_MID"]):
            self._record_signal_context(buy_block_reason="indicators_not_ready")
            return "HOLD", None
        if pd.isna(previous["RSI"]) or pd.isna(previous["BB_LOWER"]):
            self._record_signal_context(buy_block_reason="indicators_not_ready")
            return "HOLD", None

        close_price = float(latest["close"])
        rsi = float(latest["RSI"])
        position_qty, avg_cost = self._position_context(position_qty, avg_cost)
        ema_fast = float(latest["EMA_FAST"]) if not pd.isna(latest["EMA_FAST"]) else None

        if position_qty > 0:
            profitable = self._profit_after_estimated_commissions(close_price, position_qty, avg_cost)
            rsi_profit_exit = rsi >= self.profit_rsi
            ema_profit_exit = profitable and ema_fast is not None and close_price < ema_fast
            sell_block_reason = ""
            if not (rsi_profit_exit or ema_profit_exit):
                sell_block_reason = "v3_dynamic_exit_not_reached"

            self._record_signal_context(
                sell_block_reason=sell_block_reason,
                exit_reason="rsi_profit_exit" if rsi_profit_exit else ("ema_profit_exit" if ema_profit_exit else ""),
                ema_fast=ema_fast,
                position_qty=position_qty,
                avg_cost=avg_cost,
                position_profitable_after_fees=profitable,
            )
            if rsi_profit_exit or ema_profit_exit:
                logger.info(
                    f"!!! FEE-AWARE V3 SELL SIGNAL !!! at {close_price} "
                    f"(RSI: {rsi:.2f}, EMA{self.ema_fast_period}: {ema_fast})"
                )
                return "SELL", close_price
            return "HOLD", close_price

        bb_lower_raw, bb_mid_raw, bb_upper_raw = self._buy_band_values(latest, params)
        bb_lower = float(bb_lower_raw)
        bb_mid = float(bb_mid_raw)
        bb_upper = float(bb_upper_raw)
        bb_width = bb_upper - bb_lower
        tolerance = bb_width * params["bb_tolerance"]
        bb_width_pct = bb_width / close_price if close_price else 0.0
        expected_reward_pct = (bb_mid - close_price) / close_price if close_price else 0.0
        volume_status = self._entry_volume_status(latest)

        trend = "NEUTRAL"
        if self.trend_sma_period > 0 and not pd.isna(latest["TREND_SMA"]):
            trend = "BULL" if close_price > latest["TREND_SMA"] else "BEAR"

        rsi_oversold = rsi < params["oversold"]
        price_at_lower = close_price <= bb_lower + tolerance
        volatility_ok = bb_width_pct >= self.min_bb_width_pct
        required_reward_pct = self.required_reward_pct_for_notional(planned_trade_notional)
        reward_ok = expected_reward_pct >= required_reward_pct
        confirmation_ok = self._confirmed_reversal(latest, previous)
        bear_filter_ok = trend != "BEAR" or self.bear_dip_rsi == -1 or rsi < self.bear_dip_rsi

        is_extended = params["profile"] == "extended"
        required_buy_setup_bars = self.ext_hours_buy_confirmation_bars if is_extended else 1
        buy_setup_streak = (
            self._ext_buy_setup_streak(params)
            if is_extended
            else (1 if (rsi_oversold and price_at_lower) else 0)
        )
        buy_persistence_ok = buy_setup_streak >= required_buy_setup_bars

        buy_block_reason = ""
        if not rsi_oversold:
            buy_block_reason = "rsi_not_oversold"
        elif not price_at_lower:
            buy_block_reason = "price_not_at_lower_band"
        elif not bear_filter_ok:
            buy_block_reason = "bear_trend_filter"
        elif is_extended and not buy_persistence_ok:
            buy_block_reason = "awaiting_extended_hours_buy_persistence"
        elif not volatility_ok:
            buy_block_reason = "bb_width_too_narrow"
        elif not reward_ok:
            buy_block_reason = "reward_below_fee_threshold"
        elif not confirmation_ok:
            buy_block_reason = "reversal_confirmation_missing"
        elif not volume_status["passed"]:
            buy_block_reason = volume_status["reason"]

        self._record_signal_context(
            effective_oversold=params["oversold"],
            effective_bb_std=float(latest.get("DYNAMIC_BB_STD", params["bb_std"])),
            effective_bb_tolerance=params["bb_tolerance"],
            effective_bb_lower=bb_lower,
            effective_bb_mid=bb_mid,
            effective_bb_upper=bb_upper,
            ema_fast=ema_fast,
            atr=float(latest["ATR"]) if "ATR" in latest and not pd.isna(latest["ATR"]) else None,
            atr_sma=float(latest["ATR_SMA"]) if "ATR_SMA" in latest and not pd.isna(latest["ATR_SMA"]) else None,
            volume_sma=volume_status["volume_sma"],
            volume_ratio=volume_status["volume_ratio"],
            volume_filter_passed=volume_status["passed"],
            buy_block_reason=buy_block_reason,
            buy_setup_bars=buy_setup_streak,
            required_buy_setup_bars=required_buy_setup_bars,
            position_qty=position_qty,
            avg_cost=avg_cost,
        )

        if (
            rsi_oversold
            and price_at_lower
            and bear_filter_ok
            and buy_persistence_ok
            and volatility_ok
            and reward_ok
            and confirmation_ok
            and volume_status["passed"]
        ):
            logger.info(
                "!!! FEE-AWARE V3 BUY SIGNAL !!! "
                f"price={close_price} rsi={rsi:.2f} reward={expected_reward_pct:.3%} "
                f"required={required_reward_pct:.3%} bb_width={bb_width_pct:.3%} "
                f"volume_ratio={volume_status['volume_ratio']:.2f}"
            )
            return "BUY", close_price

        return "HOLD", close_price
