"""Collect raw dmesg/syslog lines (with dedup) and scan for PCIe link-drop phrases.

``_DMESG_MSG_NORMALIZE`` is shared with the report's dmesg condense logic in ``analyze.py``.
"""

import re

from nvbug_report.sections import _get_dmesg_range, _get_syslog_ranges

_DMESG_MSG_NORMALIZE = re.compile(
    r"\[\s*[\d.]+\]|"
    r"^\w{3}\s+\d{1,2}\s+[\d:]+\s+\S+\s+\S+:\s*|"
    r"\bkernel:\s*|"
    r"pid=\d+|pid='[^']*'|"
    r"GPU\s*\d+|GPU-[0-9a-f-]+|"
    r"0x[0-9a-fA-F]+|"
    r"\d{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]|"
    r"\bminor number \d+|"
    r"\bdevice \d+|"
    r"\b\d{10,}"
)


def extract_pcie_fallen_off(lines, cache=None):
    results = []
    seen = set()

    def _scan_fallen_range(start, end):
        for i in range(start, end):
            line = lines[i]
            if "fallen off" in line.lower() or "fell off" in line.lower():
                stripped = line.strip()[:300]
                if stripped not in seen:
                    seen.add(stripped)
                    results.append(stripped)

    for s, e in _get_syslog_ranges(lines, cache):
        _scan_fallen_range(s, e)
    dmesg_start, dmesg_end = _get_dmesg_range(lines, cache)
    _scan_fallen_range(dmesg_start, dmesg_end)
    return results


def extract_dmesg_and_messages(lines, cache=None):
    """Extract lines from both dmesg and syslog/messages sections, deduplicated.

    Scans dmesg: section first, then journalctl/messages sections.
    Deduplicates across sections using normalized message signatures.
    """
    all_lines = []
    seen_sigs = set()

    def _msg_signature(text):
        """Normalize a line for cross-section dedup."""
        return _DMESG_MSG_NORMALIZE.sub("", text).strip()

    dmesg_start, dmesg_end = _get_dmesg_range(lines, cache)
    if dmesg_start >= 0:
        for i in range(dmesg_start + 1, dmesg_end):
            stripped = lines[i].strip()
            if stripped:
                sig = _msg_signature(stripped)
                seen_sigs.add(sig)
                all_lines.append((i + 1, lines[i]))

    for s, e in _get_syslog_ranges(lines, cache):
        for i in range(s + 1, e):
            stripped = lines[i].strip()
            if stripped:
                sig = _msg_signature(stripped)
                if sig not in seen_sigs:
                    seen_sigs.add(sig)
                    all_lines.append((i + 1, lines[i]))

    return all_lines
