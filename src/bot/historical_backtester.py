import argparse
import csv
import json
import logging
import os
import sys
from datetime import datetime, timezone

# Hack for broken ibapi protobuf imports
script_dir = os.path.dirname(os.path.abspath(__file__))
ibapi_path = os.path.join(script_dir, "ibapi")
protobuf_path = os.path.join(ibapi_path, "protobuf")
sys.path.insert(0, ibapi_path)
sys.path.insert(0, protobuf_path)

from simulation.sim_manager import SimManager
from core.market_data_provider import NormalizedBar
from core.trading_hours import TradingHours

# Configure logging
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("HistoricalBacktester")


def _safe_filename(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(value)).strip("_")

def _bar_in_regular_session_et(date_str: str, trading_hours: TradingHours) -> bool:
    """CSV dates from yf_historical_downloader are UTC (%Y%m%d  %H:%M:%S)."""
    raw = (date_str or "").strip()
    dt = datetime.strptime(raw, "%Y%m%d  %H:%M:%S").replace(tzinfo=timezone.utc)
    return trading_hours.status(dt).session_name == "regular"


class HistoricalBacktester:
    def __init__(
        self,
        csv_path,
        config_path,
        regular_session_only: bool = False,
        json_results_dir: str = "simulation/results/historical/json",
    ):
        self.csv_path = csv_path
        self.config_path = config_path
        self.regular_session_only = regular_session_only
        self.json_results_dir = json_results_dir
        self._trading_hours = TradingHours.from_config(enabled=True) if regular_session_only else None
        self.sim_manager = SimManager(results_dir="simulation/results/historical")

    def _export_json_artifacts(self, results: dict):
        os.makedirs(self.json_results_dir, exist_ok=True)
        timestamp = results.get("timestamp", datetime.now(timezone.utc).isoformat()).replace(":", "").replace("+", "_")
        for sim in results.get("simulations", []):
            sim_id = _safe_filename(sim.get("id", "simulation"))
            summary_path = os.path.join(self.json_results_dir, f"{sim_id}_summary.json")
            trades_path = os.path.join(self.json_results_dir, f"{sim_id}_trades.json")
            timestamped_summary_path = os.path.join(self.json_results_dir, f"{sim_id}_{timestamp}_summary.json")
            timestamped_trades_path = os.path.join(self.json_results_dir, f"{sim_id}_{timestamp}_trades.json")
            trades = sim.get("all_trades", sim.get("recent_trades", []))
            with open(summary_path, "w", encoding="utf-8") as file_handle:
                json.dump(sim, file_handle, indent=2)
            with open(trades_path, "w", encoding="utf-8") as file_handle:
                json.dump(trades, file_handle, indent=2)
            with open(timestamped_summary_path, "w", encoding="utf-8") as file_handle:
                json.dump(sim, file_handle, indent=2)
            with open(timestamped_trades_path, "w", encoding="utf-8") as file_handle:
                json.dump(trades, file_handle, indent=2)
            logger.info(f"Saved JSON artifacts: {summary_path}, {trades_path}")
        
    def run(self):
        if not os.path.exists(self.csv_path):
            logger.error(f"Data file not found: {self.csv_path}")
            return

        # Load config
        with open(self.config_path, "r") as f:
            config = json.load(f)
        
        self.sim_manager.initialize_simulations(
            configs=config.get("simulations", []),
            defaults=config.get("defaults", {})
        )
        
        logger.info(f"Starting backtest using data from {self.csv_path}")
        
        # Load bars from CSV
        bars = []
        skipped_rth = 0
        with open(self.csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if self._trading_hours and not _bar_in_regular_session_et(row["date"], self._trading_hours):
                    skipped_rth += 1
                    continue
                bar = NormalizedBar(
                    date=row["date"],
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    barCount=int(row["barCount"]),
                    wap=float(row["wap"]),
                    source="historical_csv"
                )
                bars.append(bar)

        if self.regular_session_only:
            logger.info(f"Regular session (ET) filter: skipped {skipped_rth} bars outside RTH.")
        logger.info(f"Loaded {len(bars)} bars. Running simulations...")
        
        # Process bars
        last_bar = None
        for i, bar in enumerate(bars):
            # We use on_live_bar so SimManager exports/logs correctly
            self.sim_manager.on_live_bar(bar)
            last_bar = bar
            if i % 1000 == 0:
                logger.info(f"Processed {i}/{len(bars)} bars...")
        
        # Force close positions at last price for final PnL calculation
        if last_bar:
            logger.info(f"Force closing positions at last price: {last_bar.close}")
            for vp in self.sim_manager.portfolios:
                if vp.position != 0:
                    # Manually trigger a sell (or buy if short)
                    sold_quantity = vp.position
                    revenue = sold_quantity * last_bar.close
                    commission = getattr(vp, "commission_per_trade", 0.0)
                    pnl = (last_bar.close - vp.avg_cost) * sold_quantity - commission
                    vp.balance += revenue - commission
                    vp.realized_pnl += pnl
                    if hasattr(vp, "total_commissions"):
                        vp.total_commissions += commission
                    trade_event = {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "sim_id": vp.sim_id,
                        "side": "SELL",
                        "qty": sold_quantity,
                        "price": round(float(last_bar.close), 4),
                        "commission": round(float(commission), 4),
                        "realized_pnl": round(float(pnl), 4),
                        "forced_close": True,
                        "signal_reason": "forced_close",
                        "exit_reason": "forced_close",
                    }
                    vp.recent_trades.appendleft(trade_event)
                    if hasattr(vp, "all_trades"):
                        vp.all_trades.append(trade_event)
                    self.sim_manager.recent_trades.appendleft(trade_event)
                    vp.position = 0
                    vp.avg_cost = 0.0
                    vp.equity = vp.balance
                    vp.total_trades += 1
            
        logger.info("Backtest complete. Exporting results...")
        results = self.sim_manager.export_results()
        self._export_json_artifacts(results)
        self.display_summary()

    def display_summary(self):
        print("\n" + "="*50)
        print(f"HISTORICAL BACKTEST SUMMARY")
        print("="*50)
        
        for vp in self.sim_manager.portfolios:
            print(f"Simulation ID: {vp.sim_id}")
            print(f"  Name:          {vp.name}")
            print(f"  Final Balance: ${vp.balance:,.2f}")
            print(f"  Final Equity:  ${vp.equity:,.2f}")
            print(f"  Total Trades:  {vp.total_trades}")
            print(f"  Realized PnL:  ${vp.realized_pnl:,.2f}")
            print("-" * 30)
        print("="*50 + "\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Replay 5m CSV bars through SimManager strategies.")
    parser.add_argument("--csv", default="data/TSLA_5m_yf_60d.csv", help="Path to normalized 5m CSV")
    parser.add_argument(
        "--config",
        default="simulation/config/simulation_config.json",
        help="Simulation JSON (default simulation_config.json runs V4B)",
    )
    parser.add_argument(
        "--regular-session-only",
        action="store_true",
        help="Keep only America/New_York regular session (09:30–16:00 ET, weekdays); CSV times are UTC.",
    )
    parser.add_argument(
        "--json-results-dir",
        default="simulation/results/historical/json",
        help="Directory for per-run summary/trade JSON artifacts.",
    )
    args = parser.parse_args()

    backtester = HistoricalBacktester(
        args.csv,
        args.config,
        regular_session_only=args.regular_session_only,
        json_results_dir=args.json_results_dir,
    )
    backtester.run()
    
    # Copy the summary result next to historical data with a symbol-specific name.
    import shutil
    source_summary = "simulation/results/historical/latest_sims.json"
    csv_base = os.path.basename(args.csv)
    symbol = csv_base.split("_", 1)[0] if "_" in csv_base else os.path.splitext(csv_base)[0]
    dest_summary = os.path.join("data", f"{symbol}_60d_backtest_results.json")
    if os.path.exists(source_summary):
        try:
            shutil.copy(source_summary, dest_summary)
            print(f"Results copied to {dest_summary}")
        except OSError as exc:
            logger.warning(f"Backtest summary exported, but copy to {dest_summary} failed: {exc}")

