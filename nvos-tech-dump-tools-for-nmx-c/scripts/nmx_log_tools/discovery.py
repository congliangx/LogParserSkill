"""Fast validation and file discovery under log/nmx/nmx-c."""

from __future__ import annotations

import re
import tarfile
from pathlib import Path
from typing import List, Set, Tuple, Union


NMX_SEGMENT = "log/nmx"
NMX_C_SEGMENT = "log/nmx/nmx-c"

# Platform identity files extracted alongside log/nmx for report headings.
IDENTITY_REL_PATHS = (
    "dump/platform.chassis-location",
    "etc/hostname",
    "dump/ip.addr",
)


def normalize_arc_path(name: str) -> str:
    return name.replace("\\", "/").lstrip("./")


def scan_tar_nmx_layout(tar_path: Union[str, Path]) -> Tuple[bool, List[str]]:
    """Single pass over tarball members: returns (has_log_nmx, sorted_nmx_c_prefixes).

    Replaces the two-pass combination of ``tar_has_log_nmx`` + ``tar_nmx_c_prefixes``
    so a multi-GB ``.tar.gz`` is gunzipped only once for layout detection.
    """
    has_nmx = False
    prefixes: Set[str] = set()
    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar:
            p = normalize_arc_path(member.name)
            if not has_nmx and not member.isdir():
                if f"/{NMX_SEGMENT}/" in f"/{p}/" or p.endswith(NMX_SEGMENT):
                    has_nmx = True
            idx = p.find(NMX_C_SEGMENT)
            if idx >= 0:
                prefixes.add(p[: idx + len(NMX_C_SEGMENT)])
    return has_nmx, sorted(prefixes)


def tar_has_log_nmx(tar_path: Union[str, Path]) -> bool:
    """Scan tarball member names only (no extract) for log/nmx."""
    has_nmx, _ = scan_tar_nmx_layout(tar_path)
    return has_nmx


def tar_nmx_c_prefixes(tar_path: Union[str, Path]) -> List[str]:
    """Return archive path prefixes ending at log/nmx/nmx-c (for extract)."""
    _, prefixes = scan_tar_nmx_layout(tar_path)
    return prefixes


def dir_has_log_nmx(path: Path) -> bool:
    if (path / "log" / "nmx").is_dir():
        return True
    for sub in path.rglob("log/nmx"):
        if sub.is_dir():
            return True
    return False


def find_nmx_c_dirs(path: Path) -> List[Path]:
    """Find all log/nmx/nmx-c directories under path."""
    found: List[Path] = []
    direct = path / "log" / "nmx" / "nmx-c"
    if direct.is_dir():
        found.append(direct.resolve())
    for candidate in path.rglob("nmx-c"):
        if candidate.is_dir() and candidate.parent.name == "nmx":
            parent = candidate.parent.parent
            if parent.name == "log":
                resolved = candidate.resolve()
                if resolved not in found:
                    found.append(resolved)
    return sorted(set(found))


def collect_nvlsm_log_files(nmx_c: Path) -> List[Path]:
    # logrotate convention: higher index = older; the un-suffixed `nvlsm.log[.gz]`
    # is the active (newest) file. Return in chronological order (oldest first)
    # so downstream parsing (which infers the missing year from month rollovers)
    # sees lines in time order.
    def sort_key(p: Path) -> tuple:
        name = p.name
        m = re.match(r"^nvlsm\.log\.(\d+)\.gz$", name)
        if m:
            return (0, -int(m.group(1)))
        if name in ("nvlsm.log.gz", "nvlsm.log"):
            return (1, 0)
        return (2, name)

    return sorted(
        (
            p for p in nmx_c.iterdir()
            if p.is_file() and p.name.startswith("nvlsm.log") and p.name.endswith(".gz")
        ),
        key=sort_key,
    )


def collect_fabric_manager_logs(nmx_c: Path, unlimited: bool = True) -> List[Path]:
    def sort_key(p: Path) -> tuple:
        name = p.name.lower()
        if name in ("fabricmanager.log", "fabricmanager.log.gz"):
            return (0, 0)
        m = re.search(r"fabricmanager\.log\.(\d+)", name)
        if m:
            return (1, int(m.group(1)))
        return (2, 0)

    files = [
        p for p in nmx_c.iterdir()
        if p.is_file() and re.match(r"^fabricmanager\.log", p.name, re.I)
    ]
    files = sorted(files, key=sort_key)
    if not unlimited and len(files) > 3:
        files = files[:3]
    return files


def collect_topology_files(nmx_c: Path) -> dict:
    nvlsm_dir = nmx_c / "nvlsm"
    out = {}
    if nvlsm_dir.is_dir():
        for fname, key in (
            ("guid2lid.gz", "guid2lid"),
            ("neighbors.gz", "neighbors"),
            ("guid2planes.gz", "guid2planes"),
        ):
            fp = nvlsm_dir / fname
            if fp.is_file():
                out[key] = fp
    return out
