"""Self-contained sidecar HTML report generator.

Converts an already-rendered markdown report into a single self-contained HTML
document with:

- Sticky-header / sticky-first-column wide-table rendering for NVLink and the
  Xid comparison matrix (class ``wide-matrix``).
- Non-zero numeric highlighting (class ``nz``) and muted placeholder cells
  (``.`` and the BER sentinel ``15e-255``).
- Auto-generated left sidebar TOC from headings.
- Click-header table sorting (numeric / lexicographic auto-detect).
- Top-bar search box that live-filters every table row by text.
- "Expand all" / "Collapse all" controls for ``<details>`` blocks (IMEX events,
  Xid raw logs, etc.).
- ``prefers-color-scheme`` aware light / dark theme.
- All CSS and JavaScript inlined; no external CDN, works offline.

The module only raises:
- ``ImportError`` if the ``markdown`` package is not installed.
- Any exception from ``markdown.markdown`` itself (very rare).

Callers should wrap the call in ``try/except`` so HTML generation never blocks
the existing markdown output.
"""

from __future__ import annotations

import html as html_mod
import os
import re
import sys
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Anchor handling
# ---------------------------------------------------------------------------

# Authors mark a table by inserting ``<!-- nvbug:table-class=foo bar -->`` on
# its own line right before a markdown table. python-markdown preserves block
# HTML comments verbatim in its output, so we can locate them after the
# md→html conversion and assign the class to the next ``<table>``.
_TABLE_ANCHOR_RE = re.compile(
    r"<!--\s*nvbug:table-class=([^>]+?)\s*-->", re.IGNORECASE
)

# Heading-based fallback patterns: when the author didn't add an anchor, we
# match the section heading text instead. Headings in the rendered HTML look
# like ``<h2 id="...">5. NVLink Status</h2>``; the trailing inline ``¶``
# permalink (if toc.permalink is enabled) is *not* enabled in our config.
_HEADING_NVLINK_RE = re.compile(
    r"<h2[^>]*>\s*\d+\.\s*NVLink\s+Status\s*</h2>", re.IGNORECASE
)
_HEADING_XID_MATRIX_RE = re.compile(
    r"<h2[^>]*>\s*\d+\.\s*Xid\s+Comparison\s+Matrix\s*</h2>", re.IGNORECASE
)
_HEADING_IMEX_TIMELINE_RE = re.compile(
    r"<h2[^>]*>\s*\d+\.\s*IMEX\s+Node\s+Disconnect\s+Timeline\s*</h2>", re.IGNORECASE
)
_HEADING_XID_TIMELINE_RE = re.compile(
    r"<h2[^>]*>\s*\d+\.\s*Xid\s+Unified\s+Timeline\s*</h2>", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Cell highlighting
# ---------------------------------------------------------------------------

# Tokens that should NOT be highlighted as "alarming non-zero":
#   - "0" / "0.0"  → zero
#   - "15e-255"     → BER sentinel meaning "no data"
#   - "."           → placeholder for empty FEC bin
#   - "N/A"         → no data
_MUTED_TOKENS = {"0", "0.0", ".", "15e-255", "N/A", "n/a", "-"}

# A numeric subtoken inside a cell. Allows scientific notation like "1.2e-9".
_NUMERIC_SUBTOKEN_RE = re.compile(
    r"(?<![A-Za-z_])(\d+(?:\.\d+)?(?:[eE]-?\d+)?)(?![A-Za-z_])"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_html_from_markdown(
    md_text: str, *, title: str, kind: str = "per_node"
) -> str:
    """Convert a markdown report to a self-contained HTML document.

    Parameters
    ----------
    md_text:
        The markdown source (the same text that gets written to ``.md``).
    title:
        Document title, also used as the visible H1 in the top bar.
    kind:
        ``'per_node'`` or ``'cross_node'``. Only affects the ``<title>`` and
        a ``data-kind`` attribute on ``<body>`` for potential CSS hooks.

    Returns
    -------
    str
        The complete HTML document.

    Raises
    ------
    ImportError
        If the ``markdown`` PyPI package is not installed.
    """
    import markdown  # noqa: WPS433 — deferred so missing dep stays soft

    # python-markdown disables markdown processing inside any block-level raw
    # HTML element by default. Our reports embed many ``<details>`` blocks that
    # *contain* markdown tables and lists (IMEX timeline, Xid raw logs, etc.),
    # so we (a) enable the ``md_in_html`` extension and (b) transparently
    # inject ``markdown="1"`` into every ``<details>`` opening tag in the
    # in-memory copy. The on-disk .md file is NOT modified — keeping it clean
    # for GitLab / GitHub markdown viewers (which use their own GFM parsers
    # that handle ``<details>`` + tables natively without the attribute).
    md_text = _enable_markdown_in_details(md_text)

    md = markdown.Markdown(
        extensions=["tables", "fenced_code", "attr_list", "toc", "md_in_html"],
        extension_configs={
            "toc": {
                "title": "",
                "permalink": False,
                "baselevel": 1,
                "anchorlink": False,
            },
        },
    )
    body_html = md.convert(md_text)
    toc_html = getattr(md, "toc", "") or ""

    body_html = _decorate(body_html)

    return _wrap_template(
        body_html=body_html,
        toc_html=toc_html,
        title=title,
        kind=kind,
    )


# ---------------------------------------------------------------------------
# Pre-processing (md text mutation before markdown.markdown)
# ---------------------------------------------------------------------------

# Match a ``<details>`` opening tag (with any existing attributes), but skip
# tags that already carry ``markdown="..."``.
_DETAILS_OPEN_RE = re.compile(
    r"<details(?![^>]*\bmarkdown\s*=)([^>]*)>", re.IGNORECASE
)


def _enable_markdown_in_details(md_text: str) -> str:
    """Add ``markdown="1"`` to every ``<details>`` opening tag.

    Required so the ``md_in_html`` extension re-enables markdown processing
    inside collapsible blocks. Done in-memory only — the on-disk .md file is
    untouched so external markdown viewers (GitLab / GitHub) keep working.
    """

    def _inject(m: re.Match) -> str:
        existing = m.group(1)
        # Insert the attribute right after ``<details``, preserving any other
        # attributes (e.g. ``open``, ``class="..."``).
        sep = "" if existing.startswith(" ") or existing == "" else " "
        return f"<details markdown=\"1\"{sep}{existing}>"

    return _DETAILS_OPEN_RE.sub(_inject, md_text)


# ---------------------------------------------------------------------------
# Decoration pipeline
# ---------------------------------------------------------------------------

def _decorate(body_html: str) -> str:
    """Apply all post-processing passes to the markdown-converted HTML body."""
    body_html = _apply_table_anchors(body_html)
    body_html = _tag_tables_by_heading(body_html)
    body_html = _wrap_tables_in_scroll_div(body_html)
    body_html = _highlight_numeric_cells(body_html)
    return body_html


def _apply_table_anchors(html_body: str) -> str:
    """Replace ``<!-- nvbug:table-class=X -->`` + next ``<table>`` with class.

    The comment is removed; the class string ``X`` (which may contain spaces,
    e.g. ``nvlink-matrix wide-matrix``) is added/merged onto the immediately
    following ``<table>`` opening tag.
    """

    def _consume_one(match: re.Match) -> str:
        cls = html_mod.escape(match.group(1).strip(), quote=True)
        return f"<!--__NVBUG_TABLE_CLASS_PLACEHOLDER:{cls}-->"

    tagged = _TABLE_ANCHOR_RE.sub(_consume_one, html_body)

    pat = re.compile(
        r"<!--__NVBUG_TABLE_CLASS_PLACEHOLDER:([^>]+?)-->\s*<table\b([^>]*)>",
        re.DOTALL,
    )

    def _merge(match: re.Match) -> str:
        cls = match.group(1)
        rest = match.group(2)
        return _table_with_class(cls, rest)

    return pat.sub(_merge, tagged)


def _tag_tables_by_heading(html_body: str) -> str:
    """Heading-based fallback for tables that lack an explicit anchor.

    Adds:
    - ``nvlink-matrix wide-matrix`` to all tables that appear between the
      ``## N. NVLink Status`` heading and the next ``<h2>``.
    - ``xid-matrix wide-matrix numeric-matrix`` to the (single) table
      following ``## N. Xid Comparison Matrix`` (cross-node report).
    """

    def _add_class_to_tables_in_section(
        body: str, heading_re: re.Pattern, classes: str
    ) -> str:
        m = heading_re.search(body)
        if not m:
            return body
        start = m.end()
        next_h2 = re.search(r"<h2[^>]*>", body[start:])
        end = start + (next_h2.start() if next_h2 else len(body) - start)
        section = body[start:end]

        def _add(m2: re.Match) -> str:
            return _table_with_class(classes, m2.group(1))

        section = re.sub(r"<table\b([^>]*)>", _add, section)
        return body[:start] + section + body[end:]

    html_body = _add_class_to_tables_in_section(
        html_body, _HEADING_NVLINK_RE, "nvlink-matrix wide-matrix numeric-matrix"
    )
    html_body = _add_class_to_tables_in_section(
        html_body, _HEADING_XID_MATRIX_RE, "xid-matrix wide-matrix numeric-matrix"
    )
    html_body = _add_class_to_tables_in_section(
        html_body, _HEADING_IMEX_TIMELINE_RE, "imex-timeline-matrix"
    )
    html_body = _add_class_to_tables_in_section(
        html_body, _HEADING_XID_TIMELINE_RE, "xid-timeline-matrix"
    )
    return html_body


def _table_with_class(new_classes: str, existing_attrs: str) -> str:
    """Render a ``<table>`` opening tag merging ``new_classes`` into class=."""
    new_set = set(new_classes.split())
    m = re.search(r'class="([^"]*)"', existing_attrs)
    if m:
        existing_set = set(m.group(1).split())
        merged = " ".join(sorted(existing_set | new_set))
        new_attrs = (
            existing_attrs[: m.start()]
            + f'class="{merged}"'
            + existing_attrs[m.end():]
        )
    else:
        new_attrs = f' class="{" ".join(sorted(new_set))}"' + existing_attrs
    return f"<table{new_attrs}>"


def _wrap_tables_in_scroll_div(html_body: str) -> str:
    """Wrap every ``<table>...</table>`` in ``<div class="table-wrap">``.

    The wrap ensures that wide tables get a horizontal scrollbar instead of
    overflowing the layout when the column count exceeds viewport width.
    """
    pat = re.compile(r"(<table\b[^>]*>.*?</table>)", re.DOTALL)
    return pat.sub(lambda m: f'<div class="table-wrap">{m.group(1)}</div>', html_body)


def _highlight_numeric_cells(html_body: str) -> str:
    """Highlight non-zero numeric subtokens in ``numeric-matrix`` tables.

    Within a ``<table class="... numeric-matrix ...">``, every ``<td>`` after
    the first column gets each numeric subtoken wrapped in ``<span class="nz">``
    (non-zero) or ``<span class="muted">`` (zero / placeholder).
    """
    pat = re.compile(
        r'(<table\b[^>]*\bclass="[^"]*\bnumeric-matrix\b[^"]*"[^>]*>)(.*?)(</table>)',
        re.DOTALL,
    )

    def _process_table(m: re.Match) -> str:
        return m.group(1) + _process_table_body(m.group(2)) + m.group(3)

    return pat.sub(_process_table, html_body)


def _process_table_body(table_inner: str) -> str:
    row_pat = re.compile(r"(<tr\b[^>]*>)(.*?)(</tr>)", re.DOTALL)

    def _process_row(m: re.Match) -> str:
        open_tr, inner, close_tr = m.group(1), m.group(2), m.group(3)
        # Skip if all cells are <th> (header row)
        if "<td" not in inner:
            return open_tr + inner + close_tr
        return open_tr + _process_row_cells(inner) + close_tr

    return row_pat.sub(_process_row, table_inner)


def _process_row_cells(row_inner: str) -> str:
    cell_pat = re.compile(r"(<td\b[^>]*>)(.*?)(</td>)", re.DOTALL)
    cells = list(cell_pat.finditer(row_inner))
    if not cells:
        return row_inner

    out = []
    last_end = 0
    for idx, cm in enumerate(cells):
        out.append(row_inner[last_end:cm.start()])
        open_td, inner, close_td = cm.group(1), cm.group(2), cm.group(3)
        if idx == 0:
            out.append(open_td + inner + close_td)
        else:
            out.append(open_td + _highlight_cell_inner(inner) + close_td)
        last_end = cm.end()
    out.append(row_inner[last_end:])
    return "".join(out)


def _highlight_cell_inner(inner: str) -> str:
    stripped = inner.strip()
    if not stripped:
        return inner
    if stripped in _MUTED_TOKENS:
        return f'<span class="muted">{inner}</span>'

    def _wrap(match: re.Match) -> str:
        token = match.group(1)
        if token in _MUTED_TOKENS:
            return f'<span class="muted">{token}</span>'
        # Treat any value parsable to zero as muted too (handles "0.0", "0e0").
        try:
            if float(token) == 0.0:
                return f'<span class="muted">{token}</span>'
        except ValueError:
            pass
        return f'<span class="nz">{token}</span>'

    return _NUMERIC_SUBTOKEN_RE.sub(_wrap, inner)


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

_CSS = r"""
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
  max-width: 1400px;
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
thead th.sort-asc::after { content: " \2191"; }
thead th.sort-desc::after { content: " \2193"; }
tbody tr:nth-child(even) td { background: var(--tr-alt); }
tbody tr:hover td { background: var(--hover); }

/* Wide matrix: sticky first column + smaller cells */
.wide-matrix th, .wide-matrix td {
  padding: 4px 8px;
  font-size: 12px;
  white-space: nowrap;
}
.wide-matrix tbody td:first-child,
.wide-matrix thead th:first-child {
  position: sticky;
  left: 0;
  z-index: 3;
  background: var(--th-bg);
  font-weight: 600;
  border-right: 2px solid var(--border-strong);
}
.wide-matrix thead th:first-child { z-index: 4; }

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
  content: "\25B8";
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

_JS = r"""
(function () {
  // ---------- 1. table sort ----------
  function compareCells(a, b) {
    var na = parseFloat(a.replace(/[, ]/g, ''));
    var nb = parseFloat(b.replace(/[, ]/g, ''));
    if (!isNaN(na) && !isNaN(nb) && /^-?\d/.test(a.trim()) && /^-?\d/.test(b.trim())) {
      return na - nb;
    }
    return a.localeCompare(b, undefined, {numeric: true, sensitivity: 'base'});
  }

  function sortTable(table, colIdx, asc) {
    var tbody = table.tBodies[0];
    if (!tbody) return;
    var rows = Array.prototype.slice.call(tbody.rows);
    rows.sort(function (r1, r2) {
      var c1 = (r1.cells[colIdx] && r1.cells[colIdx].innerText || '').trim();
      var c2 = (r2.cells[colIdx] && r2.cells[colIdx].innerText || '').trim();
      var cmp = compareCells(c1, c2);
      return asc ? cmp : -cmp;
    });
    rows.forEach(function (r) { tbody.appendChild(r); });
  }

  document.querySelectorAll('table').forEach(function (table) {
    var headers = table.querySelectorAll('thead th');
    headers.forEach(function (th, idx) {
      th.addEventListener('click', function () {
        var asc = !th.classList.contains('sort-asc');
        headers.forEach(function (h) { h.classList.remove('sort-asc', 'sort-desc'); });
        th.classList.add(asc ? 'sort-asc' : 'sort-desc');
        sortTable(table, idx, asc);
      });
      th.title = 'Click to sort';
    });
  });

  // ---------- 2. search box ----------
  var searchBox = document.getElementById('search-box');
  if (searchBox) {
    var debounceTimer = null;
    searchBox.addEventListener('input', function () {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(function () { applyFilter(searchBox.value); }, 120);
    });
  }

  function applyFilter(q) {
    q = (q || '').trim().toLowerCase();
    document.querySelectorAll('table tbody tr').forEach(function (tr) {
      if (!q) {
        tr.classList.remove('search-hidden');
      } else {
        var hit = (tr.innerText || '').toLowerCase().indexOf(q) !== -1;
        tr.classList.toggle('search-hidden', !hit);
      }
    });
    // Auto-open <details> that contain matching content.
    if (q) {
      document.querySelectorAll('details').forEach(function (d) {
        if ((d.innerText || '').toLowerCase().indexOf(q) !== -1) d.open = true;
      });
    }
  }

  // ---------- 3. expand/collapse all ----------
  var expandBtn = document.getElementById('expand-all');
  var collapseBtn = document.getElementById('collapse-all');
  if (expandBtn) {
    expandBtn.addEventListener('click', function () {
      document.querySelectorAll('details').forEach(function (d) { d.open = true; });
    });
  }
  if (collapseBtn) {
    collapseBtn.addEventListener('click', function () {
      document.querySelectorAll('details').forEach(function (d) { d.open = false; });
    });
  }

  // ---------- 4. TOC active link ----------
  var tocLinks = document.querySelectorAll('.sidebar a[href^="#"]');
  if (tocLinks.length) {
    var headings = [];
    tocLinks.forEach(function (a) {
      var id = decodeURIComponent(a.getAttribute('href').slice(1));
      var el = document.getElementById(id);
      if (el) headings.push({id: id, el: el, link: a});
    });
    function onScroll() {
      var top = window.scrollY + 80;
      var current = null;
      for (var i = 0; i < headings.length; i++) {
        if (headings[i].el.offsetTop <= top) current = headings[i];
        else break;
      }
      tocLinks.forEach(function (a) { a.classList.remove('active'); });
      if (current) current.link.classList.add('active');
    }
    window.addEventListener('scroll', onScroll, {passive: true});
    onScroll();
  }
})();
"""

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>{css}</style>
</head>
<body data-kind="{kind}">
<header class="topbar">
  <h1 class="topbar-title">{title}</h1>
  <div class="topbar-actions">
    <input type="search" id="search-box" placeholder="Filter rows..." aria-label="Filter rows">
    <button type="button" id="expand-all" title="Expand all collapsible sections">Expand all</button>
    <button type="button" id="collapse-all" title="Collapse all collapsible sections">Collapse all</button>
  </div>
</header>
<div class="layout">
  <nav class="sidebar" aria-label="Table of contents">{toc}</nav>
  <main class="content">{body}</main>
</div>
<script>{js}</script>
</body>
</html>
"""


def _wrap_template(*, body_html: str, toc_html: str, title: str, kind: str) -> str:
    safe_title = html_mod.escape(title)
    safe_kind = html_mod.escape(kind, quote=True)
    return _TEMPLATE.format(
        title=safe_title,
        kind=safe_kind,
        css=_CSS,
        js=_JS,
        toc=toc_html,
        body=body_html,
    )


# ---------------------------------------------------------------------------
# Convenience writer used by analyze.py / pipeline.py
# ---------------------------------------------------------------------------

def write_sidecar_html(
    md_path: str,
    md_text: str,
    *,
    title: Optional[str] = None,
    kind: str = "per_node",
) -> Optional[str]:
    """Render and write ``<md_path stem>.html`` next to a markdown report.

    Failure-tolerant: any exception (missing ``markdown`` package, write error,
    template format issue) is logged to stderr as a warning and ``None`` is
    returned. Never raises — the caller's markdown output path is unaffected.

    Returns the written HTML path on success, or ``None`` on failure.
    """
    if not md_path:
        return None
    try:
        if title is None:
            title = os.path.basename(md_path).rsplit(".md", 1)[0] or os.path.basename(md_path)
        html_doc = render_html_from_markdown(md_text, title=title, kind=kind)
    except ImportError:
        print(
            "[warn] python 'markdown' package not installed; skipping HTML sidecar "
            "(run: pip install markdown)",
            file=sys.stderr,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — defensive: never block .md output
        print(f"[warn] HTML sidecar render failed for {md_path}: {exc}", file=sys.stderr)
        return None

    base, ext = os.path.splitext(md_path)
    html_path = (base if ext.lower() == ".md" else md_path) + ".html"
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_doc)
    except OSError as exc:
        print(f"[warn] HTML sidecar write failed for {html_path}: {exc}", file=sys.stderr)
        return None
    print(f"HTML report saved to: {html_path}", file=sys.stderr)
    return html_path
