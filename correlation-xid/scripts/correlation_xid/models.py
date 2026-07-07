"""Data model shared by the parsers, correlation engine, and renderer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple


@dataclass
class Event:
    """One time-stamped event group extracted from a source report.

    ``start``/``end`` are naive local datetimes as written in the source report
    (year already inferred for syslog stamps). The correlation engine applies any
    timezone offset when comparing across sources.
    """

    source_kind: str          # 'compute_tray' | 'switch'
    source_id: str            # hostname / switch hostname (short identity)
    chassis: str              # chassis serial (rack key), or '' if unknown
    kind: str                 # 'xid' | 'imex' | 'port_state' | 'fnm_port_loss'
    start: datetime
    end: datetime
    label: str                # short human label (e.g. "Xid 145 x4 GPUs")
    detail: str = ""          # longer detail line
    ref: str = ""             # provenance, e.g. "Event Group 1" / md path
    extra: Dict = field(default_factory=dict)


@dataclass
class TrayReport:
    """Parsed compute-tray nv-bug-report Markdown report."""

    path: str
    hostname: str = ""
    system_sn: str = ""
    chassis_sn: str = ""
    slot: str = ""
    tray_index: str = ""
    collect_date: Optional[datetime] = None
    boot_time: Optional[datetime] = None
    xid_events: List[Event] = field(default_factory=list)
    imex_events: List[Event] = field(default_factory=list)
    xid_summary: List[Dict[str, str]] = field(default_factory=list)

    def all_events(self) -> List[Event]:
        return self.xid_events + self.imex_events


@dataclass
class SwitchReport:
    """Parsed NVOS / NMX-C dump Markdown report (may hold several node sections)."""

    path: str
    nodes: List["SwitchNode"] = field(default_factory=list)

    def all_events(self) -> List[Event]:
        out: List[Event] = []
        for n in self.nodes:
            out.extend(n.all_events())
        return out


@dataclass
class SwitchNode:
    """One nvos node section (one switch) inside a dump report."""

    title: str = ""
    hostname: str = ""
    chassis: str = ""
    port_state_events: List[Event] = field(default_factory=list)
    fnm_events: List[Event] = field(default_factory=list)

    def all_events(self) -> List[Event]:
        return self.port_state_events + self.fnm_events


@dataclass
class CrossNodeReport:
    """Parsed nv-bug-report cross-node comparison report (aggregate).

    Holds the merged timeline event groups so the correlation can cite the
    cross-node Xid / IMEX event-group numbers instead of the per-node ones.
    Each group is ``(gid, start, end)``.
    """

    path: str
    xid_groups: List[Tuple[int, datetime, datetime]] = field(default_factory=list)
    imex_groups: List[Tuple[int, datetime, datetime]] = field(default_factory=list)
    # {xid_gid: [(xid, mnemonic, severity, example_nvrm_line, [hostnames]), ...]}
    # node-deduped by signature; hostnames = every compute tray that reported it.
    xid_details: Dict[int, List[Tuple[str, str, str, str, List[str]]]] = field(default_factory=dict)
    # {xid_gid: [(hostname, "+N more derivative Xid X (caused by Xid Y) suppressed"), ...]}
    xid_suppressed: Dict[int, List[Tuple[str, str]]] = field(default_factory=dict)
