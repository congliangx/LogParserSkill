"""Analyze a single dump and write its Markdown + HTML report.

This is the shared core used by both the single-dump CLI path and each batch
worker process. It returns a slim, picklable summary (plain str/int/dict) so a
batch worker can hand its results back to the parent process without pickling
the full ``AnalysisBundle`` (which holds large parsed structures).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import AnalysisConfig
from ..report.html import render_html
from ..report.markdown import render_markdown
from ..sources import open_source
from .pipeline import NodeAnalysis, analyze_source

_TARBALL_SUFFIXES = (".tar.gz", ".tgz", ".tar")


def sanitize_name(name: str) -> str:
    """Make a string safe to use as a report filename stem."""
    name = os.path.basename(str(name))
    for ch in '\0/\\<>:"|?*':
        name = name.replace(ch, "_")
    name = name.strip(" .")
    return name or "dump"


def dump_basename(input_path: Path) -> str:
    """Derive a report basename from a dump directory or ``.tar.gz`` path."""
    name = Path(input_path).name
    lowered = name.lower()
    for suffix in _TARBALL_SUFFIXES:
        if lowered.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return sanitize_name(name)


def _node_summary(node: NodeAnalysis) -> Dict[str, Any]:
    """Slim, picklable per-node metrics for the rack comparison report."""
    forensics = node.nvlsm_forensics
    return {
        "label": node.label,
        "node_title": node.node_title,
        "fm_files_parsed": node.fm_files_parsed,
        "fm_events_total": len(node.fm_events),
        "fm_category_counts": dict(node.fm_category_counts),
        "port_event_groups": len(node.nvlsm_port_event_groups),
        "forensics_state_changes": getattr(forensics, "state_changes", 0) if forensics else 0,
        "fm_lifecycle": len(node.fm_lifecycle),
        "fm_switch_info_failures": len(node.fm_switch_info_failures),
        "fm_partition_errors": len(node.fm_partition_errors),
        "fm_multicast_team_limits": len(node.fm_multicast_team_limits),
    }


def analyze_and_write_one(
    input_path,
    output_dir,
    name: str,
    cfg: Optional[AnalysisConfig] = None,
) -> Dict[str, Any]:
    """Analyze one dump, write ``<name>.md`` + ``<name>.html`` under ``output_dir``.

    Returns a slim summary dict. Raises/propagates ``SystemExit`` if the source
    fails validation (``open_source`` calls ``sys.exit(2)`` on a bad layout) —
    callers that need isolation (batch workers) catch it.
    """
    input_path = Path(input_path)
    output_dir = Path(output_dir)
    cfg = cfg or AnalysisConfig(report_basename=name)
    source = None
    try:
        source = open_source(input_path)
        n_roots = len(source.nmx_c_roots())
        bundle = analyze_source(source, input_path.resolve(), cfg)

        output_dir.mkdir(parents=True, exist_ok=True)
        md_path = output_dir / f"{name}.md"
        html_path = output_dir / f"{name}.html"
        md_path.write_text(render_markdown(bundle), encoding="utf-8")
        html_path.write_text(render_html(bundle), encoding="utf-8")

        return {
            "input": str(input_path),
            "name": name,
            "md_path": str(md_path),
            "html_path": str(html_path),
            "n_roots": n_roots,
            "nodes": [_node_summary(n) for n in bundle.nodes],
            "warnings": list(bundle.errors),
            "error": None,
        }
    finally:
        if source is not None:
            source.cleanup()
