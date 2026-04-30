import logging

import pandas as pd

from strategies.rsi_bb_fee_aware import _as_float, _as_int
from strategies.rsi_bb_fee_aware_v3 import RsiBBFeeAwareV3Strategy


logger = logging.getLogger(__name__)


class RsiBBFeeAwareV4AStrategy(RsiBBFeeAwareV3Strategy):
    """V4A smart-aggression strategy: volume window, tighter dynamic BB, smarter exits."""

    def __init__(
        self,
        *args,
        volume_spike_window_bars=3,
        max_stagnation_bars=24,
        stagnation_min_pnl_pct=0.0,
        **kwargs,
    ):
        kwargs.setdefault("volume_multiplier", 1.25)
        kwargs.setdefault("dynamic_bb_min_std", 2.1)
        kwargs.setdefault("dynamic_bb_max_std", 2.3)
        super().__init__(*args, **kwargs)
        self.volume_spike_window_bars = max(1, _as_int(volume_spike_window_bars, 3))
        self.max_stagnation_bars = max(0, _as_int(max_stagnation_bars, 24))
        self.stagnation_min_pnl_pct = _as_float(stagnation_min_pnl_pct, 0.0)
        self._position_active = False
        self._entry_bar_index = None
        self._ema_reclaimed = False

    def _reset_position_state(self):
        self._position_active = False
        self._entry_bar_index = None
        self._ema_reclaimed = False

    def _sync_position_state(self, position_qty, close_price, ema_fast):
        if position_qty <= 0:
            self._reset_position_state()
            return 0

        latest_index = max(0, len(self.df) - 1)
        if not self._position_active:
            self._position_active = True
            self._entry_bar_index = latest_index
            self._ema_reclaimed = bool(ema_fast is not None and close_price >= ema_fast)
        elif ema_fast is not None and close_price >= ema_fast:
            self._ema_reclaimed = True

        entry_index = self._entry_bar_index if self._entry_bar_index is not None else latest_index
        return max(0, latest_index - entry_index)

    def _entry_volume_status(self, latest):
        _ = latest
        tail = self.df.tail(self.volume_spike_window_bars)
        ratios = []
        for _, row in tail.iterrows():
            volume = _as_float(row.get("volume"), 0.0)
            volume_sma = row.get("VOLUME_SMA")
            if volume_sma is None or pd.isna(volume_sma):
                continue
            volume_sma = float(volume_sma)
            if volume_sma <= 0:
                continue
            ratios.append(volume / volume_sma)

        if not ratios:
            return {
                "passed": False,
                "volume_sma": None,
                "volume_ratio": None,
                "volume_window_ratio": None,
                "reason": "insufficient_entry_volume_history",
            }

        window_ratio = max(ratios)
        passed = window_ratio >= self.volume_multiplier
        latest_sma = tail.iloc[-1].get("VOLUME_SMA")
        return {
            "passed": passed,
            "volume_sma": None if latest_sma is None or pd.isna(latest_sma) else float(latest_sma),
            "volume_ratio": window_ratio,
            "volume_window_ratio": window_ratio,
            "reason": "" if passed else "low_entry_volume_window",
        }

    def _position_pnl_pct(self, close_price, avg_cost):
        if avg_cost <= 0:
            return 0.0
        return (close_price - avg_cost) / avg_cost

    def _long_exit_status(self, close_price, rsi, ema_fast, position_qty, avg_cost):
        bars_held = self._sync_position_state(position_qty, close_price, ema_fast)
        pnl_pct = self._position_pnl_pct(close_price, avg_cost)
        profitable = self._profit_after_estimated_commissions(close_price, position_qty, avg_cost)
        rsi_profit_exit = rsi >= self.profit_rsi
        ema_profit_exit = (
            profitable
            and self._ema_reclaimed
            and ema_fast is not None
            and close_price < ema_fast
        )
        stagnation_exit = (
            self.max_stagnation_bars > 0
            and bars_held >= self.max_stagnation_bars
            and pnl_pct >= self.stagnation_min_pnl_pct
        )

        if rsi_profit_exit:
            reason = "rsi_profit_exit"
        elif ema_profit_exit:
            reason = "ema_reclaim_lost_exit"
        elif stagnation_exit:
            reason = "stagnation_exit"
        else:
            reason = ""

        return {
            "should_exit": bool(reason),
            "exit_reason": reason,
            "bars_held": bars_held,
            "pnl_pct": pnl_pct,
            "profitable_after_fees": profitable,
            "ema_reclaimed": self._ema_reclaimed,
        }

    def get_latest_signal(
        self,
        planned_trade_notional=None,
        session_name=None,
        position_qty=0,
        avg_cost=0.0,
    ):
        position_qty, avg_cost = self._position_context(position_qty, avg_cost)
        if position_qty <= 0:
            signal, price = super().get_latest_signal(
                planned_trade_notional=planned_trade_notional,
                session_name=session_name,
                position_qty=0,
                avg_cost=0.0,
            )
            return signal, price

        self.set_planned_trade_notional(planned_trade_notional)
        self.set_session_context(session_name)
        for col in ["RSI", "EMA_FAST"]:
            if col not in self.df.columns:
                self._record_signal_context(sell_block_reason="indicators_not_ready")
                return "HOLD", None
        if len(self.df) < 1:
            self._record_signal_context(sell_block_reason="insufficient_bars")
            return "HOLD", None

        latest = self.df.iloc[-1]
        if pd.isna(latest["RSI"]) or pd.isna(latest["EMA_FAST"]):
            self._record_signal_context(sell_block_reason="indicators_not_ready")
            return "HOLD", None

        close_price = float(latest["close"])
        rsi = float(latest["RSI"])
        ema_fast = float(latest["EMA_FAST"])
        exit_status = self._long_exit_status(close_price, rsi, ema_fast, position_qty, avg_cost)

        self._record_signal_context(
            sell_block_reason="" if exit_status["should_exit"] else "v4_dynamic_exit_not_reached",
            exit_reason=exit_status["exit_reason"],
            ema_fast=ema_fast,
            ema_reclaimed=exit_status["ema_reclaimed"],
            position_qty=position_qty,
            avg_cost=avg_cost,
            bars_held=exit_status["bars_held"],
            pnl_pct=exit_status["pnl_pct"],
            position_profitable_after_fees=exit_status["profitable_after_fees"],
        )

        if exit_status["should_exit"]:
            logger.info(
                f"!!! FEE-AWARE V4A SELL SIGNAL !!! at {close_price} "
                f"(reason={exit_status['exit_reason']}, RSI={rsi:.2f}, EMA{self.ema_fast_period}: {ema_fast})"
            )
            return "SELL", close_price

        return "HOLD", close_price


class RsiBBFeeAwareV4BStrategy(RsiBBFeeAwareV4AStrategy):
    """V4B adds one DCA scale-in after an initial partial entry."""

    def __init__(
        self,
        *args,
        scale_in_enabled=True,
        scale_in_initial_fraction=0.4,
        scale_in_fraction=0.6,
        scale_in_drop_pct=0.0125,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.scale_in_enabled = bool(scale_in_enabled)
        self.scale_in_initial_fraction = min(1.0, max(0.0, _as_float(scale_in_initial_fraction, 0.4)))
        self.scale_in_fraction = min(1.0, max(0.0, _as_float(scale_in_fraction, 0.6)))
        self.scale_in_drop_pct = max(0.0, _as_float(scale_in_drop_pct, 0.0125))
        self._scale_in_done = False

    def _reset_position_state(self):
        super()._reset_position_state()
        self._scale_in_done = False

    def _should_scale_in(self, close_price, rsi, position_qty, avg_cost):
        if not self.scale_in_enabled or self._scale_in_done:
            return False
        if position_qty <= 0 or avg_cost <= 0:
            return False
        return close_price <= avg_cost * (1.0 - self.scale_in_drop_pct) and rsi < self.oversold

    def get_latest_signal(
        self,
        planned_trade_notional=None,
        session_name=None,
        position_qty=0,
        avg_cost=0.0,
    ):
        position_qty, avg_cost = self._position_context(position_qty, avg_cost)
        if position_qty > 0:
            self.set_planned_trade_notional(planned_trade_notional)
            self.set_session_context(session_name)
            if "RSI" in self.df.columns and len(self.df) > 0:
                latest = self.df.iloc[-1]
                if not pd.isna(latest.get("RSI")):
                    close_price = float(latest["close"])
                    rsi = float(latest["RSI"])
                    if self._should_scale_in(close_price, rsi, position_qty, avg_cost):
                        self._scale_in_done = True
                        self._record_signal_context(
                            buy_block_reason="",
                            scale_in_signal=True,
                            buy_fraction=self.scale_in_fraction,
                            scale_in_drop_pct=self.scale_in_drop_pct,
                            position_qty=position_qty,
                            avg_cost=avg_cost,
                        )
                        logger.info(
                            f"!!! FEE-AWARE V4B SCALE-IN BUY SIGNAL !!! price={close_price} "
                            f"rsi={rsi:.2f} avg_cost={avg_cost:.4f}"
                        )
                        return "BUY", close_price

            return super().get_latest_signal(
                planned_trade_notional=planned_trade_notional,
                session_name=session_name,
                position_qty=position_qty,
                avg_cost=avg_cost,
            )

        signal, price = super().get_latest_signal(
            planned_trade_notional=planned_trade_notional,
            session_name=session_name,
            position_qty=0,
            avg_cost=0.0,
        )
        if signal == "BUY" and self.scale_in_enabled:
            self.last_signal_context.update(
                {
                    "scale_in_signal": False,
                    "buy_fraction": self.scale_in_initial_fraction,
                }
            )
        return signal, price
