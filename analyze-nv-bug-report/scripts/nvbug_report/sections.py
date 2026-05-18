"""Locate `____`-delimited sections inside a bugreport (with optional memoization).

Within one pair of `____` lines, nv-bug-report runs many commands in sequence. The next command
is introduced by either:

- a classic ``*** /path/to/cmd ...`` line (but not ``*** ls: ...`` metadata after ``*** /path``),
  or
- (common on newer logs, e.g. GB200) a **standalone** absolute-path command line such as
  ``/usr/bin/nvidia-smi ...`` or ``/sbin/lspci ...`` with no ``***`` prefix.

If we only split on ``***``, every ``nvidia-smi`` / ``lspci`` subsection that shares one outer
``____`` block collapses to the same end line → overlapping slices and duplicate
``line_range`` in artifacts. Boundaries must include these bare-path command echoes.
"""

import re


def _is_bugreport_ls_meta_line(stripped):
    """``*** ls: ...`` file metadata after ``*** /path`` — not the next command/subsection."""
    return stripped.startswith("*** ls:") or stripped.startswith("*** ls ")


def _is_next_command_echo_line(stripped):
    """True if ``stripped`` starts the next collected command (subsection boundary)."""
    if not stripped:
        return False
    if stripped.startswith("***"):
        if _is_bugreport_ls_meta_line(stripped):
            return False
        return True
    for prefix in (
        "/usr/bin/nvidia-smi",
        "/sbin/lspci",
        "/usr/bin/lspci",
        "/sbin/dmidecode",
        "/usr/bin/dmidecode",
    ):
        if stripped.startswith(prefix):
            return True
    return False


def _next_subsection_boundary_index(lines, start, limit):
    """First subsection boundary strictly after ``start``, before ``limit``; else ``limit``."""
    for i in range(start + 1, limit):
        if _is_next_command_echo_line(lines[i].strip()):
            return i
    return limit


def _find_section_range_impl(lines, marker, exact=False):
    """Find [start, end) line slice: start is the header line, end is exclusive (uncached)."""
    start = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if marker == "dmesg:":
            if stripped.startswith("dmesg:"):
                start = i
                break
            continue
        if stripped == marker or stripped.endswith("/" + marker):
            start = i
            break
        if not exact and marker in stripped:
            start = i
            break

    if start < 0:
        return -1, -1

    outer_end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip().startswith("____________________________________"):
            outer_end = i
            break

    inner_end = _next_subsection_boundary_index(lines, start, outer_end)
    return start, inner_end


class SectionRangeCache:
    """Memoize section (start, end) per (marker, exact) for one bugreport log.

    Multiple extractors call ``find_section_range`` with the same markers; without
    caching each call rescans from line 0 — O(n) per marker — which dominates
    ``extract_xid_nvlink_pcie_imex`` on multi‑MB logs.
    """

    __slots__ = ("lines", "_memo")

    def __init__(self, lines):
        self.lines = lines
        self._memo = {}

    def get(self, marker, exact=False):
        k = (marker, exact)
        if k not in self._memo:
            self._memo[k] = _find_section_range_impl(self.lines, marker, exact)
        return self._memo[k]

    def filled(self):
        """Number of distinct section keys resolved (for diagnostics)."""
        return len(self._memo)


def find_section_range(lines, marker, exact=False, cache=None):
    """Find ``[start, end)`` line indices for a bugreport section.

    ``end`` is the earlier of: (1) the next subsection boundary (``***`` line or standalone
    ``/usr/bin/nvidia-smi`` / ``/sbin/lspci`` / ``dmidecode`` path echoes, etc.) within the same
    outer ``____`` block, or (2) the outer closing ``____``. Slice ``lines[start:end]`` includes
    the header line at ``start`` and excludes the boundary line at ``end``.

    Pass ``cache`` (a ``SectionRangeCache``) to avoid repeated full-file scans.
    """
    if cache is not None:
        return cache.get(marker, exact)
    return _find_section_range_impl(lines, marker, exact)


def _get_dmesg_range(lines, cache=None):
    """Return (start, end) of the dmesg: section to avoid scanning duplicates."""
    start, end = find_section_range(lines, "dmesg:", cache=cache)
    if start >= 0:
        return start, end
    return 0, len(lines)


def _get_syslog_ranges(lines, cache=None):
    """Return list of (start, end) tuples for ALL syslog/kernel log sections.

    Always scans ALL of the following sections (if present):
    - "Scanning kernel log files for NVIDIA kernel messages" (contains
      /var/log/kern.log with historical Xid from previous boots)
    - "journalctl -b -0:" / "journalctl -b -1:"
    - "/var/log/messages"
    Each is included independently; dedup is handled by callers.
    """
    ranges = []
    seen = set()

    def _add(marker):
        s, e = find_section_range(lines, marker, cache=cache)
        if s >= 0 and (s, e) not in seen:
            if not any(rs <= s and e <= re_ for rs, re_ in ranges):
                seen.add((s, e))
                ranges.append((s, e))

    _add("Scanning kernel log files")
    _add("journalctl -b -0:")
    _add("journalctl -b -1:")
    _add("/var/log/messages")

    return ranges
