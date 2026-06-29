"""Single-file markdown report (system, PCIe, NVLink, IMEX, Xid, summary)."""

import os
import re
from collections import defaultdict
from datetime import datetime, timedelta

from nvbug_report.constants import C2C_GPU_KEYWORDS
from nvbug_report.imex import (
    _find_related_imex_events,
    _format_imex_related_line,
    _group_imex_events,
    _parse_imex_log_ts,
    _summarize_imex_event,
)
from nvbug_report.syslog_ts import _parse_syslog_ts
from nvbug_report.xid_analyzer_runner import MISSING_ASSET_PREFIX


def group_xids_into_bursts(xids, gap_seconds=60):
    """Group Xid entries into burst events separated by > gap_seconds silence."""
    if not xids:
        return []

    ref_year = 2026
    for x in xids:
        if x["timestamp"] and not x["timestamp"].startswith("["):
            m = re.search(r"\b(20\d{2})\b", x.get("raw_line", ""))
            if m:
                ref_year = int(m.group(1))
            break

    bursts = []
    current = []
    last_dt = None

    for x in xids:
        dt = _parse_syslog_ts(x["timestamp"], ref_year)
        if dt and last_dt and (dt - last_dt) > timedelta(seconds=gap_seconds):
            if current:
                bursts.append(current)
            current = []
        current.append(x)
        if dt:
            last_dt = dt

    if current:
        bursts.append(current)

    return bursts


def _collapse_burst_derivatives(burst):
    """Within a burst, keep every primary entry and collapse each
    (xid, caused_by) derivative group to one representative.

    Returns ``(display_list, notes)`` where:

    * ``display_list`` preserves all primary entries plus one rep per
      derivative (xid, caused_by) group, time-sorted for determinism.
    * ``notes`` maps ``id(rep_dict) -> "+N more derivative Xid ... suppressed"``;
      ``_render_burst_log`` consults this and appends the note immediately
      after the rep's raw line.

    The full burst is still passed unchanged to context-window / IMEX
    correlation callers; only the rendered list is collapsed.
    """
    display = []
    notes = {}
    derivative_groups = {}  # (xid, caused_by) -> list of entries
    for x in burst:
        if x.get("is_derivative"):
            key = (x["xid"], x.get("caused_by") or "?")
            derivative_groups.setdefault(key, []).append(x)
        else:
            display.append(x)
    for (xid_num, caused_by), group in derivative_groups.items():
        rep = group[0]
        display.append(rep)
        if len(group) > 1:
            notes[id(rep)] = (
                f"+ {len(group) - 1} more derivative Xid {xid_num} entries "
                f"(caused by Xid {caused_by}) suppressed"
            )
    display.sort(key=lambda x: x.get("timestamp", ""))
    return display, notes


_XID_SUBTYPE_RE = re.compile(
    r"Xid \(PCI:[^)]+\):\s*\d+,\s*(?:pid=\d+,\s*name=\S+,\s*)?"
    r"([A-Z][A-Z0-9_]+)\s+(Fatal|Nonfatal)",
    re.IGNORECASE,
)


# NVLink-class Xid numbers per NVIDIA Server-RAS-Catalog "XID 144-150 Decode"
# sheet -- the only Xid range whose NVRM driver lines carry the textual
# `<Category> <Severity>` payload format (e.g. ``RLW_SRC_TRACK Nonfatal``).
# Section 7.6 (per-node NVLink breakdown) and Section 3.1 (cross-node sub-type
# breakdown) both filter on this tuple to ensure consistent scope.
NVLINK_XID_NUMS = (144, 145, 146, 147, 148, 149, 150)


def _xid_subtype_parts(raw_line):
    """Return ``(category, severity)`` extracted from an NVRM Xid line.

    Examples::

        "... Xid (...): 145, RLW_SRC_TRACK Nonfatal XC1 ..."  -> ("RLW_SRC_TRACK", "Nonfatal")
        "... Xid (...): 149, NETIR_LINK_DOWN Fatal XC0 ..."   -> ("NETIR_LINK_DOWN", "Fatal")
        "... Xid (...): 45, pid=N, name=python, channel ..."  -> ("-", "-")
        "... Xid (...): 137, RLW_RXPIPE interrupt hit ..."    -> ("-", "-")

    The textual sub-type convention only applies to the NVLink-class Xid
    range (144-150) per NVIDIA's Server-RAS-Catalog. For everything else
    (cleanup Xid 45, GR fault Xid 44, NVLINK_PRIV_ERR Xid 137, recovery
    notice Xid 154, ECC Xids, etc.) the NVRM driver emits a different
    message format that doesn't carry a `<Category> <Severity>` token,
    so the regex doesn't match and we return ``('-', '-')`` -- callers
    treat that as "no further sub-type partitioning needed" and render
    those rows in a single bucket.

    Consumed by:
      * section 7.1 (Xid Summary)        -- to split rows per sub-type
      * section 7.6 (NVLink Breakdown)   -- per-link per-sub-type clusters
      * cross-node 3.1                   -- per-host sub-type breakdown
    """
    if not raw_line:
        return ("-", "-")
    m = _XID_SUBTYPE_RE.search(raw_line)
    if not m:
        return ("-", "-")
    return (m.group(1), m.group(2).capitalize())


def _xid_subtype_tag(raw_line):
    """Return a stable sub-type tag for collapse-keying purposes.

    NVRM kernel driver tags NVLink-related Xid (typically 145/149) with a
    textual category + severity, e.g.::

        Xid (PCI:...): 145, RLW_SRC_TRACK Nonfatal XC1 i0 Link 03 (...)
        Xid (PCI:...): 145, RLW_RXPIPE   Nonfatal XC0 i0 Link 03 (...)
        Xid (PCI:...): 145, RLW_SRC_TRACK Fatal   XC0 i0 Link 03 (...)
        Xid (PCI:...): 149, NETIR_LINK_DOWN Fatal XC0 i0 Link 03 (...)

    These distinguish very different fault categories (e.g. Fatal vs Nonfatal,
    RLW_SRC_TRACK vs RLW_RXPIPE). If raw-log collapse keyed on Xid number
    alone, dozens of common-subtype lines would push rare-but-critical
    sibling subtypes (a single Fatal entry hidden among hundreds of Nonfatal)
    into the "...N more omitted..." bucket. Keying on (xid_num, subtype_tag)
    keeps each subtype visible up to its own ``max_per_xid`` quota.

    For Xid lines without a textual subtype (e.g. cleanup Xid 45 channel
    lines), returns an empty string -- callers should treat that as "no
    further partitioning needed".
    """
    category, severity = _xid_subtype_parts(raw_line)
    if category == "-":
        return ""
    return f"{category}_{severity.lower()}"


def _render_burst_log(burst, context_lines=None, max_per_xid=5, imex_lines=None,
                      derivative_notes=None):
    """Render a burst's raw log lines with interleaved NVRM context, collapsing repeated Xid types.

    If imex_lines is provided (list of IMEX log entry dicts with "timestamp" and
    "raw_line"/"message" keys), the first few entries starting from the burst's
    earliest timestamp are prepended to the output.

    If derivative_notes is provided (dict mapping ``id(xid_dict) -> note``,
    produced by :func:`_collapse_burst_derivatives`), the note is emitted as
    a ``# +N ... suppressed`` comment line immediately after the matching
    representative line, so the rendered code block stays compact while
    making the suppression explicit.
    """
    ref_year = 2026

    merged = []
    for x in burst:
        merged.append(("xid", x["timestamp"], x))
    if context_lines:
        for cl in context_lines:
            merged.append(("ctx", cl["timestamp"], cl))

    if imex_lines:
        burst_dts = [_parse_syslog_ts(x["timestamp"], ref_year) for x in burst]
        valid_burst_dts = [d for d in burst_dts if d]
        burst_start = min(valid_burst_dts) if valid_burst_dts else None
        if burst_start:
            MAX_IMEX = 5
            added = 0
            for ie in imex_lines:
                ie_dt = _parse_imex_log_ts(ie["timestamp"])
                if ie_dt and ie_dt >= burst_start:
                    raw = ie.get("raw_line") or f"{ie['timestamp']} nvidia-imex: [{ie.get('level', 'ERROR')}] {ie['message']}"
                    merged.append(("imex", ie["timestamp"], {"raw_line": raw, "timestamp": ie["timestamp"]}))
                    added += 1
                    if added >= MAX_IMEX:
                        break

    merged.sort(key=lambda item: _parse_syslog_ts(item[1], ref_year) or _parse_imex_log_ts(item[1]) or datetime.min)

    lines_out = []
    # Collapse key is (xid_num, subtype_tag) so e.g. Xid 145 RLW_SRC_TRACK
    # Nonfatal does not eat the display quota of Xid 145 RLW_SRC_TRACK Fatal
    # or Xid 145 RLW_RXPIPE Nonfatal -- rare-but-critical sibling sub-types
    # remain visible up to their own ``max_per_xid`` budget.
    consecutive_key = None       # tuple (xid_num, subtype_tag) or None
    consecutive_count = 0
    consecutive_shown = 0

    def _format_kind_label(key):
        xid_num, subtype = key
        if subtype:
            # subtype tag e.g. "RLW_SRC_TRACK_nonfatal" -> "Xid 145 RLW_SRC_TRACK Nonfatal"
            base, _, sev = subtype.rpartition("_")
            sev_disp = sev.capitalize() if sev else ""
            return f"Xid {xid_num} {base} {sev_disp}".rstrip()
        return f"Xid {xid_num}"

    def flush_consecutive():
        nonlocal consecutive_count, consecutive_shown, consecutive_key
        if consecutive_count > consecutive_shown and consecutive_key is not None:
            label = _format_kind_label(consecutive_key)
            lines_out.append(
                f"  ...({consecutive_count - consecutive_shown} more {label} omitted)..."
            )
        consecutive_key = None
        consecutive_count = 0
        consecutive_shown = 0

    _notes = derivative_notes or {}

    def _maybe_emit_note(xid_item):
        note = _notes.get(id(xid_item))
        if note:
            lines_out.append(f"  # {note}")

    for kind, ts, item in merged:
        if kind in ("ctx", "imex"):
            flush_consecutive()
            lines_out.append(item["raw_line"])
        else:
            xid_num = item["xid"]
            subtype = _xid_subtype_tag(item.get("raw_line", ""))
            key = (xid_num, subtype)
            if key == consecutive_key:
                consecutive_count += 1
                if consecutive_shown < max_per_xid:
                    lines_out.append(item["raw_line"])
                    _maybe_emit_note(item)
                    consecutive_shown += 1
            else:
                flush_consecutive()
                consecutive_key = key
                consecutive_count = 1
                consecutive_shown = 1
                lines_out.append(item["raw_line"])
                _maybe_emit_note(item)

    flush_consecutive()
    return lines_out


def generate_report(filepath, sys_info, lspci_gpus, lspci_detail, smi_gpus, xids, nvrm_errors, nvlink, fallen_off, xid_decoded=None, dmesg_highlights=None, nvlink_status=None, imex=None, xid_analyzer_error="", bursts=None, context_by_burst=None):
    r = []

    is_c2c_gpu = False
    if smi_gpus:
        gpu_name = smi_gpus[0].get("name", "")
        for kw in C2C_GPU_KEYWORDS:
            if kw in gpu_name:
                is_c2c_gpu = True
                break

    sec = 0  # dynamic section counter

    r.append("# NVIDIA Bug Report Analysis")
    r.append(f"\n**Source File**: `{os.path.basename(filepath)}`\n")

    sec += 1
    r.append(f"## {sec}. System Overview\n")
    r.append("| Item | Value |")
    r.append("|------|-------|")
    for k, label in [("hostname", "Hostname"), ("system_sn", "System Serial Number"),
                     ("chassis_sn", "Chassis Serial Number"),
                     ("slot_number", "Slot Number"), ("tray_index", "Tray Index"),
                     ("date_short", "Date"), ("os", "OS"),
                     ("kernel", "Kernel"), ("arch", "Arch"), ("driver", "Driver Version"),
                     ("cuda", "CUDA Version"), ("boot_time", "Boot Time")]:
        r.append(f"| {label} | {sys_info.get(k, 'N/A')} |")
    gpu_count = max(len(smi_gpus), len(lspci_gpus))
    r.append(f"| GPU Count | {gpu_count} |")

    lspci_bdfs = {g["bdf"].lower() for g in lspci_gpus}
    smi_bdfs = {g.get("bdf", "").lower() for g in smi_gpus}
    gpu_init_failed = lspci_bdfs - smi_bdfs if len(lspci_gpus) > len(smi_gpus) else set()
    if gpu_init_failed:
        r.append(f"\n> **WARNING**: lspci detected {len(lspci_gpus)} GPUs but nvidia-smi only recognizes "
                 f"{len(smi_gpus)}. The following GPU(s) may have failed initialization:")
        for bdf in sorted(gpu_init_failed):
            r.append(f"> - {bdf}")

    sec += 1
    r.append(f"\n## {sec}. GPU List\n")
    r.append("| GPU# | BDF | Model | SN | UUID | VBIOS |")
    r.append("|------|-----|-------|-----|------|-------|")
    for idx, gpu in enumerate(smi_gpus):
        r.append(f"| {idx} | {gpu.get('bdf', 'N/A')} | {gpu.get('name', 'N/A')} | "
                 f"{gpu.get('sn', 'N/A')} | {gpu.get('uuid', 'N/A')} | {gpu.get('vbios', 'N/A')} |")

    sec += 1; sec_pcie = sec
    r.append(f"\n## {sec}. PCIe Status\n")
    has_pcie_issue = False
    has_aer = False

    r.append(f"### {sec_pcie}.1 PCIe Link Status\n")
    r.append("| GPU# | BDF | LnkCap | LnkSta | Status | Retimer | Equalization |")
    r.append("|------|-----|--------|--------|------|---------|--------------|")
    for idx, gpu in enumerate(lspci_gpus):
        bdf = gpu["bdf"]
        detail = lspci_detail.get(bdf, {})
        cap = detail.get("lnk_cap_short", "N/A")
        sta = detail.get("lnk_sta_short", "N/A")
        ok = detail.get("lnk_ok", True)
        retimer = detail.get("retimer", "N/A") or "N/A"
        equalization = detail.get("equalization", "N/A") or "N/A"

        status_parts = []
        if detail.get("rev_ff"):
            status_parts.append("rev ff (device dead)")
        if detail.get("unknown_header"):
            status_parts.append("Unknown Header")
        if detail.get("lane_errors"):
            status_parts.append(f"LaneErr: {detail['lane_errors']}")
        if not ok or status_parts:
            has_pcie_issue = True
        if status_parts:
            status = " / ".join(status_parts)
            if not ok and "rev ff" not in status and "Unknown" not in status:
                status += " / Degraded"
            status = "❌ " + status
        elif not ok:
            status = "❌ Degraded"
        else:
            status = "✅ OK"

        r.append(f"| {idx} | {bdf} | {cap} | {sta} | {status} | {retimer} | {equalization} |")

    if has_pcie_issue and is_c2c_gpu:
        r.append(f"\n> **Note**: {smi_gpus[0].get('name', 'GPU')} uses **NVLink C2C** as the primary CPU-GPU data path. "
                 "PCIe link degradation (e.g. x16→x1) has limited impact on actual bandwidth; prioritize NVLink status.")

    smi_pcie_issues = []
    for idx, gpu in enumerate(smi_gpus):
        max_w = gpu.get("pcie_max_w", "")
        cur_w = gpu.get("pcie_cur_w", "")
        if max_w and cur_w and max_w != cur_w:
            smi_pcie_issues.append(f"GPU {idx} ({gpu['bdf']}): PCIe Width Max={max_w}, Current={cur_w}")
    if smi_pcie_issues:
        r.append("\n**PCIe Link Width anomalies reported by nvidia-smi:**")
        for issue in smi_pcie_issues:
            r.append(f"- {issue}")
    r.append("")

    # --- PCIe BAR Regions ---
    all_regions = {}
    for idx, gpu in enumerate(lspci_gpus):
        bdf = gpu["bdf"]
        detail = lspci_detail.get(bdf, {})
        regions = detail.get("regions", [])
        all_regions[idx] = regions

    r.append(f"### {sec_pcie}.2 PCIe BAR Regions\n")
    if any(all_regions.values()):
        region_issues = []
        ref_signature = None
        for idx, regions in all_regions.items():
            bdf = lspci_gpus[idx]["bdf"]
            sig = tuple((reg["num"], reg["size"]) for reg in sorted(regions, key=lambda r: r["num"]))
            if ref_signature is None:
                ref_signature = sig
            elif sig != ref_signature:
                region_issues.append(f"GPU {idx} ({bdf}): BAR layout differs from GPU 0 — "
                                     + ", ".join(f"Region {reg['num']}: {reg['size']}" for reg in regions))
            for reg in regions:
                if reg["disabled"]:
                    region_issues.append(f"GPU {idx} ({bdf}): Region {reg['num']} **disabled**")
                if reg["virtual"]:
                    region_issues.append(f"GPU {idx} ({bdf}): Region {reg['num']} **virtual** (no physical address assigned)")

        all_reg_nums = sorted(set(reg["num"] for regs in all_regions.values() for reg in regs))
        if not all_reg_nums:
            all_reg_nums = [0, 2, 4]
        header = "| GPU# | BDF | " + " | ".join(f"Region {n}" for n in all_reg_nums) + " |"
        sep = "|------|-----|" + "|".join("-------" for _ in all_reg_nums) + "|"
        r.append(header)
        r.append(sep)
        for idx, regions in all_regions.items():
            bdf = lspci_gpus[idx]["bdf"]
            reg_map = {reg["num"]: reg for reg in regions}
            cols = []
            for rn in all_reg_nums:
                if rn in reg_map:
                    reg = reg_map[rn]
                    flag = ""
                    if reg["disabled"]:
                        flag = " ❌ disabled"
                    elif reg["virtual"]:
                        flag = " ⚠ virtual"
                    cols.append(f"{reg['size']}{flag}")
                else:
                    cols.append("N/A")
            r.append(f"| {idx} | {bdf} | " + " | ".join(cols) + " |")

        if region_issues:
            has_pcie_issue = True
            r.append("\n**BAR Region Anomalies:**")
            for issue in region_issues:
                r.append(f"- {issue}")
        else:
            r.append("\nBAR Region mapping is normal.")
    else:
        r.append("No BAR Region information detected.")
    r.append("")

    r.append(f"### {sec_pcie}.3 PCIe AER Errors\n")
    aer_lines = []
    aer_lines.append("| GPU# | BDF | DevSta | UESta Issues | CESta Issues |")
    aer_lines.append("|------|-----|--------|--------------|--------------|")
    for idx, gpu in enumerate(lspci_gpus):
        bdf = gpu["bdf"]
        detail = lspci_detail.get(bdf, {})
        dev_sta = detail.get("dev_sta", "N/A")
        ue_sta = detail.get("ue_sta", "N/A")
        ce_sta = detail.get("ce_sta", "N/A")

        ue_issues = [item.rstrip("+") for item in ue_sta.split() if item.endswith("+")]
        ce_issues = [item.rstrip("+") for item in ce_sta.split() if item.endswith("+")]
        dev_issues = [item.rstrip("+") for item in dev_sta.split() if item.endswith("+")
                      and item.rstrip("+") in ("FatalErr", "NonFatalErr", "CorrErr")]

        if ue_issues or ce_issues or dev_issues:
            has_aer = True

        ue_str = ", ".join(ue_issues) if ue_issues else "None"
        ce_str = ", ".join(ce_issues) if ce_issues else "None"

        aer_lines.append(f"| {idx} | {bdf} | {dev_sta} | {ue_str} | {ce_str} |")

    if has_aer:
        r.extend(aer_lines)
    else:
        r.append("All GPU PCIe AER status is normal.")

    sec += 1
    r.append(f"\n## {sec}. Remapped Rows\n")
    r.append("| GPU# | Correctable | Uncorrectable | Pending | Failure |")
    r.append("|------|-------------|---------------|---------|---------|")
    for idx, gpu in enumerate(smi_gpus):
        rem = gpu.get("remapped", {})
        r.append(f"| {idx} | {rem.get('ce', 'N/A')} | {rem.get('ue', 'N/A')} | "
                 f"{rem.get('pending', 'N/A')} | {rem.get('failure', 'N/A')} |")

    has_nvlink_inactive = False
    has_nvlink_speed_mismatch = False
    has_nvlink_err = False
    has_fec_high = False
    has_ber_concern = False
    nvlink_inactive_count = 0
    has_nvlink_data = bool(nvlink) or bool(nvlink_status)

    status_map = {}
    if nvlink_status:
        for nv in nvlink_status:
            for link_id, info in nv["links"].items():
                status_map[(nv["gpu_idx"], link_id)] = info

    all_link_errors = {}
    all_fec = {}
    all_ber = {}
    for nv in nvlink:
        gpu_idx = nv["gpu_idx"]
        for link_id, errs in nv.get("link_errors", {}).items():
            all_link_errors[(gpu_idx, link_id)] = errs
        for link_id, bins in nv.get("fec_errors", {}).items():
            all_fec[(gpu_idx, link_id)] = dict(bins)
        for link_id, ber in nv.get("ber", {}).items():
            all_ber[(gpu_idx, link_id)] = dict(ber)

    gpu_indices = sorted({k[0] for k in status_map} | {k[0] for k in all_link_errors}
                         | {k[0] for k in all_fec} | {k[0] for k in all_ber})
    all_link_ids = sorted({k[1] for k in status_map} | {k[1] for k in all_link_errors}
                          | {k[1] for k in all_fec} | {k[1] for k in all_ber})

    gb200_fields = {"Rx Errors", "Effective Errors", "Effective BER"}
    has_gb200_nvlink = any(
        any(f in errs for f in gb200_fields)
        for errs_dict in [all_link_errors]
        for errs in errs_dict.values()
    ) or bool(all_ber)

    sec += 1; sec_nvlink = sec
    r.append(f"\n## {sec}. NVLink Status\n")

    if not has_nvlink_data:
        r.append("N/A - No NVLink data in this log.\n")
    elif has_gb200_nvlink and gpu_indices and all_link_ids:
        # --- Full NVLink output (GB200 style with detailed counters) ---
        sub = 0

        packet_err_fields = [
            "Rx Errors", "Rx remote Errors", "Rx General Errors",
            "Malformed packet Errors", "Buffer overrun Errors", "Tx discards",
        ]
        sub += 1
        r.append(f"### {sec_nvlink}.{sub} Packet/Link Errors\n")
        r.append("Cell format: Rx Errors / Rx remote Errors / Rx General Errors / Malformed packet Errors / Buffer overrun Errors / Tx discards\n")
        link_hdrs = " | ".join(f"L{l}" for l in all_link_ids)
        r.append("<!-- nvbug:table-class=nvlink-matrix wide-matrix numeric-matrix -->")
        r.append(f"| GPU# | {link_hdrs} |")
        r.append("|------" + "|------" * len(all_link_ids) + "|")
        for gpu_idx in gpu_indices:
            cells = []
            for link_id in all_link_ids:
                errs = all_link_errors.get((gpu_idx, link_id), {})
                vals = [errs.get(f, 0) for f in packet_err_fields]
                if any(v > 0 for v in vals):
                    has_nvlink_err = True
                cells.append(" / ".join(str(v) for v in vals))
            r.append(f"| {gpu_idx} | " + " | ".join(cells) + " |")
        r.append("")

        recovery_fields = [
            "Link recovery successful events",
            "Link recovery failed events",
            "Total link recovery events",
        ]
        sub += 1
        r.append(f"### {sec_nvlink}.{sub} Link Recovery Events\n")
        r.append("Cell format: Link recovery successful events / Link recovery failed events / Total link recovery events\n")
        r.append("<!-- nvbug:table-class=nvlink-matrix wide-matrix numeric-matrix -->")
        r.append(f"| GPU# | {link_hdrs} |")
        r.append("|------" + "|------" * len(all_link_ids) + "|")
        for gpu_idx in gpu_indices:
            cells = []
            for link_id in all_link_ids:
                errs = all_link_errors.get((gpu_idx, link_id), {})
                vals = [errs.get(f, 0) for f in recovery_fields]
                if any(v > 0 for v in vals):
                    has_nvlink_err = True
                cells.append(" / ".join(str(v) for v in vals))
            r.append(f"| {gpu_idx} | " + " | ".join(cells) + " |")
        r.append("")

        sub += 1
        r.append(f"### {sec_nvlink}.{sub} Effective/Symbol Errors & BER\n")
        r.append("Cell format (line-separated): Effective Errors / Effective BER / Symbol Errors / Symbol BER\n")
        r.append("<!-- nvbug:table-class=nvlink-matrix wide-matrix numeric-matrix -->")
        r.append(f"| GPU# | {link_hdrs} |")
        r.append("|------" + "|------" * len(all_link_ids) + "|")
        for gpu_idx in gpu_indices:
            cells = []
            for link_id in all_link_ids:
                errs = all_link_errors.get((gpu_idx, link_id), {})
                ber = all_ber.get((gpu_idx, link_id), {})
                eff_err = errs.get("Effective Errors", 0)
                eff_ber = ber.get("Effective BER", "15e-255")
                sym_err = errs.get("Symbol Errors", 0)
                sym_ber = ber.get("Symbol BER", "15e-255")
                if eff_err != 0 or sym_err != 0 or eff_ber != "15e-255" or sym_ber != "15e-255":
                    has_ber_concern = True
                cells.append(f"{eff_err}<br>{eff_ber}<br>{sym_err}<br>{sym_ber}")
            r.append(f"| {gpu_idx} | " + " | ".join(cells) + " |")
        r.append("")

        sub += 1
        r.append(f"### {sec_nvlink}.{sub} FEC Errors\n")

        fec_bins_all = defaultdict(lambda: defaultdict(int))
        for (gpu_idx, link_id), bins in all_fec.items():
            for bin_idx, count in bins.items():
                fec_bins_all[bin_idx][(gpu_idx, link_id)] = count

        if 3 in fec_bins_all:
            bin3 = fec_bins_all[3]
            if any(v > 0 for v in bin3.values()):
                has_fec_high = True
            r.append("#### FEC Errors-3\n")
            r.append("<!-- nvbug:table-class=nvlink-matrix wide-matrix numeric-matrix -->")
            r.append(f"| GPU# | {link_hdrs} |")
            r.append("|------" + "|------" * len(all_link_ids) + "|")
            for gpu_idx in gpu_indices:
                cells = []
                for link_id in all_link_ids:
                    val = bin3.get((gpu_idx, link_id), 0)
                    cells.append(str(val) if val > 0 else ".")
                r.append(f"| {gpu_idx} | " + " | ".join(cells) + " |")
            r.append("")

        if 4 in fec_bins_all:
            bin4 = fec_bins_all[4]
            if any(v > 0 for v in bin4.values()):
                has_fec_high = True
            r.append("#### FEC Errors-4\n")
            r.append("<!-- nvbug:table-class=nvlink-matrix wide-matrix numeric-matrix -->")
            r.append(f"| GPU# | {link_hdrs} |")
            r.append("|------" + "|------" * len(all_link_ids) + "|")
            for gpu_idx in gpu_indices:
                cells = []
                for link_id in all_link_ids:
                    val = bin4.get((gpu_idx, link_id), 0)
                    cells.append(str(val) if val > 0 else ".")
                r.append(f"| {gpu_idx} | " + " | ".join(cells) + " |")
            r.append("")

        fec_5_15_anomalies = []
        for bin_idx in range(5, 16):
            if bin_idx in fec_bins_all:
                for (gpu_idx, link_id), count in sorted(fec_bins_all[bin_idx].items()):
                    if count > 0:
                        fec_5_15_anomalies.append((gpu_idx, link_id, bin_idx, count))
                        has_fec_high = True

        r.append("#### FEC Errors 5-15\n")
        if fec_5_15_anomalies:
            for gpu_idx, link_id, bin_idx, count in fec_5_15_anomalies:
                r.append(f"- GPU {gpu_idx} Link {link_id}: FEC Errors-{bin_idx} = {count}")
        else:
            r.append("FEC Errors 5-15 are all zero.")
        r.append("")

        sub += 1
        r.append(f"### {sec_nvlink}.{sub} Link Status\n")
        inactive_links = []
        if nvlink_status:
            for nv in nvlink_status:
                for link_id, info in nv["links"].items():
                    if not info["active"]:
                        inactive_links.append((nv["gpu_idx"], link_id))
                        has_nvlink_inactive = True
                        nvlink_inactive_count += 1
        if inactive_links:
            r.append("**Inactive Links:**\n")
            for gpu_idx, link_id in inactive_links:
                r.append(f"- GPU {gpu_idx} Link {link_id}")
            r.append("")

        all_speeds = set()
        if nvlink_status:
            for nv in nvlink_status:
                for link_id, info in nv["links"].items():
                    if info["active"]:
                        all_speeds.add(info["raw"])
            if len(all_speeds) > 1:
                has_nvlink_speed_mismatch = True
                r.append(f"**Speed mismatch**: detected {len(all_speeds)} different speeds: {', '.join(sorted(all_speeds))}")
            elif all_speeds:
                if not inactive_links:
                    r.append(f"All links Active, speed consistent ({next(iter(all_speeds))}).")
                else:
                    r.append(f"Active link speed consistent ({next(iter(all_speeds))}).")
    else:
        # --- Simplified NVLink output (legacy: Replay/Recovery/CRC only) ---
        sub = 0
        legacy_err_fields = ["Replay Errors", "Recovery Errors", "CRC Errors"]

        sub += 1
        r.append(f"### {sec_nvlink}.{sub} Error Summary\n")
        if gpu_indices and all_link_ids:
            link_hdrs = " | ".join(f"L{l}" for l in all_link_ids)
            r.append("<!-- nvbug:table-class=nvlink-matrix wide-matrix numeric-matrix -->")
            r.append(f"| GPU# | {link_hdrs} |")
            r.append("|------" + "|------" * len(all_link_ids) + "|")
            r.append(f"| | " + " | ".join("Replay / Recovery / CRC" for _ in all_link_ids) + " |")
            for gpu_idx in gpu_indices:
                cells = []
                for link_id in all_link_ids:
                    errs = all_link_errors.get((gpu_idx, link_id), {})
                    vals = []
                    for f in legacy_err_fields:
                        v = errs.get(f)
                        if v is not None:
                            if v > 0:
                                has_nvlink_err = True
                            vals.append(str(v))
                        else:
                            vals.append("N/A")
                    cells.append(" / ".join(vals))
                r.append(f"| {gpu_idx} | " + " | ".join(cells) + " |")
        else:
            r.append("No NVLink error counter data available.")
        r.append("")

        sub += 1
        r.append(f"### {sec_nvlink}.{sub} Link Status\n")
        inactive_links = []
        if nvlink_status:
            for nv in nvlink_status:
                for link_id, info in nv["links"].items():
                    if not info["active"]:
                        inactive_links.append((nv["gpu_idx"], link_id))
                        has_nvlink_inactive = True
                        nvlink_inactive_count += 1
        if inactive_links:
            r.append("**Inactive Links:**\n")
            for gpu_idx, link_id in inactive_links:
                r.append(f"- GPU {gpu_idx} Link {link_id}")
            r.append("")

        all_speeds = set()
        if nvlink_status:
            for nv in nvlink_status:
                for link_id, info in nv["links"].items():
                    if info["active"]:
                        all_speeds.add(info["raw"])
            if len(all_speeds) > 1:
                has_nvlink_speed_mismatch = True
                r.append(f"**Speed mismatch**: detected {len(all_speeds)} different speeds: {', '.join(sorted(all_speeds))}")
            elif all_speeds:
                if not inactive_links:
                    r.append(f"All links Active, speed consistent ({next(iter(all_speeds))}).")
                else:
                    r.append(f"Active link speed consistent ({next(iter(all_speeds))}).")

    # --- IMEX Status ---
    has_imex_issue = False
    has_imex_conn_issue = False

    def _imex_has_data(imex_dict):
        if not imex_dict:
            return False
        return (imex_dict.get("service_active") is not None
                or imex_dict.get("service_status")
                or imex_dict.get("nodes")
                or imex_dict.get("non_connected")
                or imex_dict.get("error_lines")
                or imex_dict.get("imex_log_entries")
                or imex_dict.get("domain_state"))

    has_imex_data = _imex_has_data(imex)
    sec += 1
    r.append(f"\n## {sec}. IMEX Status\n")
    if not has_imex_data:
        r.append("N/A - No IMEX data in this log.\n")
    imex_events = []
    if imex and has_imex_data:
        svc_status = imex.get("service_status", "")
        svc_active = imex.get("service_active")
        if svc_active is None and not svc_status:
            r.append("No nvidia-imex.service status found in log.\n")
        else:
            r.append(f"**Service Status**: {svc_status}\n")
            if not svc_active:
                has_imex_issue = True

        imex_log_entries = imex.get("imex_log_entries", [])
        svc_error_lines = imex.get("error_lines", [])

        if imex_log_entries:
            has_imex_issue = True
            imex_events = _group_imex_events(imex_log_entries, gap_seconds=60)
            r.append("**IMEX Node Disconnect Events**:\n")
            for ev_idx, event in enumerate(imex_events):
                dts = [_parse_imex_log_ts(e["timestamp"]) for e in event]
                valid_dts = [d for d in dts if d]
                if valid_dts:
                    t_start = min(valid_dts).strftime("%b %d %Y %H:%M:%S")
                    t_end = max(valid_dts).strftime("%H:%M:%S")
                    if min(valid_dts).date() != max(valid_dts).date():
                        t_end = max(valid_dts).strftime("%b %d %Y %H:%M:%S")
                    ts_label = t_start if t_start.endswith(t_end) else f"{t_start} ~ {t_end}"
                else:
                    ts_label = "unknown time"
                summary_lines = _summarize_imex_event(event)
                n_total = len(event)
                n_unique = len(summary_lines)
                count_label = f"{n_total} messages, {n_unique} unique" if n_unique != n_total else f"{n_total} messages"
                r.append(f"<details><summary>Event Group {ev_idx + 1}: {ts_label} ({count_label})</summary>")
                r.append("")
                for sl in summary_lines:
                    r.append(f"- {sl}")
                r.append("")
                r.append("</details>")
                r.append("")
        elif svc_error_lines:
            has_imex_issue = True
            r.append("**Service Log Warnings**:\n")
            for el in svc_error_lines:
                r.append(f"- {el}")
            r.append("")

        nodes = imex.get("nodes", [])
        if nodes:
            if imex.get("ctl_timestamp"):
                r.append(f"**IMEX-ctl Timestamp**: {imex['ctl_timestamp']}\n")
            r.append("| Node# | Hostname | Status | Version |")
            r.append("|-------|----------|--------|---------|")
            for nd in nodes:
                status_icon = "✅" if nd["status"] == "READY" else "❌"
                r.append(f"| {nd['id']} | {nd['hostname']} | {status_icon} {nd['status']} | {nd['version']} |")
                if nd["status"] != "READY":
                    has_imex_issue = True
            r.append("")

        non_conn = imex.get("non_connected", [])
        if non_conn:
            has_imex_conn_issue = True
            r.append("**Non-Connected Node Pairs**:\n")
            r.append("| From | To | Status |")
            r.append("|------|-----|--------|")
            for fr, to, st in non_conn:
                r.append(f"| {fr} | {to} | {st} |")
            r.append("")
        elif nodes:
            r.append("All node interconnections normal (all Connected).\n")

        domain = imex.get("domain_state", "")
        if domain:
            r.append(f"**Domain State**: {domain}\n")
            if domain != "UP":
                has_imex_conn_issue = True

    # --- Message/dmesg Analysis (merged Xid + NVRM + dmesg) ---
    sec += 1; sec_msg = sec
    r.append(f"\n## {sec}. Message/dmesg Analysis\n")

    _boot_time = sys_info.get("boot_time", "N/A")
    _msg_start = sys_info.get("message_start_time", "N/A")
    r.append(f"> **Boot Time**: {_boot_time} | **Message Log Start**: {_msg_start}\n")

    if fallen_off:
        r.append("### ⚠️ GPU Fallen Off The Bus Events\n")
        for f_line in fallen_off:
            r.append(f"- `{f_line}`")
        r.append("")

    # --- 7.1 Xid Summary ---
    r.append(f"### {sec_msg}.1 Xid Summary\n")
    if xids:
        # Split primary vs derivative AND further partition by NVLink sub-type
        # (Category + Severity) so rare Fatal / RLW_RXPIPE variants get their
        # own row instead of disappearing under "Xid 145 = 1413" aggregate.
        # Non-NVLink Xids (44/45/137/154/...) have no textual sub-type in the
        # NVRM driver, so they bucket into a single ("-", "-") row per
        # (BDF, Xid) -- visually identical to the pre-split layout.
        #
        # Key tuple: (bdf, xid_num, category, severity)
        # Tracked via x["is_derivative"] / x["caused_by"] from extractors.
        xid_primary = defaultdict(int)         # key -> primary count
        xid_deriv = defaultdict(int)           # key -> derivative count
        xid_deriv_source = defaultdict(set)    # key -> set of caused-by primaries
        total_primary = 0
        total_deriv = 0
        for x in xids:
            category, severity = _xid_subtype_parts(x.get("raw_line", ""))
            key = (x["bdf"], x["xid"], category, severity)
            if x.get("is_derivative"):
                xid_deriv[key] += 1
                if x.get("caused_by"):
                    xid_deriv_source[key].add(x["caused_by"])
                total_deriv += 1
            else:
                xid_primary[key] += 1
                total_primary += 1

        r.append(
            f"**Total {len(xids)} Xid errors** "
            f"({total_primary} primary, {total_deriv} derivative)\n"
        )
        r.append(
            "| GPU BDF | Xid | Category | Severity | Total | Primary | "
            "Derivative | Caused-by |"
        )
        r.append(
            "|---------|-----|----------|----------|-------|---------|"
            "------------|-----------|"
        )

        def _row_sort_key(key):
            bdf, xid_num, category, severity = key
            # Fatal rows first within each (BDF, Xid), then Nonfatal, then "-"
            sev_prio = {"Fatal": 0, "Nonfatal": 1}.get(severity, 2)
            return (bdf, xid_num, sev_prio, category)

        all_keys = sorted(set(xid_primary) | set(xid_deriv), key=_row_sort_key)
        for key in all_keys:
            bdf, xid_num, category, severity = key
            p = xid_primary[key]
            d = xid_deriv[key]
            sources = xid_deriv_source[key]
            if sources:
                try:
                    caused_by = ", ".join(sorted(sources, key=int))
                except ValueError:
                    caused_by = ", ".join(sorted(sources))
            else:
                caused_by = "-"
            r.append(
                f"| {bdf} | {xid_num} | {category} | {severity} | "
                f"{p + d} | {p} | {d} | {caused_by} |"
            )

        if total_deriv:
            inferred_n = sum(
                1 for x in xids
                if x.get("is_derivative") and x.get("derivative_inferred")
            )
            footer = (
                "\n*Derivative Xids are NVRM lines tagged "
                "\"caused by previous Xid N\" (typically Xid 45 channel "
                "cleanup after a primary fault). Section 7.2 decodes every "
                "Xid type (primary and derivative both feed the analyzer "
                "so the decode table reflects all observed Xid mnemonics, "
                "deduplicated by decode signature); section 7.3 collapses "
                "repeated derivatives per burst."
            )
            if inferred_n:
                footer += (
                    f" Includes **{inferred_n} inferred** entries: bare Xid 45 "
                    "lines without the textual tag that the NVRM driver "
                    "emitted within 10s of a primary Xid 145/149 on the same "
                    "BDF -- the same cleanup burst, attribution lost in the "
                    "kernel log."
                )
            footer += (
                " Category + Severity columns are populated for Xid 144-150 "
                "(NVLink range, per NVIDIA Server-RAS-Catalog); other Xids "
                "show \"-\" since the NVRM driver doesn't emit a textual "
                "sub-type for them.*"
            )
            r.append(footer)
    else:
        r.append("No Xid errors found.")

    # --- 7.2 Xid Detailed Decode ---
    if xids:
        r.append(f"\n### {sec_msg}.2 Xid Detailed Decode (nvidia_xid_analyzer)\n")
    if xids and xid_decoded:
        seen_signatures = set()
        unique_decoded = []
        for entry in xid_decoded:
            sig = (entry.get("decoded_xid", ""), entry.get("mnemonic", ""),
                   entry.get("resolution", ""))
            if sig not in seen_signatures:
                seen_signatures.add(sig)
                unique_decoded.append(entry)

        r.append("| Decoded XID | Mnemonic | Severity | HW/SW Classification | Resolution | Investigation | Comments |")
        r.append("|-------------|----------|----------|----------------------|------------|---------------|----------|")
        for entry in unique_decoded:
            decoded = entry.get("decoded_xid", "N/A")
            mnemonic = entry.get("mnemonic", "N/A")
            severity = entry.get("job_severity", "N/A")
            hw_sw = entry.get("hw_sw", "N/A")
            resolution = entry.get("resolution", "N/A")
            investigatory = entry.get("investigatory", "N/A")
            comments = entry.get("comments", "")
            r.append(f"| {decoded} | {mnemonic} | {severity} | {hw_sw} | {resolution} | {investigatory} | {comments} |")

        r.append(f"\n*(Total {len(xid_decoded)} decoded, {len(unique_decoded)} unique categories after dedup)*")
    elif xids:
        if xid_analyzer_error.startswith(MISSING_ASSET_PREFIX):
            friendly = xid_analyzer_error[len(MISSING_ASSET_PREFIX):]
            r.append(f"> **XID analyzer assets missing**: {friendly}")
            r.append(
                "> (expected at `scripts/third_party/nvidia_xid_analyzer.py` "
                "alongside `Server-RAS-Catalog.xlsx`)"
            )
        elif xid_analyzer_error:
            r.append(f"**(nvidia_xid_analyzer failed: {xid_analyzer_error})**")
        else:
            r.append("*(Pending — Agent will invoke nvidia_xid_analyzer to populate)*")

    # --- 7.3 Xid Raw Logs ---
    _bursts = bursts if bursts is not None else group_xids_into_bursts(xids, gap_seconds=60)
    _ctx = context_by_burst or {}
    _xid_ref_year = 2026
    if xids:
        for x in xids:
            if x["timestamp"] and not x["timestamp"].startswith("["):
                _ry = re.search(r"\b(20\d{2})\b", x.get("raw_line", ""))
                if _ry:
                    _xid_ref_year = int(_ry.group(1))
                break
    _imex_xid_correlations = []
    if xids and _bursts:
        r.append(f"\n### {sec_msg}.3 Xid Raw Logs\n")
        for idx_b, burst in enumerate(_bursts):
            ts_first = burst[0]["timestamp"]
            ts_last = burst[-1]["timestamp"]
            # Burst-internal counts surface the suppression in the summary line.
            _b_primary = sum(1 for x in burst if not x.get("is_derivative"))
            _b_deriv = len(burst) - _b_primary
            if ts_first == ts_last:
                summary = f"Event Group {idx_b + 1}: {ts_first} ({len(burst)} entries"
            else:
                summary = (
                    f"Event Group {idx_b + 1}: {ts_first} ~ {ts_last} "
                    f"({len(burst)} entries"
                )
            if _b_deriv:
                summary += f"; {_b_primary} primary + {_b_deriv} derivative)"
            else:
                summary += ")"
            r.append(f"<details>")
            r.append(f"<summary>{summary}</summary>")
            r.append("")
            _related_imex_lines = None
            if imex_events:
                # IMEX correlation uses the FULL burst (time range), not the
                # collapsed display list, so derivative count doesn't affect
                # the +/-60s correlation window.
                _related = _find_related_imex_events(imex_events, burst, ref_year=_xid_ref_year, window_seconds=60)
                if _related:
                    r.append(_format_imex_related_line(_related))
                    r.append("")
                    _related_imex_lines = [e for _idx, grp in _related for e in grp]
                    imex_labels = []
                    for ie_idx, ie_grp in _related:
                        ie_dts = [_parse_imex_log_ts(e["timestamp"]) for e in ie_grp]
                        ie_valid = [d for d in ie_dts if d]
                        ie_ts = min(ie_valid).strftime("%Y-%m-%d %H:%M:%S") if ie_valid else "N/A"
                        imex_labels.append((ie_idx + 1, ie_ts))
                    xid_ts_label = ts_first if ts_first == ts_last else f"{ts_first} ~ {ts_last}"
                    _imex_xid_correlations.append((idx_b + 1, xid_ts_label, imex_labels))
            display_burst, derivative_notes = _collapse_burst_derivatives(burst)
            r.append("```")
            r.extend(_render_burst_log(
                display_burst,
                context_lines=_ctx.get(idx_b),
                imex_lines=_related_imex_lines,
                derivative_notes=derivative_notes,
            ))
            r.append("```")
            r.append("")
            r.append("</details>")
            r.append("")

    # --- 7.4 Other GPU Related ---
    r.append(f"### {sec_msg}.4 Other GPU Related\n")
    if nvrm_errors:
        seen = set()
        unique = []
        for e in nvrm_errors:
            key = e["message"][:100]
            if key not in seen:
                seen.add(key)
                unique.append(e)
        _n74_total = len(nvrm_errors)
        _n74_unique = len(unique)
        r.append(f"<details>")
        r.append(f"<summary>Other GPU Related ({_n74_unique} unique / {_n74_total} total)</summary>")
        r.append("")
        for e in unique[:20]:
            r.append(f"- [{e['timestamp']}] `{e['message'][:200]}`")
        if _n74_unique > 20:
            r.append(f"\n*(Total {_n74_total} entries, {_n74_unique} unique after dedup, showing first 20)*")
        r.append("")
        r.append("</details>")
    else:
        r.append("No other GPU-related NVRM errors found.")

    # --- 7.5 Other Warnings ---
    if dmesg_highlights and any(dmesg_highlights.get(k) for k in ("system_errors", "other_warnings")):
        r.append(f"\n### {sec_msg}.5 Other Warnings\n")
        _hl_sections = [
            ("System Errors", dmesg_highlights.get("system_errors", [])),
            ("Other Warnings", dmesg_highlights.get("other_warnings", [])),
        ]
        for title, items in _hl_sections:
            if items:
                r.append(f"<details>")
                r.append(f"<summary>{title} ({len(items)} entries)</summary>")
                r.append("")
                for item in items:
                    if isinstance(item, tuple) and len(item) == 2:
                        r.append(f"- `{item[1].strip()}`")
                    else:
                        r.append(f"- `{item}`")
                r.append("")
                r.append("</details>")
                r.append("")

    # --- 7.6 NVLink Xid 144-150 fault breakdown ---
    # One combined collapsible block per dump that pivots all NVLink-class
    # Xid 144-150 lines on (GPU BDF, Link, Xid number, sub-type Category,
    # Severity), with the (HEX1, HEX2) payload fingerprint attached to each
    # row. Covers the full NVLink5 Xid range per NVIDIA's Server-RAS-Catalog:
    #   144 SAW_MVB         147 TREX
    #   145 RLW_* (6 sub)   148 NVLPW_CTRL/NVLPW
    #   146 TLW_* (5 sub)   149 NETIR_* (4 sub)
    #                       150 MSE_*  (2 sub)
    # Replaces the old 7.6 (PCI+Link cross-tab) and 7.7 (errorStatus combo)
    # which forced the reader to mentally join two tables. Adding the
    # sub-type axis surfaces rare Fatal / RLW_RXPIPE variants that the
    # per-Xid-number aggregation would otherwise hide. NVLINK_XID_NUMS and
    # _xid_subtype_parts are module-level so cross-node section 3.1 reuses
    # the same scope and parsing.
    nvlink_xids = [x for x in xids if x["xid"] in NVLINK_XID_NUMS]
    if nvlink_xids:
        link_re = re.compile(r"\bLink\s+(-?\d+)\b")
        hex_re = re.compile(r"\b0x[0-9a-fA-F]{8}\b")

        # cluster_key -> aggregate dict; cluster_key = (bdf, link, xid, category, severity)
        clusters = defaultdict(
            lambda: {"count": 0, "combos": [], "first": None}
        )
        for x in nvlink_xids:
            raw = x.get("raw_line", "")
            link_m = link_re.search(raw)
            link = link_m.group(1) if link_m else "?"
            category, severity = _xid_subtype_parts(raw)
            hexes = hex_re.findall(raw)
            combo = f"{hexes[0]} {hexes[1]}" if len(hexes) >= 2 else "-"
            key = (x["bdf"], link, x["xid"], category, severity)
            row = clusters[key]
            row["count"] += 1
            if combo not in row["combos"]:
                row["combos"].append(combo)
            if row["first"] is None:
                row["first"] = x["timestamp"]

        def _row_sort_key(key):
            bdf, link, xid_num, category, severity = key
            try:
                link_n = int(link)
            except (TypeError, ValueError):
                link_n = 10 ** 9
            # Severity priority: Fatal first (more urgent), then Nonfatal,
            # then unknown ("-"). Lower number = sorted first.
            sev_prio = {"Fatal": 0, "Nonfatal": 1}.get(severity, 2)
            return (bdf, link_n, sev_prio, category, xid_num)

        # Header summary metrics: per-Xid totals and Fatal counts for the
        # full 144-150 range, dropping any Xid number absent from this dump
        # so the summary stays compact.
        bdf_set = {k[0] for k in clusters.keys()}
        per_xid_total = {n: 0 for n in NVLINK_XID_NUMS}
        per_xid_fatal = {n: 0 for n in NVLINK_XID_NUMS}
        for k, v in clusters.items():
            per_xid_total[k[2]] += v["count"]
            if k[4] == "Fatal":
                per_xid_fatal[k[2]] += v["count"]
        per_xid_summary = ", ".join(
            f"Xid {n} = {per_xid_total[n]}"
            + (f" (Fatal {per_xid_fatal[n]})" if per_xid_fatal[n] else "")
            for n in NVLINK_XID_NUMS if per_xid_total[n] > 0
        )
        summary_76 = (
            f"NVLink Xid 144-150 Breakdown "
            f"({len(bdf_set)} GPU(s), {len(clusters)} clusters; "
            f"{per_xid_summary})"
        )

        r.append(f"\n### {sec_msg}.6 NVLink Xid 144-150 Fault Breakdown\n")
        r.append("<details>")
        r.append(f"<summary>{summary_76}</summary>")
        r.append("")
        r.append(
            "| GPU BDF | Link | Xid | Category | Severity | Count | "
            "errorStatus combo | First seen |"
        )
        r.append(
            "|---------|------|-----|----------|----------|-------|"
            "-------------------|------------|"
        )
        for key in sorted(clusters.keys(), key=_row_sort_key):
            bdf, link, xid_num, category, severity = key
            row = clusters[key]
            if len(row["combos"]) == 1:
                combo_disp = f"`{row['combos'][0]}`"
            else:
                # very rare; same cluster with multiple HEX1/HEX2 fingerprints
                combo_disp = (
                    f"`{row['combos'][0]}` *(+{len(row['combos']) - 1} other)*"
                )
            r.append(
                f"| {bdf} | {link} | {xid_num} | {category} | {severity} | "
                f"{row['count']} | {combo_disp} | {row['first'] or 'N/A'} |"
            )
        r.append("")
        r.append(
            "*Covers the NVLink Xid family per NVIDIA Server-RAS-Catalog: "
            "**144** (SAW_MVB), **145** (RLW_* x6 subcodes), "
            "**146** (TLW_* x5), **147** (TREX), **148** (NVLPW), "
            "**149** (NETIR_* x4), **150** (MSE_* x2) -- "
            "the only Xid range that uses the textual `<Category> <Severity>` "
            "payload format in the NVRM driver. One row per (GPU BDF, Link, "
            "Xid, Sub-type Category, Severity) cluster; Fatal rows sort first "
            "within each Link. **Category** + **Severity** come from the "
            "NVRM driver text (e.g. `RLW_SRC_TRACK Nonfatal`, "
            "`NETIR_LINK_DOWN Fatal`). The **errorStatus combo** is the "
            "(HEX1, HEX2) fingerprint of the first two hex words in the raw "
            "Xid line -- an NVIDIA-internal payload pair whose bit layout is "
            "not publicly documented, but empirically same-cluster lines "
            "share the same combo. Same number-different-sub-type rows "
            "(e.g. Xid 145 RLW_SRC_TRACK Fatal vs Nonfatal) are intentionally "
            "kept separate so rare critical variants stay visible. Lifted "
            "from AutoProcess.sh's per-link + first-2-word breakdown.*"
        )
        r.append("")
        r.append("</details>")

    sec += 1
    r.append(f"\n## {sec}. Summary & Recommendations\n")
    issues_critical = []
    issues_warn = []

    if gpu_init_failed:
        issues_critical.append(f"GPU initialization failure: lspci detected {len(lspci_gpus)} GPUs but "
                               f"nvidia-smi only recognizes {len(smi_gpus)} — BDF(s): {', '.join(sorted(gpu_init_failed))}")
    if fallen_off:
        issues_critical.append("GPU fallen off the bus event(s) detected")
    if has_pcie_issue:
        bad_lnk = []
        bad_hw = []
        for g in lspci_gpus:
            det = lspci_detail.get(g["bdf"], {})
            if not det.get("lnk_ok", True):
                if det.get("rev_ff") or det.get("unknown_header"):
                    bad_hw.append(f"{g['bdf']} (HW dead/offline)")
                else:
                    bad_lnk.append(g["bdf"])
        
        if bad_hw:
            issues_critical.append(f"PCIe critical HW failure: {', '.join(bad_hw)}")
        if bad_lnk:
            if is_c2c_gpu:
                issues_warn.append(f"PCIe link degraded: {', '.join(bad_lnk)} (NVLink C2C is primary data path, limited impact)")
            else:
                issues_critical.append(f"PCIe link degraded: {', '.join(bad_lnk)}")
            
        bar_bad = []
        if lspci_gpus:
            ref_sig = None
            for idx, gpu in enumerate(lspci_gpus):
                detail = lspci_detail.get(gpu["bdf"], {})
                regions = detail.get("regions", [])
                sig = tuple((r["num"], r["size"]) for r in sorted(regions, key=lambda x: x["num"]))
                if ref_sig is None:
                    ref_sig = sig
                for reg in regions:
                    if reg.get("disabled") or reg.get("virtual"):
                        bar_bad.append(f"GPU {idx} ({gpu['bdf']}) Region {reg['num']}")
                if sig != ref_sig:
                    bar_bad.append(f"GPU {idx} ({gpu['bdf']}) BAR layout inconsistent")
        if bar_bad:
            issues_critical.append(f"PCIe BAR Region anomaly: {', '.join(bar_bad)}")
    for idx, gpu in enumerate(smi_gpus):
        for ecc_type in ["ecc_vol", "ecc_agg"]:
            ecc = gpu.get(ecc_type, {})
            for key in ["sram_ue_parity", "sram_ue_secded", "dram_ue"]:
                val = ecc.get(key, "0")
                try:
                    if int(val) > 0:
                        label = "Volatile" if ecc_type == "ecc_vol" else "Aggregate"
                        issues_critical.append(
                            f"GPU {idx} ({gpu.get('bdf', '?')}) {label} uncorrectable ECC error: {key}={val}")
                except ValueError:
                    pass
    if xids:
        summary = defaultdict(int)
        for x in xids:
            summary[x["xid"]] += 1
        parts = [f"Xid {k} x{v}" for k, v in sorted(summary.items())]
        issues_warn.append(f"Found {len(xids)} Xid errors: {', '.join(parts)}")
    if nvrm_errors:
        issues_warn.append(f"Found {len(nvrm_errors)} NVRM error messages")
    if has_nvlink_data:
        if has_nvlink_inactive:
            issues_critical.append(f"NVLink has inactive links ({nvlink_inactive_count} links)")
        if has_nvlink_speed_mismatch:
            issues_warn.append("NVLink speed mismatch detected, some links may be running at reduced speed")
        if has_nvlink_err:
            issues_warn.append("NVLink has non-zero link error counts")
        if has_fec_high:
            issues_warn.append("NVLink FEC high-order errors (bin >= 3) have non-zero values, signal quality may need attention")
        if has_ber_concern:
            issues_warn.append("NVLink has links with abnormal Effective/Symbol Errors or BER")
    if has_imex_data:
        if has_imex_issue:
            if imex and imex.get("service_active") is False:
                issues_critical.append("nvidia-imex.service is not in active state")
            else:
                issues_warn.append("IMEX anomalies (node status not READY or service log warnings)")
        if has_imex_conn_issue:
            issues_warn.append("IMEX node connection anomaly or Domain State not UP")
        if _imex_xid_correlations:
            for xid_ev, xid_ts, imex_list in _imex_xid_correlations:
                for ie_num, ie_ts in imex_list:
                    issues_warn.append(
                        f"IMEX Event Group {ie_num} ({ie_ts}) <-> Xid Event Group {xid_ev} ({xid_ts})")
    if smi_pcie_issues:
        issues_warn.append("nvidia-smi reports PCIe link width not at maximum")
    if dmesg_highlights is None:
        dmesg_highlights = {"system_errors": [], "other_warnings": []}
    sys_hl = dmesg_highlights.get("system_errors", [])
    if sys_hl:
        issues_warn.append(f"Found {len(sys_hl)} system-level errors in dmesg (panic/oops/OOM/HW errors etc.), deep analysis needed")

    if not issues_critical and not issues_warn:
        r.append("- 🟢 **OK**: All checks passed")
    for issue in issues_critical:
        r.append(f"- 🔴 **Critical**: {issue}")
    for issue in issues_warn:
        r.append(f"- 🟡 **Warning**: {issue}")

    return "\n".join(r)


