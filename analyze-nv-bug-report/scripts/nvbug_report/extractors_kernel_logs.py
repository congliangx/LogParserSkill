"""Journal/syslog/dmesg scans for timestamps, Xid events, and non-Xid NVRM errors."""

import re

from nvbug_report.basics import normalize_bdf
from nvbug_report.constants import XID_PATTERN_QUICK
from nvbug_report.sections import _get_dmesg_range, _get_syslog_ranges
from nvbug_report.syslog_ts import _normalize_syslog_ts

_SYSLOG_TS_RE = re.compile(r"^(\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})")


def extract_message_start_time(lines, cache=None):
    """Extract the earliest syslog timestamp from journalctl/messages sections."""
    for s, e in _get_syslog_ranges(lines, cache):
        for i in range(s + 1, e):
            m = _SYSLOG_TS_RE.match(lines[i].strip())
            if m:
                return _normalize_syslog_ts(m.group(1))
    return "N/A"


def _xid_dedup_key(line):
    """Extract the NVRM Xid payload (after timestamp) for dedup across sections."""
    m = re.search(r"(NVRM:\s*Xid\s*\(PCI:\S+\):\s*\d+.*)", line)
    return m.group(1) if m else line


def extract_xid_errors(lines, cache=None):
    """Extract Xid errors from all available log sections.

    Scans the following sections (all mandatory when present):
    - "Scanning kernel log files" (includes /var/log/kern.log, /var/log/dmesg,
      journalctl — kern.log may contain Xid from previous boots)
    - "journalctl -b -0:" / "journalctl -b -1:"
    - "/var/log/messages"
    - "dmesg:" (standalone section)

    Within a section, dedup by full line (preserves different-time same-payload events).
    Across sections, dedup dmesg entries by NVRM payload if already captured from syslog.
    """
    xid_pattern_nvrm = re.compile(
        r"NVRM:\s*Xid\s*\(PCI:([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2})\):\s*(\d+)"
    )
    xid_pattern_full_bdf = re.compile(
        r"NVRM.*?(\d{4,}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]).*?Xid.*?:\s*(\d+)"
    )

    xids = []
    raw_lines = []
    raw_line_source_numbers = []
    seen_lines = set()
    syslog_payloads = set()

    def _extract_entry(stripped, m, source_line_1based):
        bdf_raw = m.group(1)
        if "." not in bdf_raw:
            bdf_raw += ".0"
        bdf = normalize_bdf(bdf_raw)
        xid_num = int(m.group(2))
        timestamp = ""
        # ISO 8601 format from kern.log: 2026-04-03T06:20:28.545191+00:00
        ts_iso = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", stripped)
        if ts_iso:
            timestamp = ts_iso.group(1)
        else:
            ts_match = re.match(r"^(\w+ \d+ [\d:]+)", stripped)
            if ts_match:
                timestamp = ts_match.group(1)
            else:
                ts_match2 = re.match(r"^\[[\s]*([0-9.]+)\]", stripped)
                if ts_match2:
                    timestamp = f"[{ts_match2.group(1)}]"
        xids.append(
            {
                "timestamp": timestamp,
                "bdf": bdf,
                "xid": xid_num,
                "raw_line": stripped,
            }
        )
        raw_lines.append(stripped)
        raw_line_source_numbers.append(source_line_1based)

    for s, e in _get_syslog_ranges(lines, cache):
        for i in range(s, e):
            line = lines[i]
            m = xid_pattern_nvrm.search(line)
            if not m:
                m = xid_pattern_full_bdf.search(line)
            if m:
                stripped = line.strip()
                if stripped in seen_lines:
                    continue
                seen_lines.add(stripped)
                syslog_payloads.add(_xid_dedup_key(stripped))
                _extract_entry(stripped, m, i + 1)

    dmesg_start, dmesg_end = _get_dmesg_range(lines, cache)
    for i in range(dmesg_start, dmesg_end):
        line = lines[i]
        m = xid_pattern_nvrm.search(line)
        if not m:
            m = xid_pattern_full_bdf.search(line)
        if m:
            stripped = line.strip()
            if stripped in seen_lines:
                continue
            seen_lines.add(stripped)
            payload = _xid_dedup_key(stripped)
            if payload in syslog_payloads:
                continue
            _extract_entry(stripped, m, i + 1)

    return xids, raw_lines, raw_line_source_numbers


def extract_nvrm_errors(lines, exclude_payloads=None, cache=None):
    """Extract NVRM errors (excluding Xid lines, routine messages, and Xid-associated context)."""
    errors = []
    seen_lines = set()
    syslog_nvrm_payloads = set()
    skip_patterns = ["loading NVIDIA", "Persistence mode", "nvidia-modeset"]
    _exclude = exclude_payloads or set()

    def _get_nvrm_payload(stripped):
        m = re.search(r"(NVRM:.*)", stripped)
        return m.group(1) if m else stripped

    def _should_skip(stripped):
        if XID_PATTERN_QUICK.search(stripped):
            return True
        if any(sp in stripped for sp in skip_patterns):
            return True
        payload = _get_nvrm_payload(stripped)
        if payload in _exclude:
            return True
        return False

    def _try_append(stripped):
        if len(stripped) > 20:
            timestamp = ""
            ts_match = re.match(r"^(\w+ \d+ [\d:]+)", stripped)
            if ts_match:
                timestamp = ts_match.group(1)
            else:
                ts_match2 = re.match(r"^\[[\s]*([0-9.]+)\]", stripped)
                if ts_match2:
                    timestamp = f"[{ts_match2.group(1)}]"
            errors.append({"timestamp": timestamp, "message": stripped[:300]})

    for s, e in _get_syslog_ranges(lines, cache):
        for i in range(s, e):
            line = lines[i]
            if "NVRM:" in line:
                stripped = line.strip()
                if stripped in seen_lines:
                    continue
                seen_lines.add(stripped)
                if _should_skip(stripped):
                    continue
                syslog_nvrm_payloads.add(_get_nvrm_payload(stripped))
                _try_append(stripped)

    dmesg_start, dmesg_end = _get_dmesg_range(lines, cache)
    for i in range(dmesg_start, dmesg_end):
        line = lines[i]
        if "NVRM:" in line:
            stripped = line.strip()
            if stripped in seen_lines:
                continue
            seen_lines.add(stripped)
            if _should_skip(stripped):
                continue
            if _get_nvrm_payload(stripped) in syslog_nvrm_payloads:
                continue
            _try_append(stripped)
    return errors
