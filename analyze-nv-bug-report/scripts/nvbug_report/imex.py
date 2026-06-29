"""IMEX service status, log parsing, and temporal correlation with Xid bursts."""

import re
from collections import defaultdict
from datetime import datetime, timedelta

from nvbug_report.sections import find_section_range
from nvbug_report.syslog_ts import _parse_syslog_ts

_IMEX_LOG_TS_RE = re.compile(
    r"^\[(\w+ \d+ \d{4} \d{2}:\d{2}:\d{2})\]\s+\[(ERROR|WARNING)\]\s+\[tid \d+\]\s+(.*)"
)

_IMEX_DISCONNECT_RE = re.compile(
    r"Node disconnect event detected for node (\d+) with address (\S+),\s*attempting to (\w+)"
)

_IMEX_NORM_PATTERNS = [
    (re.compile(r"\bwith id \d+"), "with id **"),
    (re.compile(r"\bevent id \d+"), "event id **"),
    (re.compile(r"(Oldest incomplete message delivery: )\S+ \S+"), r"\1**"),
    (
        re.compile(
            r"(ImportResponse|UnimportRequest|UnimportResponse|ImportRequest|MulticastImport): \d+"
        ),
        r"\1: **",
    ),
    (re.compile(r"\bfor UUID: [0-9A-Fa-f-]+"), "for UUID: **"),
]


def _parse_imex_log_ts(ts_str):
    """Parse IMEX log timestamp like 'Dec 30 2025 00:11:39' into datetime."""
    if not ts_str:
        return None
    try:
        return datetime.strptime(ts_str, "%b %d %Y %H:%M:%S")
    except ValueError:
        return None


def extract_imex_status(lines, cache=None):
    """Extract IMEX service status, nvidia-imex-ctl -N connectivity, and /var/log/nvidia-imex.log errors."""
    result = {
        "service_active": None,
        "service_status": "",
        "pid": "",
        "error_lines": [],
        "imex_log_entries": [],
        "ctl_timestamp": "",
        "nodes": [],
        "non_connected": [],
        "domain_state": "",
    }

    svc_start, svc_end = find_section_range(
        lines, "systemctl status nvidia-imex.service", exact=False, cache=cache
    )
    if svc_start >= 0:
        for i in range(svc_start, svc_end):
            stripped = lines[i].strip()
            if stripped.startswith("Active:"):
                result["service_status"] = stripped.split(":", 1)[1].strip()
                result["service_active"] = (
                    "active" in stripped.lower() and "inactive" not in stripped.lower()
                )
            elif stripped.startswith("Main PID:"):
                result["pid"] = stripped.split(":", 1)[1].strip()
            elif "failure" in stripped.lower() or "error" in stripped.lower():
                result["error_lines"].append(stripped)

    # --- Parse /var/log/nvidia-imex.log section ---
    log_start, log_end = find_section_range(lines, "/var/log/nvidia-imex.log", exact=False, cache=cache)
    if log_start >= 0:
        svc_msg_set = set(result["error_lines"])
        for i in range(log_start, log_end):
            m = _IMEX_LOG_TS_RE.match(lines[i].strip())
            if not m:
                continue
            ts_str, level, message = m.group(1), m.group(2), m.group(3)
            if message in svc_msg_set:
                continue
            result["imex_log_entries"].append(
                {
                    "timestamp": ts_str,
                    "level": level,
                    "message": message,
                    "raw_line": lines[i].strip(),
                }
            )
        result["imex_log_entries"] = [
            e
            for e in result["imex_log_entries"]
            if e["level"] == "ERROR" and "Node disconnect event detected" in e["message"]
        ]

    ctl_start, ctl_end = find_section_range(lines, "nvidia-imex-ctl -N", exact=False, cache=cache)
    if ctl_start >= 0:
        in_matrix = False
        matrix_cols = []
        for i in range(ctl_start, ctl_end):
            stripped = lines[i].strip()
            if not stripped:
                continue

            ts_m = re.match(r"^\d+/\d+/\d+\s+\d+:\d+:\d+", stripped)
            if ts_m:
                result["ctl_timestamp"] = stripped
                continue

            node_m = re.match(
                r"Node\s+#(\d+)\s+[-*]\s+(\S+)\s+.*?-\s+(\S+)\s+.*?Version:\s+(\S+)",
                stripped,
            )
            if node_m:
                result["nodes"].append(
                    {
                        "id": int(node_m.group(1)),
                        "hostname": node_m.group(2),
                        "status": node_m.group(3),
                        "version": node_m.group(4),
                    }
                )
                continue

            if "Nodes From\\To" in stripped or "Nodes From\\" in stripped:
                parts = stripped.split()
                for p in parts:
                    if p.isdigit():
                        matrix_cols.append(int(p))
                in_matrix = True
                continue

            if in_matrix:
                parts = stripped.split()
                if parts and parts[0].isdigit():
                    row_id = int(parts[0])
                    statuses = parts[1:]
                    for col_offset, st in enumerate(statuses):
                        if col_offset < len(matrix_cols) and st != "C":
                            result["non_connected"].append((row_id, matrix_cols[col_offset], st))

            if stripped.startswith("Domain State:"):
                result["domain_state"] = stripped.split(":", 1)[1].strip()
                in_matrix = False

    return result


def _group_imex_events(entries, gap_seconds=60):
    """Group IMEX log entries into events separated by > gap_seconds silence."""
    if not entries:
        return []
    sorted_entries = sorted(entries, key=lambda e: _parse_imex_log_ts(e["timestamp"]) or datetime.min)
    events = []
    current = []
    last_dt = None
    for entry in sorted_entries:
        dt = _parse_imex_log_ts(entry["timestamp"])
        if dt and last_dt and (dt - last_dt) > timedelta(seconds=gap_seconds):
            if current:
                events.append(current)
            current = []
        current.append(entry)
        if dt:
            last_dt = dt
    if current:
        events.append(current)
    return events


def _summarize_imex_event(entries):
    """Summarize an IMEX event group, collapsing repeated patterns.

    Returns list of summary strings like:
        "[WARNING] Response not received for unimport event with id ** sent to node id 10 (x 11260)"
    """

    def _normalize_msg(msg):
        n = msg
        for pat, repl in _IMEX_NORM_PATTERNS:
            n = pat.sub(repl, n)
        return n

    pattern_counts = defaultdict(int)
    pattern_example = {}
    for entry in entries:
        level = entry["level"]
        msg = entry["message"]
        norm = _normalize_msg(msg)
        key = (level, norm)
        pattern_counts[key] += 1
        if key not in pattern_example:
            pattern_example[key] = msg

    lines = []
    for key, count in pattern_counts.items():
        level, norm = key
        display = f"[{level}] {norm}"
        if count > 1:
            display += f" (x {count})"
        lines.append(display)
    return lines


def _find_related_imex_events(imex_events, burst, ref_year=2026, window_seconds=60):
    """Find IMEX events temporally related to a Xid burst (within +/- window_seconds).

    Returns list of (event_index, event_group) tuples where event_index is
    the 0-based position in the imex_events list (used for "Event N" display).
    """
    if not imex_events:
        return []
    burst_dts = [_parse_syslog_ts(x["timestamp"], ref_year) for x in burst]
    valid_burst_dts = [d for d in burst_dts if d]
    if not valid_burst_dts:
        return []
    burst_start = min(valid_burst_dts)
    burst_end = max(valid_burst_dts)
    window = timedelta(seconds=window_seconds)

    matches = []
    for idx, event in enumerate(imex_events):
        dts = [_parse_imex_log_ts(e["timestamp"]) for e in event]
        valid = [d for d in dts if d]
        if not valid:
            continue
        ie_start, ie_end = min(valid), max(valid)
        if ie_end >= (burst_start - window) and ie_start <= (burst_end + window):
            matches.append((idx, event))
    return matches


def _format_imex_related_line(matching_events, show_reporting_host=False):
    """Format a compact blockquote line listing related IMEX event numbers.

    matching_events is a list of (event_index, event_group) tuples.
    """
    if not matching_events:
        return ""
    event_labels = [f"Event Group {idx + 1}" for idx, _event in matching_events]
    return "> **Related IMEX Event Groups**: " + ", ".join(event_labels)
