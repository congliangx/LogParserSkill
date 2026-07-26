"""Data model shared by the parsers, correlation engine, and renderer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Switch-side (NVOS / NMX-C) event taxonomy
#
# Every time-stamped section of the nvos report becomes one of these kinds, so
# the correlation joins compute-tray Xid/IMEX against *all* switch evidence and
# not only the port-state groups. Order is the reporting order (most to least
# diagnostic).
# ---------------------------------------------------------------------------

SWITCH_KIND_LABEL: Dict[str, str] = {
    "port_state": "NVLSM port-state event group",
    "fm_outside": "Fabric Manager log outside port-state groups",
    "fnm_port_loss": "FNM port loss (Fabric Manager)",
    "fnm_nvlsm_unmatched": "FNM loss seen only in NVLSM",
    "fnm_nvlsm_recovery": "FNM NVLSM transition not linked to FM",
    "switch_info_failure": "Failed to get switch info",
    "partition_error": "Partition unexpected error state",
    "multicast_limit": "Multicast team limit reached",
    "fm_lifecycle": "Fabric Manager lifecycle (start/stop/restart)",
    "nvlsm_health": "NVLSM health check (invalid topology / UTF-8)",
}

SWITCH_KIND_ORDER: List[str] = list(SWITCH_KIND_LABEL)

# Short tag used in chips / sidebar / evidence tables.
SWITCH_KIND_TAG: Dict[str, str] = {
    "port_state": "PORT",
    "fm_outside": "FM",
    "fnm_port_loss": "FNM",
    "fnm_nvlsm_unmatched": "FNMU",
    "fnm_nvlsm_recovery": "FNMR",
    "switch_info_failure": "SWINFO",
    "partition_error": "PART",
    "multicast_limit": "MCAST",
    "fm_lifecycle": "LIFE",
    "nvlsm_health": "HEALTH",
}

FNM_KINDS = ("fnm_port_loss", "fnm_nvlsm_unmatched", "fnm_nvlsm_recovery")
RAW_KINDS = ("switch_info_failure", "partition_error", "multicast_limit")

# Severity ordering shared by parsers (picking a cluster's worst category) and
# the renderer (card rail colour). Keys are Fabric-Manager categories plus the
# synthetic ones the parser assigns to non-FM sections.
SEVERITY_RANK: Dict[str, int] = {
    "nvl_fatal": 6,
    "nvl_non_fatal": 5,
    "switch_info_failed": 4,
    "partition_error": 4,
    "multicast_limit": 4,
    "nvlsm_check": 4,
    "port_loss": 3,
    "connection_lost": 2,
    "lifecycle": 1,
    "none": 0,
    "": 0,
}


def worst_severity(sevs) -> str:
    """Highest-ranking severity string in ``sevs`` (``'none'`` when empty)."""
    best, rank = "none", -1
    for s in sevs:
        r = SEVERITY_RANK.get(s or "none", 3)
        if r > rank:
            best, rank = (s or "none"), r
    return best


@dataclass
class Event:
    """One time-stamped event group extracted from a source report.

    ``start``/``end`` are naive local datetimes as written in the source report
    (year already inferred for syslog stamps). The correlation engine applies any
    timezone offset when comparing across sources.

    ``extra['anchor_list']``, when present, replaces the default ``start``/``end``
    anchor pair — clustered sources (a Fabric-Manager burst, a raw-log block)
    carry every distinct moment they contain so a compute event landing in the
    middle of the cluster still matches.
    """

    source_kind: str          # 'compute_tray' | 'switch'
    source_id: str            # hostname / switch hostname (short identity)
    chassis: str              # chassis serial (rack key), or '' if unknown
    kind: str                 # 'xid' | 'imex' | one of SWITCH_KIND_ORDER
    start: datetime
    end: datetime
    label: str                # short human label (e.g. "Xid 145 x4 GPUs")
    detail: str = ""          # longer detail line
    ref: str = ""             # provenance, e.g. "Event Group 1" / md path
    extra: Dict = field(default_factory=dict)

    @property
    def severity(self) -> str:
        return self.extra.get("severity", "none") or "none"


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
    """One nvos node section (one switch) inside a dump report.

    Every time-stamped section of the node lands in the single ``events`` list,
    tagged by ``Event.kind`` (see ``SWITCH_KIND_ORDER``).
    """

    title: str = ""
    hostname: str = ""
    chassis: str = ""
    events: List[Event] = field(default_factory=list)

    def of_kind(self, *kinds: str) -> List[Event]:
        wanted = set(kinds)
        return [e for e in self.events if e.kind in wanted]

    @property
    def port_state_events(self) -> List[Event]:
        return self.of_kind("port_state")

    @property
    def fnm_events(self) -> List[Event]:
        return self.of_kind(*FNM_KINDS)

    def all_events(self) -> List[Event]:
        return list(self.events)


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
