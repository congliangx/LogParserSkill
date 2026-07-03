#!/usr/bin/env python3
"""
Analyze NVOS dump / tar.gz for log/nmx/nmx-c (NVLSM + Fabric Manager logs).

Single dump:
  python main.py <dump_dir_or.tar.gz> -o /path/to/output [--name report_basename]

Batch (multiple dumps, analyzed in parallel + a rack comparison report):
  python main.py --batch <dump1> <dump2.tar.gz> ... -o /path/to/output [-j N]
  python main.py --batch <dir_of_dumps> -o /path/to/output      # scans the dir

Requires log/nmx in each archive/directory (validated before heavy work).
Fabric Manager uses -vvv-equivalent settings: all log files, no age cutoff.

Batch mode runs each dump in its own process (Linux: fork; else spawn) so the
CPU-bound parsing actually runs in parallel despite the GIL. It writes one
``<dump>.md`` + ``.html`` per dump plus ``rack-comparison.md`` + ``.html``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _run_single(input_path: Path, output_dir: Path, name: str) -> int:
    from nmx_log_tools.analyze.run import analyze_and_write_one

    res = analyze_and_write_one(input_path, output_dir, name)
    print(f"Found {res['n_roots']} log/nmx/nmx-c root(s)", file=sys.stderr)
    print(f"Wrote {res['md_path']}")
    print(f"Wrote {res['html_path']}")
    if res.get("warnings"):
        print("Warnings:", file=sys.stderr)
        for w in res["warnings"]:
            print(f"  - {w}", file=sys.stderr)
    return 0


def _run_batch(inputs, output_dir: Path, jobs: int) -> int:
    from nmx_log_tools.analyze.batch import run_batch
    from nmx_log_tools.report.comparison import (
        render_comparison_html,
        render_comparison_markdown,
    )

    results = run_batch(inputs, output_dir, jobs=jobs)
    if not results:
        return 2

    n_ok = 0
    for res in results:
        if res.get("error"):
            last = str(res["error"]).strip().splitlines()[-1] if res["error"] else ""
            print(f"  [FAILED] {res.get('name')}: {last}", file=sys.stderr)
        else:
            n_ok += 1
            print(f"  [ok] {res.get('name')}: {len(res.get('nodes', []))} node(s) -> "
                  f"{res.get('md_path')}", file=sys.stderr)

    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / "rack-comparison.md"
    html_path = output_dir / "rack-comparison.html"
    md_path.write_text(render_comparison_markdown(results), encoding="utf-8")
    html_path.write_text(render_comparison_html(results), encoding="utf-8")

    print(f"Wrote {md_path}")
    print(f"Wrote {html_path}")
    print(f"Batch complete: {n_ok}/{len(results)} dump(s) ok; rack comparison: {md_path}")
    # Non-zero only if every dump failed, so scripts can distinguish total failure.
    return 0 if n_ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="NMX-C log analysis (NVLSM + Fabric Manager)")
    parser.add_argument(
        "input",
        type=Path,
        nargs="+",
        help="NVOS dump directory or .tar.gz. With --batch, may be several dumps "
             "and/or a parent directory to scan for dumps.",
    )
    parser.add_argument(
        "-o", "--output-dir",
        type=Path,
        required=True,
        help="Directory for Markdown and HTML reports",
    )
    parser.add_argument(
        "--name",
        default="nmx_log_analysis",
        help="Report base filename without extension (single-dump mode only; "
             "in batch mode names are derived per dump). Default: nmx_log_analysis",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Batch mode: analyze multiple dumps in parallel and also emit a "
             "rack-comparison report. Auto-enabled when more than one input is given.",
    )
    parser.add_argument(
        "-j", "--jobs",
        type=int,
        default=0,
        help="Batch worker processes (default: min(#dumps, CPU count)).",
    )
    args = parser.parse_args(argv)

    # Batch when explicitly asked, or when more than one input path is supplied.
    if args.batch or len(args.input) > 1:
        return _run_batch(args.input, args.output_dir, args.jobs)
    return _run_single(args.input[0], args.output_dir, args.name)


if __name__ == "__main__":
    sys.exit(main())
