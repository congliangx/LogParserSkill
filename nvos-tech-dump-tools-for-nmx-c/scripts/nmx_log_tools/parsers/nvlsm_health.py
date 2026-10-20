"""NVLSM log health patterns (checks/nvlsm parse_logs_single_pass)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple, TYPE_CHECKING

from ..io.gzip_text import iter_lines

if TYPE_CHECKING:
    from ..time_range import TimeWindow


_TIMESTAMP = re.compile(r"^(\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})")


class YearTracker:
    """Infer the missing year for nvlsm.log lines from month rollovers.

    nvlsm.log entries carry only "Mon DD HH:MM:SS" -- no year. Within a single
    file and across rotated files fed in chronological order (oldest first),
    timestamps are monotonically non-decreasing. When the month drops sharply
    (interpreted as a Dec -> Jan wrap) we bump a relative year offset by 1.

    After feeding every line, the caller anchors the final offset to a known
    reference year (typically the newest log file's mtime year) so that
    base_year = reference_year - final_offset.
    """

    # Month decrease considered a year rollover. ~6 months avoids being tripped
    # by minor out-of-order events while still catching the Dec(12) -> Jan(1)
    # wrap-around (= -11).
    _ROLLOVER_THRESHOLD = 6

    def __init__(self) -> None:
        self._prev_month: Optional[int] = None
        self.offset: int = 0

    def feed(self, month: int) -> int:
        if (
            self._prev_month is not None
            and month < self._prev_month - self._ROLLOVER_THRESHOLD
        ):
            self.offset += 1
        self._prev_month = month
        return self.offset


def infer_reference_year(log_paths: Iterable[Path]) -> int:
    """Year for the most recent nvlsm log entry.

    Prefer the mtime of the newest log file (tar extraction preserves member
    mtimes), fall back to the current year when no path is usable.
    """
    newest_mtime: Optional[float] = None
    for p in log_paths:
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        if newest_mtime is None or mt > newest_mtime:
            newest_mtime = mt
    if newest_mtime is None:
        return datetime.now().year
    return datetime.fromtimestamp(newest_mtime).year
_PORT_STATE = re.compile(
    r"osm_spst_rcv_process:\s+Switch\s+(0x[0-9a-fA-F]+)\s+"
    r"MF\d+;([^:]+):([^\s]+)\s+port\s+(\d+)\s+\(([^)]+)\)\s+"
    r"changed\s+state\s+from\s+(\w+)\s+to\s+(\w+)"
)
_EPOCH = re.compile(r"E\d+\s+\d+:\d+:(\d+)\.\d+")
_FIELD = re.compile(r"String field '([^']+)'")


@dataclass
class LogErrorBucket:
    count: int = 0
    earliest: Optional[str] = None
    latest: Optional[str] = None
    fields: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class PortStateEvent:
    ts: str
    datetime: Optional[datetime]
    switch_guid: str
    switch_name: str
    device: str
    port: int
    port_name: str
    old_state: str
    new_state: str
    source: str
    line_no: int = 0


@dataclass
class NvlsmLogParseResult:
    invalid_topology: LogErrorBucket
    invalid_utf8: LogErrorBucket
    port_events: List[PortStateEvent]


def _resolve_range(bucket: LogErrorBucket, stamps: List[Tuple[Optional[datetime], str]]) -> None:
    if not stamps:
        return
    valid = [(d, s) for d, s in stamps if d is not None]
    if valid:
        valid.sort(key=lambda x: x[0])
        bucket.earliest = valid[0][1]
        bucket.latest = valid[-1][1]
    else:
        bucket.earliest = stamps[0][1]
        bucket.latest = stamps[-1][1]


def parse_nvlsm_logs(
    log_paths: List,
    *,
    extract_port_events: bool = True,
    time_window: Optional["TimeWindow"] = None,
) -> NvlsmLogParseResult:
    topo = LogErrorBucket()
    utf8 = LogErrorBucket()
    utf8_fields: set = set()
    port_events: List[PortStateEvent] = []
    port_event_offsets: List[int] = []
    # Each stamps list holds (datetime_placeholder, year_offset, ts_str).
    # The datetime starts with year=1900 for entries derived from the year-less
    # "Mon DD HH:MM:SS" prefix, and carries the real year for entries derived
    # from an epoch field. We finalize the year in a second pass below.
    topo_ts: List[Tuple[Optional[datetime], int, str]] = []
    utf8_ts: List[Tuple[Optional[datetime], int, str]] = []
    utf8_field_at: List[Optional[str]] = []
    tracker = YearTracker()

    for log_path in log_paths:
        source = log_path.name
        try:
            for line_no, line in enumerate(iter_lines(log_path), start=1):
                if "Invalid topology detected" in line:
                    topo.count += 1
                    m = _TIMESTAMP.match(line)
                    if m:
                        ts_str = m.group(1)
                        try:
                            dt = datetime.strptime(ts_str, "%b %d %H:%M:%S")
                            off = tracker.feed(dt.month)
                            topo_ts.append((dt, off, ts_str))
                        except ValueError:
                            topo_ts.append((None, tracker.offset, ts_str))

                if "invalid UTF-8" in line:
                    utf8.count += 1
                    fm = _FIELD.search(line)
                    field_name = fm.group(1) if fm else None
                    if field_name:
                        utf8_fields.add(field_name)
                    em = _EPOCH.search(line)
                    if em:
                        try:
                            dt = datetime.fromtimestamp(int(em.group(1)))
                            # Epoch entries already have the real year; do not
                            # route them through the tracker (they aren't part
                            # of the year-less "Mon DD" sequence).
                            utf8_ts.append((dt, tracker.offset, dt.strftime("%Y-%m-%d %H:%M:%S")))
                        except (ValueError, OSError, OverflowError):
                            utf8_ts.append((None, tracker.offset, em.group(0)))
                        utf8_field_at.append(field_name)
                    else:
                        m = _TIMESTAMP.match(line)
                        if m:
                            ts_str = m.group(1)
                            try:
                                dt = datetime.strptime(ts_str, "%b %d %H:%M:%S")
                                off = tracker.feed(dt.month)
                                utf8_ts.append((dt, off, ts_str))
                            except ValueError:
                                utf8_ts.append((None, tracker.offset, ts_str))
                            utf8_field_at.append(field_name)

                if extract_port_events and "osm_spst_rcv_process" in line:
                    m = _TIMESTAMP.match(line)
                    if not m:
                        continue
                    ts_str = m.group(1)
                    sm = _PORT_STATE.search(line)
                    if not sm:
                        continue
                    try:
                        dt = datetime.strptime(ts_str, "%b %d %H:%M:%S")
                        off = tracker.feed(dt.month)
                    except ValueError:
                        dt = None
                        off = tracker.offset
                    port_events.append(
                        PortStateEvent(
                            ts=ts_str,
                            datetime=dt,
                            switch_guid=sm.group(1),
                            switch_name=sm.group(2),
                            device=sm.group(3),
                            port=int(sm.group(4)),
                            port_name=sm.group(5),
                            old_state=sm.group(6),
                            new_state=sm.group(7),
                            source=source,
                            line_no=line_no,
                        )
                    )
                    port_event_offsets.append(off)
        except OSError as e:
            topo.errors.append(f"Error reading {log_path}: {e}")
            utf8.errors.append(f"Error reading {log_path}: {e}")

    # Anchor relative offsets to a calendar year so the latest entry lands on
    # the newest log file's mtime year.
    base_year = infer_reference_year(log_paths) - tracker.offset

    for ev, off in zip(port_events, port_event_offsets):
        if ev.datetime is not None and ev.datetime.year == 1900:
            ev.datetime = ev.datetime.replace(year=base_year + off)
            # Rewrite the ts string to ISO so downstream string-sort works
            # correctly across year boundaries.
            ev.ts = ev.datetime.strftime("%Y-%m-%d %H:%M:%S")

    def _finalize_stamps(
        stamps: List[Tuple[Optional[datetime], int, str]],
    ) -> List[Tuple[Optional[datetime], str]]:
        out: List[Tuple[Optional[datetime], str]] = []
        for dt, off, ts_str in stamps:
            if dt is not None and dt.year == 1900:
                dt = dt.replace(year=base_year + off)
                ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            out.append((dt, ts_str))
        return out

    topo_final = _finalize_stamps(topo_ts)
    utf8_final = _finalize_stamps(utf8_ts)
    if time_window is not None:
        topo_final = [
            (dt, ts_str)
            for dt, ts_str in topo_final
            if time_window.contains_datetime(dt, ts_str)
        ]
        kept_utf8: List[Tuple[Optional[datetime], str]] = []
        kept_fields: set = set()
        for (dt, ts_str), field_name in zip(utf8_final, utf8_field_at):
            if time_window.contains_datetime(dt, ts_str):
                kept_utf8.append((dt, ts_str))
                if field_name:
                    kept_fields.add(field_name)
        utf8_final = kept_utf8
        utf8_fields = kept_fields
        topo.count = len(topo_final)
        utf8.count = len(utf8_final)
        port_events = [
            ev
            for ev in port_events
            if time_window.contains_datetime(ev.datetime, ev.ts)
        ]

    _resolve_range(topo, topo_final)
    _resolve_range(utf8, utf8_final)
    utf8.fields = sorted(utf8_fields)
    port_events.sort(key=lambda e: e.datetime or datetime.min)
    return NvlsmLogParseResult(
        invalid_topology=topo,
        invalid_utf8=utf8,
        port_events=port_events,
    )
