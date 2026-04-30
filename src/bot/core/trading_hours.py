from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional
from zoneinfo import ZoneInfo


DEFAULT_SESSIONS = (
    "overnight:20:00-03:50,"
    "pre_market:04:00-09:30,"
    "regular:09:30-16:00,"
    "after_hours:16:00-20:00"
)


@dataclass(frozen=True)
class TradingSession:
    name: str
    start_minute: int
    end_minute: int

    @property
    def crosses_midnight(self) -> bool:
        return self.end_minute <= self.start_minute


@dataclass(frozen=True)
class TradingSessionStatus:
    enabled: bool
    is_open: bool
    session_name: str
    reason: str
    now_et: str
    next_open: Optional[str]
    next_close: Optional[str]
    minutes_to_open: Optional[int]

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "is_open": self.is_open,
            "session_name": self.session_name,
            "reason": self.reason,
            "now_et": self.now_et,
            "next_open": self.next_open,
            "next_close": self.next_close,
            "minutes_to_open": self.minutes_to_open,
        }


def _parse_hhmm(value: str) -> int:
    hour_raw, minute_raw = value.strip().split(":", 1)
    hour = int(hour_raw)
    minute = int(minute_raw)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid session time: {value}")
    return hour * 60 + minute


def parse_sessions(raw_value: str | None) -> List[TradingSession]:
    raw = (raw_value or DEFAULT_SESSIONS).strip()
    sessions: List[TradingSession] = []
    for chunk in raw.split(","):
        token = chunk.strip()
        if not token:
            continue
        name, hours = token.split(":", 1)
        start_raw, end_raw = hours.split("-", 1)
        sessions.append(
            TradingSession(
                name=name.strip(),
                start_minute=_parse_hhmm(start_raw),
                end_minute=_parse_hhmm(end_raw),
            )
        )
    if not sessions:
        raise ValueError("At least one trading session is required")
    return sessions


def interval_to_minutes(raw_value: str | None, default: int = 5) -> int:
    raw = str(raw_value or "").strip().lower()
    if not raw:
        return default
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return default
    value = max(1, int(digits))
    if "h" in raw:
        return value * 60
    return value


class TradingHours:
    def __init__(
        self,
        sessions: Iterable[TradingSession],
        timezone_name: str = "America/New_York",
        enabled: bool = True,
    ):
        self.sessions = list(sessions)
        self.timezone_name = timezone_name
        self.tz = ZoneInfo(timezone_name)
        self.enabled = bool(enabled)

    @classmethod
    def from_config(
        cls,
        sessions_raw: str | None = None,
        timezone_name: str = "America/New_York",
        enabled: bool = True,
    ) -> "TradingHours":
        return cls(
            sessions=parse_sessions(sessions_raw),
            timezone_name=timezone_name,
            enabled=enabled,
        )

    def localize(self, value: datetime | None = None) -> datetime:
        if value is None:
            value = datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(self.tz).replace(second=0, microsecond=0)

    def _session_for_local(self, local_dt: datetime) -> Optional[TradingSession]:
        minute = local_dt.hour * 60 + local_dt.minute
        for session in self.sessions:
            if session.crosses_midnight:
                if minute >= session.start_minute:
                    start_day = local_dt.weekday()
                elif minute < session.end_minute:
                    start_day = (local_dt - timedelta(days=1)).weekday()
                else:
                    continue

                # US equity overnight trading runs Sunday evening through Friday morning.
                if start_day in (6, 0, 1, 2, 3):
                    return session
                continue

            if session.start_minute <= minute < session.end_minute and local_dt.weekday() < 5:
                return session

        return None

    def _next_transition(self, local_dt: datetime, want_open: bool) -> Optional[datetime]:
        probe = local_dt.replace(second=0, microsecond=0)
        was_open = self._session_for_local(probe) is not None
        for offset in range(0, 10 * 24 * 60 + 1):
            candidate = probe + timedelta(minutes=offset)
            is_open = self._session_for_local(candidate) is not None
            if want_open and is_open and (offset == 0 or not was_open):
                return candidate
            if not want_open and not is_open and (offset == 0 or was_open):
                return candidate
            was_open = is_open
        return None

    def status(self, value: datetime | None = None) -> TradingSessionStatus:
        local_dt = self.localize(value)
        if not self.enabled:
            return TradingSessionStatus(
                enabled=False,
                is_open=True,
                session_name="disabled",
                reason="trading_hours_disabled",
                now_et=local_dt.isoformat(),
                next_open=None,
                next_close=None,
                minutes_to_open=None,
            )

        session = self._session_for_local(local_dt)
        next_open = None if session else self._next_transition(local_dt, want_open=True)
        next_close = self._next_transition(local_dt, want_open=False)
        minutes_to_open = None
        if session is None and next_open is not None:
            minutes_to_open = max(0, int((next_open - local_dt).total_seconds() // 60))

        return TradingSessionStatus(
            enabled=True,
            is_open=session is not None,
            session_name=session.name if session else "closed",
            reason="open" if session else "outside_trading_hours",
            now_et=local_dt.isoformat(),
            next_open=next_open.isoformat() if next_open else None,
            next_close=next_close.isoformat() if next_close else None,
            minutes_to_open=minutes_to_open,
        )
