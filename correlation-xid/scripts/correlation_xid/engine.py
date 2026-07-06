"""Correlation engine: line up compute-tray and switch events in time.

Compute-tray events (Xid / IMEX) come from nv-bug-report reports; switch events
(port-state / FNM port loss) come from NVOS dump reports. Both are local
wall-clock with no timezone marker, so a minute ``offset`` is applied to the
switch side before comparing.

Matching is by **anchor proximity**, not interval overlap: each event is reduced
to its discrete anchor moments — its ``start`` and (if different) its ``end``.
This matters because a port-state group can lump an ACTIVE→DOWN and its recovery
tens of days apart into one group; treating that whole span as "active" would
spuriously match everything in between. Two events correlate when any anchor of
one is within ``window_s`` of any anchor of the other (after the offset).
``suggest_offsets`` sweeps candidate offsets and scores each by anchor hits so
the user can pick the right timezone delta.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from .models import Event, SwitchReport, TrayReport


@dataclass
class Correlation:
    compute: Event
    switches: List[Tuple[Event, int]] = field(default_factory=list)  # (event, delta_s)


@dataclass
class Result:
    offset_min: int
    window_s: int
    correlations: List[Correlation]
    unmatched_compute: List[Event]
    matched_switch: List[Event]
    total_switch: int
    total_compute: int
    chassis_scoped: bool
    suggestions: List[Tuple[int, int]]  # (offset_min, anchor_hits), best first


def anchors(e: Event) -> List[datetime]:
    """Discrete moments an event represents: its start, and its end if distinct."""
    if e.end and e.end != e.start:
        return [e.start, e.end]
    return [e.start]


def _same_chassis(a: Event, b: Event, scoped: bool) -> bool:
    if not scoped:
        return True
    if a.chassis and b.chassis:
        return a.chassis == b.chassis
    return True  # unknown chassis on either side -> don't exclude


def _min_delta_s(ce: Event, se: Event, off: timedelta) -> Optional[int]:
    """Smallest |Δ| in seconds between any compute anchor and any (shifted) switch
    anchor."""
    best: Optional[float] = None
    for ca in anchors(ce):
        for sa in anchors(se):
            d = abs((ca - (sa + off)).total_seconds())
            if best is None or d < best:
                best = d
    return int(best) if best is not None else None


def gather_compute(trays: List[TrayReport]) -> List[Event]:
    out: List[Event] = []
    for t in trays:
        out.extend(t.all_events())
    out.sort(key=lambda e: (0 if e.kind == "xid" else 1, e.start))
    return out


def gather_switch(switches: List[SwitchReport]) -> List[Event]:
    out: List[Event] = []
    for s in switches:
        out.extend(s.all_events())
    out.sort(key=lambda e: e.start)
    return out


def suggest_offsets(compute: List[Event], switch: List[Event], window_s: int,
                    scoped: bool, grid_minutes: Optional[List[int]] = None,
                    ) -> List[Tuple[int, int]]:
    """Score each candidate offset by how many switch anchors land within
    ``window_s`` of a compute anchor. Returns ``(offset_min, hits)`` best first."""
    if not compute or not switch:
        return []
    if grid_minutes is None:
        s = {0}
        for step in range(-26, 27):   # ±13h at 30-min steps (covers half-hour zones)
            s.add(step * 30)
        grid_minutes = sorted(s)

    tol = window_s
    c_anch = sorted(a.timestamp() for e in compute for a in anchors(e))
    s_anch = [a.timestamp() for e in switch for a in anchors(e)]
    scored: List[Tuple[int, int]] = []
    for off in grid_minutes:
        off_s = off * 60
        hits = 0
        for t0 in s_anch:
            t = t0 + off_s
            lo = bisect.bisect_left(c_anch, t - tol)
            hi = bisect.bisect_right(c_anch, t + tol)
            hits += hi - lo
        scored.append((off, hits))
    scored.sort(key=lambda x: (-x[1], abs(x[0])))
    return scored


def correlate(trays: List[TrayReport], switches: List[SwitchReport],
              offset_min: int, window_s: int, scoped: bool = True) -> Result:
    compute = gather_compute(trays)
    switch = gather_switch(switches)
    off = timedelta(minutes=offset_min)

    correlations: List[Correlation] = []
    unmatched_compute: List[Event] = []
    matched_ids = set()

    for ce in compute:
        hits: List[Tuple[Event, int]] = []
        for se in switch:
            if not _same_chassis(ce, se, scoped):
                continue
            d = _min_delta_s(ce, se, off)
            if d is not None and d <= window_s:
                hits.append((se, d))
                matched_ids.add(id(se))
        if hits:
            hits.sort(key=lambda x: x[1])
            correlations.append(Correlation(compute=ce, switches=hits))
        else:
            unmatched_compute.append(ce)

    matched_switch = [e for e in switch if id(e) in matched_ids]
    suggestions = suggest_offsets(compute, switch, window_s, scoped)
    return Result(
        offset_min=offset_min, window_s=window_s, correlations=correlations,
        unmatched_compute=unmatched_compute, matched_switch=matched_switch,
        total_switch=len(switch), total_compute=len(compute),
        chassis_scoped=scoped, suggestions=suggestions,
    )
