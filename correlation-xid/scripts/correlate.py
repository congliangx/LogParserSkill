#!/usr/bin/env python3
"""
Correlate compute-tray (nv-bug-report) and NVOS/NMX-C dump reports by time.

Reads the Markdown reports produced by the analyze-nv-bug-report and
nvos-tech-dump-tools-for-nmx-c skills, extracts time-stamped event groups
(compute: Xid + IMEX; switch: port-state + FNM port loss), and reports events
that overlap in time — accounting for a timezone offset between the two sources.

Usage:
  python correlate.py <report.md | dir> [more ...] -o OUT
      [--tz-offset-minutes N] [--auto-tz | --interactive-tz]
      [--window-seconds S] [--cross-chassis]

Inputs may be individual report .md files and/or directories (scanned for
*.md). Each file is auto-classified as nv-bug-report or NVOS; anything else
(cross-node / rack-comparison / this tool's own output) is ignored.

Timezone selection (recorded in the report header):
  --tz-offset-minutes N   manual offset (minutes added to switch timestamps)
  --auto-tz               pick the sweep's best-scoring offset automatically
  --interactive-tz        sweep like --auto-tz, print the top candidates, then
                          ask on stdin: Enter accepts the proposal, or type an
                          offset (integer minutes or ±HH:MM). EOF / empty input
                          accepts the proposal, so non-interactive runs are safe.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import List, Tuple

from correlation_xid.engine import correlate, gather_compute, gather_switch, suggest_offsets
from correlation_xid.models import CrossNodeReport, SwitchReport, TrayReport
from correlation_xid.parsers import parse_report
from correlation_xid.render import build_report


def _discover_md(inputs: List[str]) -> List[str]:
    files: List[str] = []
    seen = set()
    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            found = sorted(p.rglob("*.md"))
        elif p.is_file() and p.suffix.lower() == ".md":
            found = [p]
        else:
            print(f"Warning: not a .md file or directory, skipping: {p}", file=sys.stderr)
            found = []
        for f in found:
            r = str(f.resolve())
            if r not in seen:
                seen.add(r)
                files.append(str(f))
    return files


_OFFSET_HHMM = re.compile(r"([+-])?(\d{1,2}):([0-5]\d)$")
_MAX_OFFSET_MIN = 26 * 60   # sanity bound, matches the sweep's ±13h grid twice over


def _parse_offset_text(s: str) -> int:
    """Parse a user-entered offset: integer minutes (e.g. ``60`` / ``-480``) or
    ``±HH:MM`` (e.g. ``+01:00``). Raises ValueError on anything else."""
    s = s.strip()
    m = _OFFSET_HHMM.match(s)
    if m:
        v = int(m.group(2)) * 60 + int(m.group(3))
        v = -v if m.group(1) == "-" else v
    else:
        v = int(s)   # ValueError propagates
    if abs(v) > _MAX_OFFSET_MIN:
        raise ValueError(f"offset out of range (±{_MAX_OFFSET_MIN} min)")
    return v


def _confirm_offset(suggestions, proposed: int, err=sys.stderr) -> Tuple[int, str]:
    """Interactive timezone confirmation (--interactive-tz).

    Prints the top sweep candidates, then reads stdin: empty input / EOF accepts
    ``proposed`` (so piped or unattended runs never hang — stdin EOF falls back
    to the auto behavior), anything else must parse as an offset. Returns
    ``(offset_minutes, tz_mode)``.
    """
    if suggestions:
        err.write("[interactive-tz] Top offset candidates (switch → tray):\n")
        for off, hits in suggestions[:5]:
            mark = "  ◀ proposed" if off == proposed else ""
            err.write(f"    {off:+5d} min ({off // 60:+03d}:{abs(off) % 60:02d})"
                      f"  {hits:6d} aligned hits{mark}\n")
    else:
        err.write("[interactive-tz] No sweep candidates (not enough events on both sides).\n")
    for _ in range(3):
        err.write(f"[interactive-tz] Press Enter to accept {proposed:+d} min, or type an "
                  f"offset in minutes (e.g. 60 / -480) or ±HH:MM: ")
        err.flush()
        try:
            line = input()
        except EOFError:
            err.write(f"\n[interactive-tz] stdin closed — accepting {proposed:+d} min.\n")
            return proposed, "interactive_confirmed"
        if not line.strip():
            err.write(f"[interactive-tz] accepted {proposed:+d} min.\n")
            return proposed, "interactive_confirmed"
        try:
            v = _parse_offset_text(line)
            err.write(f"[interactive-tz] using user-entered offset {v:+d} min.\n")
            return v, "interactive_manual"
        except ValueError:
            err.write("[interactive-tz] could not parse that — expected integer minutes "
                      "or ±HH:MM.\n")
    err.write(f"[interactive-tz] giving up after 3 attempts — accepting {proposed:+d} min.\n")
    return proposed, "interactive_confirmed"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Correlate nv-bug-report and NVOS dump reports by time.")
    ap.add_argument("input", nargs="+", help="Report .md file(s) and/or directories to scan")
    ap.add_argument("-o", "--output-dir", type=Path, required=True, help="Directory for the report")
    ap.add_argument("--name", default="correlation-xid-report", help="Report base filename")
    ap.add_argument("--tz-offset-minutes", type=int, default=None,
                    help="Minutes to add to switch (NVOS) timestamps to align with compute-tray "
                         "time (default 0). Positive = switch clock is behind the tray clock.")
    ap.add_argument("--auto-tz", action="store_true",
                    help="Auto-pick the offset that maximizes time-aligned event pairs.")
    ap.add_argument("--interactive-tz", action="store_true",
                    help="Sweep offsets like --auto-tz, print the top candidates, then confirm "
                         "on stdin: Enter accepts the proposal; or type an offset (integer "
                         "minutes or ±HH:MM). EOF/empty input accepts the proposal, so "
                         "non-interactive runs are safe.")
    ap.add_argument("--window-seconds", type=int, default=120,
                    help="Overlap tolerance for 'same time window' (default 120).")
    ap.add_argument("--cross-chassis", action="store_true",
                    help="Correlate across different chassis serials (default: same chassis only).")
    args = ap.parse_args(argv)

    trays: List[TrayReport] = []
    switches: List[SwitchReport] = []
    crosses: List[CrossNodeReport] = []
    for path in _discover_md(args.input):
        kind, rep = parse_report(path)
        if kind == "nvbug":
            trays.append(rep)
        elif kind == "nvos":
            switches.append(rep)
        elif kind == "nvbug_cross":
            crosses.append(rep)
    cross = None
    if crosses:
        cross = crosses[0]
        for c in crosses[1:]:  # multi-rack: merge the timelines
            cross.xid_groups.extend(c.xid_groups)
            cross.imex_groups.extend(c.imex_groups)
    print(f"Parsed {len(trays)} nv-bug-report(s), {len(switches)} NVOS dump report(s), "
          f"{len(crosses)} cross-node report(s).", file=sys.stderr)
    if not trays or not switches:
        print("Error: need at least one nv-bug-report AND one NVOS dump report to correlate.",
              file=sys.stderr)
        return 2

    scoped = not args.cross_chassis
    manual = args.tz_offset_minutes
    offset = manual if manual is not None else 0
    tz_mode = "manual" if manual is not None else "default"
    if args.auto_tz or args.interactive_tz:
        sugg = suggest_offsets(gather_compute(trays), gather_switch(switches),
                               args.window_seconds, scoped)
        proposed, prop_from_sweep = offset, False
        if sugg and sugg[0][1] > 0:
            proposed, prop_from_sweep = sugg[0][0], True
        if args.interactive_tz:
            offset, tz_mode = _confirm_offset(sugg, proposed)
        elif prop_from_sweep:
            offset, tz_mode = proposed, "auto"
            print(f"[auto-tz] selected offset {offset:+d} min ({sugg[0][1]} aligned hits).",
                  file=sys.stderr)
        else:
            print("[auto-tz] no offset produced any alignment; "
                  f"using {offset:+d}.", file=sys.stderr)
            tz_mode = "auto"

    res = correlate(trays, switches, offset_min=offset,
                    window_s=args.window_seconds, scoped=scoped)

    doc = build_report(res, trays, switches, tz_mode=tz_mode, cross=cross)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    md_path = args.output_dir / f"{args.name}.md"
    html_path = args.output_dir / f"{args.name}.html"
    md_path.write_text(doc.render_md(), encoding="utf-8")
    html_path.write_text(doc.render_html(), encoding="utf-8")

    print(f"Correlated {len(res.correlations)} compute event(s); "
          f"{len(res.matched_switch)}/{res.total_switch} switch events matched; "
          f"offset {offset:+d} min [{tz_mode}].", file=sys.stderr)
    print(f"Wrote {md_path}")
    print(f"Wrote {html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
