"""Aggregate repetitive log patterns for concise report sections."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..analyze.pipeline import NodeAnalysis
from ..event_grouping.adaptive import (
    parse_ts,
    port_events_for_event_group_report,
    split_port_lifecycle_episodes,
)


@dataclass
class CountRangeRow:
    key: str
    count: int
    first_ts: str = ""
    last_ts: str = ""
    extra: str = ""


@dataclass
class FmFnmPortLossRow:
    """One FM node-manager FNM port loss line, enriched from nvlsm osm_spst when matched."""

    ts: str
    node_guid: str
    port_num: str
    in_nvlsm: str = "N"
    nvos_hostname: str = ""
    nvlsm_transition: str = ""
    followed_by_init: str = "N"
    init_ts: str = ""
    nvlsm_line: str = ""


@dataclass
class NvlsmFnmSpstRow:
    """One nvlsm osm_spst line on an FNM port (FNM in port name / configured FNM ports)."""

    ts: str
    switch_guid: str
    port_num: str
    port_name: str
    nvos_hostname: str
    transition: str
    matched_fm: str = "N"
    nearest_fm_ts: str = ""
    unmatched_reason: str = ""
    nvlsm_line: str = ""


@dataclass
class NvlsmFnmSpstIndexes:
    """FNM osm_spst split: *→DOWN (loss, pairs with FM) vs DOWN→INIT (recovery only)."""

    loss: Dict[Tuple[str, str], List[Any]] = field(default_factory=dict)
    recovery: Dict[Tuple[str, str], List[Any]] = field(default_factory=dict)
    loss_count: int = 0
    recovery_count: int = 0


@dataclass
class FnmPortLossReport:
    """FM-primary FNM port loss table plus explicit unmatched FM / nvlsm lists."""

    fm_rows: List[FmFnmPortLossRow] = field(default_factory=list)
    nvlsm_fnm_spst_total: int = 0
    nvlsm_recovery_total: int = 0
    nvlsm_matched_fm_count: int = 0
    nvlsm_unmatched: List[NvlsmFnmSpstRow] = field(default_factory=list)
    nvlsm_recovery_unmatched: List[NvlsmFnmSpstRow] = field(default_factory=list)
    fm_unmatched: List[FmFnmPortLossRow] = field(default_factory=list)


def _ts_sort_key(ts: str) -> datetime:
    """Sort key for FM timestamps; unparseable values sort last."""
    dt = parse_ts(ts or "")
    return dt if dt is not None else datetime.max


def _ts_min_max(ts_list: List[str]) -> Tuple[str, str]:
    valid = [t for t in ts_list if t]
    if not valid:
        return "", ""
    valid.sort()
    return valid[0], valid[-1]


# Preferred column order for port-event-group transition tables
_NVLSM_EVENT_GROUP_TRANSITION_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("ACTIVE", "DOWN"),
    ("DOWN", "INIT"),
)


def _transition_at_cell(old: str, new: str, first: str, last: str) -> str:
    """e.g. ACTIVE→DOWN (at Apr 08 04:02:30 - Apr 08 04:02:31)."""
    if not first:
        return ""
    label = f"{old}→{new}"
    if not last or first == last:
        return f"{label} (at {first})"
    return f"{label} (at {first} - {last})"


def _cells_for_transition_events(evs: List[Any], old: str, new: str) -> str:
    stamps = [getattr(e, "ts", "") for e in evs if getattr(e, "ts", "")]
    first, last = _ts_min_max(stamps)
    return _transition_at_cell(old, new, first, last)


def _norm_switch_guid(guid: str) -> str:
    g = (guid or "").strip()
    if not g:
        return ""
    return g if g.lower().startswith("0x") else f"0x{g}"


def _format_event_group_switches_column(events: List[Any]) -> str:
    """
    One NVOS hostname per group; multiple Switch GUIDs (chips) as host(g1/g2).
    Example: nvos-3cf4c0(0x6433aa03003cf4c0/0x6433aa03003cf4e0), nvos-3d0240(0x...)
    """
    by_host: Dict[str, set] = defaultdict(set)
    for ev in events:
        host = (getattr(ev, "switch_name", "") or "").strip() or "?"
        guid = _norm_switch_guid(getattr(ev, "switch_guid", "") or "")
        if guid:
            by_host[host].add(guid)
        elif host not in by_host:
            by_host[host] = set()
    parts: List[str] = []
    for host in sorted(by_host.keys()):
        guids = sorted(by_host[host])
        if guids:
            parts.append(f"{host}({'/'.join(guids)})")
        else:
            parts.append(host)
    return ", ".join(parts)


def aggregate_nvlsm_event_group_details(
    event_groups: List[Dict],
    *,
    last_n: Optional[int] = None,
    top_transitions_per_event_group: Optional[int] = None,
    lifecycle_pair_max_seconds: int = 120,
) -> List[Dict[str, Any]]:
    """Per event group: one row per port lifecycle episode (paired ACTIVE→DOWN / DOWN→INIT)."""
    selected = event_groups if last_n is None else event_groups[-last_n:]
    col_headers = [f"{o}→{n}" for o, n in _NVLSM_EVENT_GROUP_TRANSITION_COLUMNS]
    out: List[Dict[str, Any]] = []
    for cl in selected:
        events = cl.get("events") or []
        by_port: Dict[Tuple[int, str], List[Any]] = defaultdict(list)
        for ev in events:
            port_key = (
                int(getattr(ev, "port", 0) or 0),
                getattr(ev, "port_name", "") or "",
            )
            by_port[port_key].append(ev)

        port_rows: List[Dict[str, Any]] = []
        pattern_keys: set = set()
        port_items = sorted(by_port.items(), key=lambda item: (item[0][0], item[0][1]))
        for (port, _pname), port_evs in port_items:
            for ep in split_port_lifecycle_episodes(
                port_evs, lifecycle_pair_max_seconds=lifecycle_pair_max_seconds,
            ):
                ep_events = ep["active_down"] + ep["down_init"] + ep["other"]
                if ep["active_down"]:
                    pattern_keys.add((port, "ACTIVE", "DOWN"))
                if ep["down_init"]:
                    pattern_keys.add((port, "DOWN", "INIT"))
                for ev in ep["other"]:
                    pattern_keys.add((
                        port,
                        getattr(ev, "old_state", "") or "",
                        getattr(ev, "new_state", "") or "",
                    ))

                cells = {
                    "ACTIVE→DOWN": _cells_for_transition_events(
                        ep["active_down"], "ACTIVE", "DOWN",
                    ),
                    "DOWN→INIT": _cells_for_transition_events(
                        ep["down_init"], "DOWN", "INIT",
                    ),
                }
                other_parts = [
                    _cells_for_transition_events(
                        [ev],
                        getattr(ev, "old_state", "") or "",
                        getattr(ev, "new_state", "") or "",
                    )
                    for ev in ep["other"]
                    if getattr(ev, "ts", "")
                ]

                port_rows.append({
                    "port": str(port),
                    "switches": _format_event_group_switches_column(ep_events),
                    "transition_cells": {h: cells.get(h, "") for h in col_headers},
                    "other_transitions": "; ".join(other_parts),
                })

        if top_transitions_per_event_group is not None:
            port_rows = port_rows[:top_transitions_per_event_group]

        # ACTIVE→DOWN-only ts window, used internally by FM-event attachment so
        # log lines aren't mis-bucketed into an event group's tail (e.g. DOWN→INIT
        # recovery period). Not surfaced in the rendered report.
        # Use the event's native .datetime attribute -- the .ts string is the
        # NVLSM display format ("Apr 08 04:01:48") which parse_ts can't read.
        active_down_dts: List[datetime] = []
        for ev in events:
            if (getattr(ev, "old_state", "") or "") == "ACTIVE" and (
                getattr(ev, "new_state", "") or ""
            ) == "DOWN":
                dt = getattr(ev, "datetime", None)
                if dt is not None:
                    active_down_dts.append(dt)
        if active_down_dts:
            active_down_window: Tuple[Optional[datetime], Optional[datetime]] = (
                min(active_down_dts),
                max(active_down_dts),
            )
        else:
            active_down_window = (None, None)

        s = cl.get("summary", {})
        out.append({
            "start": cl.get("start_ts"),
            "end": cl.get("end_ts"),
            "_active_down_window": active_down_window,
            "total": s.get("total_events", len(events)),
            "down": s.get("down_transitions", 0),
            "active": s.get("active_transitions", 0),
            "switches": s.get("switches_affected", 0),
            "ports": s.get("ports_affected", 0),
            "transition_column_headers": col_headers,
            "port_rows": port_rows,
            # legacy key for callers/tests expecting a list
            "transitions": port_rows,
            "unique_transition_patterns": len(pattern_keys),
            "has_other_transitions": any(r.get("other_transitions") for r in port_rows),
        })
    return out


def _hex_token(val: str) -> str:
    """Normalize FM hex field to lowercase 0x.. for grouping."""
    s = str(val or "").strip()
    if not s:
        return ""
    if " " in s:
        s = s.split()[0]
    s = s.lower()
    if s.startswith("0x"):
        return s
    try:
        return f"0x{int(s, 0):x}"
    except ValueError:
        return s


def _fnm_port_link_key(node_guid: str, port_num: str) -> Tuple[str, str]:
    return (_hex_token(node_guid), str(port_num or "").strip())


def _nvlsm_line_ref(ev: Any) -> str:
    """`logfile.gz:line` for manual lookup in nvlsm.log."""
    source = getattr(ev, "source", "") or ""
    line_no = int(getattr(ev, "line_no", 0) or 0)
    if source and line_no > 0:
        return f"{source}:{line_no}"
    return ""


def _is_nvlsm_fnm_spst_event(ev: Any, fnm_ports: Tuple[int, ...]) -> bool:
    """osm_spst on FNM links: port name contains FNM (e.g. FNMA2P73) or configured port nums."""
    pname = (getattr(ev, "port_name", "") or "").upper()
    if "FNM" in pname:
        return True
    try:
        return int(getattr(ev, "port", -1)) in fnm_ports
    except (TypeError, ValueError):
        return False


def _fm_fnm_loss_times_by_link(node: NodeAnalysis) -> Dict[Tuple[str, str], List[datetime]]:
    """FM FNM port loss timestamps grouped by (nodeGuid, portNum)."""
    by_link: Dict[Tuple[str, str], List[datetime]] = defaultdict(list)
    for ev in node.fm_events:
        if ev.get("category") != "fnm_port_loss":
            continue
        fld = ev.get("fields") or {}
        key = _fnm_port_link_key(str(fld.get("nodeGuid") or ""), str(fld.get("portNum") or ""))
        dt = parse_ts(ev.get("ts") or "")
        if dt is not None:
            by_link[key].append(dt)
    for key in by_link:
        by_link[key].sort()
    return by_link


def _nearest_fm_ts(
    ev_dt: datetime,
    fm_times: List[datetime],
    window_seconds: int,
) -> Tuple[Optional[datetime], bool]:
    """Return (nearest FM time in window, matched)."""
    if not fm_times:
        return None, False
    best: Optional[datetime] = None
    best_delta = window_seconds + 1.0
    matched = False
    for fm_dt in fm_times:
        delta = abs((ev_dt - fm_dt).total_seconds())
        if delta <= window_seconds:
            matched = True
        if delta < best_delta:
            best_delta = delta
            best = fm_dt
    return best, matched


def _is_nvlsm_fnm_loss_spst(ev: Any) -> bool:
    """Port transition into DOWN (loss side), e.g. ACTIVE→DOWN or INIT→DOWN."""
    return (getattr(ev, "new_state", "") or "").upper() == "DOWN"


def _is_nvlsm_fnm_recovery_spst(ev: Any) -> bool:
    """Recovery only: DOWN→INIT — not used as primary FM cross-check."""
    return (
        (getattr(ev, "old_state", "") or "").upper() == "DOWN"
        and (getattr(ev, "new_state", "") or "").upper() == "INIT"
    )


def _index_nvlsm_fnm_spst_events(
    port_events: List[Any],
    fnm_ports: Tuple[int, ...],
) -> NvlsmFnmSpstIndexes:
    """Index FNM osm_spst by link; split loss (*→DOWN) vs recovery (DOWN→INIT)."""
    loss: Dict[Tuple[str, str], List[Any]] = defaultdict(list)
    recovery: Dict[Tuple[str, str], List[Any]] = defaultdict(list)
    loss_count = 0
    recovery_count = 0
    for ev in port_events:
        if not _is_nvlsm_fnm_spst_event(ev, fnm_ports):
            continue
        if _is_nvlsm_fnm_recovery_spst(ev):
            bucket = recovery
        elif _is_nvlsm_fnm_loss_spst(ev):
            bucket = loss
        else:
            continue
        if not getattr(ev, "datetime", None):
            continue
        key = _fnm_port_link_key(getattr(ev, "switch_guid", ""), str(getattr(ev, "port", "")))
        bucket[key].append(ev)
        if bucket is recovery:
            recovery_count += 1
        else:
            loss_count += 1
    for idx in (loss, recovery):
        for key in idx:
            idx[key].sort(key=lambda e: e.datetime)
    return NvlsmFnmSpstIndexes(
        loss=dict(loss),
        recovery=dict(recovery),
        loss_count=loss_count,
        recovery_count=recovery_count,
    )


def _find_init_after_down(
    recovery_events: List[Any],
    after_dt: datetime,
    init_gap_seconds: int,
) -> Optional[Any]:
    for ev in recovery_events:
        if ev.datetime <= after_dt:
            continue
        if ev.old_state != "DOWN" or ev.new_state != "INIT":
            continue
        if (ev.datetime - after_dt).total_seconds() <= init_gap_seconds:
            return ev
    return None


def _nvlsm_fnm_spst_row(
    ev: Any,
    *,
    unmatched_reason: str = "",
) -> NvlsmFnmSpstRow:
    return NvlsmFnmSpstRow(
        ts=getattr(ev, "ts", "") or "",
        switch_guid=getattr(ev, "switch_guid", "") or "",
        port_num=str(getattr(ev, "port", "")),
        port_name=getattr(ev, "port_name", "") or "",
        nvos_hostname=getattr(ev, "switch_name", "") or "",
        transition=f"{getattr(ev, 'old_state', '')}→{getattr(ev, 'new_state', '')}",
        unmatched_reason=unmatched_reason,
        nvlsm_line=_nvlsm_line_ref(ev),
    )


def _nvlsm_line_refs_for_pair(
    loss_ev: Any,
    nvlsm_indexes: NvlsmFnmSpstIndexes,
    init_gap_seconds: int,
) -> Tuple[str, str, str, str, str, Optional[Any]]:
    """
    Build display fields from a paired nvlsm loss event.
    Returns (hostname, transition, followed_by_init, init_ts, nvlsm_line, init_ev).
    nvlsm_line: loss file:line; optional second ref for DOWN→INIT if different line.
    """
    transition = f"{loss_ev.old_state}→{loss_ev.new_state}"
    hostname = getattr(loss_ev, "switch_name", "") or ""
    line_ref = _nvlsm_line_ref(loss_ev)
    key = _fnm_port_link_key(getattr(loss_ev, "switch_guid", ""), str(getattr(loss_ev, "port", "")))
    recovery_events = nvlsm_indexes.recovery.get(key, [])
    init_ev = _find_init_after_down(recovery_events, loss_ev.datetime, init_gap_seconds)
    if not init_ev:
        return hostname, transition, "N", "", line_ref, None
    init_ref = _nvlsm_line_ref(init_ev)
    if init_ref and init_ref != line_ref:
        combined = f"{line_ref}; {init_ref}" if line_ref else init_ref
    else:
        combined = line_ref
    return (
        hostname,
        transition,
        "Y",
        getattr(init_ev, "ts", "") or "",
        combined,
        init_ev,
    )


def _pair_nvlsm_fnm_loss_to_fm(
    nvlsm_indexes: NvlsmFnmSpstIndexes,
    fm_fnm_events: List[Dict[str, Any]],
    match_window_seconds: int,
) -> Tuple[Dict[int, Any], set]:
    """
    nvlsm-driven 1:1 pairing: each *→DOWN osm_spst matches at most one FM row;
    each FM FNM port loss matches at most one nvlsm loss line.
    Greedy by smallest |Δt| among candidates on same Switch GUID + port.
    """
    fm_by_key: Dict[Tuple[str, str], List[Tuple[int, datetime]]] = defaultdict(list)
    for fm_idx, ev in enumerate(fm_fnm_events):
        fld = ev.get("fields") or {}
        key = _fnm_port_link_key(str(fld.get("nodeGuid") or ""), str(fld.get("portNum") or ""))
        fm_dt = parse_ts(ev.get("ts") or "")
        if fm_dt is not None:
            fm_by_key[key].append((fm_idx, fm_dt))

    candidates: List[Tuple[float, int, int, Any]] = []
    for key, loss_events in nvlsm_indexes.loss.items():
        fm_on_link = fm_by_key.get(key, [])
        if not fm_on_link:
            continue
        for nvlsm_ev in loss_events:
            nv_dt = getattr(nvlsm_ev, "datetime", None)
            if nv_dt is None:
                continue
            nv_id = id(nvlsm_ev)
            for fm_idx, fm_dt in fm_on_link:
                delta = abs((nv_dt - fm_dt).total_seconds())
                if delta <= match_window_seconds:
                    candidates.append((delta, nv_id, fm_idx, nvlsm_ev))

    candidates.sort(key=lambda item: item[0])
    used_nvlsm: set = set()
    used_fm: set = set()
    fm_to_nvlsm: Dict[int, Any] = {}
    for _delta, nv_id, fm_idx, nvlsm_ev in candidates:
        if nv_id in used_nvlsm or fm_idx in used_fm:
            continue
        used_nvlsm.add(nv_id)
        used_fm.add(fm_idx)
        fm_to_nvlsm[fm_idx] = nvlsm_ev
    return fm_to_nvlsm, used_nvlsm


def collect_fnm_port_loss_report(
    node: NodeAnalysis,
    *,
    fnm_ports: Tuple[int, ...] = (73, 74),
    match_window_seconds: int = 300,
    init_gap_seconds: int = 600,
) -> FnmPortLossReport:
    """
    nvlsm-driven cross-check: index FNM *→DOWN osm_spst lines, pair 1:1 to FM FNM port loss
    (same Switch GUID + port, ±window). FM table is still the report shell; nvlsm fields
    are filled only on FM rows that won a pair. Unmatched nvlsm loss lines are listed.
    """
    nvlsm_indexes = NvlsmFnmSpstIndexes()
    if node.nvlsm_health and node.nvlsm_health.port_events:
        nvlsm_indexes = _index_nvlsm_fnm_spst_events(
            node.nvlsm_health.port_events,
            fnm_ports,
        )
    fm_fnm_events = [e for e in node.fm_events if e.get("category") == "fnm_port_loss"]
    fm_to_nvlsm, used_nvlsm_ids = _pair_nvlsm_fnm_loss_to_fm(
        nvlsm_indexes,
        fm_fnm_events,
        match_window_seconds,
    )
    fm_by_link = _fm_fnm_loss_times_by_link(node)

    fm_rows: List[FmFnmPortLossRow] = []
    used_recovery_ids: set = set()
    for fm_idx, ev in enumerate(fm_fnm_events):
        fld = ev.get("fields") or {}
        node_guid = str(fld.get("nodeGuid") or "")
        port_num = str(fld.get("portNum") or "")
        fm_ts = ev.get("ts") or ""
        loss_ev = fm_to_nvlsm.get(fm_idx)
        if loss_ev is None:
            fm_rows.append(
                FmFnmPortLossRow(
                    ts=fm_ts,
                    node_guid=node_guid,
                    port_num=port_num,
                    in_nvlsm="N",
                )
            )
            continue
        hostname, transition, has_init, init_ts, nvlsm_line, init_ev = _nvlsm_line_refs_for_pair(
            loss_ev,
            nvlsm_indexes,
            init_gap_seconds,
        )
        if init_ev is not None:
            used_recovery_ids.add(id(init_ev))
        fm_rows.append(
            FmFnmPortLossRow(
                ts=fm_ts,
                node_guid=node_guid,
                port_num=port_num,
                in_nvlsm="Y",
                nvos_hostname=hostname,
                nvlsm_transition=transition,
                followed_by_init=has_init,
                init_ts=init_ts,
                nvlsm_line=nvlsm_line,
            )
        )
    fm_rows.sort(key=lambda r: _ts_sort_key(r.ts))
    fm_unmatched = [r for r in fm_rows if r.in_nvlsm != "Y"]

    nvlsm_unmatched: List[NvlsmFnmSpstRow] = []
    nvlsm_matched = len(used_nvlsm_ids)
    nvlsm_total = nvlsm_indexes.loss_count
    for key, events in nvlsm_indexes.loss.items():
        fm_times = fm_by_link.get(key, [])
        for ev in events:
            if id(ev) in used_nvlsm_ids:
                continue
            ev_dt = getattr(ev, "datetime", None)
            if ev_dt is None:
                nvlsm_unmatched.append(
                    NvlsmFnmSpstRow(
                        ts=getattr(ev, "ts", "") or "",
                        switch_guid=getattr(ev, "switch_guid", "") or "",
                        port_num=str(getattr(ev, "port", "")),
                        port_name=getattr(ev, "port_name", "") or "",
                        nvos_hostname=getattr(ev, "switch_name", "") or "",
                        transition=f"{ev.old_state}→{ev.new_state}",
                        unmatched_reason="unparseable timestamp",
                        nvlsm_line=_nvlsm_line_ref(ev),
                    )
                )
                continue
            nearest, _matched = _nearest_fm_ts(ev_dt, fm_times, match_window_seconds)
            reason = "no FM FNM port loss on this Switch GUID + port" if not fm_times else (
                f"no FM FNM port loss within ±{match_window_seconds}s (or already paired)"
            )
            nearest_str = ""
            if nearest is not None and fm_times:
                for fm_ev in fm_fnm_events:
                    fld = fm_ev.get("fields") or {}
                    if _fnm_port_link_key(
                        str(fld.get("nodeGuid") or ""),
                        str(fld.get("portNum") or ""),
                    ) != key:
                        continue
                    if parse_ts(fm_ev.get("ts") or "") == nearest:
                        nearest_str = fm_ev.get("ts") or ""
                        break
            nvlsm_unmatched.append(
                NvlsmFnmSpstRow(
                    ts=getattr(ev, "ts", "") or "",
                    switch_guid=getattr(ev, "switch_guid", "") or "",
                    port_num=str(getattr(ev, "port", "")),
                    port_name=getattr(ev, "port_name", "") or "",
                    nvos_hostname=getattr(ev, "switch_name", "") or "",
                    transition=f"{ev.old_state}→{ev.new_state}",
                    matched_fm="N",
                    nearest_fm_ts=nearest_str,
                    unmatched_reason=reason,
                    nvlsm_line=_nvlsm_line_ref(ev),
                )
            )

    nvlsm_unmatched.sort(key=lambda r: (r.ts, r.switch_guid, r.port_num))

    recovery_unmatched: List[NvlsmFnmSpstRow] = []
    for _key, events in nvlsm_indexes.recovery.items():
        for ev in events:
            if id(ev) in used_recovery_ids:
                continue
            recovery_unmatched.append(
                _nvlsm_fnm_spst_row(
                    ev,
                    unmatched_reason=(
                        "DOWN→INIT not linked to any paired *→DOWN→FM row "
                        f"(outside {init_gap_seconds}s after loss, or no loss/FM pair on link)"
                    ),
                )
            )
    recovery_unmatched.sort(key=lambda r: (r.ts, r.switch_guid, r.port_num))

    return FnmPortLossReport(
        fm_rows=fm_rows,
        nvlsm_fnm_spst_total=nvlsm_total,
        nvlsm_recovery_total=nvlsm_indexes.recovery_count,
        nvlsm_matched_fm_count=nvlsm_matched,
        nvlsm_unmatched=nvlsm_unmatched,
        nvlsm_recovery_unmatched=recovery_unmatched,
        fm_unmatched=fm_unmatched,
    )


FM_LOGS_MAX_PER_EVENT_GROUP = 600
FM_LOGS_MAX_UNASSIGNED = 1200

# Levels filtered out of the per-event-group FM event table (user requirement: drop INFO).
FM_EVENT_DROP_LEVELS: Tuple[str, ...] = ("INFO",)


@dataclass
class GpuMappingObservation:
    """One observed (gpuGuid, nodeId, slot, ...) tuple from a 'received general info' event."""

    ts: str
    node_id: str = ""
    chassis_phy_slot: str = ""
    compute_slot_index: str = ""
    module_id: str = ""
    rack_guid: str = ""


@dataclass
class GpuMappingStateRow:
    """Consecutive observations with identical id fields collapsed into a time range."""

    first_ts: str
    last_ts: str
    node_id: str
    chassis_phy_slot: str
    compute_slot_index: str
    module_id: str
    rack_guid: str = ""
    count: int = 1


@dataclass
class GpuMappingEntry:
    """Mapping history for one GPU GUID, sorted by timestamp."""

    gpu_guid: str
    observations: List[GpuMappingObservation] = field(default_factory=list)

    def state_rows(self) -> List[GpuMappingStateRow]:
        rows: List[GpuMappingStateRow] = []
        for obs in self.observations:
            key = (
                obs.node_id,
                obs.chassis_phy_slot,
                obs.compute_slot_index,
                obs.module_id,
            )
            if rows:
                last = rows[-1]
                last_key = (
                    last.node_id,
                    last.chassis_phy_slot,
                    last.compute_slot_index,
                    last.module_id,
                )
                if key == last_key:
                    if obs.ts and (not last.last_ts or obs.ts > last.last_ts):
                        last.last_ts = obs.ts
                    last.count += 1
                    continue
            rows.append(
                GpuMappingStateRow(
                    first_ts=obs.ts,
                    last_ts=obs.ts,
                    node_id=obs.node_id,
                    chassis_phy_slot=obs.chassis_phy_slot,
                    compute_slot_index=obs.compute_slot_index,
                    module_id=obs.module_id,
                    rack_guid=obs.rack_guid,
                )
            )
        return rows


@dataclass
class SlotMappingRow:
    """One row in the per-Chassis-Phy-Slot mapping table (combined across GPUs)."""

    gpu_guid: str
    node_id: str
    chassis_phy_slot: str
    compute_slot_index: str
    module_id: str
    first_ts: str
    last_ts: str
    rack_guid: str = ""
    count: int = 1


@dataclass
class SlotMappingGroup:
    """All mapping observations bucketed under a single Chassis Phy Slot value."""

    chassis_phy_slot: str
    sort_key: float
    rows: List[SlotMappingRow] = field(default_factory=list)
    gpu_guids: List[str] = field(default_factory=list)

    @property
    def distinct_guid_count(self) -> int:
        return len(self.gpu_guids)

    @property
    def dominant_rack_guid(self) -> str:
        """Most-common rack_guid across this slot's rows; '' when none observed."""
        counts: Counter = Counter(r.rack_guid for r in self.rows if r.rack_guid)
        if not counts:
            return ""
        return counts.most_common(1)[0][0]

    @property
    def dominant_compute_slot_index(self) -> str:
        """Most-common compute_slot_index across this slot's rows; '' when none."""
        counts: Counter = Counter(
            r.compute_slot_index for r in self.rows if r.compute_slot_index
        )
        if not counts:
            return ""
        return counts.most_common(1)[0][0]


@dataclass
class RackMappingGroup:
    """All Chassis-Phy-Slot groups discovered under a single rackGuid."""

    rack_guid: str
    slot_groups: List[SlotMappingGroup] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(len(g.rows) for g in self.slot_groups)

    @property
    def total_distinct_guids(self) -> int:
        seen = set()
        for sg in self.slot_groups:
            seen.update(sg.gpu_guids)
        return len(seen)


@dataclass
class FmEventRow:
    """One FM event rendered as a structured table row (post INFO filter)."""

    ts: str
    level: str
    category: str
    node_id: str = ""
    gpu_guid: str = ""
    chassis_phy_slot: str = ""
    compute_slot_index: str = ""
    module_id: str = ""
    gpu_id: str = ""
    host_id: str = ""
    chassis_id: str = ""
    port_num: str = ""
    resolved_from: str = ""  # "" | "guid" | "node_id" -- provenance of slot/module/idx
    detail: str = ""


def _min_numeric(values: Iterable[str]) -> float:
    """Smallest int parse of `values`; +inf when none parseable. Mirrors nvos_parser sort."""
    best: Optional[int] = None
    for v in values:
        if v in (None, "", "UNKNOWN"):
            continue
        try:
            n = int(str(v).strip())
        except (TypeError, ValueError):
            continue
        if best is None or n < best:
            best = n
    return float(best) if best is not None else float("inf")


def _field_str(fld: Dict[str, Any], *keys: str, default: str = "") -> str:
    """Case-sensitive first non-empty match in `fld`; returns `default` otherwise."""
    if not isinstance(fld, dict):
        return default
    for k in keys:
        v = fld.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return default


def _normalize_gpu_guid_str(value: str) -> str:
    """Normalize GPU GUID to '0x' + 16 hex chars (matches parser._normalize_gpu_guid)."""
    s = (value or "").strip()
    if not s:
        return ""
    if " " in s:
        s = s.split()[0]
    s = s.lower()
    if s.startswith("0x"):
        hex_part = s[2:]
    else:
        hex_part = s
    try:
        return f"0x{int(hex_part, 16):016x}"
    except (TypeError, ValueError):
        return s


def build_gpu_mappings(fm_events: List[Dict[str, Any]]) -> Dict[str, GpuMappingEntry]:
    """Per GPU GUID, collect (ts, nodeId, slot, ...) observations from gpu_node_mapping events."""
    mappings: Dict[str, GpuMappingEntry] = {}
    for ev in fm_events:
        if ev.get("category") != "gpu_node_mapping":
            continue
        fld = ev.get("fields") or {}
        guid = _normalize_gpu_guid_str(_field_str(fld, "gpuGuid", "gpuGUID"))
        if not guid:
            continue
        obs = GpuMappingObservation(
            ts=ev.get("ts") or "",
            node_id=_field_str(fld, "nodeID", "nodeId"),
            chassis_phy_slot=_field_str(fld, "chassisPhySlotNo"),
            compute_slot_index=_field_str(fld, "computeSlotIndex"),
            module_id=_field_str(fld, "moduleId"),
            rack_guid=_field_str(fld, "rackGuid"),
        )
        mappings.setdefault(guid, GpuMappingEntry(gpu_guid=guid)).observations.append(obs)

    for entry in mappings.values():
        entry.observations.sort(key=lambda o: _ts_sort_key(o.ts))
    return mappings


# (datetime, GpuMappingObservation, gpu_guid) tuples, sorted by datetime.
NodeIdLookup = Dict[str, List[Tuple[Optional[datetime], GpuMappingObservation, str]]]


def build_node_id_lookup(mappings: Dict[str, GpuMappingEntry]) -> NodeIdLookup:
    """nodeId → time-sorted list of (dt, observation, gpu_guid) for ts-based resolution."""
    by_node_id: NodeIdLookup = defaultdict(list)
    for guid, entry in mappings.items():
        for obs in entry.observations:
            if not obs.node_id:
                continue
            dt = parse_ts(obs.ts) if obs.ts else None
            by_node_id[obs.node_id].append((dt, obs, guid))
    for lst in by_node_id.values():
        lst.sort(key=lambda x: (x[0] or datetime.min, x[2]))
    return by_node_id


def sorted_gpu_mappings(mappings: Dict[str, GpuMappingEntry]) -> List[GpuMappingEntry]:
    """Sort by (min chassis_phy_slot, min module_id, gpu_guid) -- user-requested order."""
    return sorted(
        mappings.values(),
        key=lambda e: (
            _min_numeric(o.chassis_phy_slot for o in e.observations),
            _min_numeric(o.module_id for o in e.observations),
            e.gpu_guid,
        ),
    )


def _slot_sort_key(slot: str) -> float:
    """Numeric sort key for chassis_phy_slot; non-numeric goes last."""
    try:
        return float(int((slot or "").strip()))
    except (TypeError, ValueError):
        return float("inf")


def group_mappings_by_slot(
    entries: List[GpuMappingEntry],
) -> List[SlotMappingGroup]:
    """Re-bucket every per-GPU state row under its Chassis Phy Slot value.

    Each state row contributes one row to its slot's bucket; if a GPU ever
    moved across slots (rare), it shows up in multiple buckets, which is
    exactly what the hardware-replacement heuristic wants to surface.

    Returns slot buckets sorted by numeric slot value.
    """
    by_slot_rows: Dict[str, List[SlotMappingRow]] = defaultdict(list)
    by_slot_guids: Dict[str, List[str]] = defaultdict(list)
    by_slot_guid_seen: Dict[str, set] = defaultdict(set)

    for entry in entries:
        for sr in entry.state_rows():
            slot = sr.chassis_phy_slot or "(unknown)"
            by_slot_rows[slot].append(
                SlotMappingRow(
                    gpu_guid=entry.gpu_guid,
                    node_id=sr.node_id,
                    chassis_phy_slot=sr.chassis_phy_slot,
                    compute_slot_index=sr.compute_slot_index,
                    module_id=sr.module_id,
                    first_ts=sr.first_ts,
                    last_ts=sr.last_ts,
                    rack_guid=sr.rack_guid,
                    count=sr.count,
                )
            )
            if entry.gpu_guid not in by_slot_guid_seen[slot]:
                by_slot_guid_seen[slot].add(entry.gpu_guid)
                by_slot_guids[slot].append(entry.gpu_guid)

    groups: List[SlotMappingGroup] = []
    for slot in sorted(by_slot_rows.keys(), key=_slot_sort_key):
        # Inside each slot's table: sort by First Seen (chronological).
        rows = sorted(
            by_slot_rows[slot],
            key=lambda r: (r.first_ts or "", r.gpu_guid),
        )
        groups.append(
            SlotMappingGroup(
                chassis_phy_slot=slot,
                sort_key=_slot_sort_key(slot),
                rows=rows,
                gpu_guids=list(by_slot_guids[slot]),
            )
        )
    return groups


def group_slots_by_rack(
    slot_groups: List[SlotMappingGroup],
) -> List[RackMappingGroup]:
    """Wrap Chassis-Phy-Slot buckets inside an outer rackGuid bucket.

    Each slot is attached to its **dominant** rack_guid (the rack_guid
    contributing the most state rows under that slot). Slots without any
    rack_guid observation fall under "(unknown)". Racks are sorted by GUID.
    """
    by_rack: Dict[str, List[SlotMappingGroup]] = defaultdict(list)
    for sg in slot_groups:
        rack = sg.dominant_rack_guid or "(unknown)"
        by_rack[rack].append(sg)

    groups: List[RackMappingGroup] = []
    for rack in sorted(by_rack.keys()):
        # Preserve the existing slot ordering inside each rack.
        groups.append(RackMappingGroup(rack_guid=rack, slot_groups=by_rack[rack]))
    return groups


def _resolve_by_gpu_guid(
    mappings: Dict[str, GpuMappingEntry],
    gpu_guid: str,
    event_dt: Optional[datetime],
) -> Optional[GpuMappingObservation]:
    """Pick the observation for `gpu_guid` whose ts ≤ event_dt is closest; fallback nearest."""
    entry = mappings.get(gpu_guid)
    if not entry or not entry.observations:
        return None
    if event_dt is None:
        return entry.observations[-1]
    best: Optional[GpuMappingObservation] = None
    first_after: Optional[GpuMappingObservation] = None
    for obs in entry.observations:
        dt = parse_ts(obs.ts) if obs.ts else None
        if dt is None:
            continue
        if dt <= event_dt:
            best = obs
        else:
            if first_after is None:
                first_after = obs
            break
    return best or first_after or entry.observations[0]


def _resolve_by_node_id(
    node_id_lookup: NodeIdLookup,
    node_id: str,
    event_dt: Optional[datetime],
) -> Optional[Tuple[str, GpuMappingObservation]]:
    """Same time-window logic as _resolve_by_gpu_guid but indexed by nodeId."""
    if not node_id:
        return None
    entries = node_id_lookup.get(node_id)
    if not entries:
        return None
    if event_dt is None:
        _, obs, guid = entries[-1]
        return guid, obs
    best: Optional[Tuple[Optional[datetime], GpuMappingObservation, str]] = None
    first_after: Optional[Tuple[Optional[datetime], GpuMappingObservation, str]] = None
    for entry in entries:
        dt = entry[0]
        if dt is None:
            continue
        if dt <= event_dt:
            best = entry
        else:
            if first_after is None:
                first_after = entry
            break
    chosen = best or first_after or entries[0]
    _, obs, guid = chosen
    return guid, obs


def _build_event_detail(ev: Dict[str, Any], fld: Dict[str, Any], category: str) -> str:
    """Concise detail string for the FM event row's last column."""
    if category == "health":
        return (
            f"{fld.get('healthType', '?')} health → "
            f"{fld.get('healthStatus', '?')}"
        )
    if category == "connection_lost":
        # GPU variant: "Lost connection to GPU on nodeId X, ... GPU state Y".
        # The switch-level "Lost connection to switch on chassisId X ..." has
        # its own category (switch_connection_lost) and never reaches this
        # detail builder.
        state = _field_str(fld, "gpuState")
        if state:
            return f"Lost connection (gpuState={state})"
        target = _field_str(fld, "target")
        return f"Lost connection (target={target or '?'})"
    if category == "fnm_port_loss":
        return (
            f"FNM port loss nodeGuid={_field_str(fld, 'nodeGuid') or '?'} "
            f"port={_field_str(fld, 'portNum') or '?'}"
        )
    if category == "fm_lifecycle":
        et = _field_str(fld, "event_type") or "?"
        ver = _field_str(fld, "version")
        return f"FM lifecycle: {et}" + (f" (v{ver})" if ver else "")
    if category == "switch_info_failed":
        # slotNumber is already shown in the "Chassis Phy Slot" column;
        # include only the fields that don't have their own table cell.
        return (
            f"Failed switch info: "
            f"chassisId={_field_str(fld, 'chassisId') or '-'} / "
            f"hostId={_field_str(fld, 'hostId') or '-'} / "
            f"switchId={_field_str(fld, 'switchId') or '-'}"
        )
    if category == "partition_error":
        return (
            f"Partition Id {_field_str(fld, 'partitionId') or '?'} "
            f"unexpected error state"
        )
    code = _field_str(fld, "errorCode")
    sub = _field_str(fld, "errorSubcode", "errorSubCode")
    pdrc = _field_str(fld, "portDownReasonCode")
    status = _field_str(fld, "errorStatus")
    # Field order matches the original NVL error block in fabricmanager.log:
    #   errorCode → errorSubcode → portDownReasonCode → errorStatus.
    if code or sub or pdrc or status:
        # Label casing follows the original FM log field names with the
        # leading "error" stripped: errorCode → Code, errorSubcode → Subcode,
        # portDownReasonCode unchanged, errorStatus → Status.
        return (
            f"Code={code or '-'} / "
            f"Subcode={sub or '-'} / "
            f"portDownReasonCode={pdrc or '-'} / "
            f"Status={status or '-'}"
        )
    msg = (ev.get("message") or "").strip()
    if len(msg) > 200:
        return msg[:199] + "…"
    return msg


def build_fm_event_row(
    ev: Dict[str, Any],
    mappings: Dict[str, GpuMappingEntry],
    node_id_lookup: NodeIdLookup,
) -> FmEventRow:
    """Map one parsed FM event to a structured row, resolving slot/module via mapping."""
    fld = ev.get("fields") or {}
    ts = ev.get("ts") or ""
    level = (ev.get("level") or "").upper() or "?"
    category = ev.get("category") or "generic"

    node_id = _field_str(fld, "nodeID", "nodeId")
    raw_guid = _field_str(fld, "gpuGuid", "gpuGUID")
    gpu_guid_norm = _normalize_gpu_guid_str(raw_guid) if raw_guid else ""

    chassis_phy_slot = _field_str(fld, "slotNumber", "chassisPhySlotNo")
    module_id = _field_str(fld, "moduleId")
    compute_slot_index = _field_str(fld, "computeSlotIndex")
    gpu_id = _field_str(fld, "gpuId")
    host_id = _field_str(fld, "hostId")
    chassis_id = _field_str(fld, "chassisId")
    port_num = _field_str(fld, "portNum", "port")

    event_dt = parse_ts(ts) if ts else None
    resolved_obs: Optional[GpuMappingObservation] = None
    resolved_from = ""
    resolved_guid = gpu_guid_norm

    if gpu_guid_norm:
        resolved_obs = _resolve_by_gpu_guid(mappings, gpu_guid_norm, event_dt)
        if resolved_obs:
            resolved_from = "guid"
    if resolved_obs is None and node_id:
        r = _resolve_by_node_id(node_id_lookup, node_id, event_dt)
        if r:
            resolved_guid_via_node, resolved_obs = r
            if not resolved_guid:
                resolved_guid = resolved_guid_via_node
            resolved_from = "node_id"

    if resolved_obs is not None:
        if not chassis_phy_slot:
            chassis_phy_slot = resolved_obs.chassis_phy_slot
        if not module_id:
            module_id = resolved_obs.module_id
        if not compute_slot_index:
            compute_slot_index = resolved_obs.compute_slot_index

    return FmEventRow(
        ts=ts,
        level=level,
        category=category,
        node_id=node_id,
        gpu_guid=resolved_guid,
        chassis_phy_slot=chassis_phy_slot,
        compute_slot_index=compute_slot_index,
        module_id=module_id,
        gpu_id=gpu_id,
        host_id=host_id,
        chassis_id=chassis_id,
        port_num=port_num,
        resolved_from=resolved_from,
        detail=_build_event_detail(ev, fld, category),
    )


def _fm_event_keep_for_table(ev: Dict[str, Any]) -> bool:
    """Filter for the per-event-group FM event table: drop mapping noise and INFO-level lines."""
    cat = ev.get("category") or ""
    if cat == "gpu_node_mapping":
        return False
    # Switch-level connection losses are dumped as raw text in the dedicated
    # "Failed to get switch info" section; suppress them here so the event-group
    # tables stay focused on GPU/port-level lifecycle.
    if cat == "switch_connection_lost":
        return False
    # Multicast-team-limit-reached entries are surfaced in their own raw-log
    # section. They are partition-resource-exhaustion notices, not part of any
    # NVLSM port-state event group, so don't pollute the per-event-group FM tables.
    if cat == "multicast_team_limit_reached":
        return False
    level = (ev.get("level") or "").upper()
    if level in FM_EVENT_DROP_LEVELS:
        return False
    return True


# Empirical slack added on both sides of the ACTIVE→DOWN window when
# bucketing FM events into an event group. The raw ACTIVE→DOWN range is too
# tight in practice (FM frequently reports a port-down a beat before/after the
# matching nvlsm `osm_spst` line because of clocking/IO buffering on the two
# log paths), so 2s of jitter empirically recaptures the matching FM lines
# without pulling in unrelated noise. Hardcoded on purpose — this is a tuning
# constant, not a user-facing knob.
_EVENT_GROUP_WINDOW_SLACK = timedelta(seconds=2)


def _event_group_time_window(cl: Dict[str, Any]) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Prefer the ACTIVE→DOWN-only ts window for FM-event placement.

    The event group's overall (start, end) span includes DOWN→INIT recovery
    transitions which can stretch the window minutes past the actual fault
    burst, causing unrelated FM lines to be bucketed under the event group.
    Use the ACTIVE→DOWN window when available (widened by
    ``_EVENT_GROUP_WINDOW_SLACK`` on each side to absorb sub-second skew
    between the FM and nvlsm log streams); otherwise fall back to the event
    group's overall window.
    """
    ad_window = cl.get("_active_down_window")
    if (
        isinstance(ad_window, tuple)
        and len(ad_window) == 2
        and ad_window[0] is not None
        and ad_window[1] is not None
    ):
        return (
            ad_window[0] - _EVENT_GROUP_WINDOW_SLACK,
            ad_window[1] + _EVENT_GROUP_WINDOW_SLACK,
        )
    start = parse_ts(str(cl.get("start") or ""))
    end = parse_ts(str(cl.get("end") or ""))
    return start, end


def attach_fm_event_rows_to_nvlsm_event_groups(
    event_groups: List[Dict[str, Any]],
    fm_events: List[Dict[str, Any]],
    mappings: Dict[str, GpuMappingEntry],
    node_id_lookup: NodeIdLookup,
    *,
    max_per_event_group: int = FM_LOGS_MAX_PER_EVENT_GROUP,
    max_unassigned: int = FM_LOGS_MAX_UNASSIGNED,
) -> Dict[str, Any]:
    """Bucket FM events (post INFO filter) as structured rows under NVLSM event-group windows."""
    buckets: List[List[FmEventRow]] = [[] for _ in event_groups]
    omitted_per_event_group = [0 for _ in event_groups]
    unassigned: List[FmEventRow] = []
    unassigned_omitted = 0
    no_ts = 0

    sorted_events = sorted(
        fm_events,
        key=lambda e: (
            parse_ts(e.get("ts") or "") or datetime.max,
            e.get("message") or "",
        ),
    )

    event_group_windows = [_event_group_time_window(cl) for cl in event_groups]

    for ev in sorted_events:
        if not _fm_event_keep_for_table(ev):
            continue
        row = build_fm_event_row(ev, mappings, node_id_lookup)
        dt = parse_ts(ev.get("ts") or "")
        if dt is None:
            no_ts += 1
            unassigned.append(row)
            continue

        placed = False
        for idx, (start, end) in enumerate(event_group_windows):
            if start is None or end is None:
                continue
            if start <= dt <= end:
                if len(buckets[idx]) < max_per_event_group:
                    buckets[idx].append(row)
                else:
                    omitted_per_event_group[idx] += 1
                placed = True
                break

        if not placed:
            unassigned.append(row)

    for idx, cl in enumerate(event_groups):
        cl["fm_event_rows"] = buckets[idx]
        cl["fm_logs_omitted"] = omitted_per_event_group[idx]

    return {
        "unassigned": unassigned,
        "unassigned_omitted": unassigned_omitted,
        "no_timestamp": no_ts,
    }


def build_node_report_context(
    node: NodeAnalysis,
    *,
    fnm_ports: Tuple[int, ...] = (73, 74),
    fm_fnm_nvlsm_match_window_seconds: int = 300,
    fm_fnm_init_follow_gap_seconds: int = 600,
    lifecycle_pair_max_seconds: int = 120,
) -> Dict[str, Any]:
    """Precompute all aggregated sections for one node."""
    nvlsm_event_groups = aggregate_nvlsm_event_group_details(
        node.nvlsm_port_event_groups,
        last_n=None,
        top_transitions_per_event_group=None,
        lifecycle_pair_max_seconds=lifecycle_pair_max_seconds,
    )
    gpu_mappings = build_gpu_mappings(node.fm_events)
    node_id_lookup = build_node_id_lookup(gpu_mappings)
    fm_event_placement = attach_fm_event_rows_to_nvlsm_event_groups(
        nvlsm_event_groups,
        node.fm_events,
        gpu_mappings,
        node_id_lookup,
    )
    sorted_entries = sorted_gpu_mappings(gpu_mappings)
    slot_groups = group_mappings_by_slot(sorted_entries)

    # Split the unassigned (outside-all-event-group-windows) FM events into two
    # buckets so review can separate pre-NVLSM history from contemporaneous
    # but unattributed events.
    earliest_nvlsm_dt: Optional[datetime] = None
    for cl in nvlsm_event_groups:
        dt = parse_ts(str(cl.get("start") or ""))
        if dt is not None and (earliest_nvlsm_dt is None or dt < earliest_nvlsm_dt):
            earliest_nvlsm_dt = dt

    pre_nvlsm_all: List[Any] = []
    outside_after_nvlsm_all: List[Any] = []
    for row in fm_event_placement.get("unassigned") or []:
        row_dt = parse_ts(getattr(row, "ts", "") or "")
        if (
            earliest_nvlsm_dt is not None
            and row_dt is not None
            and row_dt < earliest_nvlsm_dt
        ):
            pre_nvlsm_all.append(row)
        else:
            outside_after_nvlsm_all.append(row)

    pre_cap = FM_LOGS_MAX_UNASSIGNED
    after_cap = FM_LOGS_MAX_UNASSIGNED
    # Keep the *tail* of the pre-NVLSM bucket: events closest to the first
    # NVLSM observation are far more informative than the historical leading
    # edge, so when we drop, we drop from the oldest end.
    pre_nvlsm = pre_nvlsm_all[-pre_cap:] if pre_nvlsm_all else []
    pre_nvlsm_cutoff_ts = (
        pre_nvlsm[0].ts if len(pre_nvlsm_all) > pre_cap and pre_nvlsm else ""
    )
    outside_after_nvlsm = outside_after_nvlsm_all[:after_cap]
    outside_after_nvlsm_omitted = max(0, len(outside_after_nvlsm_all) - after_cap)

    non_fnm_port_event_count = 0
    if node.nvlsm_health and node.nvlsm_health.port_events:
        non_fnm_port_event_count = len(
            port_events_for_event_group_report(node.nvlsm_health.port_events)
        )

    ctx: Dict[str, Any] = {
        "label": node.label,
        "node_title": node.node_title or f"Node: {node.label}",
        "path": str(node.nmx_c_path),
        "port_event_group_non_fnm_event_count": non_fnm_port_event_count,
        "port_event_group_count": len(nvlsm_event_groups),
        "nvlsm_event_groups": nvlsm_event_groups,
        "gpu_mappings": sorted_entries,
        "gpu_mapping_slots": slot_groups,
        "gpu_mapping_racks": group_slots_by_rack(slot_groups),
        "fm_log_pre_nvlsm": pre_nvlsm,
        "fm_log_pre_nvlsm_cutoff_ts": pre_nvlsm_cutoff_ts,
        "fm_log_outside_after_nvlsm": outside_after_nvlsm,
        "fm_log_outside_after_nvlsm_omitted": outside_after_nvlsm_omitted,
        "earliest_nvlsm_ts": (
            earliest_nvlsm_dt.strftime("%Y-%m-%d %H:%M:%S")
            if earliest_nvlsm_dt is not None
            else ""
        ),
        "fm_log_no_timestamp": fm_event_placement.get("no_timestamp", 0),
        "fm_fnm_port_loss": collect_fnm_port_loss_report(
            node,
            fnm_ports=fnm_ports,
            match_window_seconds=fm_fnm_nvlsm_match_window_seconds,
            init_gap_seconds=fm_fnm_init_follow_gap_seconds,
        ),
    }
    return ctx
