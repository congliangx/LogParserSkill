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
/* NMX-C fabric forensics — instrument-panel theme, shared with the
   correlation-xid report so the toolkit reads as one product. Signature:
   severity rails on <details> (fatal red / non-fatal amber / neutral) that
   let an engineer see where the fatal fabric faults cluster, in the body and
   in the sidebar. All machine data (timestamps, GUIDs, counts) is set mono. */
:root {
  --paper: #f6f7f9;
  --panel: #ffffff;
  --ink: #171b21;
  --muted: #5c6673;
  --line: #dfe4eb;
  --line-soft: #eceff4;
  --code: #eef1f5;
  --bar: rgba(255, 255, 255, 0.9);
  --signal: #537f00;
  --signal-bg: rgba(118, 185, 0, 0.13);
  --fatal: #c42b3d;
  --fatal-bg: rgba(196, 43, 61, 0.09);
  --warn: #9a6700;
  --warn-bg: rgba(245, 166, 35, 0.14);
  --info: #3b6ea5;
  --info-bg: rgba(59, 110, 165, 0.10);
  --nz-bg: rgba(245, 166, 35, 0.18);
  --nz-fg: #8a5a00;
  --shadow: 0 1px 2px rgba(15, 23, 42, 0.06), 0 4px 14px rgba(15, 23, 42, 0.05);
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  --disp: "Segoe UI Variable Display", "SF Pro Display", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: ui-monospace, "SFMono-Regular", "Cascadia Mono", "JetBrains Mono", Menlo, Consolas, monospace;
  --barh: 48px;
}

@media (prefers-color-scheme: dark) {
  :root {
    --paper: #0c1116;
    --panel: #121922;
    --ink: #e7edf3;
    --muted: #8d9aa8;
    --line: #223040;
    --line-soft: #1a2531;
    --code: #182230;
    --bar: rgba(18, 25, 34, 0.9);
    --signal: #8cd211;
    --signal-bg: rgba(140, 210, 17, 0.14);
    --fatal: #ff737c;
    --fatal-bg: rgba(255, 115, 124, 0.12);
    --warn: #f0b429;
    --warn-bg: rgba(240, 180, 41, 0.13);
    --info: #7aa7d9;
    --info-bg: rgba(122, 167, 217, 0.12);
    --nz-bg: rgba(240, 180, 41, 0.16);
    --nz-fg: #f0b429;
    --shadow: 0 1px 2px rgba(0, 0, 0, 0.4), 0 4px 14px rgba(0, 0, 0, 0.28);
  }
}

* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }

html, body {
  margin: 0;
  padding: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: var(--sans);
  font-size: 14px;
  line-height: 1.55;
}

a { color: var(--signal); text-decoration: none; }
a:hover { text-decoration: underline; }
:focus-visible { outline: 2px solid var(--signal); outline-offset: 2px; border-radius: 4px; }

code {
  background: var(--code);
  padding: 1px 5px;
  border-radius: 4px;
  font-family: var(--mono);
  font-size: 92%;
}

pre {
  background: var(--code);
  padding: 12px 16px;
  border-radius: 8px;
  overflow-x: auto;
  font-size: 92%;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 60vh;
  border: 1px solid var(--line);
}
pre code { background: transparent; padding: 0; }

blockquote {
  margin: 0.6em 0;
  padding: 0.4em 1em;
  border-left: 3px solid var(--line);
  color: var(--muted);
  background: var(--code);
  border-radius: 0 4px 4px 0;
}

/* ---------------- top bar ---------------- */
.topbar {
  position: sticky;
  top: 0;
  z-index: 100;
  background: var(--bar);
  backdrop-filter: blur(8px);
  color: var(--ink);
  padding: 9px 18px;
  display: flex;
  align-items: center;
  gap: 12px;
  border-bottom: 1px solid var(--line);
}
.sidebar-toggle {
  background: var(--paper);
  border: 1px solid var(--line);
  color: var(--ink);
  padding: 3px 10px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 15px;
  line-height: 1.35;
  flex: 0 0 auto;
}
.sidebar-toggle:hover { border-color: var(--signal); color: var(--signal); }
.topbar-title {
  margin: 0;
  font: 600 14px/1.3 var(--disp);
  letter-spacing: -0.01em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 0 1 auto;
  max-width: 62%;
}
.topbar-actions {
  margin-left: auto;
  display: flex;
  gap: 8px;
  align-items: center;
}
.topbar-actions input[type="search"] {
  background: var(--paper);
  border: 1px solid var(--line);
  color: var(--ink);
  padding: 5px 11px;
  border-radius: 6px;
  font-size: 13px;
  width: 210px;
  font-family: var(--sans);
}
.topbar-actions input[type="search"]:focus { border-color: var(--signal); outline: none; }
.topbar-actions input[type="search"]::placeholder { color: var(--muted); }
.topbar-actions button {
  background: var(--paper);
  border: 1px solid var(--line);
  color: var(--ink);
  padding: 5px 11px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
}
.topbar-actions button:hover { border-color: var(--signal); color: var(--signal); }

/* ---------------- layout ---------------- */
.layout {
  display: grid;
  grid-template-columns: 264px minmax(0, 1fr);
  min-height: calc(100vh - var(--barh));
}

/* Collapsed: hide the sidebar and let content span the full width. */
body.sidebar-collapsed .layout { grid-template-columns: 1fr; }
body.sidebar-collapsed .sidebar { display: none; }

.sidebar {
  position: sticky;
  top: var(--barh);
  align-self: start;
  height: calc(100vh - var(--barh));
  overflow-y: auto;
  background: var(--paper);
  border-right: 1px solid var(--line);
  padding: 12px 10px 40px 12px;
}
.sidebar-head {
  font: 700 10px/1 var(--sans);
  text-transform: uppercase;
  letter-spacing: 0.13em;
  color: var(--muted);
  padding: 2px 8px 8px;
}
.sidebar .toc, .sidebar ul { list-style: none; margin: 0; padding: 0; }
/* Nested groups (sections + subs under a node) hang off a guide rail so the
   "these belong to the node above" grouping is obvious at a glance; nesting
   two rails deep shows section vs sub-section without needing to read weights. */
.sidebar .toc ul ul {
  margin: 2px 0 8px 11px;
  padding-left: 11px;
  border-left: 1px solid var(--line);
}
.sidebar a {
  display: block;
  border-radius: 6px;
  color: var(--muted);
  text-decoration: none;
  word-break: break-word;
}
.sidebar a:hover { background: var(--line-soft); color: var(--ink); }

/* A full-width divider above each node separates the per-node groups. */
.sidebar .toc > ul > li {
  border-top: 1px solid var(--line);
  margin-top: 8px;
  padding-top: 6px;
}
.sidebar .toc > ul > li:first-child { border-top: none; margin-top: 0; padding-top: 0; }

/* Tier 1 — node: the primary landmark. Bold sans; long titles clamp to two
   tidy lines (full title on hover) so wrapping never breaks the rhythm. */
.toc-l2 {
  font: 600 12.5px/1.35 var(--sans);
  color: var(--ink);
  padding: 5px 8px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
/* Tier 2 — section: the main jump targets under a node. */
.toc-l3 { font: 500 12px/1.35 var(--sans); color: var(--ink); padding: 4px 8px; }
/* Tier 3 — subsection: quietest, mono, sitting under its section rail. */
.toc-l4 { font: 400 11px/1.3 var(--mono); color: var(--muted); padding: 3px 8px; }

.sidebar a.active {
  background: var(--signal-bg);
  color: var(--ink);
  box-shadow: inset 2px 0 0 var(--signal);
}

.content {
  padding: 22px 34px 72px 34px;
  min-width: 0;
}

@media (max-width: 900px) {
  .layout { grid-template-columns: 1fr; }
  .sidebar { display: none; }
  .content { padding: 16px; }
}

/* ---------------- headings ---------------- */
.content h1, .content h2, .content h3, .content h4, .content h5 {
  line-height: 1.25;
  scroll-margin-top: calc(var(--barh) + 12px);
  font-family: var(--disp);
}
.content > h1:first-child { margin-top: 0; }
.content h1 {
  font-size: 25px;
  font-weight: 700;
  letter-spacing: -0.02em;
  margin: 0 0 0.3em;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--line);
}
/* Per-node banner: a signal tick + strong display title separates nodes. */
.content h2 {
  font-size: 20px;
  font-weight: 700;
  letter-spacing: -0.015em;
  margin: 1.9em 0 0.7em;
  padding: 4px 0 4px 14px;
  border-left: 3px solid var(--signal);
}
.content h3 {
  font-size: 16.5px;
  font-weight: 600;
  margin: 1.5em 0 0.5em;
}
/* h4 sub-sections read as instrument-panel labels. */
.content h4 {
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.09em;
  color: var(--muted);
  margin: 1.4em 0 0.5em;
}

/* ---------------- node vitals strip (summary bullets) ---------------- */
ul.node-stats {
  list-style: none;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 0;
  margin: 0.4em 0 1.2em;
}
ul.node-stats > li {
  border: 1px solid var(--line);
  background: var(--panel);
  border-radius: 8px;
  padding: 7px 13px;
  font-size: 12.5px;
  color: var(--muted);
  box-shadow: var(--shadow);
}
ul.node-stats strong {
  font-family: var(--mono);
  font-weight: 600;
  color: var(--ink);
}

/* ---------------- tables ---------------- */
.table-wrap {
  overflow-x: auto;
  margin: 0.7em 0;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  box-shadow: var(--shadow);
  position: relative;
}
table {
  border-collapse: separate;
  border-spacing: 0;
  width: max-content;
  min-width: 100%;
  font-size: 12.5px;
}
th, td {
  padding: 7px 11px;
  text-align: left;
  border-bottom: 1px solid var(--line-soft);
  vertical-align: top;
  background: var(--panel);
}
/* Data cells are machine values — set them mono for scannability. */
tbody td { font-family: var(--mono); font-size: 12px; }
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
tbody tr:last-child td { border-bottom: none; }
thead th {
  background: var(--panel);
  font: 600 11px/1.3 var(--sans);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--muted);
  position: sticky;
  top: 0;
  z-index: 2;
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
  border-bottom: 1px solid var(--line);
}
thead th:hover { color: var(--ink); }
thead th::after {
  content: "";
  display: inline-block;
  width: 10px;
  margin-left: 4px;
  color: var(--signal);
}
thead th.sort-asc::after { content: " \\2191"; }
thead th.sort-desc::after { content: " \\2193"; }
/* Subtle zebra aids row tracking across the wide fabric matrices; hover wins. */
tbody tr:nth-child(even) td { background: var(--paper); }
tbody tr:hover td { background: var(--line-soft); }

/* Highlighted numeric subtokens */
.nz {
  background: var(--nz-bg);
  color: var(--nz-fg);
  padding: 0 4px;
  border-radius: 3px;
  font-weight: 600;
}
.muted { color: var(--muted); opacity: 0.6; }
/* Critical (formerly inline color:red) — themeable so it works in dark too. */
.crit { color: var(--fatal); }

/* ---------------- meta / note / warn ---------------- */
.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 18px;
  align-items: baseline;
  background: var(--panel);
  border: 1px solid var(--line);
  border-left: 3px solid var(--signal);
  padding: 11px 16px;
  border-radius: 8px;
  margin: 0.6em 0 1.4em;
  box-shadow: var(--shadow);
}
.meta p { margin: 0; }
.meta code { font-size: 12px; }
.note { color: var(--muted); font-size: 0.9rem; }
.warn {
  color: var(--warn);
  background: var(--warn-bg);
  border-left: 3px solid var(--warn);
  padding: 0.5rem 0.75rem;
  margin: 0.5rem 0;
  border-radius: 0 6px 6px 0;
}

/* ---------------- details / summary ---------------- */
details {
  margin: 0.55em 0;
  border: 1px solid var(--line);
  border-left: 3px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  padding: 2px 14px;
}
details[open] { box-shadow: var(--shadow); }
summary {
  cursor: pointer;
  font-weight: 500;
  color: var(--ink);
  padding: 9px 0;
  margin: 0 -14px;
  padding-left: 14px;
  padding-right: 14px;
  list-style: none;
  display: flex;
  align-items: center;
  gap: 9px;
}
summary::-webkit-details-marker { display: none; }
/* Border chevron instead of a unicode glyph (no escaping, themeable color). */
summary::before {
  content: "";
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  border-right: 1.6px solid var(--muted);
  border-bottom: 1.6px solid var(--muted);
  transform: rotate(-45deg);
  transition: transform 0.16s ease;
}
details[open] > summary::before { transform: rotate(45deg); }
@media (prefers-reduced-motion: reduce) { summary::before { transition: none; } }
details[open] > summary { border-bottom: 1px solid var(--line-soft); }
details > *:last-child { margin-bottom: 12px; }

/* Severity rails — the report's signature. Fatal event groups / hardware-swap
   slots get a red rail + red chevron; non-fatal get amber. */
details.sev-fatal { border-left-color: var(--fatal); }
details.sev-fatal > summary::before { border-color: var(--fatal); }
details.sev-warn { border-left-color: var(--warn); }
details.sev-warn > summary::before { border-color: var(--warn); }

/* ---------------- search hidden rows ---------------- */
tr.search-hidden { display: none; }

/* ---------------- footer ---------------- */
.content hr { border: none; border-top: 1px solid var(--line); margin: 2.5em 0 1em; }
.content hr + p small { color: var(--muted); font-family: var(--mono); }

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
