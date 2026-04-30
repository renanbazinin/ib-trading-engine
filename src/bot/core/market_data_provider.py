from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

import pandas as pd


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_timestamp(value: Any) -> datetime:
    """Normalize a timestamp-like value to a UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = pd.to_datetime(value, utc=True, errors="coerce")
        if pd.isna(dt):
            raise ValueError(f"Could not parse timestamp: {value}")
        if hasattr(dt, "to_pydatetime"):
            dt = dt.to_pydatetime()

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc).replace(microsecond=0)


@dataclass(frozen=True)
class NormalizedBar:
    """Provider-agnostic OHLCV bar shape consumed by strategies and simulations."""

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    barCount: int = 0
    wap: float = 0.0
    source: str = "unknown"
    timestamp: datetime | None = None

    def __post_init__(self):
        ts = normalize_timestamp(self.timestamp if self.timestamp is not None else self.date)
        object.__setattr__(self, "timestamp", ts)
        object.__setattr__(self, "date", ts.isoformat())
        object.__setattr__(self, "open", _coerce_float(self.open))
        object.__setattr__(self, "high", _coerce_float(self.high))
        object.__setattr__(self, "low", _coerce_float(self.low))
        object.__setattr__(self, "close", _coerce_float(self.close))
        object.__setattr__(self, "volume", _coerce_float(self.volume))
        object.__setattr__(self, "barCount", _coerce_int(self.barCount))
        object.__setattr__(self, "wap", _coerce_float(self.wap, self.close))

    def signature(self) -> tuple:
        return (
            self.date,
            round(self.open, 6),
            round(self.high, 6),
            round(self.low, 6),
            round(self.close, 6),
            round(self.volume, 2),
        )


@runtime_checkable
class MarketDataProvider(Protocol):
    def start(self) -> None:
        ...

    def stop(self) -> None:
        ...


def from_ib_bar(bar: Any, source: str = "broker") -> NormalizedBar:
    return NormalizedBar(
        date=getattr(bar, "date", datetime.now(timezone.utc).isoformat()),
        open=getattr(bar, "open", 0.0),
        high=getattr(bar, "high", 0.0),
        low=getattr(bar, "low", 0.0),
        close=getattr(bar, "close", 0.0),
        volume=getattr(bar, "volume", 0.0),
        barCount=getattr(bar, "barCount", 0),
        wap=getattr(bar, "wap", getattr(bar, "close", 0.0)),
        source=source,
    )


def from_yfinance_row(index_value: Any, row: Any, source: str = "yfinance") -> NormalizedBar:
    open_value = row.get("open", row.get("Open", 0.0))
    high_value = row.get("high", row.get("High", 0.0))
    low_value = row.get("low", row.get("Low", 0.0))
    close_value = row.get("close", row.get("Close", 0.0))
    volume_value = row.get("volume", row.get("Volume", 0.0))

    return NormalizedBar(
        date=str(index_value),
        open=open_value,
        high=high_value,
        low=low_value,
        close=close_value,
        volume=volume_value,
        barCount=0,
        wap=close_value,
        source=source,
        timestamp=normalize_timestamp(index_value),
    )
