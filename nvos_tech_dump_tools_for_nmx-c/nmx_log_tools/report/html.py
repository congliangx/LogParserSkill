"""HTML report entrypoint.

Section traversal lives in ``node_view.render_node``; this module just owns
the HTML-specific chrome (DOCTYPE, ``<style>`` block, document body).
"""

from __future__ import annotations

from ..analyze.pipeline import AnalysisBundle
from .node_view import render_node
from .renderer import HtmlRenderer
from .aggregates import build_node_report_context


_CSS = """
body { font-family: system-ui, sans-serif; margin: 2rem; color: #1a1a1a; line-height: 1.45; }
h1 { color: #0b3d91; border-bottom: 2px solid #0b3d91; }
h2 { color: #145ea8; margin-top: 2rem; }
h3 { color: #333; margin-top: 1.5rem; }
h4 { color: #555; margin-top: 1rem; }
table { border-collapse: collapse; width: 100%; margin: 0.75rem 0 1.25rem; font-size: 0.88rem; }
th, td { border: 1px solid #ccc; padding: 0.35rem 0.5rem; text-align: left; vertical-align: top; }
th { background: #e8f0fe; position: sticky; top: 0; }
tr:nth-child(even) { background: #f8f9fa; }
.fail { color: #b00020; font-weight: bold; }
.pass { color: #0d7a3e; }
.mono { font-family: ui-monospace, monospace; font-size: 0.82rem; }
details.fm-raw-log { margin: 0.75rem 0; }
pre.fm-raw-log {
  white-space: pre-wrap; word-break: break-word;
  font-family: ui-monospace, monospace; font-size: 0.8rem;
  max-height: 50vh; overflow: auto;
  background: #f6f8fa; padding: 0.75rem; margin: 0.5rem 0;
}
.meta { background: #f0f4f8; padding: 1rem; border-radius: 6px; margin-bottom: 1.5rem; }
.note { color: #555; font-size: 0.9rem; }
.warn { color: #8a1c1c; background: #fff3f3; border-left: 4px solid #b00020; padding: 0.5rem 0.75rem; margin: 0.5rem 0; }
details { margin: 0.5rem 0 1rem; border: 1px solid #ddd; border-radius: 4px; padding: 0.5rem 0.75rem; }
summary { cursor: pointer; font-weight: 600; }
code { font-size: 0.85em; background: #f4f4f4; padding: 0.1em 0.3em; border-radius: 3px; }
"""


def render_html(bundle: AnalysisBundle) -> str:
    r = HtmlRenderer(css=_CSS)
    r.parts.append("<h1>NMX-C Log Analysis Report</h1>")
    r.parts.append("<div class='meta'>")
    r.paragraph(
        f"{r.i_bold('Input:')} {r.i_code(str(bundle.input_path))}"
    )
    if bundle.errors:
        r.bullets([r.i_text(e) for e in bundle.errors])
    r.parts.append("</div>")

    cfg = bundle.config
    fnm_ports = tuple(cfg.nvlsm_fnm_ports)
    for node in bundle.nodes:
        ctx = build_node_report_context(
            node,
            fnm_ports=fnm_ports,
            fm_fnm_nvlsm_match_window_seconds=cfg.fm_fnm_nvlsm_match_window_seconds,
            fm_fnm_init_follow_gap_seconds=cfg.fm_fnm_init_follow_gap_seconds,
            lifecycle_pair_max_seconds=cfg.nvlsm_event_group_max_seconds,
        )
        render_node(r, node, ctx)

    return r.render()
