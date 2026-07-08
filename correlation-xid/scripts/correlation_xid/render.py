"""Compact dual (Markdown + self-contained HTML) renderer for the report.

Blocks are format-neutral; ``render_md`` / ``render_html`` walk them. Table cells
are plain text (escaped per backend). The HTML shell embeds its own CSS + a small
JS for sortable/filterable tables and expand/collapse-all, so it opens offline.
"""

from __future__ import annotations

import re
from html import escape as _esc
from typing import List, Optional

from . import timeutil as T
from .engine import Result
from .models import SwitchReport, TrayReport

_CSS = """
:root{--bg:#fff;--fg:#1f2328;--muted:#6e7781;--border:#d0d7de;--th:#f6f8fa;
--alt:#f9fafb;--hover:#eef3fa;--code:#f6f8fa;--red:#cf222e;--warn:#fff8c5;
--topbar:#24292f;--topfg:#fff;}
@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8b949e;
--border:#30363d;--th:#161b22;--alt:#161b22;--hover:#1f2937;--code:#161b22;
--red:#ff7b72;--warn:#43330b;--topbar:#161b22;--topfg:#e6edf3;}}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--fg);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
font-size:14px;line-height:1.55}
.topbar{position:sticky;top:0;z-index:9;background:var(--topbar);color:var(--topfg);
padding:8px 16px;display:flex;gap:10px;align-items:center}
.topbar h1{font-size:15px;margin:0;font-weight:600}
.topbar .actions{margin-left:auto;display:flex;gap:8px}
.topbar input,.topbar button{background:rgba(255,255,255,.12);border:1px solid
rgba(255,255,255,.25);color:var(--topfg);padding:4px 10px;border-radius:6px;font-size:13px}
.content{padding:20px 28px 64px}
h1,h2,h3,h4{line-height:1.25;margin:1.3em 0 .5em}
h2{font-size:19px;border-bottom:1px solid var(--border);padding-bottom:4px}
h3{font-size:16px}code{background:var(--code);padding:1px 5px;border-radius:4px;
font-family:ui-monospace,Menlo,Consolas,monospace;font-size:92%}
.muted{color:var(--muted)}.red{color:var(--red);font-weight:600}
.table-wrap{overflow-x:auto;border:1px solid var(--border);border-radius:6px;margin:.6em 0}
table{border-collapse:separate;border-spacing:0;width:max-content;min-width:100%;font-size:13px}
th,td{padding:6px 10px;text-align:left;border-bottom:1px solid var(--border);
border-right:1px solid var(--border);vertical-align:top}
th{background:var(--th);position:sticky;top:0;cursor:pointer;white-space:nowrap;font-weight:600}
tr td:last-child,tr th:last-child{border-right:none}
tbody tr:nth-child(even) td{background:var(--alt)}tbody tr:hover td{background:var(--hover)}
tr.hidden{display:none}
details{border:1px solid var(--border);border-radius:6px;padding:8px 12px;margin:.5em 0;background:var(--alt)}
details[open]{background:var(--bg)}summary{cursor:pointer;font-weight:500}
.note{color:var(--muted);font-size:.92rem}
.cell{max-width:520px;overflow-wrap:anywhere}
"""

_JS = """<script>
(function(){
function cmp(a,b){var x=parseFloat(a.replace(/[, ]/g,'')),y=parseFloat(b.replace(/[, ]/g,''));
if(!isNaN(x)&&!isNaN(y))return x-y;return a.localeCompare(b,undefined,{numeric:true});}
document.querySelectorAll('table').forEach(function(t){
t.querySelectorAll('thead th').forEach(function(th,i){th.addEventListener('click',function(){
var asc=!th.classList.contains('asc');t.querySelectorAll('th').forEach(function(h){h.classList.remove('asc','desc');});
th.classList.add(asc?'asc':'desc');var tb=t.tBodies[0];var rows=[].slice.call(tb.rows);
rows.sort(function(r1,r2){var c=cmp((r1.cells[i]||{}).innerText||'',(r2.cells[i]||{}).innerText||'');return asc?c:-c;});
rows.forEach(function(r){tb.appendChild(r);});});});});
var f=document.getElementById('filter');if(f)f.addEventListener('input',function(){
var q=f.value.toLowerCase();document.querySelectorAll('tbody tr').forEach(function(tr){
tr.classList.toggle('hidden',q&&tr.innerText.toLowerCase().indexOf(q)<0);});
if(q)document.querySelectorAll('details').forEach(function(d){if(d.innerText.toLowerCase().indexOf(q)>=0)d.open=true;});});
var ea=document.getElementById('exp');if(ea)ea.onclick=function(){document.querySelectorAll('details').forEach(function(d){d.open=true;});};
var ca=document.getElementById('col');if(ca)ca.onclick=function(){document.querySelectorAll('details').forEach(function(d){d.open=false;});};
})();
</script>"""


class Doc:
    def __init__(self, title: str) -> None:
        self.title = title
        self.blocks: List[tuple] = []

    def h(self, level: int, text: str) -> None:
        self.blocks.append(("h", level, text))

    def p(self, text: str, note: bool = False) -> None:
        self.blocks.append(("p", text, note))

    def bullets(self, items: List[str]) -> None:
        self.blocks.append(("ul", items))

    def table(self, headers: List[str], rows: List[List[str]]) -> None:
        self.blocks.append(("table", headers, rows))

    def details_open(self, summary: str, red: bool = False) -> None:
        self.blocks.append(("do", summary, red))

    def details_close(self) -> None:
        self.blocks.append(("dc",))

    # -- markdown --
    def render_md(self) -> str:
        out: List[str] = [f"# {self.title}", ""]
        for b in self.blocks:
            if b[0] == "h":
                out.append(f"{'#' * b[1]} {b[2]}"); out.append("")
            elif b[0] == "p":
                out.append(f"_{b[1]}_" if b[2] else b[1]); out.append("")
            elif b[0] == "ul":
                out.extend(f"- {it}" for it in b[1]); out.append("")
            elif b[0] == "table":
                out.extend(self._md_table(b[1], b[2])); out.append("")
            elif b[0] == "do":
                out.append("<details>")
                s = b[1]
                out.append(f"<summary>{'<strong>'+s+'</strong>' if b[2] else s}</summary>")
                out.append("")
            elif b[0] == "dc":
                out.append("</details>"); out.append("")
        return "\n".join(out) + "\n"

    @staticmethod
    def _md_cell(s: str) -> str:
        return str(s).replace("|", "\\|").replace("\n", " ")

    def _md_table(self, headers, rows) -> List[str]:
        if not rows:
            return ["_No data._"]
        o = ["| " + " | ".join(headers) + " |",
             "|" + "|".join(["---"] * len(headers)) + "|"]
        for r in rows:
            o.append("| " + " | ".join(self._md_cell(c) for c in r) + " |")
        return o

    # -- html --
    def render_html(self) -> str:
        parts: List[str] = []
        for b in self.blocks:
            if b[0] == "h":
                parts.append(f"<h{b[1]}>{_esc(b[2])}</h{b[1]}>")
            elif b[0] == "p":
                cls = " class='note'" if b[2] else ""
                parts.append(f"<p{cls}>{_esc(b[1])}</p>")
            elif b[0] == "ul":
                parts.append("<ul>" + "".join(f"<li>{_esc(it)}</li>" for it in b[1]) + "</ul>")
            elif b[0] == "table":
                parts.append(self._html_table(b[1], b[2]))
            elif b[0] == "do":
                inner = f"<strong class='red'>{_esc(b[1])}</strong>" if b[2] else _esc(b[1])
                parts.append(f"<details><summary>{inner}</summary>")
            elif b[0] == "dc":
                parts.append("</details>")
        head = (
            "<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{_esc(self.title)}</title><style>{_CSS}</style></head><body>"
            "<div class='topbar'><h1>" + _esc(self.title) + "</h1>"
            "<div class='actions'><input id='filter' type='search' placeholder='Filter rows...'>"
            "<button id='exp'>Expand all</button><button id='col'>Collapse all</button></div></div>"
            "<div class='content'>"
        )
        tail = "</div>" + _JS + "</body></html>"
        return head + "\n".join(parts) + tail

    @staticmethod
    def _html_table(headers, rows) -> str:
        if not rows:
            return "<p class='note'><em>No data.</em></p>"
        h = "".join(f"<th>{_esc(x)}</th>" for x in headers)
        body = []
        for r in rows:
            body.append("<tr>" + "".join(
                f"<td><div class='cell'>{_esc(str(c))}</div></td>" for c in r) + "</tr>")
        return f"<div class='table-wrap'><table><thead><tr>{h}</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"


def _fmt_off(mins: int) -> str:
    sign = "+" if mins >= 0 else "-"
    a = abs(mins)
    return f"{sign}{a // 60:02d}:{a % 60:02d} ({mins:+d} min)"


FM_ROW_CAP = 50
# Parses a cross-node "+N more derivative Xid <dxid> ... suppressed" note ->
# (count, derivative_xid) so the affected Xid row can be flagged.
_SUP_PARSE = re.compile(r"\+\s*(\d+)\s+more\s+derivative\s+Xid\s+(\d+)", re.I)


def _fm_slim_rows(ev, cap: int = FM_ROW_CAP):
    """Collapse a port_state Event's FM table (Event.extra) into distinct errors.

    Rows are merged by (Level, Category, Detail): identical error lines become one
    row that lists every affected Compute Slot and the time span over which the
    error occurred. The GPU GUID and per-row repeat counts are dropped for
    compactness. Returns (display_headers, rows, distinct, total) or None if there
    is no FM table.
    """
    hdr = ev.extra.get("fm_header") or []
    raw = ev.extra.get("fm_rows") or []
    if not raw:
        return None

    def col(name):
        return hdr.index(name) if name in hdr else -1

    ti, li, ci, si, di = (col("Time"), col("Level"), col("Category"),
                          col("Compute Slot Idx"), col("Detail"))

    def cell(cells, i):
        return (cells[i] if 0 <= i < len(cells) else "") or ""

    groups: dict = {}
    order: List[tuple] = []
    for cells, _cnt in raw:
        key = (cell(cells, li) or "-", cell(cells, ci) or "-", cell(cells, di) or "-")
        g = groups.get(key)
        if g is None:
            g = {"times": set(), "slots": set()}
            groups[key] = g
            order.append(key)
        t = cell(cells, ti)
        if t:
            g["times"].add(t)
        slot = cell(cells, si)
        if slot and slot != "-":
            g["slots"].add(slot)

    def slot_key(s):
        try:
            return (0, int(s))
        except ValueError:
            return (1, s)

    def duration(times):
        if not times:
            return "-"
        lo, hi = min(times), max(times)
        return lo if lo == hi else f"{lo} – {hi}"

    rows = []
    for level, cat, detail in order:
        g = groups[(level, cat, detail)]
        slots = ", ".join(str(s) for s in sorted(g["slots"], key=slot_key)) or "-"
        rows.append([duration(g["times"]), level, cat, slots, detail])
    rows.sort(key=lambda r: r[0])   # chronological by span start
    disp = ["Time", "Level", "Category", "Compute Tray Index", "Detail"]
    return disp, rows[:cap], len(order), ev.extra.get("fm_total", 0)


def _xref(cross, kind: str, dt, tol: int = 120) -> Optional[int]:
    """Cross-node event-group id whose [start,end] best matches dt (same kind).
    Nearest-match falls back within ``tol`` seconds (the correlation window)."""
    if cross is None:
        return None
    groups = cross.xid_groups if kind == "xid" else cross.imex_groups
    best, bestd = None, None
    for gid, s, e in groups:
        if s <= dt <= e:
            return gid
        d = min(abs((dt - s).total_seconds()), abs((dt - e).total_seconds()))
        if bestd is None or d < bestd:
            best, bestd = gid, d
    return best if (bestd is not None and bestd <= tol) else None


def _fnm_hits(fnm_all, sw, window_s: int):
    """FNM port-loss events on the same switch whose loss time is within window_s
    of the fabric group's [start, end] (both are switch-side / same clock)."""
    out = []
    for e in fnm_all:
        if e.source_id != sw.source_id:
            continue
        if e.start < sw.start:
            gap = (sw.start - e.start).total_seconds()
        elif e.start > sw.end:
            gap = (e.start - sw.end).total_seconds()
        else:
            gap = 0
        if gap <= window_s:
            out.append(e)
    return out


def build_report(res: Result, trays: List[TrayReport], switches: List[SwitchReport],
                 auto_tz: bool, cross=None) -> Doc:
    d = Doc("Xid ↔ NVOS Correlation Report")
    tray_index_by_host = {t.hostname: t.tray_index for t in trays if t.hostname}

    n_ps = sum(len(n.port_state_events) for s in switches for n in s.nodes)
    n_fnm = sum(len(n.fnm_events) for s in switches for n in s.nodes)
    chassis = sorted({t.chassis_sn for t in trays if t.chassis_sn}
                     | {n.chassis for s in switches for n in s.nodes if n.chassis})

    if cross is not None:
        # Match the cross-node report's merged timelines (what §2 now cites).
        compute_line = (f"Compute trays (nv-bug-report): {len(trays)} — cross-node Xid event "
                        f"groups: {len(cross.xid_groups)}, IMEX event groups: "
                        f"{len(cross.imex_groups)}")
    else:
        n_xid = sum(len(t.xid_events) for t in trays)
        n_imex = sum(len(t.imex_events) for t in trays)
        compute_line = (f"Compute trays (nv-bug-report): {len(trays)} — per-node Xid event "
                        f"groups: {n_xid}, IMEX event groups: {n_imex} (no cross-node report)")

    d.bullets([
        compute_line,
        f"Switch dumps (NVOS/NMX-C): {len(switches)} — nvos event groups: {n_ps}, FNM port-loss events: {n_fnm}",
        f"Chassis (rack) key(s): {', '.join(chassis) if chassis else '(none detected)'}",
        f"Correlation window: ±{res.window_s}s | Timezone offset applied to switch side: {_fmt_off(res.offset_min)}"
        + ("  [auto-selected]" if auto_tz else ""),
        f"Chassis-scoped correlation: {'yes' if res.chassis_scoped else 'no (cross-chassis allowed)'}",
    ])

    # 1. Timezone alignment
    d.h(2, "1. Timezone Alignment")
    d.p("Both report families stamp events in local wall-clock time with no timezone "
        "marker, so the switch side is shifted by an offset before matching. The table "
        "below shows the top-scoring candidate offsets (scored by how many compute↔switch "
        "event starts align); re-run with --tz-offset-minutes (or --auto-tz) to apply one.",
        note=True)
    if res.suggestions:
        top = res.suggestions[:3]
        if all(off != res.offset_min for off, _ in top):  # always keep the applied offset
            top = top + [s for s in res.suggestions if s[0] == res.offset_min][:1]
        rows = [[_fmt_off(off), str(hits), "◀ applied" if off == res.offset_min else
                 ("best" if (off, hits) == res.suggestions[0] else "")]
                for off, hits in top]
        d.table(["Offset (switch → tray)", "Aligned start hits", "Note"], rows)
    else:
        d.p("Not enough events on both sides to suggest an offset.", note=True)

    # 2. Correlated events
    d.h(2, "2. Correlated Events")
    corr_xid_egs: set = set()   # cross-node Xid EG ids cited by any fabric group
    corr_imex_egs: set = set()  # cross-node IMEX EG ids cited by any fabric group
    if res.correlations:
        # Invert compute→switch into switch→[compute]: one fold per switch (nvos)
        # event, cross-referenced to the nvbr cross-node report's event groups.
        # Fold only on port-state fabric groups; a matched FNM is surfaced inside the
        # time-aligned fold (via _fnm_hits below), never as its own fold.
        groups: dict = {}
        for c in res.correlations:
            for s, delta in c.switches:
                if s.kind != "port_state":
                    continue
                key = (s.source_id, s.ref, s.kind, s.start)
                g = groups.get(key)
                if g is None:
                    g = {"sw": s, "rows": []}
                    groups[key] = g
                g["rows"].append((c.compute, delta))
        ordered = sorted(groups.values(), key=lambda g: g["sw"].start)
        # Coverage is counted from what the folds actually DISPLAY, so the numbers
        # match the report body exactly. A fold is one port-state fabric event that
        # correlated with compute-tray Xid/IMEX; the FNM shown inside it is
        # switch-side context (see _fnm_hits: same-clock proximity to the port-state
        # group), NOT the engine's separate offset-based switch↔compute FNM match —
        # counting the two from one source keeps them from disagreeing.
        fnm_all = [e for s in switches for n in s.nodes for e in n.fnm_events]
        fnm_shown_ids = {id(e) for g in ordered
                         for e in _fnm_hits(fnm_all, g["sw"], res.window_s)}
        d.p(f"{len(ordered)} of {n_ps} port-state event group(s) correlated with "
            f"compute-tray Xid/IMEX (within ±{res.window_s}s at the applied offset); "
            f"the remaining {n_ps - len(ordered)} had no compute-side Xid/IMEX in the "
            f"same window (routine port flaps). {len(fnm_shown_ids)} FNM port-loss "
            f"event(s) are surfaced as switch-side context within the folds below.")
        d.p("Each fold is one port-state fabric event, cross-referenced to the nvbr "
            "cross-node report's Xid / IMEX event group(s), with a node-deduped Xid "
            "raw-log summary, any FNM port-loss events in the same switch-side window, "
            "and the matching Fabric Manager log (nested, collapsed).", note=True)
        if cross is None:
            d.p("nvbr cross-node report not found among the inputs — folds fall back to "
                "compute-event counts instead of cross-node event-group numbers.", note=True)
        for g in ordered:
            sw = g["sw"]
            comp_rows = g["rows"]
            is_xid = any(ce.kind == "xid" for ce, _ in comp_rows)
            sev = sw.extra.get("severity")
            sev_tag = f"[{sev}] " if sev else ""
            xid_egs = sorted({_xref(cross, "xid", ce.start, res.window_s)
                              for ce, _ in comp_rows if ce.kind == "xid"} - {None})
            imex_egs = sorted({_xref(cross, "imex", ce.start, res.window_s)
                               for ce, _ in comp_rows if ce.kind == "imex"} - {None})
            corr_xid_egs.update(xid_egs)
            corr_imex_egs.update(imex_egs)
            parts = []
            if xid_egs:
                parts.append("Xid Event Group " + ", ".join(str(i) for i in xid_egs))
            if imex_egs:
                parts.append("IMEX Event Group " + ", ".join(str(i) for i in imex_egs))
            xref = ("nvbr " + "; ".join(parts)) if parts else f"{len(comp_rows)} compute event(s)"
            d.details_open(
                f"{sw.ref} {sev_tag}@ {T.fmt(T.shift(sw.start, res.offset_min))} "
                f"(switch raw {T.fmt(sw.start)}) ↔ {xref}", red=is_xid)
            if cross is not None and xid_egs:
                agg: dict = {}   # (xid, mnem, sev) -> {"ex": str, "hosts": [..]}
                order: List[tuple] = []
                for gid in xid_egs:
                    for entry in cross.xid_details.get(gid, []):
                        xid, mnem, sev_x, ex = entry[0], entry[1], entry[2], entry[3]
                        hosts = entry[4] if len(entry) > 4 else []
                        key = (xid, mnem, sev_x)
                        if key not in agg:
                            agg[key] = {"ex": ex, "hosts": []}
                            order.append(key)
                        for h in hosts:
                            if h not in agg[key]["hosts"]:
                                agg[key]["hosts"].append(h)
                # Suppressed-derivative counts per Xid number (from §4 "+N more …
                # suppressed"), so the affected Xid row can flag "(+N more
                # suppressed)" and fold in any hosts seen only in those notes.
                sup_by_xid: dict = {}
                for gid in xid_egs:
                    for host, text in cross.xid_suppressed.get(gid, []):
                        mm = _SUP_PARSE.search(text)
                        if not mm:
                            continue
                        dxid = mm.group(2)
                        s = sup_by_xid.setdefault(dxid, {"hosts": [], "count": 0})
                        s["count"] += int(mm.group(1))
                        if host and host not in s["hosts"]:
                            s["hosts"].append(host)
                if order:
                    xrows = []
                    for key in order:
                        xid, mnem, sev_x = key
                        hosts = list(agg[key]["hosts"])
                        sup = sup_by_xid.get(xid)
                        if sup:
                            for h in sup["hosts"]:
                                if h not in hosts:
                                    hosts.append(h)
                        hosts = sorted(hosts)
                        host_cell = ", ".join(hosts) or "-"
                        tray_cell = ", ".join(tray_index_by_host.get(h, "-") for h in hosts) or "-"
                        if sup:
                            tag = " (+ more suppressed)"
                            host_cell += tag
                            tray_cell += tag
                        xrows.append([xid, mnem or "-", sev_x or "-",
                                      host_cell, tray_cell, agg[key]["ex"]])
                    d.p("Xid raw log (cross-node Xid Event Group "
                        + ", ".join(str(i) for i in xid_egs) + ", deduped across nodes; "
                        "Hostname / Compute Tray Index list every compute tray that reported each Xid):")
                    d.table(["Xid", "Mnemonic", "Severity", "Hostname", "Compute Tray Index",
                             "Example NVRM raw log"], xrows)
                    sup_seen = set()
                    for gid in xid_egs:
                        for host, text in cross.xid_suppressed.get(gid, []):
                            if (host, text) in sup_seen:
                                continue
                            sup_seen.add((host, text))
                            ti = tray_index_by_host.get(host, "")
                            d.p(f"{text} — {host}" + (f" [tray idx {ti}]" if ti else ""),
                                note=True)
            fnm = _fnm_hits(fnm_all, sw, res.window_s)
            if fnm:
                fnm = sorted(fnm, key=lambda e: e.start)
                frows = [[T.fmt(e.start), e.extra.get("port", "-"),
                          e.extra.get("down", "") or "-",
                          e.extra.get("peer_host", "") or "-",
                          e.extra.get("recovered", "") or "-"]
                         for e in fnm[:FM_ROW_CAP]]
                d.p(f"FNM port loss (nvos Other FabricManager Log Highlights, within "
                    f"±{res.window_s}s):")
                d.table(["FM Time", "Port", "Transition", "Peer host", "Recovered"], frows)
                if len(fnm) > FM_ROW_CAP:
                    d.p(f"… +{len(fnm) - FM_ROW_CAP} more FNM event(s) suppressed", note=True)
            fm = _fm_slim_rows(sw)
            if fm:
                disp, frows, unique, total = fm
                d.details_open(f"Fabric Manager log — {sw.ref} (same time window): "
                               f"{unique} distinct error(s) / {total} FM rows")
                d.table(disp, frows)
                if unique > FM_ROW_CAP:
                    d.p(f"… +{unique - FM_ROW_CAP} more distinct error(s) suppressed", note=True)
                d.p("This is a de-duplicated summary (merged by error); for the full "
                    "Fabric Manager log see the nvos-tech-dump-tools-for-nmx-c report "
                    f"for {sw.ref}.", note=True)
                d.details_close()
            d.details_close()
    else:
        d.p("No compute-tray event correlated with any switch event at the applied "
            "offset. Check the Timezone Alignment table above for a better offset.", note=True)

    # 3. Uncorrelated compute events — as cross-node event groups when available
    d.h(2, "3. Uncorrelated Compute-Tray Events")
    if cross is not None:
        d.p("Cross-node event groups (nvbr cross-node report) that did NOT time-correlate "
            "with any switch fabric event in §2 (cross-node granularity).")
        un_xid = [g for g in sorted(cross.xid_groups) if g[0] not in corr_xid_egs]
        xid_sum = (f"Xid: {len(un_xid)} of {len(cross.xid_groups)} cross-node Xid Event "
                   f"Group(s) uncorrelated")
        if un_xid:
            rows = []
            for gid, s, e in un_xid:
                xids = "; ".join(
                    " ".join(p for p in (f"Xid {xid}", mnem, sev) if p)
                    for xid, mnem, sev, _ex, _hosts in cross.xid_details.get(gid, []))
                rows.append([f"Xid Event Group {gid}", T.fmt(s), T.fmt(e), xids or "-"])
            d.details_open(xid_sum)
            d.table(["nvbr ref", "Start", "End", "Xid types"], rows)
            d.details_close()
        else:
            d.p(xid_sum + ".")
        un_imex = [g for g in sorted(cross.imex_groups) if g[0] not in corr_imex_egs]
        imex_sum = (f"IMEX: {len(un_imex)} of {len(cross.imex_groups)} cross-node IMEX Event "
                    f"Group(s) uncorrelated")
        if un_imex:
            rows = [[f"IMEX Event Group {gid}", T.fmt(s), T.fmt(e)] for gid, s, e in un_imex]
            d.details_open(imex_sum)
            d.table(["nvbr ref", "Start", "End"], rows)
            d.details_close()
            d.p("Note: IMEX event groups with no switch-side fabric correlation are commonly "
                "caused by an IMEX service restart or a transient inter-node network fluctuation, "
                "rather than a switch fabric fault.", note=True)
        else:
            d.p(imex_sum + ".")
    else:
        unmatched_xid = [e for e in res.unmatched_compute if e.kind == "xid"]
        unmatched_imex = [e for e in res.unmatched_compute if e.kind == "imex"]
        d.p(f"Xid groups with no switch correlation: {len(unmatched_xid)} | "
            f"IMEX groups with no switch correlation: {len(unmatched_imex)}")
        if res.unmatched_compute:
            rows = [[T.fmt(e.start), T.fmt(e.end), e.kind.upper(), e.source_id,
                     e.chassis or "-", e.label, e.ref]
                    for e in sorted(res.unmatched_compute, key=lambda x: (x.kind != "xid", x.start))]
            d.details_open(f"{len(res.unmatched_compute)} uncorrelated compute event(s)")
            d.table(["Start", "End", "Kind", "Tray", "Chassis", "Event", "Ref"], rows)
            d.details_close()
        if unmatched_imex:
            d.p("Note: IMEX event groups with no switch-side fabric correlation are commonly "
                "caused by an IMEX service restart or a transient inter-node network fluctuation, "
                "rather than a switch fabric fault.", note=True)

    return d
