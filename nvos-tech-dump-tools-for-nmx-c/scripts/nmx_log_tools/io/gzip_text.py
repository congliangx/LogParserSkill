"""Read plain or gzip text files line-by-line or as whole text."""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import Generator, Iterable, Union


def read_text(path: Union[str, Path]) -> str:
    path = Path(path)
    if path.suffix == ".gz" or path.name.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            return f.read()
    return path.read_text(encoding="utf-8", errors="replace")


def iter_lines(path: Union[str, Path]) -> Generator[str, None, None]:
    path = Path(path)
    if path.suffix == ".gz" or path.name.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                yield line
    else:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                yield line


def iter_lines_from_bytes(data: bytes, gz: bool = False) -> Generator[str, None, None]:
    if gz:
        import io
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as g:
            for raw in g:
                yield raw.decode("utf-8", errors="replace")
    else:
        for line in data.decode("utf-8", errors="replace").splitlines(keepends=True):
            yield line if line.endswith("\n") else line + "\n"
