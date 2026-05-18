"""Parse syslog-style timestamps from journal/dmesg-style lines in bug reports."""

from datetime import datetime

_CN_MONTH_MAP = {
    "1月": "Jan",
    "2月": "Feb",
    "3月": "Mar",
    "4月": "Apr",
    "5月": "May",
    "6月": "Jun",
    "7月": "Jul",
    "8月": "Aug",
    "9月": "Sep",
    "10月": "Oct",
    "11月": "Nov",
    "12月": "Dec",
}


def _normalize_syslog_ts(ts_str):
    """Normalize Chinese locale month names to English abbreviations."""
    for cn, en in sorted(_CN_MONTH_MAP.items(), key=lambda x: len(x[0]), reverse=True):
        if cn in ts_str:
            return ts_str.replace(cn, en)
    return ts_str


def _parse_syslog_ts(ts_str, ref_year=2026):
    """Parse timestamp into a datetime. Supports:
    - Syslog: 'Feb 28 19:50:49' or '3月 28 19:50:49'
    - ISO 8601: '2026-04-03T06:20:28' (from kern.log)
    Returns None on failure.
    """
    if not ts_str or ts_str.startswith("["):
        return None
    if ts_str[:4].isdigit() and "T" in ts_str:
        try:
            return datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            pass
    normalized = _normalize_syslog_ts(ts_str)
    try:
        dt = datetime.strptime(normalized, "%b %d %H:%M:%S")
        return dt.replace(year=ref_year)
    except ValueError:
        return None
