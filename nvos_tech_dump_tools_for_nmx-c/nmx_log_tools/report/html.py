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
:root {
  --bg: #ffffff;
  --fg: #1f2328;
  --muted-fg: #6e7781;
  --border: #d0d7de;
  --border-strong: #afb8c1;
  --code-bg: #f6f8fa;
  --th-bg: #f6f8fa;
  --tr-alt: #f9fafb;
  --hover: #eef3fa;
  --link: #0969da;
  --link-hover: #0550ae;
  --warn-bg: #fff8c5;
  --warn-fg: #7d4e00;
  --nz-bg: #ffe6cc;
  --nz-fg: #b35200;
  --details-bg: #fafbfc;
  --shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
  --topbar-bg: #24292f;
  --topbar-fg: #ffffff;
  --sidebar-bg: #f6f8fa;
}

@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117;
    --fg: #e6edf3;
    --muted-fg: #8b949e;
    --border: #30363d;
    --border-strong: #484f58;
    --code-bg: #161b22;
    --th-bg: #161b22;
    --tr-alt: #161b22;
    --hover: #1f2937;
    --link: #58a6ff;
    --link-hover: #79b8ff;
    --warn-bg: #43330b;
    --warn-fg: #f9d27a;
    --nz-bg: #5a3015;
    --nz-fg: #ffb088;
    --details-bg: #161b22;
    --shadow: 0 1px 3px rgba(0, 0, 0, 0.4);
    --topbar-bg: #161b22;
    --topbar-fg: #e6edf3;
    --sidebar-bg: #161b22;
  }
}

* { box-sizing: border-box; }

html, body {
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue",
               Helvetica, Arial, sans-serif, "Apple Color Emoji", "Segoe UI Emoji";
  font-size: 14px;
  line-height: 1.55;
}

a { color: var(--link); text-decoration: none; }
a:hover { color: var(--link-hover); text-decoration: underline; }

code {
  background: var(--code-bg);
  padding: 1px 5px;
  border-radius: 4px;
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono",
               monospace;
  font-size: 92%;
}

pre {
  background: var(--code-bg);
  padding: 12px 16px;
  border-radius: 6px;
  overflow-x: auto;
  font-size: 92%;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 60vh;
}
pre code { background: transparent; padding: 0; }

blockquote {
  margin: 0.6em 0;
  padding: 0.4em 1em;
  border-left: 4px solid var(--border-strong);
  color: var(--muted-fg);
  background: var(--code-bg);
  border-radius: 0 4px 4px 0;
}

/* ---------------- top bar ---------------- */
.topbar {
  position: sticky;
  top: 0;
  z-index: 100;
  background: var(--topbar-bg);
  color: var(--topbar-fg);
  padding: 8px 16px;
  display: flex;
  align-items: center;
  gap: 12px;
  box-shadow: var(--shadow);
}
.sidebar-toggle {
  background: rgba(255, 255, 255, 0.12);
  border: 1px solid rgba(255, 255, 255, 0.25);
  color: var(--topbar-fg);
  padding: 2px 9px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 15px;
  line-height: 1.4;
  flex: 0 0 auto;
}
.sidebar-toggle:hover { background: rgba(255, 255, 255, 0.22); }
.topbar-title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 0 1 auto;
  max-width: 60%;
}
.topbar-actions {
  margin-left: auto;
  display: flex;
  gap: 8px;
  align-items: center;
}
.topbar-actions input[type="search"] {
  background: rgba(255, 255, 255, 0.12);
  border: 1px solid rgba(255, 255, 255, 0.25);
  color: var(--topbar-fg);
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 13px;
  width: 220px;
}
.topbar-actions input[type="search"]::placeholder { color: rgba(255, 255, 255, 0.65); }
.topbar-actions button {
  background: rgba(255, 255, 255, 0.12);
  border: 1px solid rgba(255, 255, 255, 0.25);
  color: var(--topbar-fg);
  padding: 4px 10px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
}
.topbar-actions button:hover { background: rgba(255, 255, 255, 0.22); }

/* ---------------- layout ---------------- */
.layout {
  display: grid;
  grid-template-columns: 280px 1fr;
  min-height: calc(100vh - 41px);
}

/* Collapsed: hide the sidebar and let content span the full width. */
body.sidebar-collapsed .layout { grid-template-columns: 1fr; }
body.sidebar-collapsed .sidebar { display: none; }

.sidebar {
  position: sticky;
  top: 41px;
  align-self: start;
  height: calc(100vh - 41px);
  overflow-y: auto;
  background: var(--sidebar-bg);
  border-right: 1px solid var(--border);
  padding: 16px 12px;
  font-size: 13px;
}
.sidebar .toc { margin: 0; padding: 0; }
.sidebar ul { list-style: none; padding-left: 14px; margin: 4px 0; }
.sidebar > .toc > ul { padding-left: 0; }
.sidebar li { margin: 3px 0; }
.sidebar a {
  display: block;
  padding: 2px 6px;
  border-radius: 4px;
  color: var(--fg);
  border-left: 2px solid transparent;
  word-break: break-word;
}
.sidebar a:hover { background: var(--hover); text-decoration: none; }
.sidebar a.active { background: var(--hover); border-left-color: var(--link); color: var(--link); }

.content {
  padding: 24px 32px 64px 32px;
  min-width: 0;
}

@media (max-width: 900px) {
  .layout { grid-template-columns: 1fr; }
  .sidebar { display: none; }
  .content { padding: 16px; }
}

/* ---------------- headings ---------------- */
.content h1, .content h2, .content h3, .content h4, .content h5 {
  margin: 1.4em 0 0.6em 0;
  line-height: 1.25;
  scroll-margin-top: 56px;
}
.content > h1:first-child { margin-top: 0; }
.content h1 { font-size: 24px; padding-bottom: 6px; border-bottom: 1px solid var(--border); }
.content h2 { font-size: 20px; padding-bottom: 5px; border-bottom: 1px solid var(--border); }
.content h3 { font-size: 17px; }
.content h4 { font-size: 15px; }

/* ---------------- tables ---------------- */
.table-wrap {
  overflow-x: auto;
  margin: 0.6em 0;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  position: relative;
}
table {
  border-collapse: separate;
  border-spacing: 0;
  width: max-content;
  min-width: 100%;
  font-size: 13px;
}
th, td {
  padding: 6px 10px;
  text-align: left;
  border-bottom: 1px solid var(--border);
  border-right: 1px solid var(--border);
  vertical-align: top;
  background: var(--bg);
}
/* Cap how wide any single cell can grow so long values (GUIDs, detail text)
   wrap instead of stretching the column. NOTE: max-width on a <td> is ignored
   under automatic table layout, so the cap is applied to a block wrapper
   (.cell) emitted inside every <td> -- a block element's max-width is honored
   and forces the content to wrap. The bare .cell rule is the global backstop;
   the per-table rules below tune the few genuinely wide columns. Each value is
   independent -- adjust any one without touching the others. */
.cell {
  max-width: var(--cell-max-width, 520px);
  overflow-wrap: anywhere;
}
/* Port state event groups: Switches (hostname / GUID) is the dominant column;
   everything else (Port, transition cells, Other transitions) stays compact. */
.tbl-port-event td > .cell { max-width: 240px; }
.tbl-port-event td:nth-child(2) > .cell { max-width: 760px; }
/* FM event table: only the trailing Detail column needs capping. */
.tbl-fm-event td:last-child > .cell { max-width: 440px; }
/* FM lifecycle: trailing Message column. */
.tbl-lifecycle td:last-child > .cell { max-width: 460px; }
tr td:last-child, tr th:last-child { border-right: none; }
tbody tr:last-child td { border-bottom: none; }
thead th {
  background: var(--th-bg);
  font-weight: 600;
  position: sticky;
  top: 0;
  z-index: 2;
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
}
thead th::after {
  content: "";
  display: inline-block;
  width: 10px;
  margin-left: 4px;
  color: var(--muted-fg);
}
thead th.sort-asc::after { content: " \\2191"; }
thead th.sort-desc::after { content: " \\2193"; }
tbody tr:nth-child(even) td { background: var(--tr-alt); }
tbody tr:hover td { background: var(--hover); }

/* Highlighted numeric subtokens */
.nz {
  background: var(--nz-bg);
  color: var(--nz-fg);
  padding: 0 4px;
  border-radius: 3px;
  font-weight: 600;
}
.muted {
  color: var(--muted-fg);
  opacity: 0.55;
}

/* ---------------- meta / note / warn ---------------- */
.meta {
  background: var(--code-bg);
  border: 1px solid var(--border);
  padding: 12px 16px;
  border-radius: 6px;
  margin: 0.6em 0 1.2em;
}
.note { color: var(--muted-fg); font-size: 0.92rem; }
.warn {
  color: var(--warn-fg);
  background: var(--warn-bg);
  border-left: 4px solid var(--warn-fg);
  padding: 0.5rem 0.75rem;
  margin: 0.5rem 0;
  border-radius: 0 4px 4px 0;
}

/* ---------------- details / summary ---------------- */
details {
  margin: 0.6em 0;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--details-bg);
  padding: 8px 12px;
}
details[open] { background: var(--bg); }
summary {
  cursor: pointer;
  font-weight: 500;
  color: var(--fg);
  padding: 2px 0;
  list-style: none;
}
summary::-webkit-details-marker { display: none; }
summary::before {
  content: "\\25B8";
  display: inline-block;
  width: 1em;
  color: var(--muted-fg);
  transition: transform 0.15s;
  font-size: 11px;
}
details[open] > summary::before { transform: rotate(90deg); }

/* ---------------- search hidden rows ---------------- */
tr.search-hidden { display: none; }

/* ---------------- print ---------------- */
@media print {
  .topbar, .sidebar { display: none; }
  .layout { grid-template-columns: 1fr; }
  .content { padding: 0; max-width: none; }
  details { break-inside: avoid; }
  details:not([open]) { display: none; }
  table { page-break-inside: avoid; }
}
"""


def render_html(bundle: AnalysisBundle) -> str:
    title = f"{bundle.config.report_basename} — NMX-C Log Analysis"
    r = HtmlRenderer(css=_CSS, title=title)
    r.heading(1, "NMX-C Log Analysis Report")
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
