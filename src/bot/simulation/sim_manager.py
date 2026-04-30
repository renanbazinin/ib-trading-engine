import json
import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional, Tuple

import pandas as pd
from core.market_data_provider import NormalizedBar, from_ib_bar

from strategies.strategy_factory import create_strategy

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class VirtualPortfolio:
    """A simulated portfolio running a specific strategy config."""

    def __init__(
        self,
        sim_id: str,
        config: Dict,
        starting_balance: float = 10000.0,
        trade_quantity: int = 100,
        max_recent_events: int = 100,
        default_near_band: float = 0.001
    ):
        self.sim_id = sim_id
        self.name = config.get("name", sim_id)
        self.config = config
        self.balance = starting_balance
        self.equity = starting_balance
        self.position: int = 0
        self.avg_cost: float = 0.0
        self.trade_quantity = trade_quantity
        self.commission_per_trade = max(
            0.0,
            float(config.get("commission_per_trade", config.get("fee_per_trade", 0.0))),
        )
        self.trade_allocation_pct = min(1.0, max(0.0, float(config.get("trade_allocation_pct", 0.95))))

        self.total_signals = 0
        self.acted_signals = 0
        self.total_trades = 0
        self.realized_pnl = 0.0
        self.total_commissions = 0.0

        self.recent_signals: Deque[Dict] = deque(maxlen=max_recent_events)
        self.recent_trades: Deque[Dict] = deque(maxlen=max_recent_events)
        self.all_trades: List[Dict] = []

        strategy_name = config.get("strategy", "RSI_BB_FEE_AWARE_V4B").upper()
        self.strategy = create_strategy(strategy_name, config)

    def _buy_fraction(self) -> Optional[float]:
        context = getattr(self.strategy, "last_signal_context", {}) or {}
        value = context.get("buy_fraction")
        if value is None:
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return min(1.0, max(0.0, parsed))

    def _can_scale_in(self) -> bool:
        context = getattr(self.strategy, "last_signal_context", {}) or {}
        return bool(self.config.get("scale_in_enabled", False) and context.get("scale_in_signal"))

    def _get_latest_strategy_signal(self) -> Tuple[str, Optional[float]]:
        try:
            return self.strategy.get_latest_signal(
                position_qty=self.position,
                avg_cost=self.avg_cost,
            )
        except TypeError:
            return self.strategy.get_latest_signal()

    def process_bar(self, bar: NormalizedBar) -> Tuple[Optional[Dict], Optional[Dict]]:
        # ... (process_bar logic remains the same)
        self.strategy.add_bar(
            date_str=bar.date,
            open_price=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            barCount=getattr(bar, "barCount", 0),
            wap=getattr(bar, "wap", bar.close),
        )
        self.strategy.update_indicators()

        signal, current_price = self._get_latest_strategy_signal()
        signal_context = getattr(self.strategy, "last_signal_context", {}) or {}
        signal_reason = signal_context.get("signal_reason", "")
        if current_price is None:
            return None, None

        self.equity = self.balance + (self.position * current_price)
        if signal not in ("BUY", "SELL"):
            return None, None

        event_time = _utc_now_iso()
        acted = False
        reason = "no_action"
        trade_event = None

        if signal == "BUY":
            scale_in = False
            if self.position > 0 and not self._can_scale_in():
                reason = "already_long"
            else:
                scale_in = self.position > 0
                buy_fraction = self._buy_fraction()
                if buy_fraction is None:
                    can_spend = self.balance * self.trade_allocation_pct
                else:
                    total_equity = self.balance + (self.position * current_price)
                    can_spend = min(self.balance, total_equity * self.trade_allocation_pct * buy_fraction)
                qty = int(can_spend // current_price)
                
                if qty > 0:
                    cost = qty * current_price
                    commission = self.commission_per_trade
                    previous_position = self.position
                    previous_cost_basis = self.avg_cost * previous_position
                    self.balance -= cost + commission
                    self.position += qty
                    self.avg_cost = (previous_cost_basis + cost) / self.position if self.position else 0.0
                    self.realized_pnl -= commission
                    self.total_commissions += commission
                    acted = True
                    reason = "scale_in_executed" if scale_in else "executed"
                    self.total_trades += 1

                    trade_event = {
                        "ts": event_time,
                        "sim_id": self.sim_id,
                        "side": "BUY",
                        "qty": qty,
                        "price": round(float(current_price), 4),
                        "commission": round(float(commission), 4),
                        "realized_pnl": round(float(-commission), 4),
                        "scale_in": scale_in,
                        "signal_reason": signal_reason,
                    }
                    self.recent_trades.appendleft(trade_event)
                    self.all_trades.append(trade_event)
                else:
                    reason = "insufficient_balance"
                    logger.debug(f"SIM [{self.sim_id}] Insufficient balance for even 1 share: {self.balance}")

        elif signal == "SELL":
            if self.position <= 0:
                reason = "no_position"
            else:
                sold_quantity = self.position
                revenue = sold_quantity * current_price
                commission = self.commission_per_trade
                pnl = (current_price - self.avg_cost) * sold_quantity - commission

                self.balance += revenue - commission
                self.position = 0
                self.avg_cost = 0.0
                self.realized_pnl += pnl
                self.total_commissions += commission
                acted = True
                reason = "executed"
                self.total_trades += 1


                trade_event = {
                    "ts": event_time,
                    "sim_id": self.sim_id,
                    "side": "SELL",
                    "qty": sold_quantity,
                    "price": round(float(current_price), 4),
                    "commission": round(float(commission), 4),
                    "realized_pnl": round(float(pnl), 4),
                    "signal_reason": signal_reason,
                    "exit_reason": signal_context.get("exit_reason", signal_reason),
                }
                self.recent_trades.appendleft(trade_event)
                self.all_trades.append(trade_event)

        signal_event = {
            "ts": event_time,
            "sim_id": self.sim_id,
            "signal": signal,
            "acted": acted,
            "reason": reason,
            "signal_reason": signal_reason,
            "exit_reason": signal_context.get("exit_reason"),
            "price": round(float(current_price), 4),
        }

        self.total_signals += 1
        if acted:
            self.acted_signals += 1

        self.equity = self.balance + (self.position * current_price)
        self.recent_signals.appendleft(signal_event)

        if acted:
            logger.info(f"SIM [{self.sim_id}] {signal} executed at {current_price:.4f} | Balance: {self.balance:.2f} | Pos: {self.position}")

        return signal_event, trade_event

    def serialize_summary(self) -> Dict:
        # Extract latest indicators
        indicators = {}
        if len(self.strategy.df) > 0:
            latest = self.strategy.df.iloc[-1]
            for col in self.strategy.df.columns:
                if col not in ('open', 'high', 'low', 'close', 'volume', 'barCount', 'wap'):
                    val = latest.get(col)
                    if val is not None and not pd.isna(val):
                        indicators[col] = round(float(val), 4)

        return {
            "id": self.sim_id,
            "name": self.name,
            "config": self.config,
            "balance": round(float(self.balance), 2),
            "equity": round(float(self.equity), 2),
            "position": int(self.position),
            "avg_cost": round(float(self.avg_cost), 4),
            "indicators": indicators,
            "stats": {
                "signals_total": self.total_signals,
                "signals_acted": self.acted_signals,
                "trades_total": self.total_trades,
                "realized_pnl": round(float(self.realized_pnl), 4),
                "total_commissions": round(float(self.total_commissions), 4),
            },
            "recent_signals": list(self.recent_signals),
            "recent_trades": list(self.recent_trades),
            "all_trades": list(self.all_trades),
        }



class SimManager:
    def __init__(self, results_dir: str = "simulation/results", export_policy: Optional[Dict] = None):
        self.portfolios: List[VirtualPortfolio] = []
        self.results_dir = results_dir
        self.export_policy = export_policy or {}

        self.summary_filename = self.export_policy.get("summary_filename", "latest_sims.json")
        self.summary_every_n_updates = max(1, int(self.export_policy.get("summary_every_n_updates", 1)))
        self.global_recent_limit = max(1, int(self.export_policy.get("global_recent_limit", 200)))

        self.recent_signals: Deque[Dict] = deque(maxlen=self.global_recent_limit)
        self.recent_trades: Deque[Dict] = deque(maxlen=self.global_recent_limit)
        self._update_counter = 0

        os.makedirs(self.results_dir, exist_ok=True)

    def initialize_simulations(self, configs: List[Dict], defaults: Optional[Dict] = None):
        defaults = defaults or {}
        default_balance = float(defaults.get("starting_balance", 10000.0))
        default_trade_quantity = int(defaults.get("trade_quantity", 100))
        default_recent_limit = int(defaults.get("max_recent_events", 100))
        default_near_band = float(defaults.get("near_band_pct", 0.001))

        self.portfolios = []
        enabled_configs = [cfg for cfg in configs if cfg.get("enabled", True)]
        logger.info(f"Initializing {len(enabled_configs)} simulations...")

        for i, cfg in enumerate(enabled_configs):
            sim_id = cfg.get("id", f"sim_{i}")
            vp = VirtualPortfolio(
                sim_id=sim_id,
                config=cfg,
                starting_balance=float(cfg.get("starting_balance", default_balance)),
                trade_quantity=int(cfg.get("trade_quantity", default_trade_quantity)),
                max_recent_events=int(cfg.get("max_recent_events", default_recent_limit)),
                default_near_band=default_near_band
            )
            self.portfolios.append(vp)

    def _append_event_jsonl(self, event_prefix: str, event: Dict):
        date_key = datetime.now(timezone.utc).strftime("%Y%m%d")
        event_file = os.path.join(self.results_dir, f"{event_prefix}_{date_key}.jsonl")
        try:
            with open(event_file, "a", encoding="utf-8") as file_handle:
                file_handle.write(json.dumps(event, separators=(",", ":")) + "\n")
        except Exception as exc:
            logger.error(f"Failed to append {event_prefix} event: {exc}")

    def _process_bar_for_all(self, bar: NormalizedBar, persist_events: bool):
        for vp in self.portfolios:
            signal_event, trade_event = vp.process_bar(bar)

            if signal_event:
                self.recent_signals.appendleft(signal_event)
                if persist_events:
                    self._append_event_jsonl("signal_events", signal_event)

            if trade_event:
                self.recent_trades.appendleft(trade_event)
                if persist_events:
                    self._append_event_jsonl("trade_events", trade_event)

    def on_backfill_bar(self, bar: NormalizedBar):
        # Backfill updates state but avoids signal/trade event file spam.
        self._process_bar_for_all(bar, persist_events=False)

    def on_live_bar(self, bar: NormalizedBar):
        self._process_bar_for_all(bar, persist_events=True)

        self._update_counter += 1
        if self._update_counter % self.summary_every_n_updates == 0:
            self.export_results()

    def on_historical_data(self, req_id: int, bar):
        _ = req_id
        self.on_backfill_bar(from_ib_bar(bar, source="broker"))

    def on_historical_data_update(self, req_id: int, bar):
        _ = req_id
        self.on_live_bar(from_ib_bar(bar, source="broker"))

    def export_results(self):
        data = {
            "timestamp": _utc_now_iso(),
            "summary": {
                "sim_count": len(self.portfolios),
                "signals_total": sum(vp.total_signals for vp in self.portfolios),
                "signals_acted_total": sum(vp.acted_signals for vp in self.portfolios),
                "trades_total": sum(vp.total_trades for vp in self.portfolios),
            },
            "recent_signals": list(self.recent_signals),
            "recent_trades": list(self.recent_trades),
            "all_trades": [trade for vp in self.portfolios for trade in vp.all_trades],
            "simulations": [vp.serialize_summary() for vp in self.portfolios],
        }

        filepath = os.path.join(self.results_dir, self.summary_filename)
        try:
            with open(filepath, "w", encoding="utf-8") as file_handle:
                json.dump(data, file_handle, indent=2)
            return data
        except Exception as exc:
            logger.error(f"Failed to export sim results: {exc}")
            return data
