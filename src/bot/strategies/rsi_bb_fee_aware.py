import logging

import pandas as pd
import pandas_ta as ta


logger = logging.getLogger(__name__)

EXTENDED_SESSION_NAMES = {"pre_market", "after_hours", "overnight"}


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


class RsiBBFeeAwareStrategy:
    """
    Fee-aware RSI + Bollinger mean-reversion strategy.

    BUY requires the normal oversold/lower-band setup plus enough expected
    reward back to the Bollinger midline to pay round-trip fees with margin.
    """

    def __init__(
        self,
        rsi_period=14,
        bb_length=20,
        bb_std=2.2,
        overbought=70,
        oversold=33,
        trend_sma_period=50,
        bear_dip_rsi=28,
        bb_tolerance=0.0015,
        fee_per_trade=2.5,
        estimated_trade_notional=1000.0,
        min_reward_pct=0.006,
        fee_reward_multiple=3.0,
        min_bb_width_pct=0.008,
        require_confirmation=True,
        exit_rsi=55,
        ext_hours_oversold=20,
        ext_hours_bb_std=3.0,
        ext_hours_bb_tolerance=None,
        ext_hours_volume_filter_enabled=False,
        ext_hours_volume_lookback=50,
        ext_hours_volume_multiplier=2.0,
        ext_hours_buy_confirmation_bars=4,
        ext_hours_sell_confirmation_bars=4,
    ):
        self.base_columns = ["open", "high", "low", "close", "volume", "barCount", "wap"]
        self.df = pd.DataFrame(columns=self.base_columns)

        self.rsi_period = rsi_period
        self.bb_length = bb_length
        self.bb_std = bb_std
        self.overbought = overbought
        self.oversold = oversold
        self.trend_sma_period = trend_sma_period
        self.bear_dip_rsi = bear_dip_rsi
        self.bb_tolerance = bb_tolerance
        self.fee_per_trade = max(0.0, float(fee_per_trade))
        self.estimated_trade_notional = max(1.0, float(estimated_trade_notional))
        self.min_reward_pct = max(0.0, float(min_reward_pct))
        self.fee_reward_multiple = max(0.0, float(fee_reward_multiple))
        self.min_bb_width_pct = max(0.0, float(min_bb_width_pct))
        self.require_confirmation = bool(require_confirmation)
        self.exit_rsi = exit_rsi
        self.planned_trade_notional = None
        self.ext_hours_oversold = ext_hours_oversold
        self.ext_hours_bb_std = ext_hours_bb_std
        self.ext_hours_bb_tolerance = bb_tolerance if ext_hours_bb_tolerance is None else ext_hours_bb_tolerance
        self.ext_hours_volume_filter_enabled = bool(ext_hours_volume_filter_enabled)
        self.ext_hours_volume_lookback = max(1, _as_int(ext_hours_volume_lookback, 50))
        self.ext_hours_volume_multiplier = max(0.0, _as_float(ext_hours_volume_multiplier, 2.0))
        self.ext_hours_buy_confirmation_bars = max(1, _as_int(ext_hours_buy_confirmation_bars, 4))
        self.ext_hours_sell_confirmation_bars = max(1, _as_int(ext_hours_sell_confirmation_bars, 4))
        self.session_name = "regular"
        self.session_profile = "regular"
        self.last_signal_context = {}

    @property
    def min_bars_required(self) -> int:
        return max(self.rsi_period, self.bb_length, self.trend_sma_period) + 1

    @property
    def round_trip_fee_pct(self) -> float:
        return self.round_trip_fee_pct_for_notional(self.planned_trade_notional)

    @property
    def required_reward_pct(self) -> float:
        return self.required_reward_pct_for_notional(self.planned_trade_notional)

    def set_planned_trade_notional(self, notional):
        parsed = _as_float(notional, default=0.0)
        self.planned_trade_notional = parsed if parsed > 0 else None

    def set_session_context(self, session_name=None):
        self.session_name = str(session_name or "regular")
        self.session_profile = "extended" if self.session_name in EXTENDED_SESSION_NAMES else "regular"
        return self.session_profile

    def _buy_params(self, session_name=None):
        profile = self.set_session_context(session_name or self.session_name)
        if profile == "extended":
            return {
                "profile": profile,
                "oversold": self.ext_hours_oversold,
                "bb_std": self.ext_hours_bb_std,
                "bb_tolerance": self.ext_hours_bb_tolerance,
            }
        return {
            "profile": profile,
            "oversold": self.oversold,
            "bb_std": self.bb_std,
            "bb_tolerance": self.bb_tolerance,
        }

    def _record_signal_context(self, **updates):
        is_extended = self.session_profile == "extended"
        context = {
            "session_name": self.session_name,
            "session_profile": self.session_profile,
            "effective_oversold": self.ext_hours_oversold if is_extended else self.oversold,
            "effective_bb_std": self.ext_hours_bb_std if is_extended else self.bb_std,
            "volume_sma": None,
            "volume_ratio": None,
            "volume_filter_passed": True,
            "buy_block_reason": "",
            "sell_block_reason": "",
            "buy_setup_bars": 0,
            "required_buy_setup_bars": self.ext_hours_buy_confirmation_bars if is_extended else 1,
            "sell_setup_bars": 0,
            "required_sell_setup_bars": self.ext_hours_sell_confirmation_bars if is_extended else 1,
        }
        context.update(updates)
        self.last_signal_context = context

    def _volume_filter_status(self, profile):
        if profile != "extended" or not self.ext_hours_volume_filter_enabled:
            return {"passed": True, "volume_sma": None, "volume_ratio": None, "reason": ""}

        volumes = pd.to_numeric(self.df["volume"], errors="coerce").dropna()
        if len(volumes) < self.ext_hours_volume_lookback + 1:
            return {
                "passed": False,
                "volume_sma": None,
                "volume_ratio": None,
                "reason": "insufficient_extended_hours_volume_history",
            }

        current_volume = float(volumes.iloc[-1])
        volume_sma = float(volumes.iloc[-(self.ext_hours_volume_lookback + 1):-1].mean())
        if volume_sma <= 0:
            return {
                "passed": False,
                "volume_sma": volume_sma,
                "volume_ratio": None,
                "reason": "invalid_extended_hours_volume_baseline",
            }

        volume_ratio = current_volume / volume_sma
        passed = volume_ratio >= self.ext_hours_volume_multiplier
        return {
            "passed": passed,
            "volume_sma": volume_sma,
            "volume_ratio": volume_ratio,
            "reason": "" if passed else "low_extended_hours_volume",
        }

    def _buy_band_values(self, latest, params):
        if params["profile"] == "extended" and "EXT_BB_LOWER" in self.df.columns:
            ext_lower = latest.get("EXT_BB_LOWER")
            ext_mid = latest.get("EXT_BB_MID")
            ext_upper = latest.get("EXT_BB_UPPER")
            if not pd.isna(ext_lower) and not pd.isna(ext_mid) and not pd.isna(ext_upper):
                return ext_lower, ext_mid, ext_upper
        return latest["BB_LOWER"], latest["BB_MID"], latest["BB_UPPER"]

    def _trailing_streak(self, mask):
        if mask is None:
            return 0
        try:
            values = list(mask)
        except TypeError:
            return 0
        streak = 0
        for value in reversed(values):
            if bool(value):
                streak += 1
            else:
                break
        return streak

    def _ext_buy_setup_streak(self, params):
        if "RSI" not in self.df.columns:
            return 0

        if params["profile"] == "extended" and "EXT_BB_LOWER" in self.df.columns:
            lower_col, upper_col = "EXT_BB_LOWER", "EXT_BB_UPPER"
        else:
            lower_col, upper_col = "BB_LOWER", "BB_UPPER"

        if lower_col not in self.df.columns or upper_col not in self.df.columns:
            return 0

        max_lookback = max(self.ext_hours_buy_confirmation_bars, 1) * 2
        tail = self.df.tail(max_lookback)
        rsi = pd.to_numeric(tail["RSI"], errors="coerce")
        close = pd.to_numeric(tail["close"], errors="coerce")
        lower = pd.to_numeric(tail[lower_col], errors="coerce")
        upper = pd.to_numeric(tail[upper_col], errors="coerce")
        width = (upper - lower).fillna(0.0)
        tolerance = width * params["bb_tolerance"]

        mask = (
            rsi.notna()
            & lower.notna()
            & upper.notna()
            & close.notna()
            & (rsi < params["oversold"])
            & (close <= lower + tolerance)
        )
        return self._trailing_streak(mask)

    def _ext_sell_setup_streak(self):
        required_cols = ("RSI", "BB_UPPER", "BB_LOWER")
        for col in required_cols:
            if col not in self.df.columns:
                return 0

        max_lookback = max(self.ext_hours_sell_confirmation_bars, 1) * 2
        tail = self.df.tail(max_lookback)
        rsi = pd.to_numeric(tail["RSI"], errors="coerce")
        close = pd.to_numeric(tail["close"], errors="coerce")
        upper = pd.to_numeric(tail["BB_UPPER"], errors="coerce")
        lower = pd.to_numeric(tail["BB_LOWER"], errors="coerce")
        mid = (
            pd.to_numeric(tail["BB_MID"], errors="coerce")
            if "BB_MID" in tail.columns
            else None
        )
        width = (upper - lower).fillna(0.0)
        tolerance = width * self.bb_tolerance

        # Existing SELL is "rsi_overbought + price_at_upper" OR mean-reversion exit;
        # persistence guards the overbought-band path. The mean-reversion exit is a
        # hard risk-management trigger and remains a single-bar event.
        mask = (
            rsi.notna()
            & upper.notna()
            & close.notna()
            & (rsi > self.overbought)
            & (close >= upper - tolerance)
        )
        return self._trailing_streak(mask)

    def _effective_trade_notional(self, planned_trade_notional=None) -> float:
        parsed = _as_float(planned_trade_notional, default=0.0)
        if parsed <= 0:
            parsed = _as_float(self.planned_trade_notional, default=0.0)
        if parsed <= 0:
            parsed = self.estimated_trade_notional
        return max(1.0, parsed)

    def round_trip_fee_pct_for_notional(self, planned_trade_notional=None) -> float:
        return (self.fee_per_trade * 2.0) / self._effective_trade_notional(planned_trade_notional)

    def required_reward_pct_for_notional(self, planned_trade_notional=None) -> float:
        return max(
            self.min_reward_pct,
            self.round_trip_fee_pct_for_notional(planned_trade_notional) * self.fee_reward_multiple,
        )

    def add_bar(self, date_str, open_price, high, low, close, volume, barCount=0, wap=0.0):
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
        self.df.at[dt, "wap"] = _as_float(wap)

    def update_indicators(self):
        close_series = pd.to_numeric(self.df["close"], errors="coerce")

        if len(self.df) < max(self.rsi_period, self.bb_length, self.trend_sma_period):
            return

        self.df["RSI"] = ta.rsi(close_series, length=self.rsi_period)

        bbands = ta.bbands(close_series, length=self.bb_length, std=self.bb_std)
        if bbands is not None:
            self.df["BB_LOWER"] = bbands.iloc[:, 0]
            self.df["BB_MID"] = bbands.iloc[:, 1]
            self.df["BB_UPPER"] = bbands.iloc[:, 2]

        ext_bbands = ta.bbands(close_series, length=self.bb_length, std=self.ext_hours_bb_std)
        if ext_bbands is not None:
            self.df["EXT_BB_LOWER"] = ext_bbands.iloc[:, 0]
            self.df["EXT_BB_MID"] = ext_bbands.iloc[:, 1]
            self.df["EXT_BB_UPPER"] = ext_bbands.iloc[:, 2]

        if self.trend_sma_period > 0:
            self.df["TREND_SMA"] = ta.sma(close_series, length=self.trend_sma_period)

    def _confirmed_reversal(self, latest, previous) -> bool:
        if not self.require_confirmation:
            return True

        rsi_turning_up = latest["RSI"] > previous["RSI"]
        green_candle = latest["close"] > latest["open"]
        reclaimed_lower_band = previous["close"] <= previous["BB_LOWER"] and latest["close"] > latest["BB_LOWER"]
        return bool(rsi_turning_up and (green_candle or reclaimed_lower_band))

    def get_latest_signal(self, planned_trade_notional=None, session_name=None):
        self.set_planned_trade_notional(planned_trade_notional)
        params = self._buy_params(session_name)

        required_cols = ["RSI", "BB_LOWER", "BB_MID", "BB_UPPER"]
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
        bb_lower_raw, bb_mid_raw, bb_upper_raw = self._buy_band_values(latest, params)
        bb_lower = float(bb_lower_raw)
        bb_mid = float(bb_mid_raw)
        bb_upper = float(bb_upper_raw)
        bb_width = bb_upper - bb_lower
        tolerance = bb_width * params["bb_tolerance"]
        bb_width_pct = bb_width / close_price if close_price else 0.0
        expected_reward_pct = (bb_mid - close_price) / close_price if close_price else 0.0
        volume_status = self._volume_filter_status(params["profile"])

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

        rsi_overbought = rsi > self.overbought
        sell_bb_width = float(latest["BB_UPPER"]) - float(latest["BB_LOWER"])
        sell_tolerance = sell_bb_width * self.bb_tolerance
        price_at_upper = close_price >= float(latest["BB_UPPER"]) - sell_tolerance
        mean_reversion_exit = close_price >= float(latest["BB_MID"]) and rsi >= self.exit_rsi

        is_extended = params["profile"] == "extended"
        required_buy_setup_bars = self.ext_hours_buy_confirmation_bars if is_extended else 1
        required_sell_setup_bars = self.ext_hours_sell_confirmation_bars if is_extended else 1
        buy_setup_streak = (
            self._ext_buy_setup_streak(params)
            if is_extended
            else (1 if (rsi_oversold and price_at_lower) else 0)
        )
        sell_setup_streak = (
            self._ext_sell_setup_streak()
            if is_extended
            else (1 if (rsi_overbought and price_at_upper) else 0)
        )
        buy_persistence_ok = buy_setup_streak >= required_buy_setup_bars
        sell_persistence_ok = sell_setup_streak >= required_sell_setup_bars

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

        sell_block_reason = ""
        if rsi_overbought and price_at_upper and is_extended and not sell_persistence_ok:
            sell_block_reason = "awaiting_extended_hours_sell_persistence"

        self._record_signal_context(
            effective_oversold=params["oversold"],
            effective_bb_std=params["bb_std"],
            effective_bb_tolerance=params["bb_tolerance"],
            effective_bb_lower=bb_lower,
            effective_bb_mid=bb_mid,
            effective_bb_upper=bb_upper,
            volume_sma=volume_status["volume_sma"],
            volume_ratio=volume_status["volume_ratio"],
            volume_filter_passed=volume_status["passed"],
            buy_block_reason=buy_block_reason,
            sell_block_reason=sell_block_reason,
            buy_setup_bars=buy_setup_streak,
            required_buy_setup_bars=required_buy_setup_bars,
            sell_setup_bars=sell_setup_streak,
            required_sell_setup_bars=required_sell_setup_bars,
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
                "!!! FEE-AWARE BUY SIGNAL !!! "
                f"price={close_price} rsi={rsi:.2f} reward={expected_reward_pct:.3%} "
                f"required={required_reward_pct:.3%} bb_width={bb_width_pct:.3%} "
                f"setup_bars={buy_setup_streak}/{required_buy_setup_bars}"
            )
            return "BUY", close_price

        # Mean-reversion exit is a risk-management hard trigger and stays single-bar
        # so we can always close risk; only the overbought+upper-band path requires
        # extended-hours persistence.
        if mean_reversion_exit or (rsi_overbought and price_at_upper and sell_persistence_ok):
            logger.info(
                f"!!! FEE-AWARE SELL SIGNAL !!! at {close_price} (RSI: {rsi:.2f}, "
                f"setup_bars={sell_setup_streak}/{required_sell_setup_bars})"
            )
            return "SELL", close_price

        return "HOLD", close_price
