from pathlib import Path

from .base import DumpSource
from .directory import DirectorySource
from .tarball import TarballSource


def open_source(path: Path) -> DumpSource:
    path = Path(path)
    if path.suffix == ".gz" and path.name.endswith(".tar.gz"):
        return TarballSource(path)
    if path.is_file() and path.suffix == ".gz":
        # single-file .gz not supported as full dump
        raise ValueError(f"Expected directory or .tar.gz dump, got: {path}")
    return DirectorySource(path)
