"""Inclusive log time window applied to parsed records before analysis.

NVLSM lines often lack a year; Fabric Manager uses ISO or bracketed datetimes.
This module only decides *which* records enter the existing pipeline — grouping,
cross-checks, and report layout stay unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional


class TimeRangeError(ValueError):
    """Invalid user-supplied --since/--until value."""


_USER_DATE_FMT = "%Y-%m-%d"
_USER_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
)


def parse_user_time(value: str, *, end_of_day_if_date: bool = False) -> datetime:
    """Parse a CLI timestamp.

    Date-only ``YYYY-MM-DD`` is midnight, or 23:59:59.999999 when
    ``end_of_day_if_date`` is true (used for ``--until`` so the whole day is kept).
    """
    raw = (value or "").strip()
    if not raw:
        raise TimeRangeError("empty timestamp")
    s = raw
    if s.endswith(("Z", "z")):
        s = s[:-1]
    try:
        dt = datetime.strptime(s, _USER_DATE_FMT)
    except ValueError:
        dt = None
    else:
        if end_of_day_if_date:
            return dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        return dt
    for fmt in _USER_DT_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise TimeRangeError(
        f"unrecognized timestamp {value!r}; use YYYY-MM-DD or YYYY-MM-DD HH:MM[:SS]"
    )


def parse_log_ts(ts: str) -> Optional[datetime]:
    """Parse a timestamp already extracted from NVLSM / FM log records."""
    if not ts:
        return None
    s = ts.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1]
    if len(s) > 10 and s[10] == "T":
        s = s[:10] + " " + s[11:]
    # Drop a trailing numeric timezone offset: 2026-04-08 04:00:00+00:00
    if len(s) >= 25 and s[-6] in "+-" and s[-3] == ":":
        s = s[:-6]
    elif len(s) >= 22 and s[-5] in "+-" and s[-3] == ":":
        s = s[:-5]
    if "." in s:
        head, tail = s.split(".", 1)
        digits = "".join(c for c in tail if c.isdigit())[:6].ljust(6, "0")
        try:
            return datetime.strptime(f"{head}.{digits}", "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            s = head
    for fmt, width in (
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%Y-%m-%d %H:%M", 16),
        ("%Y-%m-%d", 10),
    ):
        try:
            return datetime.strptime(s[:width], fmt)
        except (ValueError, IndexError):
            continue
    return None


def _is_start_of_day(dt: datetime) -> bool:
    return dt.hour == 0 and dt.minute == 0 and dt.second == 0 and dt.microsecond == 0


def _is_end_of_day(dt: datetime) -> bool:
    return (
        dt.hour == 23
        and dt.minute == 59
        and dt.second == 59
        and dt.microsecond == 999999
    )


def format_bound(dt: Optional[datetime], *, is_end: bool = False) -> str:
    if dt is None:
        return "unbounded"
    if is_end and _is_end_of_day(dt):
        return dt.strftime("%Y-%m-%d") + " (end of day)"
    if not is_end and _is_start_of_day(dt):
        return dt.strftime("%Y-%m-%d")
    if dt.microsecond:
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f").rstrip("0").rstrip(".")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class TimeWindow:
    """Inclusive ``[start, end]``. Either bound may be omitted (unbounded)."""

    start: Optional[datetime] = None
    end: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.start is not None and self.end is not None and self.start > self.end:
            raise TimeRangeError("--since is after --until")

    def contains(self, dt: datetime) -> bool:
        if self.start is not None and dt < self.start:
            return False
        if self.end is not None and dt > self.end:
            return False
        return True

    def contains_datetime(
        self,
        dt: Optional[datetime],
        fallback_ts: str = "",
    ) -> bool:
        """True when the record can be placed inside this window.

        Untimestamped records are dropped (they cannot be ranged).
        """
        if dt is None:
            dt = parse_log_ts(fallback_ts)
        if dt is None:
            return False
        return self.contains(dt)

    def contains_ts(self, ts: str) -> bool:
        return self.contains_datetime(parse_log_ts(ts), ts)

    def format(self) -> str:
        return f"{format_bound(self.start)} – {format_bound(self.end, is_end=True)}"

    def coverage_notice(self) -> str:
        """One-line report header: the window applies to the whole analysis."""
        return (
            "This analysis only covers related logs from "
            f"{self.format()} (inclusive; records outside this window were not analyzed)."
        )


def filter_fm_events(
    events: Iterable[Dict[str, Any]],
    window: Optional[TimeWindow],
) -> List[Dict[str, Any]]:
    """Keep Fabric Manager events whose ``ts`` falls in ``window``."""
    if window is None:
        return list(events)
    return [ev for ev in events if window.contains_ts(ev.get("ts") or "")]
