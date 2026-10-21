"""Markdown report entrypoint.

Section traversal lives in ``node_view.render_node``; this module just owns
the Markdown-specific document header.
"""

from __future__ import annotations

from typing import List, Tuple

from ..analyze.pipeline import AnalysisBundle
from .aggregates import build_node_report_context
from .node_view import render_node
from .renderer import MdRenderer


def render_markdown(bundle: AnalysisBundle) -> Tuple[str, List[Tuple[str, str]]]:
    """Return Markdown text plus any sidecar files ``(filename, content)``."""
    r = MdRenderer()
    r.heading(1, "NMX-C Log Analysis Report")
    bullets = [f"{r.i_bold('Input:')} {r.i_code(str(bundle.input_path))}"]
    window = bundle.config.time_window
    if window is not None:
        bullets.append(r.i_bold("Log coverage:") + " " + r.i_text(window.coverage_notice()))
    r.bullets(bullets)
    if bundle.errors:
        r.bullets(
            [f"{r.i_bold('Warnings:')}"]
            + [f"  - {r.i_text(e)}" for e in bundle.errors]
        )

    cfg = bundle.config
    fnm_ports = tuple(cfg.nvlsm_fnm_ports)
    for node in bundle.nodes:
        ctx = build_node_report_context(
            node,
            fnm_ports=fnm_ports,
            fm_fnm_nvlsm_match_window_seconds=cfg.fm_fnm_nvlsm_match_window_seconds,
            fm_fnm_init_follow_gap_seconds=cfg.fm_fnm_init_follow_gap_seconds,
            lifecycle_pair_max_seconds=cfg.nvlsm_event_group_max_seconds,
            report_basename=cfg.report_basename,
            fm_unassigned_embed_threshold=cfg.fm_unassigned_embed_threshold,
        )
        render_node(r, node, ctx)

    return r.render(), list(r.sidecars)
