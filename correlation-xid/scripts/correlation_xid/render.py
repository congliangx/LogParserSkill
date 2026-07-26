"""Dual (Markdown + self-contained HTML) renderer for the correlation report.

Blocks are format-neutral; ``render_md`` / ``render_html`` walk them. Table cells
are plain text (escaped per backend). Every table can carry per-column
explanations — rendered as HTML header tooltips plus an optional visible column
guide — and §2 renders each correlated fabric event as a structured "event card"
in HTML (severity rail, kind chips, and a switch→tray **offset bridge** showing
both clocks joined by the applied offset) while staying a single summary line in
Markdown. The HTML shell embeds its own CSS + a small JS for sortable/filterable
tables, expand/collapse-all, anchor navigation and a left sidebar TOC (sections +
§2 event cards, scroll-spy highlighted), so it opens offline.
"""

from __future__ import annotations

import bisect
import re
from datetime import timedelta
from html import escape as _esc
from typing import Dict, List, Optional, Tuple

from . import timeutil as T
from .engine import Result
from .engine import anchors as E_anchors
from .models import (
    FNM_KINDS,
    RAW_KINDS,
    SWITCH_KIND_LABEL,
    SWITCH_KIND_ORDER,
    SWITCH_KIND_TAG,
    SwitchReport,
    TrayReport,
    worst_severity,
)

_CSS = """
:root{
--paper:#f6f7f9;--panel:#fff;--ink:#171b21;--muted:#5c6673;--line:#dfe4eb;
--line-soft:#eceff4;--code:#eef1f5;--bar:rgba(255,255,255,.92);
--signal:#537f00;--signal-bg:rgba(118,185,0,.14);
--fatal:#c42b3d;--fatal-bg:rgba(196,43,61,.09);
--warn:#9a6700;--warn-bg:rgba(245,166,35,.13);
--info:#3b6ea5;--info-bg:rgba(59,110,165,.10);
--neutral:#5c6673;--neutral-bg:rgba(92,102,115,.10);
--shadow:0 1px 2px rgba(15,23,42,.06),0 4px 14px rgba(15,23,42,.04);
--sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
--disp:"Segoe UI Variable Display","SF Pro Display",system-ui,-apple-system,"Segoe UI",sans-serif;
--mono:ui-monospace,"SFMono-Regular","Cascadia Mono","JetBrains Mono",Consolas,monospace;
}
@media(prefers-color-scheme:dark){:root{
--paper:#0c1116;--panel:#121922;--ink:#e7edf3;--muted:#8d9aa8;--line:#223040;
--line-soft:#1a2531;--code:#182230;--bar:rgba(18,25,34,.92);
--signal:#8cd211;--signal-bg:rgba(140,210,17,.14);
--fatal:#ff737c;--fatal-bg:rgba(255,115,124,.12);
--warn:#f0b429;--warn-bg:rgba(240,180,41,.13);
--info:#7aa7d9;--info-bg:rgba(122,167,217,.12);
--neutral:#8d9aa8;--neutral-bg:rgba(141,154,168,.13);
--shadow:0 1px 2px rgba(0,0,0,.4),0 4px 14px rgba(0,0,0,.25);
}}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.55 var(--sans)}
a{color:var(--signal)}
code{font-family:var(--mono);font-size:92%;background:var(--code);padding:1px 5px;border-radius:4px}
.note{color:var(--muted);font-size:12.5px}
.mono{font-family:var(--mono);font-size:12.5px}
:focus-visible{outline:2px solid var(--signal);outline-offset:2px;border-radius:4px}
.topbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:12px;
padding:10px 20px;background:var(--bar);backdrop-filter:blur(8px);
border-bottom:1px solid var(--line)}
.topbar h1{font:600 15px/1.2 var(--disp);letter-spacing:-.01em;margin:0}
.topbar .actions{margin-left:auto;display:flex;gap:8px;flex-wrap:wrap}
.topbar input,.topbar button{background:var(--paper);border:1px solid var(--line);
color:var(--ink);padding:5px 11px;border-radius:6px;font-size:13px;font-family:var(--sans)}
.topbar input{width:190px}
.topbar input:focus{border-color:var(--signal);outline:none}
.topbar button{cursor:pointer}
.topbar button:hover{border-color:var(--signal);color:var(--signal)}
.shell{max-width:1220px;margin:0 auto}
.shell.with-nav{display:grid;grid-template-columns:238px minmax(0,1fr);max-width:1480px}
.sidenav{position:sticky;top:var(--barh,50px);align-self:start;
max-height:calc(100vh - var(--barh,50px));overflow-y:auto;
padding:20px 12px 48px 20px;border-right:1px solid var(--line);
display:flex;flex-direction:column;gap:2px}
.sidenav a{display:flex;align-items:center;gap:8px;text-decoration:none;color:var(--muted);
padding:6px 10px;border-radius:6px;font-size:12.5px;border-left:2px solid transparent}
.sidenav a:hover{color:var(--ink);background:var(--line-soft)}
.sidenav a.active{color:var(--ink);background:var(--line-soft);border-left-color:var(--signal)}
.sn-h{font-weight:600;margin-top:12px}
.sidenav a.sn-h:first-of-type{margin-top:0}
.sn-no{font:600 10px/1 var(--mono);color:var(--muted);border:1px solid var(--line);
border-radius:3px;padding:3px 4px;letter-spacing:.08em;flex:0 0 auto}
.sn-item{margin-left:14px;font-family:var(--mono)}
.sn-item .dot{width:6px;height:6px;border-radius:50%;background:var(--neutral);flex:0 0 auto}
.sn-item.sev-fatal .dot{background:var(--fatal)}
.sn-item.sev-warn .dot{background:var(--warn)}
.sn-item .sn-dt{margin-left:auto;color:var(--muted);font-size:11px;white-space:nowrap}
@media(max-width:1080px){.sidenav{display:none}.shell.with-nav{display:block}}
.content{min-width:0;padding:8px 24px 80px}
h2{scroll-margin-top:64px}
.params{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0 6px}
.param{display:inline-flex;align-items:baseline;gap:7px;border:1px solid var(--line);
background:var(--panel);border-radius:6px;padding:4px 10px;
font-family:var(--mono);font-size:12px}
.param b{font-weight:600;color:var(--muted);font-family:var(--sans);font-size:10px;
text-transform:uppercase;letter-spacing:.08em}
h2{display:flex;align-items:center;gap:10px;font:700 20px/1.2 var(--disp);
letter-spacing:-.015em;margin:2.1em 0 .6em}
h2::after{content:"";flex:1;height:1px;background:var(--line)}
h2 .secno{font:600 11px/1 var(--mono);color:var(--muted);border:1px solid var(--line);
border-radius:4px;padding:5px 6px;letter-spacing:.1em;background:var(--panel)}
h3,h4{line-height:1.25;margin:1.2em 0 .5em;font-family:var(--disp)}
ul{padding-left:1.3em;margin:.5em 0}
li{margin:.15em 0}
.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:8px;margin:.7em 0;
background:var(--panel);box-shadow:var(--shadow)}
table{border-collapse:separate;border-spacing:0;width:max-content;min-width:100%;font-size:13px}
th,td{padding:7px 11px;text-align:left;border-bottom:1px solid var(--line-soft);vertical-align:top}
thead th{position:sticky;top:0;z-index:1;background:var(--panel);font:600 11px/1.3 var(--sans);
text-transform:uppercase;letter-spacing:.06em;color:var(--muted);cursor:pointer;
white-space:nowrap;border-bottom:1px solid var(--line)}
thead th:hover{color:var(--ink)}
thead th.asc::after{content:" \\25B4";color:var(--signal)}
thead th.desc::after{content:" \\25BE";color:var(--signal)}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover td{background:var(--line-soft)}
td .cell{max-width:560px;overflow-wrap:anywhere}
tr.hidden,details.hidden{display:none}
.badge{display:inline-block;font:600 11px/1 var(--mono);padding:3px 7px;border-radius:99px}
.b-fatal{color:var(--fatal);background:var(--fatal-bg)}
.b-warn{color:var(--warn);background:var(--warn-bg)}
.b-info{color:var(--info);background:var(--info-bg)}
.b-muted{color:var(--muted);background:var(--neutral-bg)}
.colguide{font-size:12px;color:var(--muted);margin:.2em 0 1em}
.colguide b{color:var(--ink);font-weight:600}
details{border:1px solid var(--line);border-radius:8px;background:var(--panel);
margin:.6em 0;padding:2px 14px}
details>summary{list-style:none;cursor:pointer;margin:0 -14px;padding:10px 14px;
display:flex;flex-wrap:wrap;align-items:center;gap:9px;font-weight:500}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"";width:7px;height:7px;flex:0 0 auto;
border-right:1.6px solid var(--muted);border-bottom:1.6px solid var(--muted);
transform:rotate(-45deg);transition:transform .16s ease}
details[open]>summary::before{transform:rotate(45deg)}
@media(prefers-reduced-motion:reduce){details>summary::before{transition:none}}
details[open]>summary{border-bottom:1px solid var(--line-soft)}
details>*:last-child{margin-bottom:12px}
details.legend{background:transparent;border-style:dashed;box-shadow:none}
.legend dl{display:grid;grid-template-columns:max-content 1fr;gap:7px 16px;
margin:12px 0;font-size:13px}
.legend dt{font:600 12px/1.5 var(--mono);white-space:nowrap;color:var(--ink)}
.legend dd{margin:0;color:var(--muted)}
@media(max-width:760px){.legend dl{grid-template-columns:1fr}
.legend dd{margin:0 0 6px}.legend dt{white-space:normal}}
details.fold{border-left:3px solid var(--neutral);scroll-margin-top:64px}
details.fold.sev-fatal{border-left-color:var(--fatal)}
details.fold.sev-warn{border-left-color:var(--warn)}
.chipset{display:inline-flex;gap:6px;flex-wrap:wrap}
.chip{display:inline-block;font:700 10px/1 var(--sans);letter-spacing:.08em;
text-transform:uppercase;padding:4px 7px;border-radius:4px}
.c-fatal{color:var(--fatal);background:var(--fatal-bg)}
.c-warn{color:var(--warn);background:var(--warn-bg)}
.c-neutral{color:var(--muted);background:var(--neutral-bg)}
.c-kind{color:var(--muted);background:none;border:1px solid var(--line)}
.c-kind.k-xid{color:var(--ink);border-color:var(--muted)}
.fref{font:600 13px var(--mono)}
.bridge{display:inline-flex;align-items:center;flex-wrap:wrap;row-gap:4px;
font-family:var(--mono);font-size:12.5px}
.bside{display:inline-flex;align-items:baseline;gap:6px}
.blab{font:600 9.5px/1 var(--sans);letter-spacing:.09em;text-transform:uppercase;
color:var(--muted)}
.bt{white-space:nowrap}
.bmid{display:inline-flex;align-items:center;justify-content:center;margin:0 10px;
position:relative;min-width:78px;height:14px}
.bmid::before{content:"";position:absolute;left:0;right:0;top:50%;
border-top:1px solid var(--line)}
.bmid::after{content:"";position:absolute;right:0;top:50%;width:5px;height:5px;
border-top:1.4px solid var(--muted);border-right:1.4px solid var(--muted);
transform:translateY(-53%) rotate(45deg)}
.boff{position:relative;z-index:1;background:var(--panel);padding:0 6px;
font:600 10.5px/1 var(--mono);color:var(--signal)}
.fxref{color:var(--muted);font-size:12.5px}
.evindex{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:.8em 0 1em}
.evlab{font:600 10.5px/1 var(--sans);text-transform:uppercase;letter-spacing:.09em;
color:var(--muted);margin-right:2px}
.evchip{display:inline-flex;align-items:center;gap:7px;padding:5px 10px;
border:1px solid var(--line);border-radius:99px;background:var(--panel);
text-decoration:none;color:var(--ink);font:500 12px var(--mono)}
.evchip:hover{border-color:var(--signal)}
.evchip .dot{width:7px;height:7px;border-radius:50%;background:var(--neutral);flex:0 0 auto}
.evchip.sev-fatal .dot{background:var(--fatal)}
.evchip.sev-warn .dot{background:var(--warn)}
.evchip .edt{color:var(--muted)}
pre.rawlog{overflow:auto;max-height:340px;margin:.6em 0;padding:10px 12px;
border:1px solid var(--line);border-radius:8px;background:var(--code);
font-family:var(--mono);font-size:12px;line-height:1.5;white-space:pre}
@media print{.topbar{position:static}.topbar .actions{display:none}
.sidenav{display:none}.shell.with-nav{display:block}
body{background:#fff}details{break-inside:avoid}}
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
var q=f.value.toLowerCase();
document.querySelectorAll('tbody tr').forEach(function(tr){
tr.classList.toggle('hidden',!!q&&tr.innerText.toLowerCase().indexOf(q)<0);});
document.querySelectorAll('details.fold').forEach(function(d){
d.classList.toggle('hidden',!!q&&d.innerText.toLowerCase().indexOf(q)<0);});
if(q)document.querySelectorAll('details').forEach(function(d){
if(d.innerText.toLowerCase().indexOf(q)>=0)d.open=true;});});
var ea=document.getElementById('exp');if(ea)ea.onclick=function(){document.querySelectorAll('details').forEach(function(d){d.open=true;});};
var ca=document.getElementById('col');if(ca)ca.onclick=function(){document.querySelectorAll('details').forEach(function(d){d.open=false;});};
function openTarget(){var h=location.hash;if(!h)return;var el=null;
try{el=document.querySelector(h);}catch(e){return;}
if(el&&el.tagName==='DETAILS')el.open=true;}
window.addEventListener('hashchange',openTarget);openTarget();
document.querySelectorAll('a.evchip, .sidenav a').forEach(function(a){a.addEventListener('click',function(){
var el=null;try{el=document.querySelector(a.getAttribute('href'));}catch(e){}
if(el&&el.tagName==='DETAILS')el.open=true;});});
var bar=document.querySelector('.topbar');
function setBar(){if(bar)document.documentElement.style.setProperty('--barh',bar.offsetHeight+'px');}
window.addEventListener('resize',setBar);setBar();
var navLinks=[].slice.call(document.querySelectorAll('.sidenav a'));
var navTargets=navLinks.map(function(a){
try{return document.querySelector(a.getAttribute('href'));}catch(e){return null;}});
function spy(){var y=window.scrollY+(bar?bar.offsetHeight:50)+30;var idx=-1;
navTargets.forEach(function(el,i){
if(el&&el.getBoundingClientRect().top+window.scrollY<=y)idx=i;});
navLinks.forEach(function(a,i){a.classList.toggle('active',i===idx);
if(i===idx)a.setAttribute('aria-current','true');else a.removeAttribute('aria-current');});}
var tick=false;
window.addEventListener('scroll',function(){if(!tick){tick=true;
requestAnimationFrame(function(){spy();tick=false;});}},{passive:true});
spy();
})();
</script>"""

# Cell values recognised as status badges in badge-enabled columns.
_BADGE = {
    "fatal": "fatal", "nvl_fatal": "fatal", "error": "fatal",
    "nonfatal": "warn", "nvl_non_fatal": "warn", "warning": "warn",
    "connection_lost": "info",
    "none": "muted", "-": "muted", "info": "muted",
}


class Doc:
    def __init__(self, title: str, meta_chips: Optional[List[Tuple[str, str]]] = None) -> None:
        self.title = title
        self.meta_chips = meta_chips or []  # [(label, value)] — HTML masthead only
        self.blocks: List[tuple] = []

    def h(self, level: int, text: str) -> None:
        self.blocks.append(("h", level, text))

    def p(self, text: str, note: bool = False) -> None:
        self.blocks.append(("p", text, note))

    def bullets(self, items: List[str]) -> None:
        self.blocks.append(("ul", items))

    def pre(self, lines: List[str]) -> None:
        """Verbatim log lines (fenced block in Markdown, ``<pre>`` in HTML)."""
        self.blocks.append(("pre", list(lines)))

    def legend(self, summary: str, items: List[Tuple[str, str]]) -> None:
        """Collapsed term→meaning glossary (definition list in HTML)."""
        self.blocks.append(("legend", summary, items))

    def table(self, headers: List[str], rows: List[List[str]],
              col_notes: Optional[List[Optional[str]]] = None,
              badges: Optional[set] = None, mono_cols: Optional[set] = None,
              guide: bool = False) -> None:
        """``col_notes`` (aligned with headers) become HTML header tooltips; with
        ``guide=True`` they are also printed as a visible column guide under the
        table (both formats). ``badges``/``mono_cols`` are column indexes that
        get status-badge / monospace styling in HTML."""
        self.blocks.append(("table", headers, rows, col_notes, badges or set(),
                            mono_cols or set(), guide))

    def details_open(self, summary: str, red: bool = False) -> None:
        self.blocks.append(("do", summary, red))

    def fold_open(self, md_summary: str, info: Dict) -> None:
        """§2 event card. ``md_summary`` is the Markdown one-liner; ``info`` keys:
        anchor, ref, sev_class ('fatal'|'warn'|'neutral'), sev_label, kind_label,
        t_raw, t_tray, off, xref, red."""
        self.blocks.append(("fo", md_summary, info))

    def details_close(self) -> None:
        self.blocks.append(("dc",))

    def chips(self, label: str, items: List[Dict]) -> None:
        """HTML-only anchor navigation strip. Item keys: anchor, ref, dt, sev_class."""
        self.blocks.append(("chips", label, items))

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
            elif b[0] == "pre":
                out.append("```text"); out.extend(b[1]); out.append("```"); out.append("")
            elif b[0] == "legend":
                out.append("<details>")
                out.append(f"<summary>{b[1]}</summary>")
                out.append("")
                out.extend(f"- **{term}** — {mean}" for term, mean in b[2])
                out.append("")
                out.append("</details>"); out.append("")
            elif b[0] == "table":
                out.extend(self._md_table(b[1], b[2])); out.append("")
                if b[6] and b[3]:  # guide
                    pairs = [f"**{h}** — {n.rstrip('.')}" for h, n in zip(b[1], b[3]) if n]
                    if pairs:
                        out.append("_Columns: " + "; ".join(pairs) + "._"); out.append("")
            elif b[0] == "do":
                out.append("<details>")
                s = b[1]
                out.append(f"<summary>{'<strong>'+s+'</strong>' if b[2] else s}</summary>")
                out.append("")
            elif b[0] == "fo":
                out.append("<details>")
                s = b[1]
                out.append(f"<summary>{'<strong>'+s+'</strong>' if b[2].get('red') else s}</summary>")
                out.append("")
            elif b[0] == "dc":
                out.append("</details>"); out.append("")
            elif b[0] == "chips":
                pass  # HTML-only navigation aid
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
    _SECNO = re.compile(r"^(\d+)\.\s*(.*)$")

    def render_html(self) -> str:
        parts: List[str] = []
        for b in self.blocks:
            if b[0] == "h":
                level, text = b[1], b[2]
                m = self._SECNO.match(text) if level == 2 else None
                if m:
                    parts.append(f"<h2 id='sec-{m.group(1)}'><span class='secno'>"
                                 f"{int(m.group(1)):02d}</span><span>{_esc(m.group(2))}</span></h2>")
                else:
                    parts.append(f"<h{level}>{_esc(text)}</h{level}>")
            elif b[0] == "p":
                cls = " class='note'" if b[2] else ""
                parts.append(f"<p{cls}>{_esc(b[1])}</p>")
            elif b[0] == "ul":
                parts.append("<ul>" + "".join(f"<li>{_esc(it)}</li>" for it in b[1]) + "</ul>")
            elif b[0] == "pre":
                parts.append("<pre class='rawlog'>"
                             + "\n".join(_esc(ln) for ln in b[1]) + "</pre>")
            elif b[0] == "legend":
                dl = "".join(f"<dt>{_esc(t)}</dt><dd>{_esc(m)}</dd>" for t, m in b[2])
                parts.append(f"<details class='legend'><summary>{_esc(b[1])}</summary>"
                             f"<dl>{dl}</dl></details>")
            elif b[0] == "table":
                parts.append(self._html_table(b[1], b[2], b[3], b[4], b[5]))
                if b[6] and b[3]:
                    pairs = [f"<b>{_esc(h)}</b> — {_esc(n.rstrip('.'))}"
                             for h, n in zip(b[1], b[3]) if n]
                    if pairs:
                        parts.append("<p class='colguide'>Columns: " + "; ".join(pairs) + ".</p>")
            elif b[0] == "do":
                inner = f"<strong>{_esc(b[1])}</strong>" if b[2] else _esc(b[1])
                parts.append(f"<details><summary>{inner}</summary>")
            elif b[0] == "fo":
                parts.append(self._html_fold(b[2]))
            elif b[0] == "dc":
                parts.append("</details>")
            elif b[0] == "chips":
                items = "".join(
                    f"<a class='evchip sev-{_esc(it['sev_class'])}' href='#{_esc(it['anchor'])}'>"
                    f"<span class='dot'></span><span class='eref'>{_esc(it['ref'])}</span>"
                    f"<span class='edt'>{_esc(it['dt'])}</span></a>"
                    for it in b[2])
                parts.append(f"<nav class='evindex' aria-label='{_esc(b[1])}'>"
                             f"<span class='evlab'>{_esc(b[1])}</span>{items}</nav>")
        chips = "".join(f"<span class='param'><b>{_esc(k)}</b>{_esc(v)}</span>"
                        for k, v in self.meta_chips)
        masthead = f"<div class='params'>{chips}</div>" if chips else ""
        nav = self._sidenav()
        shell_cls = "shell with-nav" if nav else "shell"
        head = (
            "<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{_esc(self.title)}</title><style>{_CSS}</style></head><body>"
            "<div class='topbar'><h1>" + _esc(self.title) + "</h1>"
            "<div class='actions'><input id='filter' type='search' placeholder='Filter rows…' "
            "aria-label='Filter rows'>"
            "<button id='exp'>Expand all</button><button id='col'>Collapse all</button></div></div>"
            f"<div class='{shell_cls}'>" + nav + "<div class='content'>" + masthead
        )
        tail = "</div></div>" + _JS + "</body></html>"
        return head + "\n".join(parts) + tail

    def _sidenav(self) -> str:
        """Left navigation built from the numbered h2 sections and §2 event cards.
        Empty string when there is nothing to link (sidebar omitted)."""
        items: List[str] = []
        for b in self.blocks:
            if b[0] == "h" and b[1] == 2:
                m = self._SECNO.match(b[2])
                if m:
                    items.append(
                        f"<a class='sn-h' href='#sec-{m.group(1)}'>"
                        f"<span class='sn-no'>{int(m.group(1)):02d}</span>"
                        f"<span>{_esc(m.group(2))}</span></a>")
            elif b[0] == "fo":
                info = b[2]
                anchor = info.get("anchor", "")
                if not anchor:
                    continue
                label = info.get("nav") or (info.get("ref") or "event")
                dt = (info.get("t_tray") or "")[5:16]
                items.append(
                    f"<a class='sn-item sev-{_esc(info.get('sev_class', 'neutral'))}' "
                    f"href='#{_esc(anchor)}'><span class='dot'></span>"
                    f"<span>{_esc(label)}</span><span class='sn-dt'>{_esc(dt)}</span></a>")
        if not items:
            return ""
        return "<nav class='sidenav' aria-label='Contents'>" + "".join(items) + "</nav>"

    @staticmethod
    def _html_fold(info: Dict) -> str:
        sev_cls = info.get("sev_class", "neutral")
        kind = info.get("kind_label", "")
        kind_cls = "k-xid" if info.get("red") else ""
        chips = (f"<span class='chipset'>"
                 f"<span class='chip c-{_esc(sev_cls)}'>{_esc(info.get('sev_label',''))}</span>"
                 + (f"<span class='chip c-kind {kind_cls}'>{_esc(kind)}</span>" if kind else "")
                 + "</span>")
        bridge = (
            "<span class='bridge' title='switch raw clock + offset = tray clock'>"
            f"<span class='bside'><span class='blab'>switch</span>"
            f"<span class='bt'>{_esc(info.get('t_raw',''))}</span></span>"
            f"<span class='bmid'><span class='boff'>{_esc(info.get('off',''))}</span></span>"
            f"<span class='bside'><span class='blab'>tray</span>"
            f"<span class='bt'>{_esc(info.get('t_tray',''))}</span></span></span>")
        return (f"<details class='fold sev-{_esc(sev_cls)}' id='{_esc(info.get('anchor',''))}'>"
                f"<summary>{chips}"
                f"<span class='fref'>{_esc(info.get('ref',''))}</span>{bridge}"
                f"<span class='fxref'>↔ {_esc(info.get('xref',''))}</span></summary>")

    @staticmethod
    def _html_table(headers, rows, col_notes=None, badges=None, mono_cols=None) -> str:
        if not rows:
            return "<p class='note'><em>No data.</em></p>"
        ths = []
        for i, x in enumerate(headers):
            note = col_notes[i] if col_notes and i < len(col_notes) else None
            t = f" title='{_esc(note, quote=True)}'" if note else ""
            ths.append(f"<th{t}>{_esc(x)}</th>")
        body = []
        for r in rows:
            tds = []
            for i, c in enumerate(r):
                v = str(c)
                cls = " class='mono'" if mono_cols and i in mono_cols else ""
                slug = _BADGE.get(v.strip().lower()) if badges and i in badges else None
                inner = (f"<span class='badge b-{slug}'>{_esc(v)}</span>" if slug
                         else f"<div class='cell'>{_esc(v)}</div>")
                tds.append(f"<td{cls}>{inner}</td>")
            body.append("<tr>" + "".join(tds) + "</tr>")
        return ("<div class='table-wrap'><table><thead><tr>" + "".join(ths)
                + "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>")


def _fmt_off(mins: int) -> str:
    sign = "+" if mins >= 0 else "-"
    a = abs(mins)
    return f"{sign}{a // 60:02d}:{a % 60:02d} ({mins:+d} min)"


def _fmt_off_short(mins: int) -> str:
    sign = "+" if mins >= 0 else "-"
    a = abs(mins)
    return f"{sign}{a // 60:02d}:{a % 60:02d}"


# How the applied offset was chosen (correlate.py sets this).
TZ_MODE_LABEL = {
    "auto": "auto-selected by the event-alignment sweep",
    "manual": "set manually via --tz-offset-minutes",
    "interactive_confirmed": "sweep proposal confirmed interactively",
    "interactive_manual": "entered interactively by the user",
    "default": "none applied (default 0)",
}
_TZ_MODE_SHORT = {
    "auto": "auto", "manual": "manual",
    "interactive_confirmed": "confirmed", "interactive_manual": "user",
    "default": "none",
}

FM_ROW_CAP = 50
# Parses a cross-node "+N more derivative Xid <dxid> ... suppressed" note ->
# (count, derivative_xid) so the affected Xid row can be flagged.
_SUP_PARSE = re.compile(r"\+\s*(\d+)\s+more\s+derivative\s+Xid\s+(\d+)", re.I)
_REF_NUM = re.compile(r"(\d+)\s*$")


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


# Severity → card / chip styling class + label.
_SEV_CLASS = {
    "nvl_fatal": "fatal", "nvl_non_fatal": "warn",
    "switch_info_failed": "warn", "partition_error": "warn",
    "multicast_limit": "warn", "nvlsm_check": "warn",
    "port_loss": "warn", "connection_lost": "neutral",
    "lifecycle": "neutral", "none": "neutral",
}
_SEV_LABEL = {
    "nvl_fatal": "NVL FATAL", "nvl_non_fatal": "NVL NON-FATAL",
    "switch_info_failed": "SWITCH INFO", "partition_error": "PARTITION",
    "multicast_limit": "MULTICAST", "nvlsm_check": "NVLSM CHECK",
    "port_loss": "FNM PORT LOSS", "connection_lost": "CONNECTION LOST",
    "lifecycle": "FM LIFECYCLE", "none": "PORT EVENT",
}

RAW_LINE_CAP = 40


def _moments(hits, window_s: int) -> List[Dict]:
    """Cluster correlation hits into switch-side *moments*.

    Clustering is on the **matched switch anchor**, never on an event's
    [start, end] span: a port-state group can span weeks between its down and its
    recovery, so spans would chain every event in the dump into one blob. Each
    moment therefore holds the hits whose switch anchors sit within ``window_s``
    of each other — i.e. one instant of fabric activity, whatever mix of nvos
    sections reported it.
    """
    out: List[Dict] = []
    for h in sorted(hits, key=lambda x: (x.switch_anchor, x.delta_s)):
        if out and (h.switch_anchor - out[-1]["hi"]).total_seconds() <= window_s:
            m = out[-1]
            m["hi"] = max(m["hi"], h.switch_anchor)
            m["hits"].append(h)
        else:
            out.append({"lo": h.switch_anchor, "hi": h.switch_anchor, "hits": [h]})
    return out


def _anchor_index(events):
    """Sorted ``([anchor], [event])`` index over every switch anchor, for the
    context lookup that pulls same-moment events which did not match themselves."""
    pairs = sorted(((a, i) for i, e in enumerate(events) for a in E_anchors(e)),
                   key=lambda p: p[0])
    return [p[0] for p in pairs], [p[1] for p in pairs]


def _context_events(index, events, lo, hi, window_s, exclude_ids):
    """Switch events with an anchor inside the moment's window that are not
    already part of it — the FNM loss / FM restart / raw failure sitting next to
    a correlated event but a few seconds outside the compute match."""
    ts, idx = index
    a = bisect.bisect_left(ts, lo - timedelta(seconds=window_s))
    b = bisect.bisect_right(ts, hi + timedelta(seconds=window_s))
    out, seen = [], set()
    for k in range(a, b):
        e = events[idx[k]]
        if id(e) in exclude_ids or id(e) in seen:
            continue
        seen.add(id(e))
        out.append((ts[k], e))
    return out


def _field_guide(res: Result, kinds_present: List[str]) -> List[Tuple[str, str]]:
    """Term → meaning pairs for the 'How to read this report' glossary."""
    items = [
        ("compute-tray side",
         "events parsed from the analyze-nv-bug-report reports: Xid raw-log event "
         "groups (§7.3) and IMEX node-disconnect event groups (§6). All compute "
         "times are the tray's local clock."),
        ("switch side",
         "events parsed from EVERY time-stamped section of the "
         "nvos-tech-dump-tools-for-nmx-c report — not just the port-state groups. "
         "All switch times are the NVOS local clock ('switch raw')."),
        ("fabric moment / event card",
         f"one §2 card = one instant of switch-side activity: every nvos event "
         f"whose matched anchor falls within ±{res.window_s}s of the others, "
         f"whatever section of the nvos report reported it."),
        ("Source column",
         "which nvos section an event came from — " + "; ".join(
             f"{SWITCH_KIND_TAG[k]} = {SWITCH_KIND_LABEL[k]}"
             for k in SWITCH_KIND_ORDER if k in kinds_present) + "."),
        ("Match column",
         f"±Ns = the event's anchor matched a compute-tray anchor N seconds away; "
         f"'context' = the event sits inside the same moment window but its own "
         f"anchor is more than ±{res.window_s}s from any compute anchor, so it is "
         f"shown as corroborating evidence, not as a match."),
        ("[nvl_fatal] / [nvl_non_fatal] / [none]",
         "highest Fabric-Manager error category attached to that nvos event group: "
         "fatal NVLink error / non-fatal NVLink error / port transitions only."),
        ("nvbr Xid / IMEX Event Group N",
         "event-group numbers from the nv-bug-report cross-node report's merged "
         "timelines (§4 Xid Unified Timeline / §2 IMEX Node Disconnect Timeline) — "
         "look the group up there for the full raw log."),
        ("anchor matching / window",
         f"an event contributes its start (and, if different, its end) as discrete "
         f"anchor moments — clustered sources publish every distinct moment they "
         f"contain; two events correlate when any two anchors fall within "
         f"±{res.window_s}s after the offset is applied. The span between start "
         f"and end is deliberately NOT treated as active."),
        ("timezone offset / switch raw",
         f"{_fmt_off(res.offset_min)} is added to every switch timestamp before "
         "comparing, so each §2 card shows the event on the tray clock next to "
         "the original NVOS stamp ('switch raw')."),
        ("red title / XID chip",
         "the event correlates with compute-side Xid evidence; plain cards "
         "correlate with IMEX disconnects only."),
        ("Compute Tray Index",
         "the Fabric Manager tray index (see the NVOS report's GPU Node Mapping "
         "section); listed alongside the tray Hostname parsed from each "
         "nv-bug-report."),
        ("derivative Xid 45 / suppressed",
         "Xid 45 lines tagged 'caused by previous Xid N' are channel-cleanup "
         "fallout; tables keep one representative line per burst and note "
         "'+N more suppressed' instead of listing each."),
        ("Mnemonic / Severity",
         "NVLink sub-type and severity decoded from the NVRM line for Xid 144-150 "
         "(per the NVIDIA Server-RAS catalog); '-' where the driver emits no "
         "sub-type."),
        ("GPU Node Mapping (not an event source)",
         "the nvos report's GPU Node Mapping section is a GUID inventory with "
         "first-seen / last-seen observation windows, not moments something "
         "happened, so it is deliberately excluded from the correlation."),
    ]
    per_kind = {
        "port_state": ("nvos event group N",
                       "the N-th port-state cluster in the NVOS report — NVLSM port "
                       "transitions (ACTIVE→DOWN / DOWN→INIT) grouped by adaptive time "
                       "clustering, with the Fabric Manager rows from the same window "
                       "attached."),
        "fm_outside": ("FM log outside port-state groups",
                       "Fabric-Manager rows the nvos report lists under 'Fabric Manager "
                       "log before/after earliest NVLSM event' — FM errors (often "
                       "nvl_non_fatal or connection_lost) for which NVLSM logged no port "
                       "transition, so they belong to no port-state group. Time-clustered "
                       "here; this is the evidence a port-state-only correlation misses."),
        "fnm_port_loss": ("FNM port loss",
                          "'FNM port loss' highlights from the NVOS report — the switch's "
                          "FNM access port dropping, matched to NVLSM transitions on the "
                          "same switch."),
        "fnm_nvlsm_unmatched": ("FNM loss seen only in NVLSM",
                                "an NVLSM FNM port loss with no Fabric-Manager record in "
                                "the matching window."),
        "fnm_nvlsm_recovery": ("FNM NVLSM transition not linked to FM",
                               "NVLSM FNM port transitions (usually DOWN→INIT recoveries) "
                               "the nvos report could not tie back to an FM loss event."),
        "switch_info_failure": ("Failed to get switch info",
                                "FM lost its management connection to a switch — raw "
                                "'[Mon DD YYYY HH:MM:SS] …' lines, time-clustered."),
        "partition_error": ("Partition unexpected error state",
                            "FM partition entered an unexpected error state — raw lines, "
                            "time-clustered."),
        "multicast_limit": ("Multicast team limit reached",
                            "FM hit its multicast team limit — raw lines, time-clustered."),
        "fm_lifecycle": ("FM lifecycle",
                         "Fabric Manager start / stop / restart. An Xid landing on an FM "
                         "restart usually means the fabric was reconfigured, not that a "
                         "link failed."),
        "nvlsm_health": ("NVLSM health check",
                         "invalid-topology / invalid-UTF-8 counts; only the earliest and "
                         "latest occurrence carry a timestamp, so only those two moments "
                         "can correlate."),
    }
    for k in SWITCH_KIND_ORDER:
        if k in kinds_present and k in per_kind:
            items.append(per_kind[k])
    return items


def _fmt_delta(delta: Optional[int]) -> str:
    return "context" if delta is None else f"±{delta}s"


def _evidence_rows(entries, offset_min: int) -> List[List[str]]:
    """Evidence-table rows for one moment: ``entries`` = [(anchor, event, delta)]."""
    rows = []
    for anchor, ev, delta in entries:
        rows.append([
            T.fmt(anchor),
            T.fmt(T.shift(anchor, offset_min)),
            SWITCH_KIND_TAG.get(ev.kind, ev.kind),
            ev.severity,
            ev.label,
            ev.detail or "-",
            _fmt_delta(delta),
        ])
    return rows


_EVIDENCE_HEADERS = ["Switch time (raw)", "Tray time", "Source", "Severity",
                     "Event", "Detail", "Match"]
_EVIDENCE_NOTES = [
    "The matched moment on the switch's own clock, exactly as the nvos report prints it.",
    "The same moment expressed on the compute-tray clock (switch time + applied offset).",
    "Which nvos report section the event came from — see the field guide's Source entry.",
    "Worst Fabric-Manager category / severity carried by the event.",
    "Short event label from the nvos report.",
    "Category breakdown, transition, GUID or message digest, depending on the source.",
    "±Ns = matched a compute-tray anchor that many seconds away; 'context' = same "
    "moment window but no compute anchor of its own.",
]


def _xid_evidence(d: Doc, cross, xid_egs: List[int], tray_index_by_host: Dict[str, str]) -> None:
    """The node-deduped Xid raw-log table for a moment's cross-node Xid groups."""
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
    # Suppressed-derivative counts per Xid number (from §4 "+N more … suppressed"),
    # so the affected Xid row can flag "(+N more suppressed)" and fold in any
    # hosts seen only in those notes.
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
    if not order:
        return
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
        xrows.append([xid, mnem or "-", sev_x or "-", host_cell, tray_cell, agg[key]["ex"]])
    d.p("Xid raw log (cross-node Xid Event Group "
        + ", ".join(str(i) for i in xid_egs) + ", deduped across nodes; "
        "Hostname / Compute Tray Index list every compute tray that reported each Xid):")
    d.table(
        ["Xid", "Mnemonic", "Severity", "Hostname", "Compute Tray Index",
         "Example NVRM raw log"], xrows,
        col_notes=[
            "NVIDIA Xid error number.",
            "NVLink sub-type for Xid 144-150 (Server-RAS catalog); '-' = "
            "the driver emitted no sub-type.",
            "Fatal / Nonfatal as printed on the NVRM line.",
            "Every compute tray whose log contains this Xid signature.",
            "Fabric Manager tray index of each hostname (same order).",
            "One representative raw line for the deduped signature.",
        ],
        badges={2}, mono_cols={5})
    sup_seen = set()
    for gid in xid_egs:
        for host, text in cross.xid_suppressed.get(gid, []):
            if (host, text) in sup_seen:
                continue
            sup_seen.add((host, text))
            ti = tray_index_by_host.get(host, "")
            d.p(f"{text} — {host}" + (f" [tray idx {ti}]" if ti else ""), note=True)


def _fnm_evidence(d: Doc, d_events, window_s: int) -> None:
    """FNM rows (all three nvos FNM tables) present in one moment."""
    fnm = sorted((e for e in d_events if e.kind in FNM_KINDS), key=lambda e: e.start)
    if not fnm:
        return
    rows = [[T.fmt(e.start), SWITCH_KIND_TAG.get(e.kind, e.kind),
             e.extra.get("port", "-") or "-",
             e.extra.get("down", "") or "-",
             e.extra.get("peer_host", "") or "-",
             e.extra.get("recovered", "") or "-",
             e.extra.get("line", "") or "-"]
            for e in fnm[:FM_ROW_CAP]]
    d.p(f"FNM port loss (nvos Other FabricManager Log Highlights, within "
        f"±{window_s}s):")
    d.table(
        ["FM Time", "Table", "Port", "Transition", "Peer host", "Recovered", "Log line"],
        rows,
        col_notes=[
            "Switch clock (raw, unshifted).",
            "Which FNM table in the nvos report the row came from.",
            "FNM access port number on the switch.",
            "NVLSM state change recorded for that FNM port.",
            "NVOS hostname resolved for the affected switch.",
            "Recovery time when a matching DOWN→INIT/ACTIVE was found; '-' = none.",
            "nvlsm.log reference the nvos report cited for the row.",
        ],
        mono_cols={0, 6})
    if len(fnm) > FM_ROW_CAP:
        d.p(f"… +{len(fnm) - FM_ROW_CAP} more FNM event(s) suppressed", note=True)


def _fm_log_evidence(d: Doc, d_events) -> None:
    """Collapsed, de-duplicated Fabric Manager tables for a moment.

    Both port-state groups and the 'outside the groups' clusters carry an FM
    table in ``Event.extra``, so the same slimming applies to either.
    """
    for ev in sorted((e for e in d_events if e.kind in ("port_state", "fm_outside")),
                     key=lambda e: e.start):
        fm = _fm_slim_rows(ev)
        if not fm:
            continue
        disp, frows, unique, total = fm
        d.details_open(f"{ev.ref} — Fabric Manager rows in this window: "
                       f"{unique} distinct error(s) / {total} row(s)")
        d.table(
            disp, frows,
            col_notes=[
                "Switch clock (raw, unshifted); a span means the same error "
                "repeated over that range.",
                "Fabric Manager log level.",
                "FM error class: nvl_fatal / nvl_non_fatal (NVLink errors), "
                "connection_lost (FM lost the GPU session).",
                "Tray index(es) reporting this exact error (GPU Node Mapping).",
                "FM error decode: Code/Subcode = NVLink error class / sub-reason, "
                "portDownReasonCode, Status register.",
            ],
            badges={1, 2}, mono_cols={0})
        if unique > FM_ROW_CAP:
            d.p(f"… +{unique - FM_ROW_CAP} more distinct error(s) suppressed", note=True)
        d.p("De-duplicated summary (merged by identical error); the full Fabric "
            "Manager log stays in the nvos-tech-dump-tools-for-nmx-c report.",
            note=True)
        d.details_close()


def _lifecycle_evidence(d: Doc, d_events) -> None:
    life = sorted((e for e in d_events if e.kind == "fm_lifecycle"), key=lambda e: e.start)
    if not life:
        return
    d.details_open(f"Fabric Manager lifecycle in this window ({len(life)} event(s))")
    d.table(
        ["Time", "Type", "Message"],
        [[T.fmt(e.start), e.extra.get("type", "-"), e.extra.get("message", "") or "-"]
         for e in life],
        col_notes=[
            "Switch clock (raw, unshifted).",
            "start / stop / restart as classified by the nvos report.",
            "The Fabric Manager log line that marked the transition.",
        ],
        mono_cols={0}, guide=True)
    d.details_close()


def _raw_evidence(d: Doc, d_events) -> None:
    for ev in sorted((e for e in d_events if e.kind in RAW_KINDS), key=lambda e: e.start):
        lines = ev.extra.get("lines") or []
        if not lines:
            continue
        d.details_open(f"{ev.extra.get('topic', ev.kind)} @ {T.fmt(ev.start)} "
                       f"({len(lines)} raw line(s))")
        d.pre(lines[:RAW_LINE_CAP])
        if len(lines) > RAW_LINE_CAP:
            d.p(f"… +{len(lines) - RAW_LINE_CAP} more line(s) suppressed", note=True)
        d.details_close()


def build_report(res: Result, trays: List[TrayReport], switches: List[SwitchReport],
                 tz_mode: str = "auto", cross=None, excluded_kinds=None) -> Doc:
    tray_index_by_host = {t.hostname: t.tray_index for t in trays if t.hostname}
    excluded_kinds = set(excluded_kinds or ())

    all_switch_events = [e for s in switches for n in s.nodes for e in n.events]
    considered = res.switch_events
    kind_total: Dict[str, int] = {}
    for e in considered:
        kind_total[e.kind] = kind_total.get(e.kind, 0) + 1
    matched_ids = {id(e) for e in res.matched_switch}
    kind_matched: Dict[str, int] = {}
    for e in considered:
        if id(e) in matched_ids:
            kind_matched[e.kind] = kind_matched.get(e.kind, 0) + 1
    excluded_total: Dict[str, int] = {}
    for e in all_switch_events:
        if e.kind in excluded_kinds:
            excluded_total[e.kind] = excluded_total.get(e.kind, 0) + 1
    kinds_present = [k for k in SWITCH_KIND_ORDER if kind_total.get(k)]

    n_ps = kind_total.get("port_state", 0)
    chassis = sorted({t.chassis_sn for t in trays if t.chassis_sn}
                     | {n.chassis for s in switches for n in s.nodes if n.chassis})

    d = Doc("Xid ↔ NVOS Correlation Report", meta_chips=[
        ("offset", _fmt_off_short(res.offset_min)),
        ("tz mode", _TZ_MODE_SHORT.get(tz_mode, tz_mode)),
        ("window", f"±{res.window_s}s"),
        ("scope", "same-chassis" if res.chassis_scoped else "cross-chassis"),
        ("trays", str(len(trays))),
        ("nvos events", f"{len(considered):,}"),
        ("chassis", ", ".join(chassis) if chassis else "?"),
    ])

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

    switch_line = (f"Switch dumps (NVOS/NMX-C): {len(switches)} — {len(considered):,} event(s) "
                   f"across {len(kinds_present)} nvos section(s): "
                   + ", ".join(f"{SWITCH_KIND_TAG[k]} {kind_total[k]:,}" for k in kinds_present))
    bullets = [
        compute_line,
        switch_line,
        f"Chassis (rack) key(s): {', '.join(chassis) if chassis else '(none detected)'}",
        f"Correlation window: ±{res.window_s}s | Timezone offset applied to switch side: "
        f"{_fmt_off(res.offset_min)}  [{TZ_MODE_LABEL.get(tz_mode, tz_mode)}]",
        f"Chassis-scoped correlation: {'yes' if res.chassis_scoped else 'no (cross-chassis allowed)'}",
    ]
    if excluded_total:
        bullets.append("Excluded from correlation (--exclude-switch-kinds): "
                       + ", ".join(f"{k} ({n:,})" for k, n in sorted(excluded_total.items())))
    d.bullets(bullets)
    d.legend("How to read this report (field guide)", _field_guide(res, kinds_present))

    # 1. Timezone alignment
    d.h(2, "1. Timezone Alignment")
    d.p("Both report families stamp events in local wall-clock time with no timezone "
        "marker, so the switch side is shifted by an offset before matching. The table "
        "below shows the top-scoring candidate offsets (scored by how many compute↔switch "
        "event starts align); re-run with --tz-offset-minutes (or --auto-tz / "
        "--interactive-tz) to apply a different one.",
        note=True)
    if tz_mode in ("interactive_confirmed", "interactive_manual"):
        d.p("The applied offset was confirmed interactively: the sweep proposal was "
            + ("accepted as-is." if tz_mode == "interactive_confirmed"
               else "overridden by a user-entered value."), note=True)
    if res.suggestions:
        top = res.suggestions[:3]
        if all(off != res.offset_min for off, _ in top):  # always keep the applied offset
            top = top + [s for s in res.suggestions if s[0] == res.offset_min][:1]
        rows = [[_fmt_off(off), str(hits), "◀ applied" if off == res.offset_min else
                 ("best" if (off, hits) == res.suggestions[0] else "")]
                for off, hits in top]
        d.table(
            ["Offset (switch → tray)", "Aligned start hits", "Note"], rows,
            col_notes=[
                "Minutes added to every switch/NVOS timestamp to express it on the "
                "compute-tray clock; +01:00 means the switch clock runs 1 h behind the trays.",
                "How many switch event anchors land within the correlation window of a "
                "compute-tray anchor at that offset — higher = better clock fit.",
                "'◀ applied' = the offset used throughout this report; 'best' = the "
                "sweep's top score when a different offset was applied.",
            ],
            mono_cols={0}, guide=True)
    else:
        d.p("Not enough events on both sides to suggest an offset.", note=True)

    # 2. Correlated events — one card per switch-side *moment*, across all
    #    nvos sections rather than port-state groups only.
    d.h(2, "2. Correlated Events")
    corr_xid_egs: set = set()   # cross-node Xid EG ids cited by any fabric moment
    corr_imex_egs: set = set()  # cross-node IMEX EG ids cited by any fabric moment
    moments = _moments(res.hits, res.window_s)

    cov_rows = [[SWITCH_KIND_TAG[k], SWITCH_KIND_LABEL[k], f"{kind_total[k]:,}",
                 f"{kind_matched.get(k, 0):,}"] for k in kinds_present]
    cov_rows.append(["ALL", "every nvos event source above", f"{len(considered):,}",
                     f"{len(res.matched_switch):,}"])
    d.p(f"{len(res.correlations)} compute-tray event(s) correlated with switch-side "
        f"evidence, folded into {len(moments)} fabric moment(s). Coverage per nvos "
        f"section:")
    d.table(
        ["Source", "nvos report section", "Events", "Correlated"], cov_rows,
        col_notes=[
            "Short tag used in the §2 evidence tables.",
            "Where in the nvos-tech-dump-tools-for-nmx-c report the events come from.",
            "How many events of that kind the nvos report(s) yielded.",
            "How many of them landed within the correlation window of a compute-tray "
            "Xid / IMEX anchor.",
        ],
        guide=True)

    if moments:
        d.p("Each card is one fabric moment: every nvos event whose anchor falls in the "
            "same window, cross-referenced to the nvbr cross-node report's Xid / IMEX "
            "event group(s). The evidence table lists all switch-side events at that "
            "moment — port-state groups, Fabric-Manager rows outside those groups, FNM "
            "port loss, switch-info / partition / multicast failures, FM lifecycle and "
            "NVLSM health checks. The card header shows the same moment on both clocks: "
            "switch raw time, the applied offset, then the tray-clock time the compute "
            "side was matched against.", note=True)
        if cross is None:
            d.p("nvbr cross-node report not found among the inputs — cards fall back to "
                "compute-event counts instead of cross-node event-group numbers.", note=True)

        index = _anchor_index(considered)

        # Resolve each moment's switch events (matched + same-window context) up
        # front so the chip strip and the cards agree on severity and labels.
        cards = []
        for i, m in enumerate(moments):
            per: Dict[int, Dict] = {}
            comp: List = []
            comp_seen = set()
            for h in m["hits"]:
                cur = per.get(id(h.switch))
                if cur is None or h.delta_s < cur["delta"]:
                    per[id(h.switch)] = {"ev": h.switch, "anchor": h.switch_anchor,
                                         "delta": h.delta_s}
                if id(h.compute) not in comp_seen:
                    comp_seen.add(id(h.compute))
                    comp.append(h.compute)
            entries = [(v["anchor"], v["ev"], v["delta"]) for v in per.values()]
            ctx = _context_events(index, considered, m["lo"], m["hi"], res.window_s,
                                  set(per))
            entries.extend((a, e, None) for a, e in ctx)
            entries.sort(key=lambda t: (SWITCH_KIND_ORDER.index(t[1].kind)
                                        if t[1].kind in SWITCH_KIND_ORDER else 99, t[0]))
            events = [e for _a, e, _d in entries]
            # Name and colour the card from the events that actually matched, so
            # a context-only neighbour never gets credited with the correlation.
            # entries are ordered by SWITCH_KIND_ORDER, so the first one is the
            # moment's most diagnostic evidence.
            naming = [e for _a, e, dl in entries if dl is not None] or events
            sev = worst_severity(e.severity for e in naming)
            ps_refs = [e.ref for e in naming if e.kind == "port_state"]
            if ps_refs:
                ref = "; ".join(ps_refs[:3]) + (f" +{len(ps_refs) - 3}" if len(ps_refs) > 3 else "")
                mm = _REF_NUM.search(ps_refs[0])
                nav = f"G{mm.group(1)}" if mm else "PORT"
            else:
                ref = naming[0].label
                nav = SWITCH_KIND_TAG.get(naming[0].kind, "EVT")
            # The card header quotes the naming event's own anchor, so its time
            # and its title always describe the same thing.
            head = next(a for a, e, _dl in entries if e is naming[0])
            cards.append({"m": m, "entries": entries, "events": events,
                          "comp": comp, "sev": sev, "ref": ref, "nav": nav,
                          "head": head, "anchor": f"moment-{i + 1}",
                          "sev_class": _SEV_CLASS.get(sev, "neutral")})

        d.chips("Correlated fabric moments", [
            {"anchor": c["anchor"], "ref": c["nav"],
             "dt": T.shift(c["head"], res.offset_min).strftime("%m-%d %H:%M"),
             "sev_class": c["sev_class"]} for c in cards])

        for c in cards:
            m, entries, events, comp = c["m"], c["entries"], c["events"], c["comp"]
            is_xid = any(ce.kind == "xid" for ce in comp)
            sev = c["sev"]
            xid_egs = sorted({_xref(cross, "xid", ce.start, res.window_s)
                              for ce in comp if ce.kind == "xid"} - {None})
            imex_egs = sorted({_xref(cross, "imex", ce.start, res.window_s)
                               for ce in comp if ce.kind == "imex"} - {None})
            corr_xid_egs.update(xid_egs)
            corr_imex_egs.update(imex_egs)
            parts = []
            if xid_egs:
                parts.append("Xid Event Group " + ", ".join(str(i) for i in xid_egs))
            if imex_egs:
                parts.append("IMEX Event Group " + ", ".join(str(i) for i in imex_egs))
            xref = ("nvbr " + "; ".join(parts)) if parts else f"{len(comp)} compute event(s)"
            t_tray = T.fmt(T.shift(c["head"], res.offset_min))
            t_raw = T.fmt(c["head"])
            n_kinds = len({e.kind for e in events})
            sev_tag = f"[{sev}] " if sev and sev != "none" else ""
            d.fold_open(
                f"{c['ref']} {sev_tag}@ {t_tray} (switch raw {t_raw}) ↔ {xref}",
                {"anchor": c["anchor"], "ref": c["ref"], "nav": c["nav"],
                 "sev_class": c["sev_class"],
                 "sev_label": _SEV_LABEL.get(sev, "PORT EVENT"),
                 "kind_label": "XID" if is_xid else "IMEX",
                 "t_raw": t_raw, "t_tray": t_tray,
                 "off": _fmt_off_short(res.offset_min), "xref": xref, "red": is_xid})

            d.p(f"Switch-side evidence at this moment — {len(events)} nvos event(s) "
                f"from {n_kinds} report section(s):")
            d.table(_EVIDENCE_HEADERS, _evidence_rows(entries, res.offset_min),
                    col_notes=_EVIDENCE_NOTES, badges={3}, mono_cols={0, 1}, guide=True)

            if cross is not None and xid_egs:
                _xid_evidence(d, cross, xid_egs, tray_index_by_host)
            _compute_evidence(d, comp, c["head"], res.offset_min, tray_index_by_host)
            _fnm_evidence(d, events, res.window_s)
            _lifecycle_evidence(d, events)
            _fm_log_evidence(d, events)
            _raw_evidence(d, events)
            d.details_close()
    else:
        d.p("No compute-tray event correlated with any switch event at the applied "
            "offset. Check the Timezone Alignment table above for a better offset.", note=True)

    # 3. Uncorrelated compute events — as cross-node event groups when available
    d.h(2, "3. Uncorrelated Compute-Tray Events")
    if cross is not None:
        d.p("Cross-node event groups (nvbr cross-node report) that did NOT time-correlate "
            "with any switch-side nvos event in §2 (cross-node granularity).")
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
            d.table(
                ["nvbr ref", "Start", "End", "Xid types"], rows,
                col_notes=[
                    "Event group number in the cross-node report's Xid Unified Timeline (§4).",
                    "Group start on the compute-tray clock.",
                    "Group end on the compute-tray clock.",
                    "Deduped Xid signatures observed inside the group.",
                ],
                mono_cols={1, 2}, guide=True)
            d.details_close()
            d.p("With every nvos section correlated, an uncorrelated Xid group means the "
                "switch side logged nothing at all in that window — the fault did not "
                "reach the fabric, or the dump does not cover that time range.", note=True)
        else:
            d.p(xid_sum + ".")
        un_imex = [g for g in sorted(cross.imex_groups) if g[0] not in corr_imex_egs]
        imex_sum = (f"IMEX: {len(un_imex)} of {len(cross.imex_groups)} cross-node IMEX Event "
                    f"Group(s) uncorrelated")
        if un_imex:
            rows = [[f"IMEX Event Group {gid}", T.fmt(s), T.fmt(e)] for gid, s, e in un_imex]
            d.details_open(imex_sum)
            d.table(
                ["nvbr ref", "Start", "End"], rows,
                col_notes=[
                    "Event group number in the cross-node report's IMEX Node Disconnect "
                    "Timeline (§2).",
                    "Group start on the compute-tray clock.",
                    "Group end on the compute-tray clock.",
                ],
                mono_cols={1, 2})
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
            d.table(
                ["Start", "End", "Kind", "Tray", "Chassis", "Event", "Ref"], rows,
                col_notes=[
                    "Event group start on the compute-tray clock.",
                    "Event group end on the compute-tray clock.",
                    "XID = GPU Xid raw-log group; IMEX = IMEX node-disconnect group.",
                    "Compute-tray hostname the event came from.",
                    "Chassis serial (rack key).",
                    "Short event label from the per-node report.",
                    "Event group reference inside that tray's report.",
                ],
                mono_cols={0, 1}, guide=True)
            d.details_close()
        if unmatched_imex:
            d.p("Note: IMEX event groups with no switch-side fabric correlation are commonly "
                "caused by an IMEX service restart or a transient inter-node network fluctuation, "
                "rather than a switch fabric fault.", note=True)

    return d


def _compute_evidence(d: Doc, comp, head_anchor, offset_min: int,
                      tray_index_by_host: Dict[str, str]) -> None:
    """Collapsed list of the compute-tray event groups this moment matched."""
    if not comp:
        return
    ref_tray = T.shift(head_anchor, offset_min)
    rows = []
    for ce in sorted(comp, key=lambda e: (e.kind != "xid", e.start)):
        rows.append([
            T.fmt(ce.start), ce.kind.upper(), ce.source_id or "-",
            tray_index_by_host.get(ce.source_id, "-") or "-",
            ce.label, ce.ref,
            f"{int((ce.start - ref_tray).total_seconds()):+d}s",
        ])
    d.details_open(f"Compute-tray events matched at this moment ({len(rows)})")
    d.table(
        ["Tray time", "Kind", "Tray", "Tray idx", "Event", "Per-tray ref", "Δ vs moment"],
        rows,
        col_notes=[
            "Event group start on the compute-tray clock.",
            "XID = GPU Xid raw-log group; IMEX = IMEX node-disconnect group.",
            "Compute-tray hostname the event came from.",
            "Fabric Manager tray index for that hostname.",
            "Short event label from the per-node nv-bug-report.",
            "Event-group number inside THAT tray's own report — not the cross-node "
            "group cited in the card header.",
            "Signed offset from the moment's switch anchor expressed on the tray clock.",
        ],
        mono_cols={0, 6}, guide=True)
    d.details_close()
