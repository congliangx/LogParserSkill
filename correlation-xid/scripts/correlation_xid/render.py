"""Compact dual (Markdown + self-contained HTML) renderer for the report.

Blocks are format-neutral; ``render_md`` / ``render_html`` walk them. Table cells
are plain text (escaped per backend). The HTML shell embeds its own CSS + a small
JS for sortable/filterable tables and expand/collapse-all, so it opens offline.
"""

from __future__ import annotations

from html import escape as _esc
from typing import List, Tuple

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
            body.append("<tr>" + "".join(f"<td>{_esc(str(c))}</td>" for c in r) + "</tr>")
        return f"<div class='table-wrap'><table><thead><tr>{h}</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"


def _fmt_off(mins: int) -> str:
    sign = "+" if mins >= 0 else "-"
    a = abs(mins)
    return f"{sign}{a // 60:02d}:{a % 60:02d} ({mins:+d} min)"


def build_report(res: Result, trays: List[TrayReport], switches: List[SwitchReport],
                 auto_tz: bool) -> Doc:
    d = Doc("Xid ↔ NVOS Correlation Report")

    n_xid = sum(len(t.xid_events) for t in trays)
    n_imex = sum(len(t.imex_events) for t in trays)
    n_ps = sum(len(n.port_state_events) for s in switches for n in s.nodes)
    n_fnm = sum(len(n.fnm_events) for s in switches for n in s.nodes)
    chassis = sorted({t.chassis_sn for t in trays if t.chassis_sn}
                     | {n.chassis for s in switches for n in s.nodes if n.chassis})

    d.bullets([
        f"Compute trays (nv-bug-report): {len(trays)} — Xid event groups: {n_xid}, IMEX event groups: {n_imex}",
        f"Switch dumps (NVOS/NMX-C): {len(switches)} — port-state event groups: {n_ps}, FNM port-loss events: {n_fnm}",
        f"Chassis (rack) key(s): {', '.join(chassis) if chassis else '(none detected)'}",
        f"Correlation window: ±{res.window_s}s | Timezone offset applied to switch side: {_fmt_off(res.offset_min)}"
        + ("  [auto-selected]" if auto_tz else ""),
        f"Chassis-scoped correlation: {'yes' if res.chassis_scoped else 'no (cross-chassis allowed)'}",
    ])

    # 1. Timezone alignment
    d.h(2, "1. Timezone Alignment")
    d.p("Both report families stamp events in local wall-clock time with no timezone "
        "marker, so the switch side is shifted by an offset before matching. The table "
        "below sweeps candidate offsets and scores each by how many compute↔switch event "
        "starts fall within the correlation window; re-run with --tz-offset-minutes "
        "(or --auto-tz) to apply the best one.", note=True)
    if res.suggestions:
        top = res.suggestions[:10]
        rows = [[_fmt_off(off), str(hits), "◀ applied" if off == res.offset_min else
                 ("best" if (off, hits) == res.suggestions[0] else "")]
                for off, hits in top]
        d.table(["Offset (switch → tray)", "Aligned start hits", "Note"], rows)
    else:
        d.p("Not enough events on both sides to suggest an offset.", note=True)

    # 2. Correlated events
    d.h(2, "2. Correlated Events")
    if res.correlations:
        d.p(f"{len(res.correlations)} compute-tray event(s) have ≥1 time-overlapping "
            f"switch event (within ±{res.window_s}s at the applied offset).")
        rows = []
        for c in res.correlations:
            ce = c.compute
            sw_hosts = sorted({s.source_id for s, _ in c.switches})
            rows.append([
                T.fmt(ce.start), ce.kind.upper(), ce.source_id, ce.label,
                str(len(c.switches)), ", ".join(sw_hosts), ce.ref,
            ])
        d.table(["Compute time", "Kind", "Tray", "Compute event",
                 "# switch", "Switch host(s)", "Ref"], rows)
        # Details per correlation
        for c in res.correlations:
            ce = c.compute
            is_xid = ce.kind == "xid"
            d.details_open(
                f"{ce.kind.upper()} @ {T.fmt(ce.start)}–{T.fmt(ce.end)} [{ce.source_id}] "
                f"— {ce.label} ↔ {len(c.switches)} switch event(s)", red=is_xid)
            srows = []
            for s, delta in c.switches:
                srows.append([T.fmt(s.start), T.fmt(T.shift(s.start, res.offset_min)),
                              f"{delta}s", s.kind, s.source_id, s.chassis or "-",
                              s.label, s.detail, s.ref])
            d.table(["Switch time (raw)", "Switch time (shifted)", "Δ", "Kind", "Switch",
                     "Chassis", "Event", "Detail", "Ref"], srows)
            if ce.detail:
                d.p(f"compute-side note: {ce.detail}", note=True)
            d.details_close()
    else:
        d.p("No compute-tray event correlated with any switch event at the applied "
            "offset. Check the Timezone Alignment table above for a better offset.", note=True)

    # 3. Unmatched compute events (Xid highlighted)
    d.h(2, "3. Uncorrelated Compute-Tray Events")
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

    # 4. Switch coverage
    d.h(2, "4. Switch Event Coverage")
    d.p(f"Switch events correlated to a compute event: {len(res.matched_switch)} / "
        f"{res.total_switch}. (Unmatched switch events are typically routine port "
        "flaps with no compute-tray Xid/IMEX in the same window.)")
    if res.matched_switch:
        rows = [[T.fmt(e.start), e.kind, e.source_id, e.chassis or "-", e.label, e.detail, e.ref]
                for e in sorted(res.matched_switch, key=lambda x: x.start)]
        d.details_open(f"{len(res.matched_switch)} correlated switch event(s)")
        d.table(["Switch time (raw)", "Kind", "Switch", "Chassis", "Event", "Detail", "Ref"], rows)
        d.details_close()
    return d
