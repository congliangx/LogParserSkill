"""NVLSM deep log forensics (nvlsm_analyzer.py)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

from ..io.gzip_text import iter_lines


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

    def parse_logs(self, log_paths: List[Path]) -> None:
        # Caller (discovery.collect_nvlsm_log_files) returns paths in
        # chronological order (oldest first).
        self.log_files = len(log_paths)
        for path in log_paths:
            for line in iter_lines(path):
                self.log_lines += 1
                self._parse_line(line)

    def _parse_line(self, line: str) -> None:
        if "setting state to INIT" in line:
            if re.search(r"(\w+ \d+ \d+:\d+:\d+).*guid\s+(0x[a-fA-F0-9]+).*port\s+(\d+)", line):
                self.state_changes += 1

        if "Unlinking" in line:
            if re.search(
                r"(\w+ \d+ \d+:\d+:\d+).*Unlinking.*node\s+(0x[a-fA-F0-9]+).*port\s+(\d+)\(([^)]+)\)",
                line,
            ):
                self.state_changes += 1

    def finalize(self) -> ForensicsResult:
        return ForensicsResult(
            state_changes=self.state_changes,
            log_lines=self.log_lines,
            log_files=self.log_files,
        )
