#!/usr/bin/env python3
"""
Correlate compute-tray (nv-bug-report) and NVOS/NMX-C dump reports by time.

Reads the Markdown reports produced by the analyze-nv-bug-report and
nvos-tech-dump-tools-for-nmx-c skills, extracts time-stamped event groups
(compute: Xid + IMEX; switch: port-state + FNM port loss), and reports events
that overlap in time — accounting for a timezone offset between the two sources.

Usage:
  python correlate.py <report.md | dir> [more ...] -o OUT
      [--tz-offset-minutes N] [--auto-tz] [--window-seconds S] [--cross-chassis]

Inputs may be individual report .md files and/or directories (scanned for
*.md). Each file is auto-classified as nv-bug-report or NVOS; anything else
(cross-node / rack-comparison / this tool's own output) is ignored.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List

from correlation_xid.engine import correlate, gather_compute, gather_switch, suggest_offsets
from correlation_xid.models import SwitchReport, TrayReport
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Correlate nv-bug-report and NVOS dump reports by time.")
    ap.add_argument("input", nargs="+", help="Report .md file(s) and/or directories to scan")
    ap.add_argument("-o", "--output-dir", type=Path, required=True, help="Directory for the report")
    ap.add_argument("--name", default="correlation-xid-report", help="Report base filename")
    ap.add_argument("--tz-offset-minutes", type=int, default=0,
                    help="Minutes to add to switch (NVOS) timestamps to align with compute-tray "
                         "time (default 0). Positive = switch clock is behind the tray clock.")
    ap.add_argument("--auto-tz", action="store_true",
                    help="Auto-pick the offset that maximizes time-aligned event pairs.")
    ap.add_argument("--window-seconds", type=int, default=120,
                    help="Overlap tolerance for 'same time window' (default 120).")
    ap.add_argument("--cross-chassis", action="store_true",
                    help="Correlate across different chassis serials (default: same chassis only).")
    args = ap.parse_args(argv)

    trays: List[TrayReport] = []
    switches: List[SwitchReport] = []
    for path in _discover_md(args.input):
        kind, rep = parse_report(path)
        if kind == "nvbug":
            trays.append(rep)
        elif kind == "nvos":
            switches.append(rep)
    print(f"Parsed {len(trays)} nv-bug-report(s) and {len(switches)} NVOS dump report(s).",
          file=sys.stderr)
    if not trays or not switches:
        print("Error: need at least one nv-bug-report AND one NVOS dump report to correlate.",
              file=sys.stderr)
        return 2

    scoped = not args.cross_chassis
    offset = args.tz_offset_minutes
    if args.auto_tz:
        sugg = suggest_offsets(gather_compute(trays), gather_switch(switches),
                               args.window_seconds, scoped)
        if sugg and sugg[0][1] > 0:
            offset = sugg[0][0]
            print(f"[auto-tz] selected offset {offset:+d} min ({sugg[0][1]} aligned hits).",
                  file=sys.stderr)
        else:
            print("[auto-tz] no offset produced any alignment; using 0.", file=sys.stderr)
            offset = 0

    res = correlate(trays, switches, offset_min=offset,
                    window_s=args.window_seconds, scoped=scoped)

    doc = build_report(res, trays, switches, auto_tz=args.auto_tz)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    md_path = args.output_dir / f"{args.name}.md"
    html_path = args.output_dir / f"{args.name}.html"
    md_path.write_text(doc.render_md(), encoding="utf-8")
    html_path.write_text(doc.render_html(), encoding="utf-8")

    print(f"Correlated {len(res.correlations)} compute event(s); "
          f"{len(res.matched_switch)}/{res.total_switch} switch events matched; "
          f"offset {offset:+d} min.", file=sys.stderr)
    print(f"Wrote {md_path}")
    print(f"Wrote {html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
