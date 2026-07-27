"""Parse the Markdown reports of the two source skills into the shared model.

nv-bug-report (compute tray)  -> TrayReport   (Xid + IMEX event groups)
NVOS / NMX-C dump (switch)    -> SwitchReport (**every** time-stamped section)

On the switch side the parser walks each node section end to end and emits an
``Event`` for every time-stamped source the nvos report renders:

===========================  ==================================================
kind                         nvos report section
===========================  ==================================================
``port_state``               ``#### Port state event groups`` (all 3 severities)
``fm_outside``               ``Fabric Manager log before/after earliest NVLSM
                             event`` — the FM rows that fall **outside** every
                             port-state group, clustered in time
``fnm_port_loss``            ``#### FNM port loss`` — FM FNM loss table
``fnm_nvlsm_unmatched``      ``#### FNM port loss`` — unmatched NVLSM loss table
``fnm_nvlsm_recovery``       ``#### FNM port loss`` — NVLSM rows with no FM link
``switch_info_failure``      ``#### Failed to get switch info`` (raw block)
``partition_error``          ``#### Partition unexpected error state`` (raw)
``multicast_limit``          ``#### Multicast team limit reached`` (raw)
``fm_lifecycle``             ``#### FM lifecycle``
``nvlsm_health``             invalid-topology / invalid-UTF-8 health table
===========================  ==================================================

``### GPU Node Mapping`` is deliberately **not** an event source: its
first-seen / last-seen columns describe an observation window for a GPU GUID
inventory, not a moment something happened, so correlating it would manufacture
matches for every slot.

Parsing is by regex over the rendered Markdown (the reports are the contract).
See ``models.py`` for the shapes produced.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from . import timeutil as T
from .models import (
    CrossNodeReport,
    Event,
    SwitchNode,
    SwitchReport,
    TrayReport,
    worst_severity,
)

NVBUG_TITLE = "# NVIDIA Bug Report Analysis"
NVOS_TITLE = "# NMX-C Log Analysis Report"
NVBUG_CROSS_TITLE = "# Multi-Node Xid Comparison Report"

# Idle gap that starts a new cluster when a section is a flat row/line stream
# (FM rows outside port-state groups, raw log blocks). Overridden by the CLI so
# the clustering granularity tracks the correlation window.
DEFAULT_CLUSTER_GAP_S = 120


def classify(text: str) -> Optional[str]:
    """Return 'nvbug', 'nvbug_cross', 'nvos', or None from a report's leading lines."""
    head = text[:2000]
    if NVBUG_CROSS_TITLE in head:
        return "nvbug_cross"
    if NVBUG_TITLE in head:
        return "nvbug"
    if NVOS_TITLE in head:
        return "nvos"
    return None


def _slice(text: str, start_pat: str, end_pats: Tuple[str, ...]) -> str:
    """Return the substring from the first ``start_pat`` match to the next of
    any ``end_pats`` (or end of text)."""
    m = re.search(start_pat, text, re.M)
    if not m:
        return ""
    start = m.end()
    end = len(text)
    for ep in end_pats:
        me = re.search(ep, text[start:], re.M)
        if me:
            end = min(end, start + me.start())
    return text[start:end]


# ---------------------------------------------------------------------------
# Generic Markdown readers (tables / details blocks / fenced raw text)
# ---------------------------------------------------------------------------

def _cells(line: str) -> List[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(cells: Sequence[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r"[-: ]*", c) for c in cells)


def _read_table(text: str, pos: int) -> Tuple[List[str], List[List[str]]]:
    """Read the first Markdown pipe table at/after ``pos``.

    Returns ``(headers, rows)``. Reading stops at the first non-table line once
    the body started, and bails out if the enclosing block ends (``</details>``,
    a heading, or the next ``<summary>``) before a table appears.
    """
    headers: List[str] = []
    rows: List[List[str]] = []
    started = False
    for line in text[pos:].splitlines():
        s = line.strip()
        if not s.startswith("|"):
            if started:
                break
            if s.startswith("</details>") or s.startswith("#") or s.startswith("<summary>"):
                break
            continue
        cv = _cells(s)
        if _is_separator(cv):
            continue
        if not started:
            headers, started = cv, True
        else:
            rows.append(cv)
    return headers, rows


def _summary_pos(region: str, needle: str) -> int:
    """End offset of the ``<summary>`` line containing ``needle`` (-1 if none).

    The nvos renderer may wrap a summary in ``<strong>`` / a red ``<span>``, so
    the match is a substring test rather than an exact one.
    """
    for m in re.finditer(r"^<summary>.*$", region, re.M):
        if needle in m.group(0):
            return m.end()
    return -1


def _table_under(region: str, needle: str) -> List[Dict[str, str]]:
    """Rows (as header->value dicts) of the table under the ``needle`` summary."""
    pos = _summary_pos(region, needle)
    if pos < 0:
        return []
    headers, rows = _read_table(region, pos)
    if not headers:
        return []
    return [{h: (r[i] if i < len(r) else "") for i, h in enumerate(headers)}
            for r in rows]


def _val(row: Dict[str, str], *names: str) -> str:
    """First non-empty value among ``names``, stripped of Markdown code ticks."""
    for n in names:
        v = (row.get(n) or "").strip().strip("`").strip()
        if v:
            return v
    return ""


def _fenced_lines(region: str) -> List[str]:
    """Lines of the first ```` ```text ```` fenced block in ``region``."""
    m = re.search(r"^```text\s*$", region, re.M)
    if not m:
        return []
    rest = region[m.end():]
    end = re.search(r"^```\s*$", rest, re.M)
    body = rest[:end.start()] if end else rest
    return [ln for ln in body.splitlines() if ln.strip()]


def _cluster(items: List[Tuple[datetime, object]], gap_s: int
             ) -> List[List[Tuple[datetime, object]]]:
    """Split time-sorted ``(ts, payload)`` pairs whenever the idle gap exceeds
    ``gap_s``."""
    out: List[List[Tuple[datetime, object]]] = []
    cur: List[Tuple[datetime, object]] = []
    last: Optional[datetime] = None
    for ts, payload in sorted(items, key=lambda x: x[0]):
        if cur and last is not None and (ts - last).total_seconds() > gap_s:
            out.append(cur)
            cur = []
        cur.append((ts, payload))
        last = ts
    if cur:
        out.append(cur)
    return out


def _anchor_list(times: Sequence[datetime]) -> List[datetime]:
    """Distinct, sorted moments a clustered event publishes as match anchors."""
    return sorted(set(times))


# ---------------------------------------------------------------------------
# nv-bug-report (compute tray)
# ---------------------------------------------------------------------------

def _kv_table(region: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for m in re.finditer(r"^\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*$", region, re.M):
        out[m.group(1).strip()] = m.group(2).strip()
    return out


_IMEX_RE = re.compile(
    r"Event Group (\d+):\s*"
    r"([A-Z][a-z]{2} \d{1,2} \d{4} \d{2}:\d{2}:\d{2})"     # start (with year)
    r"(?:\s*~\s*(.+?))?"                                    # optional end
    r"\s*\((\d+) messages?\)"
)

_XID_GRP_RE = re.compile(
    r"Event Group (\d+):\s*"
    r"([A-Z][a-z]{2} \d{1,2} \d{2}:\d{2}:\d{2})"           # start (syslog, no year)
    r"(?:\s*~\s*([A-Z][a-z]{2} \d{1,2} \d{2}:\d{2}:\d{2}))?"  # optional end
    r"\s*\((\d+) entries"
)

_XID_LINE_RE = re.compile(
    r"NVRM: Xid \(PCI:([0-9A-Fa-f:.]+)\):\s*(\d+),"
    r"(?:[^\n]*?\b([A-Z][A-Z0-9_]{3,})\s+(Fatal|Nonfatal)\b)?"
)


def _imex_end_dt(end_raw: Optional[str], start: datetime) -> datetime:
    if not end_raw:
        return start
    end_raw = end_raw.strip()
    full = T.parse_month_day_year(end_raw)
    if full:
        return full
    m = re.match(r"^(\d{2}):(\d{2}):(\d{2})$", end_raw)  # time-only -> same date
    if m:
        end = start.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                            second=int(m.group(3)))
        if end < start:
            end += timedelta(days=1)
        return end
    return start


def parse_nvbug(path: str, text: str) -> TrayReport:
    rep = TrayReport(path=path)

    sys_region = _slice(text, r"^## 1\. System Overview", (r"^## 2\.",))
    kv = _kv_table(sys_region)
    rep.hostname = kv.get("Hostname", "")
    rep.system_sn = kv.get("System Serial Number", "")
    rep.chassis_sn = kv.get("Chassis Serial Number", "")
    rep.slot = kv.get("Slot Number", "")
    rep.tray_index = kv.get("Tray Index", "")
    rep.collect_date = T.parse_full(kv.get("Date", ""))
    rep.boot_time = T.parse_full(kv.get("Boot Time", ""))
    ref = rep.collect_date or rep.boot_time or datetime.now()

    # Section 6: IMEX Node Disconnect Events (timestamps carry a year)
    imex_region = _slice(text, r"^## 6\. IMEX Status", (r"^## 7\.",))
    for m in _IMEX_RE.finditer(imex_region):
        gid, start_raw, end_raw, nmsg = m.groups()
        start = T.parse_month_day_year(start_raw)
        if not start:
            continue
        end = _imex_end_dt(end_raw, start)
        rep.imex_events.append(Event(
            source_kind="compute_tray", source_id=rep.hostname or rep.system_sn,
            chassis=rep.chassis_sn, kind="imex", start=start, end=end,
            label=f"IMEX disconnect ({nmsg} msg)", ref=f"IMEX Event Group {gid}",
        ))

    # Section 7.1: Xid Summary table (per-tray inventory, for enrichment)
    xs_region = _slice(text, r"^### 7\.1 Xid Summary", (r"^### 7\.2",))
    for m in re.finditer(
        r"^\|\s*([0-9A-Fa-f:.]+)\s*\|\s*(\d+)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|"
        r"\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([^|]*?)\s*\|",
        xs_region, re.M,
    ):
        rep.xid_summary.append({
            "bdf": m.group(1), "xid": m.group(2), "category": m.group(3),
            "severity": m.group(4), "total": m.group(5), "primary": m.group(6),
            "derivative": m.group(7), "caused_by": m.group(8),
        })

    # Section 7.3: Xid Raw Logs (syslog stamps, no year -> infer from collect date)
    xid_region = _slice(text, r"^### 7\.3 Xid Raw Logs", (r"^### 7\.4",))
    matches = list(_XID_GRP_RE.finditer(xid_region))
    for i, m in enumerate(matches):
        gid, start_raw, end_raw, nentries = m.groups()
        start = _parse_syslog_str(start_raw, ref)
        if not start:
            continue
        end = _parse_syslog_str(end_raw, ref) if end_raw else start
        if end and end < start:  # crossed a year boundary within the group
            end = end.replace(year=end.year + 1)
        block = xid_region[m.end(): matches[i + 1].start() if i + 1 < len(matches) else len(xid_region)]
        related = ""
        rm = re.search(r"Related IMEX Event Groups\*\*:\s*(.+)", block)
        if rm:
            related = rm.group(1).strip()
        rep.xid_events.append(Event(
            source_kind="compute_tray", source_id=rep.hostname or rep.system_sn,
            chassis=rep.chassis_sn, kind="xid", start=start, end=end or start,
            label=_xid_group_label(block, nentries),
            detail=(f"related IMEX: {related}" if related else ""),
            ref=f"Xid Event Group {gid}",
            extra={"related_imex": related},
        ))
    return rep


def _parse_syslog_str(s: Optional[str], ref: datetime) -> Optional[datetime]:
    if not s:
        return None
    m = re.match(r"^([A-Z][a-z]{2}) (\d{1,2}) (\d{2}:\d{2}:\d{2})$", s.strip())
    if not m:
        return None
    return T.parse_syslog(m.group(1), m.group(2), m.group(3), ref)


def _xid_group_label(block: str, nentries: str) -> str:
    """Summarize the distinct primary Xid numbers/mnemonics inside a group block."""
    seen: List[str] = []
    bdfs = set()
    for lm in _XID_LINE_RE.finditer(block):
        bdf, num, mnem, sev = lm.groups()
        bdfs.add(bdf)
        tag = f"Xid {num}" + (f" {mnem} {sev}" if mnem else "")
        if tag not in seen:
            seen.append(tag)
    head = "; ".join(seen[:4]) if seen else f"{nentries} Xid entries"
    if len(seen) > 4:
        head += f"; +{len(seen) - 4} more"
    return f"{head} ({len(bdfs)} GPU BDF)" if bdfs else head


# ---------------------------------------------------------------------------
# NVOS / NMX-C dump (switch)
# ---------------------------------------------------------------------------

_NODE_TITLE_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
_NVOS_GRP_RE = re.compile(
    r"Event group (\d+):\s*"
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*[–\-]\s*"
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
)

# Section boundaries inside one node section.
_NVLSM_SECTION = r"^### NVLSM & FM log checks"
_HIGHLIGHTS_SECTION = r"^### Other FabricManager Log Highlights"


def _nvos_node_identity(title: str) -> Tuple[str, str]:
    """(chassis, hostname) from a node title like
    ``1821425180267-Slot 9: CGK3A-...-U19 - 10.x/26 / 10.y/24``."""
    chassis = ""
    hostname = ""
    m = re.match(r"^(\d+)-Slot\s+\d+:\s*(\S+)", title)
    if m:
        chassis, hostname = m.group(1), m.group(2)
    else:
        m2 = re.search(r":\s*(\S+)", title)
        if m2:
            hostname = m2.group(1)
    return chassis, hostname


def parse_nvos(path: str, text: str,
               cluster_gap_s: int = DEFAULT_CLUSTER_GAP_S) -> SwitchReport:
    rep = SwitchReport(path=path)
    # Split into node sections at level-2 headings (skip the doc H1).
    title_positions = [(m.start(), m.group(1)) for m in _NODE_TITLE_RE.finditer(text)]
    for idx, (pos, title) in enumerate(title_positions):
        end = title_positions[idx + 1][0] if idx + 1 < len(title_positions) else len(text)
        section = text[pos:end]
        chassis, hostname = _nvos_node_identity(title)
        node = SwitchNode(title=title, hostname=hostname, chassis=chassis)

        nvlsm_region = _slice(section, _NVLSM_SECTION, (r"^### ",))
        highlights = _slice(section, _HIGHLIGHTS_SECTION, (r"^## ",))

        _parse_nvos_port_state(node, section)
        _parse_nvos_fm_outside(node, nvlsm_region, cluster_gap_s)
        _parse_nvos_health(node, nvlsm_region)
        _parse_nvos_fnm(node, highlights)
        _parse_nvos_raw_blocks(node, highlights, cluster_gap_s)
        _parse_nvos_lifecycle(node, highlights)

        node.events.sort(key=lambda e: e.start)
        rep.nodes.append(node)
    return rep


def _mk(node: SwitchNode, **kw) -> Event:
    return Event(source_kind="switch", source_id=node.hostname or node.title[:24],
                 chassis=node.chassis, **kw)


# -- port-state event groups -------------------------------------------------

def _parse_nvos_port_state(node: SwitchNode, section: str) -> None:
    region = _slice(section, r"^#### Port state event groups",
                    (r"^### ", r"^#### GPU Node Mapping"))
    if not region:
        return
    matches = list(_NVOS_GRP_RE.finditer(region))
    # Track which severity sub-bucket we are in as we walk the region.
    sev_markers = [(m.start(), _sev_of(m.group(0)))
                   for m in re.finditer(r"Event groups with Xid \((nvl_fatal|nvl_non_fatal)\) events"
                                        r"|Event groups without Xid events", region)]
    for i, m in enumerate(matches):
        gid, start_raw, end_raw = m.groups()
        start = T.parse_full(start_raw)
        end = T.parse_full(end_raw)
        if not start:
            continue
        severity = _sev_at(sev_markers, m.start())
        blk_end = matches[i + 1].start() if i + 1 < len(matches) else len(region)
        block = region[m.end():blk_end]
        ad = len(re.findall(r"ACTIVE→DOWN", block))
        di = len(re.findall(r"DOWN→INIT", block))
        fm_header, fm_rows, fm_total = _parse_fm_table(block)
        node.events.append(_mk(
            node, kind="port_state", start=start, end=end or start,
            label=f"port-state group [{severity}]",
            detail=f"ACTIVE→DOWN x{ad}, DOWN→INIT x{di}",
            ref=f"nvos event group {gid}",
            extra={"severity": severity, "fm_header": fm_header,
                   "fm_rows": fm_rows, "fm_total": fm_total},
        ))


def _parse_fm_table(block: str):
    """Extract the nested 'Fabric Manager log (same time window)' markdown table
    from one port-state event-group block. Returns (header, rows, total) where
    rows is [(cells, count)] deduped by identical full row, first-seen order.

    The read is bounded to that one table: the *last* event group's block runs to
    the end of the section, which also holds the two 'Fabric Manager log
    before/after earliest NVLSM event' tables — those belong to ``fm_outside``,
    not to the group.
    """
    pos = _summary_pos(block, "Fabric Manager log (same time window)")
    if pos < 0:
        return [], [], 0
    header, rows = _read_table(block, pos)
    if not header:
        return [], [], 0
    return (header,) + _dedupe_rows(rows)


def _dedupe_rows(rows) -> Tuple[List[Tuple[List[str], int]], int]:
    """Collapse identical rows: ``([(cells, count)], total)`` in first-seen order."""
    counts: Dict[tuple, int] = {}
    order: List[tuple] = []
    total = 0
    for cv in rows:
        if _is_separator(cv):
            continue
        total += 1
        key = tuple(cv)
        if key not in counts:
            counts[key] = 0
            order.append(key)
        counts[key] += 1
    return [(list(k), counts[k]) for k in order], total


def _sev_of(marker: str) -> str:
    if "nvl_fatal" in marker:
        return "nvl_fatal"
    if "nvl_non_fatal" in marker:
        return "nvl_non_fatal"
    return "none"


def _sev_at(markers: List[Tuple[int, str]], pos: int) -> str:
    sev = "none"
    for mp, s in markers:
        if mp <= pos:
            sev = s
        else:
            break
    return sev


# -- Fabric Manager rows outside every port-state group ----------------------

_FM_OUTSIDE_SUMMARIES = (
    ("Fabric Manager log before earliest NVLSM event", "before earliest NVLSM event"),
    ("Fabric Manager log after earliest NVLSM event", "outside port-state groups"),
)


def _parse_nvos_fm_outside(node: SwitchNode, region: str, gap_s: int) -> None:
    """Cluster the FM rows the nvos report keeps *outside* the port-state groups.

    These two tables are where a Fabric-Manager NVLink error lands when NVLSM
    logged no matching port transition — exactly the case the port-state-only
    correlation used to miss.
    """
    if not region:
        return
    for needle, bucket in _FM_OUTSIDE_SUMMARIES:
        pos = _summary_pos(region, needle)
        if pos < 0:
            continue
        headers, rows = _read_table(region, pos)
        if not headers or not rows:
            continue
        ti = headers.index("Time") if "Time" in headers else 0
        ci = headers.index("Category") if "Category" in headers else -1
        stamped: List[Tuple[datetime, List[str]]] = []
        for cv in rows:
            ts = T.parse_full(cv[ti] if ti < len(cv) else "")
            if ts:
                stamped.append((ts, cv))
        for group in _cluster(stamped, gap_s):
            times = [ts for ts, _ in group]
            cats: Dict[str, int] = {}
            for _ts, cv in group:
                cat = (cv[ci] if 0 <= ci < len(cv) else "") or "-"
                cats[cat] = cats.get(cat, 0) + 1
            fm_rows, total = _dedupe_rows(cv for _ts, cv in group)
            breakdown = ", ".join(f"{c} x{n}" for c, n in
                                  sorted(cats.items(), key=lambda kv: -kv[1]))
            node.events.append(_mk(
                node, kind="fm_outside", start=times[0], end=times[-1],
                label=f"FM log {bucket} ({total} row(s))",
                detail=breakdown,
                ref=f"nvos FM log {bucket} @ {T.fmt(times[0])}",
                extra={"severity": worst_severity(cats),
                       "bucket": bucket, "categories": cats,
                       "fm_header": headers, "fm_rows": fm_rows, "fm_total": total,
                       "anchor_list": _anchor_list(times)},
            ))


# -- NVLSM health checks -----------------------------------------------------

def _parse_nvos_health(node: SwitchNode, region: str) -> None:
    """The ``| Check | Count | Earliest | Latest |`` table.

    Only earliest/latest are published, so the event carries exactly those two
    anchors — enough to flag an Xid landing on the first or last occurrence.
    """
    if not region:
        return
    headers, rows = _read_table(region, 0)
    if not headers or headers[:2] != ["Check", "Count"]:
        return
    for cv in rows:
        row = {h: (cv[i] if i < len(cv) else "") for i, h in enumerate(headers)}
        try:
            count = int(_val(row, "Count") or "0")
        except ValueError:
            count = 0
        start = T.parse_full(_val(row, "Earliest"))
        if count <= 0 or not start:
            continue
        end = T.parse_full(_val(row, "Latest")) or start
        check = _val(row, "Check") or "NVLSM check"
        node.events.append(_mk(
            node, kind="nvlsm_health", start=start, end=end,
            label=f"{check}: {count} occurrence(s)",
            detail=f"earliest {T.fmt(start)}, latest {T.fmt(end)}",
            ref=f"nvos NVLSM health / {check}",
            extra={"severity": "nvlsm_check", "check": check, "count": count},
        ))


# -- FNM port loss (three tables) --------------------------------------------

def _parse_nvos_fnm(node: SwitchNode, region: str) -> None:
    if not region:
        return

    for row in _table_under(region, "Fabricmanager logs which report FNM port loss events"):
        ts = T.parse_full(_val(row, "FM Time"))
        if not ts:
            continue
        port = _val(row, "port num") or "-"
        guid = _val(row, "node GUID") or "-"
        host = _val(row, "NVOS hostname")
        down = _val(row, "Down Details")
        recovered = _val(row, "Recovered Time")
        node.events.append(_mk(
            node, kind="fnm_port_loss", start=ts, end=ts,
            label=f"FNM port {port} loss ({down or '-'})",
            detail=f"node GUID {guid}; peer host {host or '-'}",
            ref="Other FM Highlights / FNM port loss",
            extra={"severity": "port_loss", "port": port, "peer_host": host,
                   "guid": guid, "down": down, "recovered": recovered,
                   "line": _val(row, "related log line number")},
        ))

    _parse_fnm_nvlsm_table(
        node, region, "Unmatched nvlsm loss", "fnm_nvlsm_unmatched")
    _parse_fnm_nvlsm_table(
        node, region, "nvlsm logs which can not linked to FM FNM port loss event",
        "fnm_nvlsm_recovery")


def _parse_fnm_nvlsm_table(node: SwitchNode, region: str, needle: str,
                           kind: str) -> None:
    for row in _table_under(region, needle):
        ts = T.parse_full(_val(row, "Time"))
        if not ts:
            continue
        port = _val(row, "port") or "-"
        name = _val(row, "port name")
        guid = _val(row, "Switch GUID") or "-"
        host = _val(row, "NVOS hostname")
        transition = _val(row, "transition") or "-"
        reason = _val(row, "reason")
        node.events.append(_mk(
            node, kind=kind, start=ts, end=ts,
            label=f"FNM port {port}{f' ({name})' if name else ''} {transition}",
            detail=f"switch GUID {guid}; host {host or '-'}"
                   + (f"; {reason}" if reason else ""),
            ref=f"Other FM Highlights / {needle}",
            extra={"severity": "port_loss", "port": port, "port_name": name,
                   "guid": guid, "peer_host": host, "down": transition,
                   "recovered": "", "reason": reason,
                   "line": _val(row, "nvlsm line", "related log line number")},
        ))


# -- raw Fabric-Manager highlight blocks -------------------------------------

_RAW_BLOCKS = (
    ("Failed to get switch info", "switch_info_failure", "switch_info_failed"),
    ("Partition unexpected error state", "partition_error", "partition_error"),
    ("Multicast team limit reached", "multicast_limit", "multicast_limit"),
)

_RAW_TS_RE = re.compile(r"^\[([A-Z][a-z]{2} \d{1,2} \d{4} \d{2}:\d{2}:\d{2})\]")


def _parse_nvos_raw_blocks(node: SwitchNode, region: str, gap_s: int) -> None:
    """Cluster the ``[Mon DD YYYY HH:MM:SS] …`` raw FM highlight blocks."""
    if not region:
        return
    for heading, kind, severity in _RAW_BLOCKS:
        block = _slice(region, r"^#### " + re.escape(heading), (r"^#### ", r"^### "))
        if not block:
            continue
        stamped: List[Tuple[datetime, str]] = []
        for line in _fenced_lines(block):
            m = _RAW_TS_RE.match(line.strip())
            if not m:
                continue
            ts = T.parse_bracket(m.group(1))
            if ts:
                stamped.append((ts, line.strip()))
        for group in _cluster(stamped, gap_s):
            times = [ts for ts, _ in group]
            lines = [ln for _ts, ln in group]
            node.events.append(_mk(
                node, kind=kind, start=times[0], end=times[-1],
                label=f"{heading} ({len(lines)} line(s))",
                detail=_first_distinct(lines),
                ref=f"Other FM Highlights / {heading} @ {T.fmt(times[0])}",
                extra={"severity": severity, "topic": heading, "lines": lines,
                       "anchor_list": _anchor_list(times)},
            ))


def _first_distinct(lines: List[str], limit: int = 2) -> str:
    """Short digest of a raw block: the first ``limit`` distinct message bodies."""
    seen: List[str] = []
    for ln in lines:
        body = _RAW_TS_RE.sub("", ln).strip()
        body = re.sub(r"^\[[A-Z]+\]\s*\[tid \d+\]\s*", "", body)
        body = re.sub(r"\b0x[0-9a-fA-F]+\b", "0x…", body)
        if body and body not in seen:
            seen.append(body)
        if len(seen) >= limit:
            break
    return "; ".join(seen)


# -- Fabric Manager lifecycle ------------------------------------------------

def _parse_nvos_lifecycle(node: SwitchNode, region: str) -> None:
    for row in _table_under(region, "FM lifecycle events"):
        ts = T.parse_full(_val(row, "Time"))
        if not ts:
            continue
        etype = (_val(row, "Type") or "?").replace("**", "").strip()
        node.events.append(_mk(
            node, kind="fm_lifecycle", start=ts, end=ts,
            label=f"Fabric Manager {etype}",
            detail=_val(row, "Message"),
            ref="Other FM Highlights / FM lifecycle",
            extra={"severity": "lifecycle", "type": etype,
                   "message": _val(row, "Message")},
        ))


# ---------------------------------------------------------------------------
# nv-bug-report cross-node comparison report
# ---------------------------------------------------------------------------

_XREF_GRP_RE = re.compile(
    r"Event Group (\d+):\s*"
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"                               # start (full)
    r"(?:\s*~\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}|\d{2}:\d{2}:\d{2}))?"  # optional end
)


def _xref_end(e_raw: Optional[str], start: datetime) -> datetime:
    if not e_raw:
        return start
    full = T.parse_full(e_raw)
    if full:
        return full
    m = re.match(r"^(\d{2}):(\d{2}):(\d{2})$", e_raw)  # time-only -> same date
    if m:
        end = start.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                            second=int(m.group(3)))
        if end < start:
            end += timedelta(days=1)
        return end
    return start


def _xref_groups(region: str) -> List[Tuple[int, datetime, datetime]]:
    out: List[Tuple[int, datetime, datetime]] = []
    for m in _XREF_GRP_RE.finditer(region):
        gid, s_raw, e_raw = m.groups()
        s = T.parse_full(s_raw)
        if not s:
            continue
        out.append((int(gid), s, _xref_end(e_raw, s)))
    return out


def _distinct_xids(block: str) -> List[Tuple[str, str, str, str, List[str]]]:
    """From one cross-node Xid event-group block's raw-log table, return distinct
    ``(xid, mnemonic, severity, example, hostnames)`` — deduped across nodes by
    signature. The cross-node §4 table has a ``Hostname`` column, so every tray
    that reported a given signature is collected; the first NVRM line is kept as
    the example (node prefix collapsed)."""
    out: List[Tuple[str, str, str, str, List[str]]] = []
    index: Dict[Tuple[str, str, str], int] = {}
    for line in block.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = _cells(s)
        if len(cells) < 5:
            continue
        host = cells[1].strip("` ")
        raw = cells[4].strip().strip("`")
        lm = _XID_LINE_RE.search(raw)
        if not lm:                       # header / separator / suppressed-summary rows
            continue
        _bdf, xid, mnem, sev = lm.groups()
        key = (xid, mnem or "", sev or "")
        if key not in index:
            index[key] = len(out)
            out.append((xid, mnem or "", sev or "", " ".join(raw.split()), []))
        hosts = out[index[key]][4]
        if host and host not in hosts:
            hosts.append(host)
    return out


_SUPPRESS_RE = re.compile(r"\+\s*\d+\s+more\b.*?suppressed", re.I)


def _suppressed_rows(block: str) -> List[Tuple[str, str]]:
    """Collect the cross-node §4 '+N more … suppressed' derivative-collapse rows:
    ``[(hostname, summary_text), ...]`` (e.g. hostname + "+5438 more derivative
    Xid 45 entries (caused by Xid 145) suppressed")."""
    out: List[Tuple[str, str]] = []
    for line in block.splitlines():
        s = line.strip()
        if not s.startswith("|") or "suppressed" not in s:
            continue
        cells = _cells(s)
        if len(cells) < 5:
            continue
        m = _SUPPRESS_RE.search(cells[4])
        if m:
            out.append((cells[1].strip("` "), m.group(0).strip()))
    return out


def _parse_xref_xid_timeline(region: str):
    """Return (groups, details, suppressed) for §4: groups=[(gid,start,end)],
    details={gid: [(xid, mnemonic, severity, example, hosts), ...]},
    suppressed={gid: [(hostname, '+N more … suppressed'), ...]}."""
    groups: List[Tuple[int, datetime, datetime]] = []
    details: Dict[int, List[Tuple[str, str, str, str, List[str]]]] = {}
    suppressed: Dict[int, List[Tuple[str, str]]] = {}
    matches = list(_XREF_GRP_RE.finditer(region))
    for i, m in enumerate(matches):
        gid_s, s_raw, e_raw = m.groups()
        s = T.parse_full(s_raw)
        if not s:
            continue
        gid = int(gid_s)
        groups.append((gid, s, _xref_end(e_raw, s)))
        blk_end = matches[i + 1].start() if i + 1 < len(matches) else len(region)
        blk = region[m.end():blk_end]
        details[gid] = _distinct_xids(blk)
        sup = _suppressed_rows(blk)
        if sup:
            suppressed[gid] = sup
    return groups, details, suppressed


def parse_nvbug_cross(path: str, text: str) -> CrossNodeReport:
    """Parse the nv-bug-report cross-node comparison report's merged timelines
    (§2 IMEX Node Disconnect Timeline, §4 Xid Unified Timeline)."""
    rep = CrossNodeReport(path=path)
    rep.imex_groups = _xref_groups(
        _slice(text, r"^## 2\. IMEX Node Disconnect Timeline", (r"^## 3\.",)))
    rep.xid_groups, rep.xid_details, rep.xid_suppressed = _parse_xref_xid_timeline(
        _slice(text, r"^## 4\. Xid Unified Timeline", (r"^## 5\.",)))
    return rep


def parse_report(path: str, cluster_gap_s: int = DEFAULT_CLUSTER_GAP_S):
    """Parse one report file; return ('nvbug', TrayReport) / ('nvos', SwitchReport)
    / ('nvbug_cross', CrossNodeReport) / (None, None)."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    kind = classify(text)
    if kind == "nvbug":
        return kind, parse_nvbug(path, text)
    if kind == "nvbug_cross":
        return kind, parse_nvbug_cross(path, text)
    if kind == "nvos":
        return kind, parse_nvos(path, text, cluster_gap_s)
    return None, None
