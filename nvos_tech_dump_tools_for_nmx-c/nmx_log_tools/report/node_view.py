"""Renderer-neutral traversal that emits one ``NodeAnalysis`` worth of report.

Used by both ``report/html.py`` and ``report/markdown.py`` so the section
order, section presence, and event-group / FNM / GPU-mapping logic live in
one place instead of being maintained twice.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..analyze.pipeline import NodeAnalysis
from .aggregates import (
    FmEventRow,
    FmFnmPortLossRow,
    FnmPortLossReport,
    NvlsmFnmSpstRow,
    RackMappingGroup,
    SlotMappingGroup,
    SlotMappingRow,
)
from .renderer import Renderer


# Above this many distinct GPU GUIDs on the same Chassis Phy Slot, the slot
# header is highlighted to flag a potential physical-hardware swap.
SLOT_GUID_WARN_THRESHOLD = 4


FM_EVENT_TABLE_COLUMNS: List[str] = [
    "Time",
    "Level",
    "Category",
    "Node ID",
    "GPU GUID",
    "Chassis Phy Slot",
    "Compute Slot Idx",
    "Module ID",
    "GPU ID",
    "Detail",
]


SLOT_MAPPING_TABLE_COLUMNS: List[str] = [
    "GPU GUID",
    "Node ID",
    "Module ID",
    "First Seen",
    "Last Seen",
    "Observations",
]


def rows_have_nvl_fatal(rows: List[FmEventRow]) -> bool:
    """True if any FM event row carries the ``nvl_fatal`` category."""
    return any((r.category or "") == "nvl_fatal" for r in rows)


def _fm_event_row_cells(row: FmEventRow) -> List[str]:
    return [
        row.ts or "-",
        row.level or "-",
        row.category or "-",
        row.node_id or "-",
        row.gpu_guid or "-",
        row.chassis_phy_slot or "-",
        row.compute_slot_index or "-",
        row.module_id or "-",
        row.gpu_id or "-",
        row.detail or "-",
    ]


def _slot_mapping_row_cells(row: SlotMappingRow) -> List[str]:
    return [
        row.gpu_guid or "-",
        row.node_id or "-",
        row.module_id or "-",
        row.first_ts or "-",
        row.last_ts or "-",
        f"{row.count:,}",
    ]


def _slot_summary_text(group: SlotMappingGroup) -> str:
    n_guids = group.distinct_guid_count
    csi = group.dominant_compute_slot_index
    csi_part = f" / Compute Slot Idx {csi}" if csi else ""
    return (
        f"Chassis Phy Slot {group.chassis_phy_slot}{csi_part} — "
        f"{n_guids} distinct GPU GUID(s)"
    )


def _slot_is_anomalous(group: SlotMappingGroup) -> bool:
    return group.distinct_guid_count > SLOT_GUID_WARN_THRESHOLD


def _rack_is_anomalous(rack: RackMappingGroup) -> bool:
    return any(_slot_is_anomalous(sg) for sg in rack.slot_groups)


# -----------------------------------------------------------------------------
# Table renderers (shared by both backends via Renderer)
# -----------------------------------------------------------------------------


def _append_fm_event_table(
    r: Renderer,
    rows: List[FmEventRow],
    summary_label: str,
    *,
    omitted: int = 0,
) -> None:
    """Render FM events as a collapsed details block with a table inside."""
    if not rows and omitted <= 0:
        return
    r.open_details(r.i_text(summary_label), red=rows_have_nvl_fatal(rows))
    if rows:
        if omitted > 0:
            r.paragraph(r.i_em(f"+{omitted} more record(s) omitted (capacity cap)."), note=True)
        table_rows = [[r.i_text(c) for c in _fm_event_row_cells(row)] for row in rows]
        r.table(FM_EVENT_TABLE_COLUMNS, table_rows)
    else:
        r.paragraph(r.i_em(f"{omitted} record(s) omitted (capacity cap)."), note=True)
    r.close_details()


def _append_slot_block(r: Renderer, group: SlotMappingGroup) -> None:
    summary = _slot_summary_text(group)
    anomalous = _slot_is_anomalous(group)
    if anomalous:
        # "🚨" + bold-red summary. The previous HTML renderer used
        # ``<details open>`` to keep flagged slots expanded by default; the
        # previous Markdown renderer did the same. Match it here.
        summary_inner = "🚨 " + r.i_red(
            f"{summary} — suspect hardware change", bold=True
        )
        r.open_details(summary_inner, default_open=True)
    else:
        r.open_details(r.i_text(summary))
    if group.rows:
        rows = [[r.i_text(c) for c in _slot_mapping_row_cells(row)] for row in group.rows]
        r.table(SLOT_MAPPING_TABLE_COLUMNS, rows)
    else:
        r.empty_note("No mapping observations.")
    r.close_details()


def _append_gpu_mappings(r: Renderer, racks: List[RackMappingGroup]) -> None:
    if not racks:
        return
    r.heading(3, "GPU Node Mapping")
    flagged_slots = [
        sg for rk in racks for sg in rk.slot_groups if _slot_is_anomalous(sg)
    ]
    if flagged_slots:
        names = ", ".join(r.i_text(sg.chassis_phy_slot) for sg in flagged_slots)
        r.paragraph(
            r.i_red(
                f"🚨 {len(flagged_slots)} slot(s) with "
                f">{SLOT_GUID_WARN_THRESHOLD} distinct GPU GUIDs "
                f"(suspect hardware change): {names}",
                bold=True,
            )
        )
    for rack in racks:
        summary = f"rack GUID {rack.rack_guid}"
        if _rack_is_anomalous(rack):
            r.open_details(r.i_red(summary, bold=True))
        else:
            r.open_details(r.i_text(summary))
        for sg in rack.slot_groups:
            _append_slot_block(r, sg)
        r.close_details()


def _append_fm_raw_events(
    r: Renderer,
    events: List[Dict[str, Any]],
    *,
    heading: str,
    summary_label: str,
) -> None:
    """Dump a list of FM events as a raw text block inside a collapsed details."""
    if not events:
        return
    r.heading(4, heading)
    r.open_details(r.i_bold(summary_label))
    r.raw_pre([ev.get("raw_text") or ev.get("message") or "" for ev in events])
    r.close_details()


# -----------------------------------------------------------------------------
# FNM port loss section
# -----------------------------------------------------------------------------


_FNM_FM_LOSS_HEADERS = [
    "FM Time",
    "node GUID",
    "port num",
    "nvlsm log founded",
    "NVOS hostname",
    "Down Details",
    "Recovered Event Found",
    "Recovered Time",
    "related log line number",
]


_FNM_NVLSM_UNMATCHED_HEADERS = [
    "Time",
    "Switch GUID",
    "port",
    "port name",
    "NVOS hostname",
    "transition",
    "reason",
    "nearest FM",
    "nvlsm line",
]


_FNM_NVLSM_RECOVERY_HEADERS = [
    "Time",
    "Switch GUID",
    "port",
    "port name",
    "NVOS hostname",
    "transition",
    "related log line number",
]


def _fm_fnm_loss_row(r: Renderer, row: FmFnmPortLossRow) -> List[str]:
    return [
        r.i_text(row.ts or "-"),
        r.i_code(row.node_guid or "-"),
        r.i_text(row.port_num or "-"),
        r.i_text(row.in_nvlsm),
        r.i_text(row.nvos_hostname or "-"),
        r.i_text(row.nvlsm_transition or "-"),
        r.i_text(row.followed_by_init),
        r.i_text(row.init_ts or "-"),
        r.i_code(row.nvlsm_line or "-"),
    ]


def _nvlsm_fnm_unmatched_row(r: Renderer, row: NvlsmFnmSpstRow) -> List[str]:
    return [
        r.i_text(row.ts or "-"),
        r.i_code(row.switch_guid or "-"),
        r.i_text(row.port_num or "-"),
        r.i_text(row.port_name or "-"),
        r.i_text(row.nvos_hostname or "-"),
        r.i_text(row.transition or "-"),
        r.i_text(row.unmatched_reason or "-"),
        r.i_text(row.nearest_fm_ts or "-"),
        r.i_code(row.nvlsm_line or "-"),
    ]


def _nvlsm_fnm_recovery_row(r: Renderer, row: NvlsmFnmSpstRow) -> List[str]:
    return [
        r.i_text(row.ts or "-"),
        r.i_code(row.switch_guid or "-"),
        r.i_text(row.port_num or "-"),
        r.i_text(row.port_name or "-"),
        r.i_text(row.nvos_hostname or "-"),
        r.i_text(row.transition or "-"),
        r.i_code(row.nvlsm_line or "-"),
    ]


def _append_fm_fnm_port_loss(r: Renderer, report: FnmPortLossReport) -> None:
    r.heading(4, "FNM port loss")
    rows = report.fm_rows
    if not rows and report.nvlsm_fnm_spst_total == 0:
        r.empty_note(
            "No FNM port loss events in fabricmanager logs and no FNM nvlsm osm_spst lines."
        )
        return

    if rows:
        r.open_details(
            r.i_bold("Fabricmanager logs which report FNM port loss events")
        )
        r.table(
            _FNM_FM_LOSS_HEADERS,
            [_fm_fnm_loss_row(r, row) for row in rows],
        )
        r.close_details()

    if report.nvlsm_unmatched:
        r.open_details(
            r.i_bold("Unmatched nvlsm loss — no FM FNM port loss in window")
        )
        r.table(
            _FNM_NVLSM_UNMATCHED_HEADERS,
            [_nvlsm_fnm_unmatched_row(r, row) for row in report.nvlsm_unmatched],
        )
        r.close_details()

    if report.nvlsm_recovery_unmatched:
        r.open_details(
            r.i_bold("nvlsm logs which can not linked to FM FNM port loss event")
        )
        r.table(
            _FNM_NVLSM_RECOVERY_HEADERS,
            [_nvlsm_fnm_recovery_row(r, row) for row in report.nvlsm_recovery_unmatched],
        )
        r.close_details()


# -----------------------------------------------------------------------------
# Event groups
# -----------------------------------------------------------------------------


def _render_event_group(r: Renderer, idx: int, cl: Dict[str, Any]) -> None:
    fm_rows = cl.get("fm_event_rows") or []
    summary_text = f"Event group {idx}: {cl['start']} – {cl['end']}"
    r.open_details(r.i_text(summary_text), red=rows_have_nvl_fatal(fm_rows))
    # Unified summary: surfaces both the switch/port count (HTML's choice) and
    # the unique-transition-pattern count (Markdown's choice). Either alone
    # under-described the event group.
    r.paragraph(
        r.i_text(
            f"DOWN={cl['down']}, ACTIVE={cl['active']}, "
            f"switches={cl['switches']}, ports={cl['ports']}, "
            f"{cl['unique_transition_patterns']} unique transition pattern(s)"
        )
    )
    port_rows = cl.get("port_rows") or cl.get("transitions") or []
    if port_rows:
        headers = cl.get("transition_column_headers") or [
            "ACTIVE→DOWN",
            "DOWN→INIT",
        ]
        table_hdr = ["Port", "Switches (hostname / GUID)", *headers]
        if cl.get("has_other_transitions"):
            table_hdr.append("Other transitions")
        table_rows: List[List[str]] = []
        for row in port_rows:
            cells = row.get("transition_cells") or {}
            line = [
                r.i_text(row.get("port") or "-"),
                r.i_text(row.get("switches") or "-"),
                *[r.i_text(cells.get(h) or "-") for h in headers],
            ]
            if cl.get("has_other_transitions"):
                line.append(r.i_text(row.get("other_transitions") or "-"))
            table_rows.append(line)
        r.table(table_hdr, table_rows)
    _append_fm_event_table(
        r,
        fm_rows,
        "Fabric Manager log (same time window)",
        omitted=int(cl.get("fm_logs_omitted") or 0),
    )
    r.close_details()


def _render_event_group_section(r: Renderer, ctx: Dict[str, Any]) -> None:
    event_groups = ctx.get("nvlsm_event_groups") or []
    if not event_groups:
        return

    r.heading(4, "Port state event groups")
    event_n = int(ctx.get("port_event_group_non_fnm_event_count") or 0)
    group_n = int(ctx.get("port_event_group_count") or len(event_groups))
    r.paragraph(
        f"(non-FNM port names only): {r.i_bold(f'{event_n:,}')} events in "
        f"{r.i_bold(str(group_n))} event group(s). "
        f"Event groups which have Xid event have been already marked with red "
        f"color ; {r.i_code('Slot Index')} / {r.i_code('Module ID')} are "
        f"resolved via the {r.i_bold('GPU Node Mapping')} section.",
        note=True,
    )

    red_pairs: List[tuple] = []
    normal_pairs: List[tuple] = []
    for i, cl in enumerate(event_groups, 1):
        fm_rows = cl.get("fm_event_rows") or []
        (red_pairs if rows_have_nvl_fatal(fm_rows) else normal_pairs).append((i, cl))

    if red_pairs:
        r.open_details(
            r.i_red("Event groups with Xid (nvl_fatal) events", bold=True)
        )
        for idx, cl in red_pairs:
            _render_event_group(r, idx, cl)
        r.close_details()

    if normal_pairs:
        r.open_details(r.i_text("Event groups without Xid events"))
        for idx, cl in normal_pairs:
            _render_event_group(r, idx, cl)
        r.close_details()


def _render_fm_log_before_after_event_groups(
    r: Renderer, ctx: Dict[str, Any]
) -> None:
    pre_cutoff = ctx.get("fm_log_pre_nvlsm_cutoff_ts") or ""
    if pre_cutoff:
        pre_label = (
            f"Fabric Manager log before earliest NVLSM event "
            f"(records before {pre_cutoff} have been omitted)"
        )
    else:
        pre_label = "Fabric Manager log before earliest NVLSM event"
    outside_label = (
        "Fabric Manager log after earliest NVLSM event (outside event group windows)"
    )
    _append_fm_event_table(r, ctx.get("fm_log_pre_nvlsm") or [], pre_label)
    _append_fm_event_table(
        r,
        ctx.get("fm_log_outside_after_nvlsm") or [],
        outside_label,
        omitted=int(ctx.get("fm_log_outside_after_nvlsm_omitted") or 0),
    )
    if ctx.get("fm_log_no_timestamp"):
        r.paragraph(
            r.i_em(
                f"Note: {ctx['fm_log_no_timestamp']} FM line(s) had no parseable "
                f"timestamp and are included only in the unassigned bucket when shown."
            ),
            note=True,
        )


# -----------------------------------------------------------------------------
# Top-level
# -----------------------------------------------------------------------------


def render_node(r: Renderer, node: NodeAnalysis, ctx: Dict[str, Any]) -> None:
    """Append a full ``NodeAnalysis`` section to ``r``."""
    r.heading(2, ctx.get("node_title") or f"Node: {ctx['label']}")
    summary_bullets: List[str] = []
    earliest = ctx.get("earliest_nvlsm_ts") or ""
    if earliest:
        summary_bullets.append(
            f"Earliest NVLSM event timestamp: {r.i_bold(earliest)}"
        )
    f = node.nvlsm_forensics
    if f:
        summary_bullets.append(
            f"NVLSM: files: {r.i_bold(str(f.log_files))} | "
            f"lines scanned: {r.i_bold(f'{f.log_lines:,}')} | "
            f"state changes (INIT/Unlink): {r.i_bold(f'{f.state_changes:,}')}"
        )
    summary_bullets.append(
        f"Fabric Manager: events: {r.i_bold(f'{len(node.fm_events):,}')} | "
        f"files: {r.i_bold(str(node.fm_files_parsed))}"
    )
    r.bullets(summary_bullets)

    r.heading(3, "NVLSM & FM log checks for Compute Trays")
    h = node.nvlsm_health
    if h is None:
        r.empty_note("No nvlsm.log files found.")
    else:
        # Health summary table
        topo = h.invalid_topology
        utf8 = h.invalid_utf8
        topo_cnt = r.i_red(str(topo.count), bold=True) if topo.count else r.i_text(str(topo.count))
        utf8_cnt = r.i_red(str(utf8.count), bold=True) if utf8.count else r.i_text(str(utf8.count))
        r.table(
            ["Check", "Count", "Earliest", "Latest"],
            [
                [
                    r.i_text("Invalid topology"),
                    topo_cnt,
                    r.i_text(str(topo.earliest or "-")),
                    r.i_text(str(topo.latest or "-")),
                ],
                [
                    r.i_text("Invalid UTF-8"),
                    utf8_cnt,
                    r.i_text(str(utf8.earliest or "-")),
                    r.i_text(str(utf8.latest or "-")),
                ],
            ],
        )
        if utf8.fields:
            r.paragraph(
                f"{r.i_bold(f'UTF-8 fields ({len(utf8.fields)}):')} "
                f"{r.i_text(', '.join(utf8.fields))}"
            )
        _render_event_group_section(r, ctx)
        if ctx.get("nvlsm_event_groups"):
            _render_fm_log_before_after_event_groups(r, ctx)

    _append_gpu_mappings(r, ctx.get("gpu_mapping_racks") or [])

    r.heading(3, "Other FabricManager Log Highlights")

    fnm = ctx.get("fm_fnm_port_loss")
    if fnm is None:
        fnm = FnmPortLossReport()
    _append_fm_fnm_port_loss(r, fnm)

    _append_fm_raw_events(
        r,
        node.fm_switch_info_failures,
        heading="Failed to get switch info",
        summary_label="Switch info failures",
    )
    _append_fm_raw_events(
        r,
        node.fm_partition_errors,
        heading="Partition unexpected error state",
        summary_label="Partition error events",
    )
    _append_fm_raw_events(
        r,
        node.fm_multicast_team_limits,
        heading="Multicast team limit reached",
        summary_label="Multicast team limit events",
    )

    if node.fm_lifecycle:
        r.heading(4, "FM lifecycle")
        r.open_details(r.i_bold("FM lifecycle events"))
        rows: List[List[str]] = []
        for ev in node.fm_lifecycle:
            fields = ev.get("fields") or {}
            et = fields.get("event_type", "?")
            ver = fields.get("version", "")
            type_cell = r.i_bold(et) + (f" v{ver}" if ver else "")
            rows.append([
                r.i_text(ev.get("ts", "-")),
                type_cell,
                r.i_text((ev.get("message") or "")[:120]),
            ])
        r.table(["Time", "Type", "Message"], rows)
        r.close_details()
