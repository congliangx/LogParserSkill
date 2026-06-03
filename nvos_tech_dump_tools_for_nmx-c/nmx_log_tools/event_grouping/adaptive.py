"""Adaptive time-window event grouping (fabric_manager group_utils)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple


def parse_ts(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(ts[:26], fmt)
        except ValueError:
            continue
    return None


def is_fnm_prefixed_port_name(port_name: str) -> bool:
    """True when osm_spst port name starts with FNM (e.g. FNMA2P73)."""
    return (port_name or "").upper().startswith("FNM")


def port_events_for_event_group_report(events: List[Any]) -> List[Any]:
    """Port state events for event-group section: exclude FNM-prefixed port names."""
    return [
        e for e in events
        if not is_fnm_prefixed_port_name(getattr(e, "port_name", ""))
    ]


def port_event_group_key(ev: Any) -> Tuple[int, str]:
    return (int(getattr(ev, "port", 0) or 0), getattr(ev, "port_name", "") or "")


def split_port_lifecycle_episodes(
    port_evs: List[Any],
    lifecycle_pair_max_seconds: int = 120,
) -> List[Dict[str, List[Any]]]:
    """
    Per-port lifecycle rows: ACTIVE→DOWN burst, then DOWN→INIT if within pair window.

    INIT later than lifecycle_pair_max_seconds after this episode's first ACTIVE→DOWN
    is not forced into the same row (AD-only episode, then separate INIT-only episode).
    """
    ordered = sorted(
        port_evs,
        key=lambda e: (
            getattr(e, "datetime", None) or datetime.min,
            int(getattr(e, "line_no", 0) or 0),
        ),
    )
    episodes: List[Dict[str, List[Any]]] = []
    cur_active: List[Any] = []
    cur_init: List[Any] = []
    cur_other: List[Any] = []

    def flush() -> None:
        nonlocal cur_active, cur_init, cur_other
        if not (cur_active or cur_init or cur_other):
            return
        episodes.append({
            "active_down": cur_active,
            "down_init": cur_init,
            "other": cur_other,
        })
        cur_active, cur_init, cur_other = [], [], []

    def first_active_down_time() -> Optional[datetime]:
        if not cur_active:
            return None
        return min(e.datetime for e in cur_active if getattr(e, "datetime", None))

    for ev in ordered:
        old = getattr(ev, "old_state", "") or ""
        new = getattr(ev, "new_state", "") or ""
        if (old, new) == ("ACTIVE", "DOWN"):
            if cur_init or cur_other:
                flush()
            cur_active.append(ev)
        elif (old, new) == ("DOWN", "INIT"):
            ad_first = first_active_down_time()
            if cur_active and ad_first is not None:
                span = (ev.datetime - ad_first).total_seconds()
                if lifecycle_pair_max_seconds > 0 and span > lifecycle_pair_max_seconds:
                    flush()
                    cur_init.append(ev)
                else:
                    cur_init.append(ev)
            else:
                cur_init.append(ev)
        else:
            cur_other.append(ev)
    flush()
    return episodes


@dataclass
class PortLifecycleEpisode:
    port_key: Tuple[int, str]
    active_down: List[Any] = field(default_factory=list)
    down_init: List[Any] = field(default_factory=list)
    other: List[Any] = field(default_factory=list)

    @property
    def events(self) -> List[Any]:
        return self.active_down + self.down_init + self.other

    @property
    def anchor(self) -> datetime:
        return min(e.datetime for e in self.events if getattr(e, "datetime", None))

    @property
    def end(self) -> datetime:
        return max(e.datetime for e in self.events if getattr(e, "datetime", None))

    @property
    def has_active_down(self) -> bool:
        return bool(self.active_down)


def build_port_lifecycle_episodes(
    events: List[Any],
    lifecycle_pair_max_seconds: int = 120,
) -> List[PortLifecycleEpisode]:
    """All ports' lifecycle episodes sorted by anchor time."""
    by_port: Dict[Tuple[int, str], List[Any]] = defaultdict(list)
    for ev in events:
        by_port[port_event_group_key(ev)].append(ev)
    out: List[PortLifecycleEpisode] = []
    for port_key, port_evs in by_port.items():
        for ep in split_port_lifecycle_episodes(port_evs, lifecycle_pair_max_seconds):
            out.append(
                PortLifecycleEpisode(
                    port_key=port_key,
                    active_down=ep["active_down"],
                    down_init=ep["down_init"],
                    other=ep["other"],
                )
            )
    out.sort(key=lambda ep: (ep.anchor, ep.port_key[0], ep.port_key[1]))
    return out


def _episode_starts_new_event_group(
    ep: PortLifecycleEpisode,
    group_start: Optional[datetime],
    group_end: Optional[datetime],
    port_last_episode_end: Dict[Tuple[int, str], datetime],
    *,
    gap_seconds: int,
    port_wave_gap_seconds: int,
) -> bool:
    if group_end is None:
        return False
    if ep.anchor > group_end + timedelta(seconds=gap_seconds):
        return True
    prev_end = port_last_episode_end.get(ep.port_key)
    if (
        prev_end is not None
        and group_start is not None
        and prev_end >= group_start
    ):
        if (ep.anchor - prev_end).total_seconds() > port_wave_gap_seconds:
            return True
    return False


def group_port_events(
    events: List[Any],
    gap_seconds: int = 120,
    max_group_seconds: int = 120,
    *,
    lifecycle_pair_max_seconds: Optional[int] = None,
    port_wave_gap_seconds: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Group port state events into incident time windows (report event groups).

    Layer 1 — lifecycle_pair_max_seconds (default: max_group_seconds):
        Table-row pairing of ACTIVE→DOWN with following DOWN→INIT on the same port.

    Layer 2 — event-group merge:
        - gap_seconds: global idle since last event in the event group.
        - port_wave_gap_seconds: max gap from a port's previous episode end to this
          episode's anchor (INIT→next AD, or any episode boundary on that port).
    """
    pair_max = lifecycle_pair_max_seconds if lifecycle_pair_max_seconds is not None else max_group_seconds
    wave_gap = port_wave_gap_seconds if port_wave_gap_seconds is not None else gap_seconds

    valid = [e for e in events if getattr(e, "datetime", None)]
    if not valid:
        return []
    episodes = build_port_lifecycle_episodes(valid, lifecycle_pair_max_seconds=pair_max)
    if not episodes:
        return []

    event_groups: List[Dict[str, Any]] = []
    current: List[Any] = []
    group_start: Optional[datetime] = None
    group_end: Optional[datetime] = None
    port_last_episode_end: Dict[Tuple[int, str], datetime] = {}

    def flush() -> None:
        nonlocal current, group_start, group_end
        if not current:
            return
        event_groups.append(_summarize_port_event_group(current, group_start, group_end))
        current = []
        group_start = group_end = None

    for ep in episodes:
        if current and _episode_starts_new_event_group(
            ep,
            group_start,
            group_end,
            port_last_episode_end,
            gap_seconds=gap_seconds,
            port_wave_gap_seconds=wave_gap,
        ):
            flush()
        current.extend(ep.events)
        group_start = (
            ep.anchor if group_start is None else min(group_start, ep.anchor)
        )
        group_end = ep.end if group_end is None else max(group_end, ep.end)
        port_last_episode_end[ep.port_key] = ep.end

    flush()
    return event_groups


def _summarize_port_event_group(events: List, start, end) -> Dict[str, Any]:
    down = sum(1 for e in events if e.new_state == "DOWN")
    active = sum(1 for e in events if e.new_state == "ACTIVE")
    switches = {e.switch_name for e in events}
    ports = {f"{e.switch_name}:{e.port}" for e in events}
    return {
        "start_ts": start.strftime("%Y-%m-%d %H:%M:%S") if start else "",
        "end_ts": end.strftime("%Y-%m-%d %H:%M:%S") if end else "",
        "events": events,
        "summary": {
            "total_events": len(events),
            "down_transitions": down,
            "active_transitions": active,
            "switches_affected": len(switches),
            "ports_affected": len(ports),
        },
    }
