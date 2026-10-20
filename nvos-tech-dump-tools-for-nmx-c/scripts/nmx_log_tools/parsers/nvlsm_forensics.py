"""NVLSM deep log forensics (nvlsm_analyzer.py)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

from ..io.gzip_text import iter_lines
from .nvlsm_health import YearTracker, infer_reference_year

if TYPE_CHECKING:
    from ..time_range import TimeWindow


_STATE_TS = re.compile(r"(\w+ \d+ \d+:\d+:\d+)")
_INIT = re.compile(r"(\w+ \d+ \d+:\d+:\d+).*guid\s+(0x[a-fA-F0-9]+).*port\s+(\d+)")
_UNLINK = re.compile(
    r"(\w+ \d+ \d+:\d+:\d+).*Unlinking.*node\s+(0x[a-fA-F0-9]+).*port\s+(\d+)\(([^)]+)\)"
)


@dataclass
class ForensicsResult:
    state_changes: int = 0
    log_lines: int = 0
    log_files: int = 0


class NvlsmForensicsAnalyzer:
    def __init__(self) -> None:
        self._reset_counters()

    def _reset_counters(self) -> None:
        self.state_changes = 0
        self.log_lines = 0
        self.log_files = 0

    @staticmethod
    def _line_is_state_change(line: str) -> bool:
        if "setting state to INIT" in line and _INIT.search(line):
            return True
        if "Unlinking" in line and _UNLINK.search(line):
            return True
        return False

    def parse_logs(
        self,
        log_paths: List[Path],
        time_window: Optional["TimeWindow"] = None,
    ) -> None:
        # Caller (discovery.collect_nvlsm_log_files) returns paths in
        # chronological order (oldest first).
        self.log_files = len(log_paths)
        if time_window is None:
            for path in log_paths:
                for line in iter_lines(path):
                    self.log_lines += 1
                    if self._line_is_state_change(line):
                        self.state_changes += 1
            return

        tracker = YearTracker()
        pending: List[tuple] = []
        for path in log_paths:
            for line in iter_lines(path):
                self.log_lines += 1
                if not self._line_is_state_change(line):
                    continue
                m = _STATE_TS.search(line)
                if not m:
                    continue
                try:
                    dt = datetime.strptime(m.group(1), "%b %d %H:%M:%S")
                    off = tracker.feed(dt.month)
                except ValueError:
                    continue
                pending.append((dt, off))
        base_year = infer_reference_year(log_paths) - tracker.offset
        n = 0
        for dt, off in pending:
            real = dt.replace(year=base_year + off) if dt.year == 1900 else dt
            if time_window.contains(real):
                n += 1
        self.state_changes = n

    def finalize(self) -> ForensicsResult:
        return ForensicsResult(
            state_changes=self.state_changes,
            log_lines=self.log_lines,
            log_files=self.log_files,
        )
