"""Journal/syslog/dmesg scans for timestamps, Xid events, and non-Xid NVRM errors."""

import re
from collections import defaultdict
from datetime import timedelta

from nvbug_report.basics import normalize_bdf
from nvbug_report.constants import DERIVATIVE_CAUSED_BY_RE, XID_PATTERN_QUICK
from nvbug_report.sections import _get_dmesg_range, _get_syslog_ranges
from nvbug_report.syslog_ts import _normalize_syslog_ts, _parse_syslog_ts

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
        # Detect "caused by previous Xid <N>" annotation that marks NVRM
        # derivative events (typically Xid 45 channel cleanup after a primary
        # NVLink Xid 145/149). Downstream (report 7.1/7.3, comparison matrix)
        # uses these flags to split counts and collapse repeated derivatives.
        caused_by_m = DERIVATIVE_CAUSED_BY_RE.search(stripped)
        is_derivative = caused_by_m is not None
        caused_by = caused_by_m.group(1) if caused_by_m else None
        xids.append(
            {
                "timestamp": timestamp,
                "bdf": bdf,
                "xid": xid_num,
                "raw_line": stripped,
                "is_derivative": is_derivative,
                "caused_by": caused_by,
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


# Default trigger / candidate / window used by infer_untagged_derivatives().
# Trigger Xids: primary NVLink faults that produce channel cleanup cascades.
# Candidate Xids: cleanup-style Xids that the NVRM driver sometimes emits
#                 without the "caused by previous Xid N" annotation.
# Window: how close in time a candidate Xid must be to its trigger.
_DEFAULT_TRIGGER_XID_NUMS = (145, 149)
_DEFAULT_CANDIDATE_XID_NUMS = (45,)
_DEFAULT_INFER_WINDOW_SECONDS = 10


def infer_untagged_derivatives(
    xids,
    *,
    window_seconds=_DEFAULT_INFER_WINDOW_SECONDS,
    trigger_xid_nums=_DEFAULT_TRIGGER_XID_NUMS,
    candidate_xid_nums=_DEFAULT_CANDIDATE_XID_NUMS,
    ref_year=None,
):
    """Retroactively flag untagged channel-cleanup Xids as derivative.

    The NVRM kernel driver emits Xid 45 (ROBUST_CHANNEL_PREEMPTIVE_REMOVAL)
    twice per NVLink-fault burst: a first batch with the annotation
    ``caused by previous Xid N`` (caught by the regex during extraction), and
    a second batch of "bare" Xid 45 lines for the remaining channels --
    semantically identical cleanup but missing the textual attribution.

    This pass walks ``xids`` after extraction and, for each candidate Xid
    (default: 45) that is not yet marked derivative, checks whether a primary
    trigger Xid (default: 145 or 149) occurred on the **same BDF** within
    ``window_seconds`` (default: 10s). When a match is found:

    * ``x["is_derivative"]`` is set to True (mutating the existing dict).
    * ``x["caused_by"]`` is set to the trigger Xid number (string, e.g. "145").
    * ``x["derivative_inferred"] = True`` is added so downstream code can
      distinguish text-tagged derivatives ("caused by previous Xid N") from
      time-inferred ones if needed.

    Returns ``xids`` (same list reference, mutated in place) for chaining.

    Args:
        xids: list of xid dicts produced by ``extract_xid_errors``.
        window_seconds: max time gap (seconds) between trigger and candidate.
        trigger_xid_nums: iterable of Xid numbers that count as "primary trigger".
        candidate_xid_nums: iterable of Xid numbers eligible for inference.
        ref_year: reference year for syslog-format timestamps (which lack a year).
                  Auto-detected from xid raw_lines when omitted.
    """
    if not xids:
        return xids

    if ref_year is None:
        ref_year = 2026  # matches the convention used elsewhere
        for x in xids:
            raw = x.get("raw_line", "")
            m = re.search(r"\b(20\d{2})\b", raw)
            if m:
                ref_year = int(m.group(1))
                break

    # Group primary triggers by BDF and sort by parsed timestamp so we can
    # quickly find the most-recent trigger preceding a candidate.
    per_bdf_triggers = defaultdict(list)  # bdf -> sorted list of (dt, xid_num)
    for x in xids:
        if x["xid"] not in trigger_xid_nums:
            continue
        dt = _parse_syslog_ts(x["timestamp"], ref_year)
        if dt is None:
            continue
        per_bdf_triggers[x["bdf"]].append((dt, x["xid"]))
    for bdf in per_bdf_triggers:
        per_bdf_triggers[bdf].sort()

    if not per_bdf_triggers:
        return xids  # no primary triggers anywhere -> nothing to infer

    window = timedelta(seconds=window_seconds)

    for x in xids:
        if x.get("is_derivative"):
            continue
        if x["xid"] not in candidate_xid_nums:
            continue
        dt = _parse_syslog_ts(x["timestamp"], ref_year)
        if dt is None:
            continue
        triggers = per_bdf_triggers.get(x["bdf"])
        if not triggers:
            continue
        # Scan for the latest trigger at or before `dt` within `window`.
        # Triggers are timestamp-sorted; iterate while still in range.
        best_xid_num = None
        for t_dt, t_xid in triggers:
            if t_dt > dt:
                break
            if dt - t_dt <= window:
                best_xid_num = t_xid  # keep the most-recent qualifying trigger
        if best_xid_num is not None:
            x["is_derivative"] = True
            x["caused_by"] = str(best_xid_num)
            x["derivative_inferred"] = True

    return xids
