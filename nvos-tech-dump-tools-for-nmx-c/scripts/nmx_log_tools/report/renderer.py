"""Renderer abstraction shared by the HTML and Markdown report backends.

The two output formats used to be implemented as two near-identical 400-line
files (``report/html.py`` and ``report/markdown.py``). They walked the same
``build_node_report_context`` dict in the same order, with the same
section/details/table structure -- but each duplicated the traversal and
diverged subtly over time (e.g. HTML showed ``switches=… ports=…`` in event
group summaries while Markdown showed ``unique transition pattern(s)``).

The ``Renderer`` protocol below collapses both backends into shared traversal
code (``node_view.render_node``). A renderer exposes:

  - Block primitives that emit into ``self.parts`` (``heading``, ``table``,
    ``open_details`` etc.).
  - Inline formatters (``i_code``, ``i_bold``, …) that return the
    format-specific string so the same callsite can build a cell or summary
    that mixes inline ``<code>``/`` `` ``, ``<strong>``/``**``, etc.

Both ``<details>`` and ``<summary>`` are emitted as raw HTML in markdown too --
that matches the existing behavior (GitHub-flavored Markdown renders them).
"""

from __future__ import annotations

import re
from html import escape as _html_escape
from typing import List, Optional, Tuple


# Inline JS for the HTML report: column sorting, row filtering, expand/collapse
# all, and TOC scroll-spy. Kept verbatim from the cross-node report styling.
_HTML_SCRIPT = """<script>
(function () {
  // ---------- 1. table sort ----------
  function compareCells(a, b) {
    var na = parseFloat(a.replace(/[, ]/g, ''));
    var nb = parseFloat(b.replace(/[, ]/g, ''));
    if (!isNaN(na) && !isNaN(nb) && /^-?\\d/.test(a.trim()) && /^-?\\d/.test(b.trim())) {
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

  // ---------- 3b. sidebar toggle ----------
  var sidebarToggle = document.getElementById('sidebar-toggle');
  if (sidebarToggle) {
    sidebarToggle.addEventListener('click', function () {
      var collapsed = document.body.classList.toggle('sidebar-collapsed');
      sidebarToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
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
</script>"""


class Renderer:
    """Abstract base. Subclasses override every method."""

    def __init__(self) -> None:
        self.parts: List[str] = []

    # ---- Block primitives ---------------------------------------------------

    def heading(self, level: int, text: str) -> None:
        raise NotImplementedError

    def paragraph(self, html_inner: str, *, note: bool = False) -> None:
        """Emit a paragraph. ``html_inner`` may already contain inline-formatted
        spans produced by ``i_code``/``i_bold``/... so renderers MUST treat the
        argument as pre-formatted (do not re-escape)."""
        raise NotImplementedError

    def bullets(self, items: List[str]) -> None:
        """Emit a bulleted list. ``items`` may contain inline formatting."""
        raise NotImplementedError

    def table(
        self,
        headers: List[str],
        rows: List[List[str]],
        *,
        css_class: Optional[str] = None,
    ) -> None:
        """Emit a table. Cell values may contain inline formatting (already
        escaped). Headers are escaped by the renderer. ``css_class`` is an
        optional class applied to the ``<table>`` for per-table styling (e.g.
        per-column ``max-width`` caps); backends without styling ignore it."""
        raise NotImplementedError

    def empty_note(self, text: str) -> None:
        """Italic placeholder for an empty section (``_No data._`` / ``<em>...</em>``)."""
        raise NotImplementedError

    def raw_pre(self, lines: List[str]) -> None:
        """Multi-line raw text block (e.g. ``<pre>`` / ``` ```text ``` ```)."""
        raise NotImplementedError

    def open_details(
        self,
        summary_inner: str,
        *,
        red: bool = False,
        default_open: bool = False,
    ) -> None:
        """Open a ``<details>`` block. ``summary_inner`` may already contain
        inline formatting."""
        raise NotImplementedError

    def close_details(self) -> None:
        raise NotImplementedError

    # ---- Inline formatters (return strings; do not emit) -------------------

    def i_text(self, s: str) -> str:
        """Escape ``s`` for safe inclusion in inline contexts (cells, summaries)."""
        return _html_escape(s)

    def i_code(self, s: str) -> str:
        raise NotImplementedError

    def i_bold(self, s: str) -> str:
        raise NotImplementedError

    def i_em(self, s: str) -> str:
        raise NotImplementedError

    def i_red(self, s: str, *, bold: bool = False) -> str:
        raise NotImplementedError

    # ---- Final output ------------------------------------------------------

    def render(self) -> str:
        raise NotImplementedError


class HtmlRenderer(Renderer):
    """Concrete renderer for the HTML report.

    Emits a GitHub-flavored document shell: a sticky top bar (title + row
    filter + expand/collapse), a sidebar table of contents auto-built from the
    headings, and a content column. Tables are sortable and filterable via the
    inline ``<script>`` at the end. Headings get slugified ``id`` anchors so the
    TOC can link to them.
    """

    # Heading levels surfaced in the sidebar TOC.
    _TOC_MIN_LEVEL = 1
    _TOC_MAX_LEVEL = 4

    def __init__(self, *, css: str, title: str = "NMX-C Log Analysis") -> None:
        super().__init__()
        self._css = css
        self._title = title
        # (level, slug, text) recorded in document order for the sidebar TOC.
        self._toc: List[Tuple[int, str, str]] = []
        self._used_slugs: dict = {}

    def _slugify(self, text: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
        if not slug:
            slug = "section"
        n = self._used_slugs.get(slug, 0)
        self._used_slugs[slug] = n + 1
        return slug if n == 0 else f"{slug}-{n}"

    def heading(self, level: int, text: str) -> None:
        slug = self._slugify(text)
        if self._TOC_MIN_LEVEL <= level <= self._TOC_MAX_LEVEL:
            self._toc.append((level, slug, text))
        self.parts.append(
            f"<h{level} id=\"{slug}\">{_html_escape(text)}</h{level}>"
        )

    def paragraph(self, html_inner: str, *, note: bool = False) -> None:
        cls = " class='note'" if note else ""
        self.parts.append(f"<p{cls}>{html_inner}</p>")

    def bullets(self, items: List[str]) -> None:
        if not items:
            return
        self.parts.append("<ul>")
        for it in items:
            self.parts.append(f"<li>{it}</li>")
        self.parts.append("</ul>")

    def table(
        self,
        headers: List[str],
        rows: List[List[str]],
        *,
        css_class: Optional[str] = None,
    ) -> None:
        if not rows:
            self.empty_note("No data.")
            return
        cls = f" class=\"{_html_escape(css_class)}\"" if css_class else ""
        self.parts.append(f"<div class=\"table-wrap\"><table{cls}>")
        self.parts.append("<thead><tr>")
        for h in headers:
            self.parts.append(f"<th>{_html_escape(h)}</th>")
        self.parts.append("</tr></thead><tbody>")
        for row in rows:
            self.parts.append("<tr>")
            for cell in row:
                # Cell values already contain renderer-formatted inline HTML.
                # The inner .cell <div> is a block box so its max-width is
                # honored (a <td>'s max-width is ignored in auto table layout),
                # letting per-column width caps actually wrap long values.
                self.parts.append(f"<td><div class=\"cell\">{cell}</div></td>")
            self.parts.append("</tr>")
        self.parts.append("</tbody></table></div>")

    def empty_note(self, text: str) -> None:
        self.parts.append(f"<p><em>{_html_escape(text)}</em></p>")

    def raw_pre(self, lines: List[str]) -> None:
        if not lines:
            return
        body = "\n".join(_html_escape(line) for line in lines)
        self.parts.append(f"<pre>{body}</pre>")

    def open_details(
        self,
        summary_inner: str,
        *,
        red: bool = False,
        default_open: bool = False,
    ) -> None:
        open_attr = " open" if default_open else ""
        self.parts.append(f"<details{open_attr}>")
        if red:
            self.parts.append(
                f"<summary><span style=\"color:red\"><strong>"
                f"{summary_inner}</strong></span></summary>"
            )
        else:
            self.parts.append(f"<summary>{summary_inner}</summary>")

    def close_details(self) -> None:
        self.parts.append("</details>")

    def i_code(self, s: str) -> str:
        return f"<code>{_html_escape(s)}</code>"

    def i_bold(self, s: str) -> str:
        return f"<strong>{_html_escape(s)}</strong>"

    def i_em(self, s: str) -> str:
        return f"<em>{_html_escape(s)}</em>"

    def i_red(self, s: str, *, bold: bool = False) -> str:
        inner = self.i_bold(s) if bold else _html_escape(s)
        return f"<span style=\"color:red\">{inner}</span>"

    def _render_toc(self) -> str:
        """Build a nested <ul> TOC from the recorded (level, slug, text)."""
        if not self._toc:
            return ""
        # Normalize so the first entry sits at depth 1 regardless of its
        # heading level, then nest by relative level changes.
        base = min(level for level, _, _ in self._toc)
        out: List[str] = ["<div class=\"toc\">"]
        prev = 0  # current open depth (number of nested <ul>/<li> pairs)
        for level, slug, text in self._toc:
            depth = level - base + 1
            if depth > prev:
                # Descend: open a fresh <ul> for each level jumped.
                out.append("<ul>" * (depth - prev))
            elif depth < prev:
                # Ascend: close the current item, then each list+item pair
                # back up to the target depth.
                out.append("</li>")
                out.append("</ul></li>" * (prev - depth))
            else:
                out.append("</li>")
            out.append(f"<li><a href=\"#{slug}\">{_html_escape(text)}</a>")
            prev = depth
        # Close the final item and unwind every still-open list.
        out.append("</li>")
        out.append("</ul></li>" * (prev - 1))
        out.append("</ul>")
        out.append("</div>")
        return "\n".join(out)

    def render(self) -> str:
        head = [
            "<!DOCTYPE html>",
            "<html lang=\"en\">",
            "<head>",
            "<meta charset=\"UTF-8\">",
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">",
            f"<title>{_html_escape(self._title)}</title>",
            f"<style>{self._css}</style>",
            "</head>",
            "<body data-kind=\"nmx_c\">",
            "<header class=\"topbar\">",
            "  <button type=\"button\" id=\"sidebar-toggle\" class=\"sidebar-toggle\""
            " title=\"Toggle table of contents\" aria-label=\"Toggle sidebar\""
            " aria-expanded=\"true\">☰</button>",
            f"  <h1 class=\"topbar-title\">{_html_escape(self._title)}</h1>",
            "  <div class=\"topbar-actions\">",
            "    <input type=\"search\" id=\"search-box\" placeholder=\"Filter rows...\" aria-label=\"Filter rows\">",
            "    <button type=\"button\" id=\"expand-all\" title=\"Expand all collapsible sections\">Expand all</button>",
            "    <button type=\"button\" id=\"collapse-all\" title=\"Collapse all collapsible sections\">Collapse all</button>",
            "  </div>",
            "</header>",
            "<div class=\"layout\">",
            "  <nav class=\"sidebar\" aria-label=\"Table of contents\">",
            self._render_toc(),
            "  </nav>",
            "  <main class=\"content\">",
        ]
        tail = [
            "<hr><p><small>log_tools_for_nmx-c</small></p>",
            "  </main>",
            "</div>",
            _HTML_SCRIPT,
            "</body></html>",
        ]
        return "\n".join(head + self.parts + tail)


class MdRenderer(Renderer):
    """Concrete renderer for the Markdown report.

    Uses raw HTML for ``<details>``/``<summary>`` (GitHub-flavored Markdown
    supports them) and uses pipe tables. Cell values get pipes escaped via
    ``_md_pipe_escape`` to avoid breaking the row.
    """

    @staticmethod
    def _md_pipe_escape(s: str) -> str:
        return s.replace("|", "\\|")

    def heading(self, level: int, text: str) -> None:
        self.parts.append(f"{'#' * level} {text}")
        self.parts.append("")

    def paragraph(self, html_inner: str, *, note: bool = False) -> None:
        # In Markdown a "note" paragraph wraps the body in underscores for
        # italics, matching the previous renderer's tone.
        body = f"_{html_inner}_" if note else html_inner
        self.parts.append(body)
        self.parts.append("")

    def bullets(self, items: List[str]) -> None:
        for it in items:
            self.parts.append(f"- {it}")
        if items:
            self.parts.append("")

    def table(
        self,
        headers: List[str],
        rows: List[List[str]],
        *,
        css_class: Optional[str] = None,
    ) -> None:
        # Markdown has no per-table styling; css_class is accepted for API
        # parity with HtmlRenderer and ignored.
        if not rows:
            self.empty_note("No data.")
            return
        self.parts.append("| " + " | ".join(headers) + " |")
        self.parts.append("|" + "|".join(["------"] * len(headers)) + "|")
        for row in rows:
            self.parts.append(
                "| "
                + " | ".join(self._md_pipe_escape(str(cell)) for cell in row)
                + " |"
            )
        self.parts.append("")

    def empty_note(self, text: str) -> None:
        self.parts.append(f"_{text}_")
        self.parts.append("")

    def raw_pre(self, lines: List[str]) -> None:
        if not lines:
            return
        self.parts.append("```text")
        self.parts.extend(lines)
        self.parts.append("```")
        self.parts.append("")

    def open_details(
        self,
        summary_inner: str,
        *,
        red: bool = False,
        default_open: bool = False,
    ) -> None:
        open_attr = " open" if default_open else ""
        self.parts.append(f"<details{open_attr}>")
        if red:
            self.parts.append(
                f"<summary><span style=\"color:red\"><strong>"
                f"{summary_inner}</strong></span></summary>"
            )
        else:
            self.parts.append(f"<summary>{summary_inner}</summary>")
        self.parts.append("")

    def close_details(self) -> None:
        self.parts.append("</details>")
        self.parts.append("")

    def i_code(self, s: str) -> str:
        # Backticks inside the value would break the inline code span; the FM
        # log values we render never contain a literal backtick so a plain
        # wrap is sufficient.
        return f"`{s}`"

    def i_bold(self, s: str) -> str:
        return f"**{s}**"

    def i_em(self, s: str) -> str:
        return f"_{s}_"

    def i_red(self, s: str, *, bold: bool = False) -> str:
        # Markdown has no built-in color, but GitHub-flavored Markdown does
        # render inline <span> HTML.
        inner = self.i_bold(s) if bold else s
        return f"<span style=\"color:red\">{inner}</span>"

    def render(self) -> str:
        return "\n".join(self.parts) + "\n"
