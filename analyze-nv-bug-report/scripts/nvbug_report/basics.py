"""Read compressed/text logs and generic time/BDF helpers."""

import array
import gzip
import re
from datetime import datetime, timedelta


class LineStore:
    """Compact in-memory line store: single bytes object + offset index.

    Drop-in replacement for ``list[str]`` returned by ``readlines()``.
    Stores the entire file as one contiguous ``bytes`` object plus an
    ``array.array`` of line-start byte offsets (~8 bytes per line).
    Individual lines are decoded from bytes on demand, so no full
    ``list[str]`` is ever allocated — eliminating the ~49-byte-per-str
    Python object overhead that dominates memory for large log files.

    Supports ``len()``, integer indexing, slice indexing (returns
    ``list[str]``), and iteration — the same operations used by all
    extractors and ``SectionRangeCache``.
    """

    __slots__ = ("_data", "_mv", "_offsets")

    def __init__(self, data):
        # ``data`` may be ``bytes`` (plain-text path) or ``bytearray`` (streamed gzip path).
        # ``.toreadonly()`` makes the cached memoryview — and every slice returned by
        # ``get_bytes`` — non-writable so callers can't mutate the backing buffer.
        self._data = data
        self._mv = memoryview(data).toreadonly()
        # 4-byte unsigned int offsets when file fits in 4GB (halves the index vs. "L" on 64-bit Linux)
        offs = array.array("I" if len(data) < (1 << 32) else "L", [0])
        pos = 0
        while (idx := data.find(b"\n", pos)) != -1:
            offs.append(idx + 1)
            pos = idx + 1
        self._offsets = offs

    def _line(self, i: int) -> str:
        start = self._offsets[i]
        end = self._offsets[i + 1] if i + 1 < len(self._offsets) else len(self._data)
        return self._data[start:end].decode("utf-8", errors="replace")

    def __len__(self) -> int:
        return len(self._offsets)

    def __getitem__(self, key):
        if isinstance(key, slice):
            return [self._line(i) for i in range(*key.indices(len(self)))]
        if key < 0:
            key += len(self)
        return self._line(key)

    def __iter__(self):
        for i in range(len(self)):
            yield self._line(i)

    def get_text(self, start_line: int, end_line: int) -> str:
        """Return lines[start_line:end_line] as a single string.

        Decodes the backing bytes range in one operation — avoids creating
        N intermediate str objects and the temporary list that
        ``"".join(lines[s:e])`` requires in ``_slice_text``.
        """
        if start_line < 0 or end_line <= start_line:
            return ""
        end_line = min(end_line, len(self._offsets))
        byte_start = self._offsets[start_line]
        byte_end = (
            self._offsets[end_line] if end_line < len(self._offsets) else len(self._data)
        )
        return self._data[byte_start:byte_end].decode("utf-8", errors="replace")

    def get_bytes(self, start_line: int, end_line: int):
        """Return ``lines[start_line:end_line]`` as a zero-copy ``memoryview`` over the
        backing bytes — use for paths that just need to write the slice somewhere
        (file/gzip stream) without UTF-8 round-tripping.
        """
        if start_line < 0 or end_line <= start_line:
            return memoryview(b"")
        end_line = min(end_line, len(self._offsets))
        byte_start = self._offsets[start_line]
        byte_end = (
            self._offsets[end_line] if end_line < len(self._offsets) else len(self._data)
        )
        return self._mv[byte_start:byte_end]


_GZIP_CHUNK_BYTES = 8 * 1024 * 1024


def read_log(filepath):
    """Read a plain-text or gzip log file and return a ``LineStore``.

    The gzip path uses two streaming decompression passes — pass 1 only counts
    bytes (no retention) so we learn the exact decompressed size, pass 2
    pre-allocates a ``bytearray`` of that exact size and copies chunks straight
    into it. This trades 2× decompression CPU for zero ``b"".join`` doubling,
    no ``bytearray`` realloc churn, and correct handling of multi-member gzip
    files (whose trailer ISIZE only describes the final member).

    Pass 2 deliberately avoids ``GzipFile.readinto`` because the inherited
    ``BufferedIOBase.readinto`` allocates a full-sized intermediate ``bytes``
    object via ``self.read(N)``, reintroducing the doubling we're trying to
    eliminate. Per-chunk copying keeps the transient overhead at one chunk
    (~8 MiB) regardless of file size.
    """
    if filepath.endswith(".gz"):
        total = 0
        with gzip.open(filepath, "rb") as f:
            while chunk := f.read(_GZIP_CHUNK_BYTES):
                total += len(chunk)
        buf = bytearray(total)
        mv = memoryview(buf)
        pos = 0
        with gzip.open(filepath, "rb") as f:
            while pos < total:
                chunk = f.read(_GZIP_CHUNK_BYTES)
                if not chunk:
                    break
                end = pos + len(chunk)
                mv[pos:end] = chunk
                pos = end
        if pos < total:
            del buf[pos:]
        return LineStore(buf)
    with open(filepath, "rb") as f:
        return LineStore(f.read())


def parse_log_date(date_str):
    """Parse the Date: line from nv-bug-report header into a datetime object.

    Handles formats like 'Thu Mar 12 01:17:04 PM CST 2026'.
    Strips timezone abbreviation (3-5 uppercase letters that are NOT AM/PM) before parsing.
    """
    if not date_str or date_str == "N/A":
        return None
    s = re.sub(r"\s+(?!AM|PM)[A-Z]{3,5}(?=\s+)", "", date_str).strip()
    for fmt in ("%a %b %d %I:%M:%S %p %Y", "%a %b %d %H:%M:%S %Y",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    cn = re.match(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日\s*\S+\s+(\d{2}):(\d{2}):(\d{2})', date_str)
    if cn:
        return datetime(int(cn.group(1)), int(cn.group(2)), int(cn.group(3)),
                        int(cn.group(4)), int(cn.group(5)), int(cn.group(6)))
    return None


def compute_boot_time(date_str, uptime_str):
    """Compute system boot time by subtracting uptime from the collection date."""
    dt = parse_log_date(date_str)
    if dt is None or not uptime_str or uptime_str == "N/A":
        return "N/A"
    weeks = int(m.group(1)) if (m := re.search(r"(\d+)\s+week", uptime_str)) else 0
    days = int(m.group(1)) if (m := re.search(r"(\d+)\s+day", uptime_str)) else 0
    hours = int(m.group(1)) if (m := re.search(r"(\d+)\s+hour", uptime_str)) else 0
    minutes = int(m.group(1)) if (m := re.search(r"(\d+)\s+min", uptime_str)) else 0
    td = timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes)
    if td.total_seconds() == 0:
        return "N/A"
    return (dt - td).strftime("%Y-%m-%d %H:%M")


def normalize_bdf(bdf):
    """Normalize BDF to long form: 0000:01:00.0"""
    # Already long form?
    if re.match(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]$", bdf):
        return bdf
    # Short form bus:dev.func?
    m = re.match(r"^([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-9a-fA-F])$", bdf)
    if m:
        return f"0000:{m.group(1)}:{m.group(2)}.{m.group(3)}"
    # Other formats
    m = re.match(r"^0*([0-9a-fA-F]{1,4}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-9a-fA-F])$", bdf)
    if m:
        domain = m.group(1).zfill(4)
        return f"{domain}:{m.group(2)}:{m.group(3)}.{m.group(4)}"
    return bdf
