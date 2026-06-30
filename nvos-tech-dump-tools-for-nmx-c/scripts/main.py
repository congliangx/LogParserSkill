#!/usr/bin/env python3
"""
Analyze NVOS dump / tar.gz for log/nmx/nmx-c (NVLSM + Fabric Manager logs).

Usage:
  python main.py <dump_dir_or.tar.gz> -o /path/to/output [--name report_basename]

Requires log/nmx in the archive or directory (validated before heavy work).
Fabric Manager uses -vvv-equivalent settings: all log files, no age cutoff, full health.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from nmx_log_tools.config import AnalysisConfig
from nmx_log_tools.analyze.pipeline import analyze_source
from nmx_log_tools.report.html import render_html
from nmx_log_tools.report.markdown import render_markdown
from nmx_log_tools.sources import open_source


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="NMX-C log analysis (NVLSM + Fabric Manager)")
    parser.add_argument("input", type=Path, help="NVOS dump directory or .tar.gz")
    parser.add_argument(
        "-o", "--output-dir",
        type=Path,
        required=True,
        help="Directory for Markdown and HTML reports",
    )
    parser.add_argument(
        "--name",
        default="nmx_log_analysis",
        help="Report base filename without extension (default: nmx_log_analysis)",
    )
    args = parser.parse_args(argv)

    cfg = AnalysisConfig(report_basename=args.name)
    source = None
    try:
        source = open_source(args.input)
        print(f"Found {len(source.nmx_c_roots())} log/nmx/nmx-c root(s)", file=sys.stderr)
        bundle = analyze_source(source, args.input.resolve(), cfg)

        args.output_dir.mkdir(parents=True, exist_ok=True)
        md_path = args.output_dir / f"{args.name}.md"
        html_path = args.output_dir / f"{args.name}.html"

        md_text = render_markdown(bundle)
        html_text = render_html(bundle)
        md_path.write_text(md_text, encoding="utf-8")
        html_path.write_text(html_text, encoding="utf-8")

        print(f"Wrote {md_path}")
        print(f"Wrote {html_path}")
        if bundle.errors:
            print("Warnings:", file=sys.stderr)
            for e in bundle.errors:
                print(f"  - {e}", file=sys.stderr)
        return 0
    finally:
        if source is not None:
            source.cleanup()


if __name__ == "__main__":
    sys.exit(main())
