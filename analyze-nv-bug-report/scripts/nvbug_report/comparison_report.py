"""Multi-node Xid / IMEX comparison markdown report."""

import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta

from nvbug_report.html_renderer import write_sidecar_html
from nvbug_report.imex import (
    _IMEX_DISCONNECT_RE,
    _find_related_imex_events,
    _format_imex_related_line,
    _group_imex_events,
    _parse_imex_log_ts,
)
from nvbug_report.report import (
    NVLINK_XID_NUMS,
    _collapse_burst_derivatives,
    _xid_subtype_parts,
    _xid_subtype_tag,
)
from nvbug_report.syslog_ts import _parse_syslog_ts
from nvbug_report.timing import (
    _analyze_stat_line,
    _analyze_timing_enabled,
    _phase_end,
    _phase_start,
)


def _imex_timeline_row_sort_key(evt):
    """Sort order for §2 table rows: Disconnected Node, then Time, then Reporting Host."""
    m = _IMEX_DISCONNECT_RE.match(evt["message"])
    if m:
        disc_node = f"node {m.group(1)} ({m.group(2).split('.')[0]})"
    else:
        disc_node = "N/A"
    dt = _parse_imex_log_ts(evt["timestamp"])
    t_key = dt if dt is not None else datetime.max
    return (disc_node, t_key, evt.get("hostname", ""))


def generate_comparison_report(all_results, output_dir):
    """Generate a cross-file Xid comparison report from multiple analysis results."""
    t_cmp = _phase_start()
    if _analyze_timing_enabled():
        print(
            f"[analyze-timing] cross-node-report | ----- begin comparison "
            f"(nodes={len(all_results)}) -----",
            file=sys.stderr,
        )
    r = []
    r.append("# Multi-Node Xid Comparison Report\n")

    # -- 1. File overview --
    r.append("## 1. File Overview\n")
    r.append("| # | File | Hostname | System SN | Chassis SN | Slot# | Tray# | Boot Time | Msg Start | Collect Date |")
    r.append("|---|------|----------|-----------|------------|-------|-------|-----------|-----------|--------------|")
    for i, res in enumerate(all_results):
        si = res["sys_info"]
        hostname = si.get("hostname", "N/A")
        system_sn = si.get("system_sn", "N/A")
        chassis_sn = si.get("chassis_sn", "N/A")
        slot_number = si.get("slot_number", "N/A")
        tray_index = si.get("tray_index", "N/A")
        boot_time = si.get("boot_time", "N/A")
        msg_start = si.get("message_start_time", "N/A")
        date_short = si.get("date_short", "N/A")
        r.append(f"| {i + 1} | `{res['basename']}` | {hostname} | {system_sn} | {chassis_sn} | {slot_number} | {tray_index} | {boot_time} | {msg_start} | {date_short} |")

    # -- 2. IMEX Node Disconnect Timeline --
    all_imex_entries = []
    for res in all_results:
        hostname = res["sys_info"].get("hostname", res["basename"])
        imex_data = res.get("imex") or {}
        for entry in imex_data.get("imex_log_entries", []):
            all_imex_entries.append({
                "hostname": hostname,
                "timestamp": entry["timestamp"],
                "level": entry["level"],
                "message": entry["message"],
                "raw_line": entry.get("raw_line", ""),
            })

    imex_bursts = []
    r.append("\n## 2. IMEX Node Disconnect Timeline\n")
    if all_imex_entries:
        imex_bursts = _group_imex_events(all_imex_entries, gap_seconds=60)

        for idx_b, burst in enumerate(imex_bursts):
            dts = [_parse_imex_log_ts(e["timestamp"]) for e in burst]
            valid_dts = [d for d in dts if d]
            if valid_dts:
                t_start = min(valid_dts).strftime("%Y-%m-%d %H:%M:%S")
                t_end = max(valid_dts).strftime("%H:%M:%S")
                if min(valid_dts).date() != max(valid_dts).date():
                    t_end = max(valid_dts).strftime("%Y-%m-%d %H:%M:%S")
                ts_label = t_start if t_start.endswith(t_end) else f"{t_start} ~ {t_end}"
            else:
                ts_label = "unknown"

            hosts_in_burst = sorted(set(e["hostname"] for e in burst))
            host_label = ", ".join(hosts_in_burst)
            summary = f"Event {idx_b + 1}: {ts_label} [{host_label}] ({len(burst)} entries)"
            r.append("<details>")
            r.append(f"<summary>{summary}</summary>")
            r.append("")
            r.append("| Time | Reporting Host | Disconnected Node |")
            r.append("|------|----------------|-------------------|")
            for evt in sorted(burst, key=_imex_timeline_row_sort_key):
                evt_dt = _parse_imex_log_ts(evt["timestamp"])
                display_ts = evt_dt.strftime("%Y-%m-%d %H:%M:%S") if evt_dt else evt["timestamp"]
                m = _IMEX_DISCONNECT_RE.match(evt["message"])
                if m:
                    disc_node = f"node {m.group(1)} ({m.group(2).split('.')[0]})"
                else:
                    disc_node = "N/A"
                r.append(f"| {display_ts} | {evt['hostname']} | {disc_node} |")
            r.append("")
            r.append("</details>")
            r.append("")
    else:
        r.append("No IMEX Node Disconnect events found on any node.\n")

    all_imex_events_grouped = imex_bursts

    # -- 3. Xid comparison matrix --
    # Per-Xid columns count primary events only; derivative ("caused by
    # previous Xid N") entries -- typically Xid 45 channel cleanup -- are
    # tallied in a separate "Deriv" column to keep the matrix readable.
    all_xid_nums = set()
    per_host_xids = {}        # hostname -> {xid: primary_count}
    per_host_deriv = {}       # hostname -> derivative_total
    any_deriv = False
    for res in all_results:
        hostname = res["sys_info"].get("hostname", res["basename"])
        counts = defaultdict(int)
        deriv_total = 0
        for x in res["xids"]:
            if x.get("is_derivative"):
                deriv_total += 1
            else:
                counts[x["xid"]] += 1
                all_xid_nums.add(x["xid"])
        per_host_xids[hostname] = counts
        per_host_deriv[hostname] = deriv_total
        if deriv_total:
            any_deriv = True

    if all_xid_nums:
        sorted_xids = sorted(all_xid_nums)
        r.append("\n## 3. Xid Comparison Matrix\n")
        header_cols = [f"Xid {x}" for x in sorted_xids]
        if any_deriv:
            header_cols += ["Primary", "Deriv", "Total"]
        else:
            header_cols += ["Total"]
        header = "| Hostname | " + " | ".join(header_cols) + " |"
        sep = "|--------|" + "|".join("-------" for _ in header_cols) + "|"
        r.append("<!-- nvbug:table-class=xid-matrix wide-matrix numeric-matrix -->")
        r.append(header)
        r.append(sep)
        for hostname, counts in per_host_xids.items():
            cells = [str(counts.get(x, 0)) for x in sorted_xids]
            primary = sum(counts.values())
            deriv = per_host_deriv.get(hostname, 0)
            if any_deriv:
                cells += [str(primary), str(deriv), str(primary + deriv)]
            else:
                cells += [str(primary)]
            r.append(f"| {hostname} | " + " | ".join(cells) + " |")
        if any_deriv:
            r.append(
                "\n*Per-Xid columns count primary events only. "
                "\"Deriv\" column totals all \"caused by previous Xid\" "
                "lines (typically Xid 45 channel cleanup after a primary "
                "NVLink fault); these share the same root cause as their "
                "primary trigger and are not decoded separately.*"
            )
    else:
        r.append("\n## 3. Xid Comparison Matrix\n")
        r.append("No Xid errors found on any node.")

    # -- 3.1 NVLink Sub-type Breakdown --
    # Per-host, per-GPU breakdown of Xid 144-150 lines by Category + Severity.
    # Complements the section 3 matrix (which is per-Xid-number aggregate) by
    # surfacing the sub-type axis as a separate long-format table, so rare
    # Fatal / RLW_RXPIPE / NETIR_LINK_DOWN variants are individually
    # countable across hosts without bloating the wide matrix. Wrapped in a
    # collapsed <details> block; the entire subsection is omitted when no
    # NVLink Xid 144-150 is present anywhere in the batch.
    nvlink_clusters = defaultdict(int)  # (host, bdf, xid, cat, sev) -> count
    for res in all_results:
        hostname = res["sys_info"].get("hostname", res["basename"])
        for x in res["xids"]:
            if x["xid"] not in NVLINK_XID_NUMS:
                continue
            cat, sev = _xid_subtype_parts(x.get("raw_line", ""))
            nvlink_clusters[(hostname, x["bdf"], x["xid"], cat, sev)] += 1

    if nvlink_clusters:
        fatal_total = sum(
            cnt for (host, _bdf, _xn, _c, sev), cnt in nvlink_clusters.items()
            if sev == "Fatal"
        )
        host_count = len({k[0] for k in nvlink_clusters.keys()})
        summary_31 = (
            f"NVLink Sub-type Breakdown ({host_count} host(s), "
            f"{len(nvlink_clusters)} clusters, {fatal_total} Fatal entries)"
        )

        def _sub_sort_key(item):
            (host, bdf, xid_n, cat, sev), _cnt = item
            sev_prio = {"Fatal": 0, "Nonfatal": 1}.get(sev, 2)
            return (host, bdf, xid_n, sev_prio, cat)

        r.append("\n## 3.1 NVLink Sub-type Breakdown\n")
        r.append("<details>")
        r.append(f"<summary>{summary_31}</summary>")
        r.append("")
        r.append("| Hostname | GPU BDF | Xid | Category | Severity | Count |")
        r.append("|----------|---------|-----|----------|----------|-------|")
        for (host, bdf, xid_n, cat, sev), cnt in sorted(
            nvlink_clusters.items(), key=_sub_sort_key
        ):
            r.append(
                f"| {host} | {bdf} | {xid_n} | {cat} | {sev} | {cnt} |"
            )
        r.append("")
        r.append(
            "*Long-format breakdown of NVLink-class Xid 144-150 lines across "
            "nodes. Same (host, BDF, Xid) cluster splits into multiple rows "
            "when different Category/Severity sub-types are present (e.g. a "
            "GPU may show separate rows for RLW_SRC_TRACK Fatal vs Nonfatal "
            "vs RLW_RXPIPE Nonfatal). Hosts with no NVLink Xid in range are "
            "omitted. Fatal rows sort first within each (host, BDF, Xid).*"
        )
        r.append("")
        r.append("</details>")

    # -- 4. Unified Xid timeline --
    r.append("\n## 4. Xid Unified Timeline\n")
    all_xid_events = []
    for res in all_results:
        hostname = res["sys_info"].get("hostname", res["basename"])
        for x in res["xids"]:
            all_xid_events.append({
                "hostname": hostname,
                "timestamp": x["timestamp"],
                "bdf": x["bdf"],
                "xid": x["xid"],
                "raw_line": x.get("raw_line", ""),
                # Propagate derivative flags so per-burst collapse works the
                # same as in the single-node 7.3 renderer.
                "is_derivative": x.get("is_derivative", False),
                "caused_by": x.get("caused_by"),
            })

    imex_xid_correlations = []
    if all_xid_events:
        # Determine ref_year from system info date fields
        ref_year = 2026
        for res in all_results:
            date_str = res.get("sys_info", {}).get("date", "")
            m = re.search(r"\b(20\d{2})\b", date_str)
            if m:
                ref_year = int(m.group(1))
                break

        def _format_ts(ts_str):
            """Format a timestamp string to a readable datetime."""
            dt = _parse_syslog_ts(ts_str, ref_year)
            if dt:
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            return ts_str

        def sort_key(evt):
            dt = _parse_syslog_ts(evt["timestamp"], ref_year)
            return dt if dt else datetime.min

        all_xid_events.sort(key=sort_key)

        bursts = []
        current = []
        last_dt = None
        for evt in all_xid_events:
            dt = _parse_syslog_ts(evt["timestamp"], ref_year)
            if dt and last_dt and (dt - last_dt) > timedelta(seconds=60):
                if current:
                    bursts.append(current)
                current = []
            current.append(evt)
            if dt:
                last_dt = dt
        if current:
            bursts.append(current)

        for idx_b, burst in enumerate(bursts):
            ts_first = _format_ts(burst[0]["timestamp"])
            ts_last = _format_ts(burst[-1]["timestamp"])
            hosts_in_burst = sorted(set(e["hostname"] for e in burst))
            host_label = ", ".join(hosts_in_burst)
            _b_primary = sum(1 for e in burst if not e.get("is_derivative"))
            _b_deriv = len(burst) - _b_primary
            if _b_deriv:
                count_label = f"{len(burst)} entries; {_b_primary} primary + {_b_deriv} derivative"
            else:
                count_label = f"{len(burst)} entries"

            if ts_first == ts_last:
                summary = f"Event {idx_b + 1}: {ts_first} [{host_label}] ({count_label})"
            else:
                summary = f"Event {idx_b + 1}: {ts_first} ~ {ts_last} [{host_label}] ({count_label})"
            r.append(f"<details>")
            r.append(f"<summary>{summary}</summary>")
            r.append("")

            if all_imex_events_grouped:
                # Full burst feeds IMEX correlation (time-range based); we
                # only collapse derivatives in the rendered table.
                _related = _find_related_imex_events(all_imex_events_grouped, burst, ref_year=ref_year, window_seconds=60)
                if _related:
                    r.append(_format_imex_related_line(_related, show_reporting_host=True))
                    r.append("")
                    imex_labels = []
                    for ie_idx, ie_grp in _related:
                        ie_dts = [_parse_imex_log_ts(e["timestamp"]) for e in ie_grp]
                        ie_valid = [d for d in ie_dts if d]
                        ie_ts = min(ie_valid).strftime("%Y-%m-%d %H:%M:%S") if ie_valid else "N/A"
                        imex_labels.append((ie_idx + 1, ie_ts))
                    xid_ts_label = ts_first if ts_first == ts_last else f"{ts_first} ~ {ts_last}"
                    imex_xid_correlations.append((idx_b + 1, xid_ts_label, imex_labels))

            display_burst, derivative_notes = _collapse_burst_derivatives(burst)

            r.append("| Time | Hostname | GPU BDF | Xid | Raw Log |")
            r.append("|------|----------|---------|-----|---------|")
            # Bucket by (hostname, xid_num, subtype) so a single rare Fatal
            # variant doesn't get hidden under hundreds of common Nonfatal
            # siblings of the same Xid number (and vice versa).
            xid_type_count = defaultdict(int)
            shown_per_type = defaultdict(int)
            MAX_PER_TYPE = 5
            for evt in display_burst:
                subtype = _xid_subtype_tag(evt.get("raw_line", ""))
                key = (evt["hostname"], evt["xid"], subtype)
                xid_type_count[key] += 1
                if shown_per_type[key] < MAX_PER_TYPE:
                    raw_short = evt.get("raw_line", "")
                    display_ts = _format_ts(evt["timestamp"])
                    r.append(f"| {display_ts} | {evt['hostname']} | {evt['bdf']} | {evt['xid']} | `{raw_short}` |")
                    shown_per_type[key] += 1
                    note = derivative_notes.get(id(evt))
                    if note:
                        r.append(f"| | {evt['hostname']} | | Xid {evt['xid']} | *{note}* |")

            for key, total in xid_type_count.items():
                if total > MAX_PER_TYPE:
                    hostname_v, xid_num, subtype = key
                    if subtype:
                        base, _, sev = subtype.rpartition("_")
                        label = f"Xid {xid_num} {base} {sev.capitalize()}".rstrip()
                    else:
                        label = f"Xid {xid_num}"
                    r.append(f"| | {hostname_v} | | {label} | *...{total - MAX_PER_TYPE} more omitted...* |")
            r.append("")
            r.append("</details>")
            r.append("")
    else:
        r.append("No Xid errors found on any node.\n")

    # -- 5. Xid decode summary --
    r.append("## 5. Xid Decode Summary\n")
    all_decoded = []
    for res in all_results:
        all_decoded.extend(res.get("xid_decoded", []))

    if all_decoded:
        seen = set()
        unique = []
        for entry in all_decoded:
            sig = (entry.get("decoded_xid", ""), entry.get("mnemonic", ""),
                   entry.get("resolution", ""))
            if sig not in seen:
                seen.add(sig)
                unique.append(entry)

        r.append("| Decoded XID | Mnemonic | Severity | Resolution | Investigation |")
        r.append("|-------------|----------|----------|------------|---------------|")
        for entry in unique:
            r.append(f"| {entry.get('decoded_xid', 'N/A')} "
                     f"| {entry.get('mnemonic', 'N/A')} "
                     f"| {entry.get('job_severity', 'N/A')} "
                     f"| {entry.get('resolution', 'N/A')} "
                     f"| {entry.get('investigatory', 'N/A')} |")
    else:
        r.append("No Xid decode data available.")

    # -- 6. Cross-node summary --
    r.append("\n## 6. Cross-Node Comparison Summary\n")
    if all_xid_nums and len(per_host_xids) > 1:
        hosts = list(per_host_xids.keys())
        common = [x for x in sorted_xids if all(per_host_xids[h].get(x, 0) > 0 for h in hosts)]
        per_node_only = {}
        for h in hosts:
            unique_to_h = [x for x in sorted_xids
                           if per_host_xids[h].get(x, 0) > 0
                           and all(per_host_xids[other].get(x, 0) == 0 for other in hosts if other != h)]
            if unique_to_h:
                per_node_only[h] = unique_to_h

        if common:
            r.append(f"- **Xid common to all nodes**: {', '.join(f'Xid {x}' for x in common)}")
        else:
            r.append("- **Xid common to all nodes**: None")

        if per_node_only:
            for h, xlist in per_node_only.items():
                r.append(f"- **Xid unique to {h}**: {', '.join(f'Xid {x}' for x in xlist)}")
        else:
            r.append("- No node-specific Xid types")

        no_xid_hosts = [h for h in hosts if sum(per_host_xids[h].values()) == 0]
        if no_xid_hosts:
            r.append(f"- **Nodes with no Xid errors**: {', '.join(no_xid_hosts)}")
    elif not all_xid_nums:
        r.append("No Xid errors found on any node.")
    else:
        r.append("Only one node, cross-node comparison not applicable.")

    if imex_xid_correlations:
        r.append("- **IMEX-Xid Event Correlation**:")
        for xid_ev, xid_ts, imex_list in imex_xid_correlations:
            for ie_num, ie_ts in imex_list:
                r.append(
                    f"  - IMEX Event {ie_num} ({ie_ts}) <-> Xid Event {xid_ev} ({xid_ts})")

    report_text = "\n".join(r)
    _analyze_stat_line(
        "cross-node-report.md",
        comparison_report_chars=len(report_text),
        nodes=len(all_results),
    )
    report_path = os.path.join(output_dir, "cross-node-report.md")
    tw = _phase_start()
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    _phase_end("cross-node-report.md", "write_cross_node_md", tw)
    _phase_end("cross-node-report.md", "generate_comparison_report_total", t_cmp)
    if _analyze_timing_enabled():
        print(
            "[analyze-timing] cross-node-report | ----- end comparison -----",
            file=sys.stderr,
        )
    print(f"\nComparison report saved to: {report_path}", file=sys.stderr)

    write_sidecar_html(
        report_path, report_text, title="cross-node-report", kind="cross_node"
    )

    return report_path

