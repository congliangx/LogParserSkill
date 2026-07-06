"""Timestamp parsing + syslog year inference + timezone-offset helpers.

The two report families stamp time differently:

- nvos (NMX-C) reports use full ``YYYY-MM-DD HH:MM:SS`` everywhere.
- nv-bug-report IMEX groups use ``Mon DD YYYY HH:MM:SS`` (year present).
- nv-bug-report Xid raw groups use syslog ``Mon DD HH:MM:SS`` (NO year) —
  the year must be inferred from the report's collection ``Date``.

Neither family embeds a timezone; both are local wall-clock at their capture
host. Correlation therefore applies a caller-supplied minute offset to one side
(see ``shift``) to line the two clocks up.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def parse_full(s: str) -> Optional[datetime]:
    """Parse ``YYYY-MM-DD HH:MM:SS`` or ``YYYY-MM-DD HH:MM``."""
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_month_day_year(s: str) -> Optional[datetime]:
    """Parse ``Mon DD YYYY HH:MM:SS`` (nv-bug-report IMEX group timestamps)."""
    try:
        return datetime.strptime((s or "").strip(), "%b %d %Y %H:%M:%S")
    except ValueError:
        return None


def parse_bracket(s: str) -> Optional[datetime]:
    """Parse a Fabric-Manager bracket stamp ``Mon DD YYYY HH:MM:SS`` (no brackets)."""
    return parse_month_day_year(s)


def infer_syslog_year(month: int, day: int, hh: int, mm: int, ss: int,
                      ref: datetime) -> Optional[datetime]:
    """Attach a year to a syslog ``Mon DD HH:MM:SS`` stamp using ``ref``.

    ``ref`` is the report's collection time. Logs are captured at or before it,
    so we assume ``ref.year`` and step back a year if that lands more than a day
    in the future (classic syslog Dec-vs-Jan rollover handling).
    """
    for year in (ref.year, ref.year - 1, ref.year + 1):
        try:
            dt = datetime(year, month, day, hh, mm, ss)
        except ValueError:
            continue  # e.g. Feb 29 on a non-leap candidate year
        if dt - ref <= timedelta(days=1):
            return dt
    # Fallback: force ref.year, clamping an invalid day is not worth it here.
    try:
        return datetime(ref.year, month, day, hh, mm, ss)
    except ValueError:
        return None


def parse_syslog(mon: str, day: str, hms: str, ref: datetime) -> Optional[datetime]:
    """Parse ``Mon DD HH:MM:SS`` pieces with year inferred from ``ref``."""
    m = _MONTHS.get(mon)
    if m is None:
        return None
    try:
        hh, mm, ss = (int(x) for x in hms.split(":"))
        d = int(day)
    except (ValueError, AttributeError):
        return None
    return infer_syslog_year(m, d, hh, mm, ss, ref)


def shift(dt: datetime, minutes: int) -> datetime:
    """Return ``dt`` shifted by ``minutes`` (used to align switch->tray clock)."""
    return dt + timedelta(minutes=minutes)


def fmt(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "-"
