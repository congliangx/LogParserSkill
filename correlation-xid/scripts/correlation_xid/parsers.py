"""Parse the Markdown reports of the two source skills into the shared model.

nv-bug-report (compute tray)  -> TrayReport   (Xid + IMEX event groups)
NVOS / NMX-C dump (switch)    -> SwitchReport (port-state groups + FNM port loss)

Parsing is by regex over the rendered Markdown (the reports are the contract).
See ``models.py`` for the shapes produced.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from . import timeutil as T
from .models import Event, SwitchNode, SwitchReport, TrayReport

NVBUG_TITLE = "# NVIDIA Bug Report Analysis"
NVOS_TITLE = "# NMX-C Log Analysis Report"


def classify(text: str) -> Optional[str]:
    """Return 'nvbug', 'nvos', or None from a report's leading lines."""
    head = text[:2000]
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


def parse_nvos(path: str, text: str) -> SwitchReport:
    rep = SwitchReport(path=path)
    # Split into node sections at level-2 headings (skip the doc H1).
    title_positions = [(m.start(), m.group(1)) for m in _NODE_TITLE_RE.finditer(text)]
    for idx, (pos, title) in enumerate(title_positions):
        end = title_positions[idx + 1][0] if idx + 1 < len(title_positions) else len(text)
        section = text[pos:end]
        chassis, hostname = _nvos_node_identity(title)
        node = SwitchNode(title=title, hostname=hostname, chassis=chassis)
        _parse_nvos_port_state(node, section)
        _parse_nvos_fnm(node, section)
        rep.nodes.append(node)
    return rep


def _parse_nvos_port_state(node: SwitchNode, section: str) -> None:
    region = _slice(section, r"^#### Port state event groups",
                    (r"^### ", r"^#### GPU Node Mapping"))
    if not region:
        return
    matches = list(_NVOS_GRP_RE.finditer(region))
    severity = "none"
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
        node.port_state_events.append(Event(
            source_kind="switch", source_id=node.hostname or node.title[:24],
            chassis=node.chassis, kind="port_state", start=start, end=end or start,
            label=f"port-state group [{severity}]",
            detail=f"ACTIVE→DOWN x{ad}, DOWN→INIT x{di}",
            ref=f"Port state event group {gid}",
            extra={"severity": severity},
        ))


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


def _parse_nvos_fnm(node: SwitchNode, section: str) -> None:
    region = _slice(section, r"^### Other FabricManager Log Highlights",
                    (r"^## ",))
    if not region:
        return
    # FNM port loss table rows: | FM Time | node GUID | port | in_nvlsm | host | down | ... |
    for m in re.finditer(
        r"^\|\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*\|\s*`?([^|`]+?)`?\s*\|"
        r"\s*(\d+)\s*\|\s*([A-Za-z-]+)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|",
        region, re.M,
    ):
        ts = T.parse_full(m.group(1))
        if not ts:
            continue
        guid, port, in_nvlsm, host, down = (m.group(2).strip(), m.group(3),
                                            m.group(4), m.group(5).strip(), m.group(6).strip())
        node.fnm_events.append(Event(
            source_kind="switch", source_id=node.hostname or node.title[:24],
            chassis=node.chassis, kind="fnm_port_loss", start=ts, end=ts,
            label=f"FNM port {port} loss ({down or '-'})",
            detail=f"node GUID {guid}; peer host {host or '-'}",
            ref="Other FM Highlights / FNM port loss",
            extra={"port": port, "peer_host": host, "guid": guid},
        ))


def parse_report(path: str):
    """Parse one report file; return ('nvbug', TrayReport) / ('nvos', SwitchReport)
    / (None, None)."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    kind = classify(text)
    if kind == "nvbug":
        return kind, parse_nvbug(path, text)
    if kind == "nvos":
        return kind, parse_nvos(path, text)
    return None, None
