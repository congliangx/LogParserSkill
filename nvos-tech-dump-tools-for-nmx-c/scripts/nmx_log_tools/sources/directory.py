"""Directory-backed dump source."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

from ..discovery import dir_has_log_nmx, find_nmx_c_dirs
from .base import DumpSource


class DirectorySource(DumpSource):
    def __init__(self, root: Path):
        self.root = root.resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"Not a directory: {self.root}")
        if not dir_has_log_nmx(self.root):
            print(
                f"ERROR: No '{self.root}/log/nmx' (or nested log/nmx) found. "
                "Refusing to analyze — wrong dump layout or path.",
                file=sys.stderr,
            )
            sys.exit(2)
        self._nmx_c = find_nmx_c_dirs(self.root)
        if not self._nmx_c:
            print(
                f"ERROR: Found log/nmx under {self.root} but no log/nmx/nmx-c directory.",
                file=sys.stderr,
            )
            sys.exit(2)

    def nmx_c_roots(self) -> List[Path]:
        return self._nmx_c

    def cleanup(self) -> None:
        pass
