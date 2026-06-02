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

from html import escape as _html_escape
from typing import List, Optional


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

    def table(self, headers: List[str], rows: List[List[str]]) -> None:
        """Emit a table. Cell values may contain inline formatting (already
        escaped). Headers are escaped by the renderer."""
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
    """Concrete renderer for the HTML report."""

    def __init__(self, *, css: str) -> None:
        super().__init__()
        self._css = css

    def heading(self, level: int, text: str) -> None:
        self.parts.append(f"<h{level}>{_html_escape(text)}</h{level}>")

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

    def table(self, headers: List[str], rows: List[List[str]]) -> None:
        if not rows:
            self.empty_note("No data.")
            return
        self.parts.append("<table><thead><tr>")
        for h in headers:
            self.parts.append(f"<th>{_html_escape(h)}</th>")
        self.parts.append("</tr></thead><tbody>")
        for row in rows:
            self.parts.append("<tr>")
            for cell in row:
                # Cell values already contain renderer-formatted inline HTML.
                self.parts.append(f"<td>{cell}</td>")
            self.parts.append("</tr>")
        self.parts.append("</tbody></table>")

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

    def render(self) -> str:
        head = [
            "<!DOCTYPE html><html><head><meta charset='utf-8'>",
            "<title>NMX-C Log Analysis</title>",
            f"<style>{self._css}</style></head><body>",
        ]
        tail = ["<hr><p><small>log_tools_for_nmx-c</small></p></body></html>"]
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

    def table(self, headers: List[str], rows: List[List[str]]) -> None:
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
