"""US equities market calendar — regular session helper.

Pure, testable helper.  No I/O, no broker calls, no env reads.
Determines whether a given datetime falls inside the NYSE regular
session (09:30–16:00 America/New_York), respecting weekends and a
statically-computed US holiday table.

Ported from ``traderjoe_scan.sh``'s inline holiday logic so the
scheduler can consult the same rules without shelling out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional, Set

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python <3.9 fallback
    ZoneInfo = None  # type: ignore


ET_TIMEZONE_NAME = "America/New_York"
REGULAR_SESSION_OPEN = time(9, 30)
REGULAR_SESSION_CLOSE = time(16, 0)


def _et_zone():
    if ZoneInfo is None:  # pragma: no cover
        raise RuntimeError("zoneinfo is not available on this Python")
    return ZoneInfo(ET_TIMEZONE_NAME)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    day = next_month - timedelta(days=1)
    while day.weekday() != weekday:
        day -= timedelta(days=1)
    return day


def _easter_sunday(year: int) -> date:
    # Anonymous Gregorian algorithm.
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed_fixed_holiday(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def us_market_holidays(year: int) -> Set[date]:
    """NYSE full-day-closed holidays for ``year``.

    Matches the widely-used NYSE observance rules (fixed-date
    holidays observed Fri/Mon when falling on a weekend).  Good
    Friday is included even though it is not a federal holiday.
    """
    return {
        _observed_fixed_holiday(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),  # MLK
        _nth_weekday(year, 2, 0, 3),  # Presidents
        _easter_sunday(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial
        _observed_fixed_holiday(date(year, 6, 19)),  # Juneteenth
        _observed_fixed_holiday(date(year, 7, 4)),  # July 4
        _nth_weekday(year, 9, 0, 1),  # Labor
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed_fixed_holiday(date(year, 12, 25)),  # Christmas
    }


@dataclass(frozen=True)
class SessionStatus:
    """Snapshot of the market's status at a given moment."""

    is_regular_session: bool
    reason: str
    now_et: datetime

    def to_dict(self) -> dict:
        return {
            "is_regular_session": self.is_regular_session,
            "reason": self.reason,
            "now_et": self.now_et.isoformat(),
        }


def session_status(now: Optional[datetime] = None) -> SessionStatus:
    """Return a :class:`SessionStatus` for ``now`` (or the process's
    current time).  Timezone-naive datetimes are interpreted as ET.
    """
    if now is None:
        now_et = datetime.now(_et_zone())
    elif now.tzinfo is None:
        now_et = now.replace(tzinfo=_et_zone())
    else:
        now_et = now.astimezone(_et_zone())
    if now_et.weekday() >= 5:
        return SessionStatus(False, "weekend", now_et)
    holidays = (
        us_market_holidays(now_et.year)
        | us_market_holidays(now_et.year - 1)
        | us_market_holidays(now_et.year + 1)
    )
    if now_et.date() in holidays:
        return SessionStatus(False, "holiday", now_et)
    if now_et.time() < REGULAR_SESSION_OPEN:
        return SessionStatus(False, "pre_market", now_et)
    if now_et.time() > REGULAR_SESSION_CLOSE:
        return SessionStatus(False, "post_market", now_et)
    return SessionStatus(True, "regular_session", now_et)


def is_regular_session(now: Optional[datetime] = None) -> bool:
    """Boolean shortcut over :func:`session_status`."""
    return session_status(now).is_regular_session


__all__ = [
    "ET_TIMEZONE_NAME",
    "REGULAR_SESSION_CLOSE",
    "REGULAR_SESSION_OPEN",
    "SessionStatus",
    "is_regular_session",
    "session_status",
    "us_market_holidays",
]
