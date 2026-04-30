from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import pandas as pd
import yfinance as yf

from core.market_data_provider import NormalizedBar, from_yfinance_row

logger = logging.getLogger(__name__)


class YFinancePollingProvider:
    """Polls yfinance and emits normalized bars for backfill and live updates."""

    def __init__(
        self,
        symbol: str,
        interval: str = "5m",
        backfill_period: str = "5d",
        lookback_period: str = "2d",
        poll_seconds: int = 20,
        max_backfill_bars: int = 500,
        on_backfill_bar: Optional[Callable[[NormalizedBar], None]] = None,
        on_backfill_complete: Optional[Callable[[], None]] = None,
        on_live_bar: Optional[Callable[[NormalizedBar], None]] = None,
        on_status: Optional[Callable[[Dict[str, object]], None]] = None,
        prepost: bool = False,
    ):
        self.symbol = symbol.upper().strip()
        self.interval = interval
        self.backfill_period = backfill_period
        self.lookback_period = lookback_period
        self.poll_seconds = max(5, int(poll_seconds))
        self.max_backfill_bars = max(1, int(max_backfill_bars))
        self.prepost = prepost

        self.on_backfill_bar = on_backfill_bar
        self.on_backfill_complete = on_backfill_complete
        self.on_live_bar = on_live_bar
        self.on_status = on_status

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_emitted_signature: Optional[tuple] = None
        self._last_emitted_timestamp: Optional[datetime] = None
        self._consecutive_errors = 0
        self._last_error = ""
        self._last_poll_diag: Optional[Dict[str, Any]] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="yfinance-provider", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _emit_status(self, payload: Dict[str, object]):
        if not self.on_status:
            return
        try:
            self.on_status(payload)
        except Exception as exc:
            logger.error(f"YFinance status callback failed: {exc}")

    def _download_frame(self, period: str) -> pd.DataFrame:
        frame = yf.download(
            tickers=self.symbol,
            period=period,
            interval=self.interval,
            auto_adjust=False,
            prepost=self.prepost,
            progress=False,
            threads=False,
            group_by="column",
        )

        if frame is None or frame.empty:
            return pd.DataFrame()

        if isinstance(frame.columns, pd.MultiIndex):
            if self.symbol in frame.columns.get_level_values(0):
                frame = frame[self.symbol]
            elif self.symbol in frame.columns.get_level_values(-1):
                frame = frame.xs(self.symbol, axis=1, level=-1, drop_level=True)
            else:
                frame.columns = [col[0] for col in frame.columns]

        normalized_columns = {col: str(col).strip().lower() for col in frame.columns}
        frame = frame.rename(columns=normalized_columns)

        for required in ("open", "high", "low", "close", "volume"):
            if required not in frame.columns:
                raise ValueError(f"Missing expected column '{required}' in yfinance response")

        index_utc = pd.to_datetime(frame.index, utc=True, errors="coerce")
        valid_mask = ~index_utc.isna()
        frame = frame.loc[valid_mask].copy()
        frame.index = index_utc[valid_mask]

        frame = frame.dropna(subset=["open", "high", "low", "close"])
        return frame

    def _download_frame_diagnostics(self, period: str) -> Dict[str, Any]:
        """When a poll returns no bars, capture why (for logs / dashboard)."""
        out: Dict[str, Any] = {
            "symbol": self.symbol,
            "period": period,
            "interval": self.interval,
            "prepost": self.prepost,
        }
        try:
            frame = yf.download(
                tickers=self.symbol,
                period=period,
                interval=self.interval,
                auto_adjust=False,
                prepost=self.prepost,
                progress=False,
                threads=False,
                group_by="column",
            )
        except Exception as exc:
            out["stage"] = "yf_download_exception"
            out["error"] = repr(exc)
            out["traceback"] = traceback.format_exc()
            return out

        if frame is None:
            out["stage"] = "yf_download_none"
            return out

        out["raw_row_count"] = int(len(frame))
        out["raw_columns"] = [str(c) for c in frame.columns.tolist()] if len(frame.columns) else []

        if frame.empty:
            out["stage"] = "empty_dataframe_after_download"
            return out

        before = len(frame)
        if isinstance(frame.columns, pd.MultiIndex):
            if self.symbol in frame.columns.get_level_values(0):
                frame = frame[self.symbol]
            elif self.symbol in frame.columns.get_level_values(-1):
                frame = frame.xs(self.symbol, axis=1, level=-1, drop_level=True)
            else:
                frame.columns = [col[0] for col in frame.columns]
        out["after_multiindex_row_count"] = int(len(frame))
        out["after_multiindex_columns"] = [str(c) for c in frame.columns.tolist()]

        normalized_columns = {col: str(col).strip().lower() for col in frame.columns}
        frame = frame.rename(columns=normalized_columns)
        missing = [c for c in ("open", "high", "low", "close", "volume") if c not in frame.columns]
        if missing:
            out["stage"] = "missing_ohlcv_columns"
            out["missing"] = missing
            out["columns_after_rename"] = list(frame.columns)
            return out

        index_utc = pd.to_datetime(frame.index, utc=True, errors="coerce")
        invalid_ts = int(index_utc.isna().sum())
        frame = frame.loc[~index_utc.isna()].copy()
        frame.index = index_utc[~index_utc.isna()]
        out["invalid_timestamp_rows_dropped"] = invalid_ts
        out["after_valid_ts_row_count"] = int(len(frame))

        before_dropna = len(frame)
        frame = frame.dropna(subset=["open", "high", "low", "close"])
        out["ohlc_dropna_rows_removed"] = before_dropna - len(frame)
        out["final_row_count"] = int(len(frame))
        out["stage"] = "ok" if len(frame) > 0 else "all_rows_dropped_by_processing"
        return out

    def _load_bars(self, period: str) -> List[NormalizedBar]:
        frame = self._download_frame(period=period)
        if frame.empty:
            return []

        bars: List[NormalizedBar] = []
        for idx, row in frame.iterrows():
            try:
                bars.append(from_yfinance_row(idx, row, source="yfinance"))
            except Exception as exc:
                logger.warning(f"Skipping invalid yfinance row at {idx}: {exc}")

        bars.sort(key=lambda bar: bar.timestamp)
        return bars

    def _emit_backfill(self):
        backfill_bars = self._load_bars(period=self.backfill_period)
        logger.info(
            f"YFinance backfill used prepost={self.prepost} for warm-up "
            f"({len(backfill_bars)} bars). Live polling uses the same setting."
        )

        if not backfill_bars:
            raise RuntimeError("No bars returned from yfinance backfill")

        oldest = backfill_bars[0]
        newest = backfill_bars[-1]
        now = datetime.now(timezone.utc)
        newest_lag = max(0.0, (now - newest.timestamp).total_seconds())
        logger.info(
            f"YFinance backfill fetched {len(backfill_bars)} bars for {self.symbol}: "
            f"oldest={oldest.date}, newest={newest.date}, "
            f"newest_lag={newest_lag:.0f}s, prepost={self.prepost}"
        )

        if len(backfill_bars) > self.max_backfill_bars:
            backfill_bars = backfill_bars[-self.max_backfill_bars :]

        for bar in backfill_bars:
            if self._stop_event.is_set():
                return
            if self.on_backfill_bar:
                self.on_backfill_bar(bar)
            self._last_emitted_signature = bar.signature()
            self._last_emitted_timestamp = bar.timestamp

        if self.on_backfill_complete:
            self.on_backfill_complete()

    def _emit_new_live_bars(self):
        last_period = self.lookback_period
        bars = self._load_bars(period=last_period)
        if not bars:
            logger.warning(
                "YFinance live poll: empty (symbol=%s period=%s interval=%s prepost=%s), retry 1.5s",
                self.symbol,
                self.lookback_period,
                self.interval,
                self.prepost,
            )
            time.sleep(1.5)
            bars = self._load_bars(period=last_period)
        if not bars:
            for alt in ("5d", "7d", "1mo"):
                if self.lookback_period == alt:
                    continue
                last_period = alt
                logger.warning(
                    "YFinance live poll: still empty, trying lookback=%s (symbol=%s interval=%s)",
                    alt,
                    self.symbol,
                    self.interval,
                )
                bars = self._load_bars(period=alt)
                if bars:
                    break
        if not bars:
            self._last_poll_diag = self._download_frame_diagnostics(period=last_period)
            diag = self._last_poll_diag
            detail = "; ".join(
                f"{k}={v}" for k, v in sorted(diag.items()) if k != "traceback"
            )
            tb = diag.get("traceback")
            if tb:
                logger.error(
                    "YFinance poll returned 0 bars. Diagnostics:\n%s\n%s",
                    detail,
                    tb,
                )
            else:
                logger.error("YFinance poll returned 0 bars. Diagnostics: %s", detail)
            raise RuntimeError(f"No bars from yfinance poll | {detail}")

        latest = bars[-1]
        emitted_count = 0

        for bar in bars:
            if self._last_emitted_timestamp is None or bar.timestamp > self._last_emitted_timestamp:
                accepted = True
                if self.on_live_bar:
                    accepted = self.on_live_bar(bar) is not False
                if not accepted:
                    continue
                self._last_emitted_signature = bar.signature()
                self._last_emitted_timestamp = bar.timestamp
                emitted_count += 1

        # If only one bar exists but its payload changed (rare), emit update once.
        if emitted_count == 0 and self._last_emitted_timestamp == latest.timestamp:
            latest_signature = latest.signature()
            if latest_signature != self._last_emitted_signature and self.on_live_bar:
                accepted = self.on_live_bar(latest) is not False
                if not accepted:
                    return
                self._last_emitted_signature = latest_signature
                emitted_count += 1

        now = datetime.now(timezone.utc)
        lag_seconds = max(0.0, (now - latest.timestamp).total_seconds())
        self._emit_status(
            {
                "provider": "yfinance",
                "last_bar_ts": latest.date,
                "lag_seconds": round(lag_seconds, 1),
                "consecutive_errors": self._consecutive_errors,
                "last_error": self._last_error,
                "emitted": emitted_count,
            }
        )

    def _run(self):
        logger.info(
            f"YFinance provider starting for {self.symbol} "
            f"(interval={self.interval}, prepost={self.prepost}, "
            f"backfill={self.backfill_period}, lookback={self.lookback_period})"
        )

        try:
            self._emit_backfill()
            self._consecutive_errors = 0
            self._last_error = ""
            self._emit_status(
                {
                    "provider": "yfinance",
                    "last_bar_ts": None if self._last_emitted_timestamp is None else self._last_emitted_timestamp.isoformat(),
                    "lag_seconds": None,
                    "consecutive_errors": self._consecutive_errors,
                    "last_error": self._last_error,
                    "phase": "backfill_complete",
                }
            )
        except Exception as exc:
            self._consecutive_errors += 1
            self._last_error = str(exc)
            logger.error(f"YFinance backfill failed: {exc}")
            self._emit_status(
                {
                    "provider": "yfinance",
                    "last_bar_ts": None,
                    "lag_seconds": None,
                    "consecutive_errors": self._consecutive_errors,
                    "last_error": self._last_error,
                    "phase": "backfill_error",
                }
            )

        while not self._stop_event.wait(self.poll_seconds):
            try:
                self._emit_new_live_bars()
                self._consecutive_errors = 0
                self._last_error = ""
            except Exception as exc:
                self._consecutive_errors += 1
                self._last_error = str(exc)
                diag = self._last_poll_diag
                if diag is None:
                    try:
                        diag = self._download_frame_diagnostics(period=self.lookback_period)
                    except Exception as diag_exc:
                        diag = {"stage": "diagnostic_fetch_failed", "error": repr(diag_exc)}
                self._last_poll_diag = diag
                logger.warning(
                    "YFinance poll failed (%s): %s | diagnostics=%s",
                    self._consecutive_errors,
                    exc,
                    diag,
                )
                self._emit_status(
                    {
                        "provider": "yfinance",
                        "last_bar_ts": None if self._last_emitted_timestamp is None else self._last_emitted_timestamp.isoformat(),
                        "lag_seconds": None,
                        "consecutive_errors": self._consecutive_errors,
                        "last_error": self._last_error,
                        "phase": "poll_error",
                        "diagnostics": diag,
                    }
                )

        logger.info("YFinance provider stopped")
