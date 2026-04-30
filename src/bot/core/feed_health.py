from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _lag_seconds(reference_ts: Optional[datetime], now: Optional[datetime] = None) -> Optional[float]:
    if reference_ts is None:
        return None

    if now is None:
        now = _utc_now()

    return max(0.0, (now - reference_ts).total_seconds())


@dataclass
class FeedHealthSnapshot:
    primary_source: str
    active_source: str
    fallback_source: str
    primary_last_bar_ts: Optional[str]
    fallback_last_bar_ts: Optional[str]
    primary_lag_seconds: Optional[float]
    fallback_lag_seconds: Optional[float]
    active_lag_seconds: Optional[float]
    primary_is_stale: bool
    active_is_stale: bool
    fallback_available: bool
    should_pause_live: bool
    consecutive_primary_errors: int
    last_primary_error: str

    def as_dict(self) -> Dict[str, object]:
        return {
            "primary_source": self.primary_source,
            "active_source": self.active_source,
            "fallback_source": self.fallback_source,
            "primary_last_bar_ts": self.primary_last_bar_ts,
            "fallback_last_bar_ts": self.fallback_last_bar_ts,
            "primary_lag_seconds": None if self.primary_lag_seconds is None else round(self.primary_lag_seconds, 1),
            "fallback_lag_seconds": None if self.fallback_lag_seconds is None else round(self.fallback_lag_seconds, 1),
            "active_lag_seconds": None if self.active_lag_seconds is None else round(self.active_lag_seconds, 1),
            "primary_is_stale": self.primary_is_stale,
            "active_is_stale": self.active_is_stale,
            "fallback_available": self.fallback_available,
            "should_pause_live": self.should_pause_live,
            "consecutive_primary_errors": self.consecutive_primary_errors,
            "last_primary_error": self.last_primary_error,
        }


class FeedHealthMonitor:
    """Tracks primary/fallback data freshness and computes guardrail signals."""

    def __init__(
        self,
        primary_source: str = "yfinance",
        fallback_source: str = "broker",
        stale_after_seconds: int = 900,
        auto_pause_live_on_stale: bool = True,
    ):
        self.primary_source = primary_source
        self.fallback_source = fallback_source
        self.active_source = primary_source
        self.stale_after_seconds = max(1, int(stale_after_seconds))
        self.auto_pause_live_on_stale = bool(auto_pause_live_on_stale)

        self.primary_last_bar_ts: Optional[datetime] = None
        self.fallback_last_bar_ts: Optional[datetime] = None

        self.consecutive_primary_errors: int = 0
        self.last_primary_error: str = ""

    def on_primary_bar(self, timestamp: datetime):
        self.primary_last_bar_ts = timestamp.astimezone(timezone.utc)
        self.consecutive_primary_errors = 0
        self.last_primary_error = ""

    def on_fallback_bar(self, timestamp: datetime):
        self.fallback_last_bar_ts = timestamp.astimezone(timezone.utc)

    def on_primary_error(self, error_text: str):
        self.consecutive_primary_errors += 1
        self.last_primary_error = str(error_text)

    def set_active_source(self, source: str):
        normalized = source.strip().lower()
        if normalized in (self.primary_source, self.fallback_source):
            self.active_source = normalized

    def snapshot(self, now: Optional[datetime] = None) -> FeedHealthSnapshot:
        if now is None:
            now = _utc_now()

        primary_lag = _lag_seconds(self.primary_last_bar_ts, now)
        fallback_lag = _lag_seconds(self.fallback_last_bar_ts, now)

        if self.active_source == self.fallback_source:
            active_lag = fallback_lag
        else:
            active_lag = primary_lag

        primary_is_stale = primary_lag is None or primary_lag > self.stale_after_seconds
        active_is_stale = active_lag is None or active_lag > self.stale_after_seconds
        fallback_available = (
            fallback_lag is not None
            and fallback_lag <= max(self.stale_after_seconds * 2, 300)
        )

        should_pause_live = self.auto_pause_live_on_stale and active_is_stale

        return FeedHealthSnapshot(
            primary_source=self.primary_source,
            active_source=self.active_source,
            fallback_source=self.fallback_source,
            primary_last_bar_ts=None if self.primary_last_bar_ts is None else self.primary_last_bar_ts.isoformat(),
            fallback_last_bar_ts=None if self.fallback_last_bar_ts is None else self.fallback_last_bar_ts.isoformat(),
            primary_lag_seconds=primary_lag,
            fallback_lag_seconds=fallback_lag,
            active_lag_seconds=active_lag,
            primary_is_stale=primary_is_stale,
            active_is_stale=active_is_stale,
            fallback_available=fallback_available,
            should_pause_live=should_pause_live,
            consecutive_primary_errors=self.consecutive_primary_errors,
            last_primary_error=self.last_primary_error,
        )

    def should_switch_to_fallback(self) -> bool:
        snap = self.snapshot()
        return snap.primary_is_stale and snap.fallback_available

    def should_recover_primary(self) -> bool:
        snap = self.snapshot()
        return not snap.primary_is_stale
