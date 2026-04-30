import pandas as pd
import pandas_ta as ta
import logging

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

class RsiBBStrategy:
    """
    RSI + Bollinger Bands Strategy with SMA Trend Filter.
    Ported from Binance bot.
    
    BUY:  RSI < oversold AND price <= lower BB (+ tolerance)
          If trend is BEAR: only when RSI < bearDipRsi
    SELL: RSI > overbought AND price >= upper BB (- tolerance)
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
        ext_hours_oversold=20,
        ext_hours_bb_std=3.0,
        ext_hours_bb_tolerance=None,
        ext_hours_volume_filter_enabled=False,
        ext_hours_volume_lookback=50,
        ext_hours_volume_multiplier=2.0,
        ext_hours_buy_confirmation_bars=4,
        ext_hours_sell_confirmation_bars=4,
    ):
        self.base_columns = ['open', 'high', 'low', 'close', 'volume', 'barCount', 'wap']
        self.df = pd.DataFrame(columns=self.base_columns)

        self.rsi_period = rsi_period
        self.bb_length = bb_length
        self.bb_std = bb_std
        self.overbought = overbought
        self.oversold = oversold
        self.trend_sma_period = trend_sma_period
        self.bear_dip_rsi = bear_dip_rsi
        self.bb_tolerance = bb_tolerance
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
        return max(self.rsi_period, self.bb_length, self.trend_sma_period)

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
            if (
                not pd.isna(ext_lower)
                and not pd.isna(ext_upper)
                and not pd.isna(ext_mid)
            ):
                return ext_lower, ext_mid, ext_upper
        return latest["BB_LOWER"], latest.get("BB_MID"), latest["BB_UPPER"]

    def _trailing_streak(self, mask):
        """Count consecutive True values ending at the most recent row.

        `mask` is a 1-D iterable aligned with self.df rows. Walks backwards from
        the last row and returns the length of the True streak (0 if the latest
        row already fails the condition).
        """
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
        """Number of consecutive recent bars that satisfy the BUY setup."""
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
        """Number of consecutive recent bars that satisfy the SELL setup.

        SELL persistence uses the regular Bollinger bands so risk-management
        exits stay reachable even when extended bands are wider.
        """
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
        width = (upper - lower).fillna(0.0)
        tolerance = width * self.bb_tolerance

        mask = (
            rsi.notna()
            & upper.notna()
            & close.notna()
            & (rsi > self.overbought)
            & (close >= upper - tolerance)
        )
        return self._trailing_streak(mask)

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
        
        if len(self.df) < max(self.rsi_period, self.bb_length, self.trend_sma_period):
            return
            
        # RSI
        self.df['RSI'] = ta.rsi(close_series, length=self.rsi_period)
        
        # Bollinger Bands
        bbands = ta.bbands(close_series, length=self.bb_length, std=self.bb_std)
        if bbands is not None:
            self.df['BB_LOWER'] = bbands.iloc[:, 0]
            self.df['BB_MID'] = bbands.iloc[:, 1]
            self.df['BB_UPPER'] = bbands.iloc[:, 2]

        ext_bbands = ta.bbands(close_series, length=self.bb_length, std=self.ext_hours_bb_std)
        if ext_bbands is not None:
            self.df['EXT_BB_LOWER'] = ext_bbands.iloc[:, 0]
            self.df['EXT_BB_MID'] = ext_bbands.iloc[:, 1]
            self.df['EXT_BB_UPPER'] = ext_bbands.iloc[:, 2]

        # Trend SMA
        if self.trend_sma_period > 0:
            self.df['TREND_SMA'] = ta.sma(close_series, length=self.trend_sma_period)
            
    def get_latest_signal(self, session_name=None):
        """
        Returns BUY, SELL, or HOLD.
        """
        params = self._buy_params(session_name)
        required_cols = ['RSI', 'BB_LOWER', 'BB_UPPER']
        if self.trend_sma_period > 0:
            required_cols.append('TREND_SMA')
            
        for col in required_cols:
            if col not in self.df.columns:
                self._record_signal_context(buy_block_reason="indicators_not_ready")
                return "HOLD", None
            
        if len(self.df) < 1:
            self._record_signal_context(buy_block_reason="no_bars")
            return "HOLD", None

        latest = self.df.iloc[-1]
        
        if pd.isna(latest['RSI']) or pd.isna(latest['BB_LOWER']):
            self._record_signal_context(buy_block_reason="indicators_not_ready")
            return "HOLD", None

        close_price = latest['close']
        rsi = latest['RSI']
        bb_lower, bb_mid, bb_upper = self._buy_band_values(latest, params)
        bb_width = bb_upper - bb_lower
        tolerance = bb_width * params["bb_tolerance"]
        volume_status = self._volume_filter_status(params["profile"])
        
        # Trend
        trend = 'NEUTRAL'
        if self.trend_sma_period > 0:
            if not pd.isna(latest['TREND_SMA']):
                trend = 'BULL' if close_price > latest['TREND_SMA'] else 'BEAR'

        # Signal Logic
        rsi_oversold = rsi < params["oversold"]
        rsi_overbought = rsi > self.overbought
        price_at_lower = close_price <= bb_lower + tolerance
        sell_bb_width = latest['BB_UPPER'] - latest['BB_LOWER']
        sell_tolerance = sell_bb_width * self.bb_tolerance
        price_at_upper = close_price >= latest['BB_UPPER'] - sell_tolerance
        bear_filter_ok = trend != 'BEAR' or (self.bear_dip_rsi != -1 and rsi < self.bear_dip_rsi)

        is_extended = params["profile"] == "extended"
        required_buy_setup_bars = self.ext_hours_buy_confirmation_bars if is_extended else 1
        required_sell_setup_bars = self.ext_hours_sell_confirmation_bars if is_extended else 1
        buy_setup_streak = self._ext_buy_setup_streak(params) if is_extended else (1 if (rsi_oversold and price_at_lower) else 0)
        sell_setup_streak = self._ext_sell_setup_streak() if is_extended else (1 if (rsi_overbought and price_at_upper) else 0)
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
        elif not volume_status["passed"]:
            buy_block_reason = volume_status["reason"]

        sell_block_reason = ""
        if rsi_overbought and price_at_upper:
            if is_extended and not sell_persistence_ok:
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

        # BUY
        if (
            rsi_oversold
            and price_at_lower
            and bear_filter_ok
            and buy_persistence_ok
            and volume_status["passed"]
        ):
            if trend == 'BEAR':
                logger.info(
                    f"!!! BEAR-DIP BUY SIGNAL !!! at {close_price} "
                    f"(RSI: {rsi:.2f}, setup_bars={buy_setup_streak}/{required_buy_setup_bars})"
                )
                return "BUY", close_price
            logger.info(
                f"!!! BUY SIGNAL !!! at {close_price} "
                f"(RSI: {rsi:.2f}, Trend: {trend}, setup_bars={buy_setup_streak}/{required_buy_setup_bars})"
            )
            return "BUY", close_price

        # SELL
        if rsi_overbought and price_at_upper and sell_persistence_ok:
            logger.info(
                f"!!! SELL SIGNAL !!! at {close_price} "
                f"(RSI: {rsi:.2f}, setup_bars={sell_setup_streak}/{required_sell_setup_bars})"
            )
            return "SELL", close_price

        return "HOLD", close_price
