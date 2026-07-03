"""Tar.gz source: validate log/nmx via member scan, extract only log/nmx subtree."""

from __future__ import annotations

import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import List

from ..discovery import (
    IDENTITY_REL_PATHS,
    NMX_SEGMENT,
    find_nmx_c_dirs,
    normalize_arc_path,
    scan_tar_nmx_layout,
)
from .base import DumpSource


# Python 3.12+ ships a built-in `data` extraction filter that rejects unsafe
# paths, symlinks escaping the destination, setuid bits, etc. On older
# interpreters we fall back to manual validation in ``_safe_extract``.
_HAS_TAR_DATA_FILTER = hasattr(tarfile, "data_filter")


def _safe_extract(tar: tarfile.TarFile, member: tarfile.TarInfo, dest: Path) -> None:
    """Extract one member into ``dest`` without allowing path traversal.

    CVE-2007-4559 class: a crafted archive can contain members named
    ``../etc/passwd`` or absolute paths. On 3.12+ we use the built-in
    ``filter='data'`` policy. On 3.9-3.11 we validate that the normalized
    extraction target stays inside ``dest`` (and that symlink/hardlink
    targets do the same) before extracting.
    """
    if _HAS_TAR_DATA_FILTER:
        tar.extract(member, dest, filter="data")
        return

    dest_resolved = str(Path(dest).resolve())

    def _within(target: str) -> bool:
        # Build the would-be path without resolving symlinks on disk -- those
        # are extracted later and may not yet exist.
        normalized = os.path.normpath(os.path.join(dest_resolved, target))
        return normalized == dest_resolved or normalized.startswith(
            dest_resolved + os.sep
        )

    if not _within(member.name):
        raise tarfile.TarError(f"unsafe tar member path: {member.name!r}")
    if member.issym() or member.islnk():
        # linkname is relative to the member's containing directory for
        # hardlinks and symlinks; resolve against that directory.
        link_base = os.path.dirname(member.name)
        link_target = os.path.normpath(os.path.join(link_base, member.linkname))
        if not _within(link_target):
            raise tarfile.TarError(
                f"unsafe tar link target: {member.name!r} -> {member.linkname!r}"
            )
    tar.extract(member, dest)


class TarballSource(DumpSource):
    def __init__(self, tar_path: Path):
        self.tar_path = tar_path.resolve()
        if not self.tar_path.is_file():
            raise FileNotFoundError(self.tar_path)

        # Single pass: detect log/nmx presence and collect nmx-c prefixes in
        # one gunzip. The actual extraction below is the only other read.
        has_nmx, prefixes = scan_tar_nmx_layout(self.tar_path)
        if not has_nmx:
            print(
                f"ERROR: Archive {self.tar_path} does not contain any 'log/nmx' path. "
                "Aborting before extract/analysis.",
                file=sys.stderr,
            )
            sys.exit(2)

        self._temp_dir = Path(tempfile.mkdtemp(prefix="nmx_log_extract_"))
        self._extract_log_nmx_only(prefixes)
        self._nmx_c = find_nmx_c_dirs(self._temp_dir)
        if not self._nmx_c:
            print(
                f"ERROR: Archive has log/nmx but extraction found no log/nmx/nmx-c under {self._temp_dir}",
                file=sys.stderr,
            )
            self.cleanup()
            sys.exit(2)

    def _member_should_extract(self, p: str, prefix_set: set[str]) -> bool:
        if prefix_set and any(p.startswith(pref) or p == pref for pref in prefix_set):
            return True
        if f"/{NMX_SEGMENT}/" in f"/{p}/" or p.endswith(NMX_SEGMENT):
            return True
        for rel in IDENTITY_REL_PATHS:
            if p.endswith(rel) or p.endswith("/" + rel):
                return True
        return False

    def _extract_log_nmx_only(self, prefixes: List[str]) -> None:
        prefix_set = set(prefixes)
        with tarfile.open(self.tar_path, "r:gz") as tar:
            for member in tar:
                p = normalize_arc_path(member.name)
                if not self._member_should_extract(p, prefix_set):
                    continue
                _safe_extract(tar, member, self._temp_dir)

    def nmx_c_roots(self) -> List[Path]:
        return self._nmx_c

    def cleanup(self) -> None:
        if self._temp_dir.exists():
            shutil.rmtree(self._temp_dir, ignore_errors=True)
