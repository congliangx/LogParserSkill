"""Collect NVRM context lines around Xid bursts (for raw-log sections in the report)."""

import re
from collections import defaultdict
from datetime import timedelta

from nvbug_report.constants import XID_PATTERN_QUICK
from nvbug_report.syslog_ts import _parse_syslog_ts


def collect_xid_context_lines(section_line_groups, bursts):
    """Collect non-Xid NVRM context lines within each burst's time window.

    ``section_line_groups`` is a list of pre-loaded section line lists
    (each element is a ``list[str]`` for one log section — syslog,
    journalctl, dmesg, etc.). The caller is responsible for selecting
    which sections to pass; this function no longer holds a reference to
    the full log ``lines`` object, so the caller can free section memory
    after this call returns.

    Returns:
        context_by_burst: dict mapping burst index -> list of context dicts
        xid_associated_payloads: set of NVRM payloads associated with Xid events
    """
    if not bursts:
        return {}, set()

    ref_year = 2026
    for b in bursts:
        for x in b:
            if x["timestamp"] and not x["timestamp"].startswith("["):
                m_y = re.search(r"\b(20\d{2})\b", x.get("raw_line", ""))
                if m_y:
                    ref_year = int(m_y.group(1))
                break
        else:
            continue
        break

    burst_ranges = []
    for burst in bursts:
        dts = [_parse_syslog_ts(x["timestamp"], ref_year) for x in burst]
        valid = [d for d in dts if d]
        if valid:
            burst_ranges.append(
                (min(valid) - timedelta(seconds=2), max(valid) + timedelta(seconds=2))
            )
        else:
            burst_ranges.append(None)

    nvrm_payload_re = re.compile(r"(NVRM:.*)")
    context_by_burst = defaultdict(list)
    xid_associated_payloads = set()
    seen = set()

    def _scan_section(section_lines):
        for line in section_lines:
            if "NVRM:" not in line:
                continue
            stripped = line.strip()
            if XID_PATTERN_QUICK.search(stripped):
                continue
            if stripped in seen:
                continue
            ts_match = re.match(r"^(\w+ \d+ [\d:]+)", stripped)
            if not ts_match:
                continue
            dt = _parse_syslog_ts(ts_match.group(1), ref_year)
            if not dt:
                continue
            for bi, br in enumerate(burst_ranges):
                if br and br[0] <= dt <= br[1]:
                    seen.add(stripped)
                    context_by_burst[bi].append({"timestamp": ts_match.group(1), "raw_line": stripped})
                    pm = nvrm_payload_re.search(stripped)
                    if pm:
                        xid_associated_payloads.add(pm.group(1))
                    break

    for section_lines in section_line_groups:
        _scan_section(section_lines)

    return dict(context_by_burst), xid_associated_payloads
