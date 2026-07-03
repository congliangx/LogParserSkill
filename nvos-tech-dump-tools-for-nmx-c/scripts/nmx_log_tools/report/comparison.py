"""Rack-level comparison report, built from the batch per-dump summaries.

Reuses the same ``MdRenderer`` / ``HtmlRenderer`` as the per-dump reports, so
the combined report gets identical styling (sortable/filterable HTML tables,
sidebar TOC, light/dark theme). The input is the list of slim result dicts
returned by ``analyze.batch.run_batch`` (see ``analyze.run.analyze_and_write_one``).
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Tuple

from .html import _CSS
from .renderer import HtmlRenderer, MdRenderer, Renderer


def _iter_nodes(results: List[Dict[str, Any]]) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Yield ``(dump_name, node_summary)`` for every node across successful dumps."""
    for res in results:
        if res.get("error"):
            continue
        dump = res.get("name") or res.get("input") or "?"
        for node in res.get("nodes", []):
            yield dump, node


def _node_label(r: Renderer, node: Dict[str, Any]) -> str:
    return r.i_text(node.get("node_title") or node.get("label") or "?")


def _emit(r: Renderer, results: List[Dict[str, Any]]) -> None:
    ok = [x for x in results if not x.get("error")]
    failed = [x for x in results if x.get("error")]
    node_rows = list(_iter_nodes(results))

    r.heading(1, "NMX-C Rack Comparison")
    r.bullets([
        f"{r.i_bold('Dumps analyzed:')} {len(results)} ({len(ok)} ok, {len(failed)} failed)",
        f"{r.i_bold('nmx-c nodes total:')} {len(node_rows)}",
    ])

    # 1. Run status — one row per dump, failures flagged.
    r.heading(2, "1. Run Status")
    status_rows: List[List[str]] = []
    for x in results:
        if x.get("error"):
            status = r.i_red("FAILED", bold=True)
            detail = r.i_text(str(x["error"]).strip().splitlines()[-1] if x["error"] else "")
        else:
            status = "ok"
            detail = r.i_text(f"{len(x.get('nodes', []))} node(s) -> {x.get('md_path', '')}")
        status_rows.append([r.i_code(str(x.get("name", "?"))), status, detail])
    r.table(["Dump", "Status", "Detail"], status_rows)

    # 2. Per-node overview — the headline cross-dump metric table.
    r.heading(2, "2. Per-Node Overview")
    if node_rows:
        rows = [
            [
                r.i_code(dump),
                _node_label(r, nd),
                str(nd.get("fm_files_parsed", 0)),
                str(nd.get("fm_events_total", 0)),
                str(nd.get("port_event_groups", 0)),
                str(nd.get("forensics_state_changes", 0)),
                str(nd.get("fm_lifecycle", 0)),
                str(nd.get("fm_switch_info_failures", 0)),
                str(nd.get("fm_partition_errors", 0)),
                str(nd.get("fm_multicast_team_limits", 0)),
            ]
            for dump, nd in node_rows
        ]
        r.table(
            ["Dump", "Node", "FM files", "FM events", "Port evt groups",
             "NVLSM state chg", "Lifecycle", "SwInfo fail", "Partition err", "Mcast limit"],
            rows,
        )
    else:
        r.empty_note("No nmx-c nodes were analyzed.")

    # 3. Fabric Manager category matrix — node x category pivot.
    r.heading(2, "3. Fabric Manager Category Matrix")
    categories = sorted({c for _, nd in node_rows for c in nd.get("fm_category_counts", {})})
    if node_rows and categories:
        rows = [
            [r.i_code(dump), _node_label(r, nd)]
            + [str(nd.get("fm_category_counts", {}).get(c, 0)) for c in categories]
            for dump, nd in node_rows
        ]
        r.table(["Dump", "Node"] + categories, rows)
    else:
        r.empty_note("No Fabric Manager categories found across the batch.")


def render_comparison_markdown(results: List[Dict[str, Any]]) -> str:
    r = MdRenderer()
    _emit(r, results)
    return r.render()


def render_comparison_html(results: List[Dict[str, Any]]) -> str:
    r = HtmlRenderer(css=_CSS, title="NMX-C Rack Comparison")
    _emit(r, results)
    return r.render()
