"""Abstract dump source: directory or tarball."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional


class DumpSource(ABC):
    """Provides paths under one or more nmx-c log roots."""

    @abstractmethod
    def nmx_c_roots(self) -> List[Path]:
        """Absolute paths to log/nmx/nmx-c directories."""

    @abstractmethod
    def cleanup(self) -> None:
        """Release temp resources."""

    def find_glob(self, pattern: str) -> List[Path]:
        out: List[Path] = []
        for root in self.nmx_c_roots():
            out.extend(sorted(root.glob(pattern)))
        return out
