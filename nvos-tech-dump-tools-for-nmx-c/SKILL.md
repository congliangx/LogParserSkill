---
name: nvos-tech-dump-tools-for-nmx-c
description: Analyze NVLSM and Fabric Manager logs under log/nmx/nmx-c in an NVOS / NMX-C tech-support dump (a directory or a .tar.gz) and emit a consolidated Markdown + HTML report — NVLSM topology / port-state events with time clustering, NVLSM forensics (INIT/Unlink counts), and full Fabric Manager (-vvv) parsing. Use this skill when the user provides an NVOS dump or tarball and asks to analyze NMX-C / NVLSM / Fabric Manager logs.
---

# Analyze NVOS NMX-C Tech Dump Logs (NVLSM + Fabric Manager)

## Overview

Parse the **NVLSM** and **Fabric Manager** logs found under `log/nmx/nmx-c` in an NVOS / NMX-C tech-support dump (a directory or a `.tar.gz`) and produce a consolidated **Markdown + HTML** report.

**Skill install paths** — this skill lives at one of:

- Cursor: `~/.cursor/skills/nvos-tech-dump-tools-for-nmx-c/`
- Claude Code: `~/.claude/skills/nvos-tech-dump-tools-for-nmx-c/` (user-level) or `.claude/skills/nvos-tech-dump-tools-for-nmx-c/` (project-level)
- Codex: `~/.codex/skills/nvos-tech-dump-tools-for-nmx-c/`

This skill ships a self-contained `uv` project (`pyproject.toml` + `uv.lock`) at its root; `uv sync` builds a `.venv/` inside the skill directory. All `scripts/...` paths below are **relative to this skill's root** — substitute `<SKILL_ROOT>` with the active tool's install root (e.g. on Claude Code: `~/.claude/skills/nvos-tech-dump-tools-for-nmx-c`).

> **Stdlib only.** The toolkit imports nothing outside the Python standard library, so the `uv` environment carries **no third-party packages** — `uv` is used purely to pin a Python interpreter (3.9+) and to give this skill the same install flow as `analyze-nv-bug-report`.

**Environment check — do this once before running, then pick the matching branch.** The agent cannot create the environment itself (its sandbox is read-only on the skill directory), so detect an existing `.venv` first:

```bash
# Does the skill's uv venv already exist?
ls <SKILL_ROOT>/.venv/bin/python
```

- **It exists** → run the script by calling that interpreter **directly** (NOT via `uv run` — invoking the venv's own `python` is pure read+execute and needs no write access, whereas `uv run` may try to re-sync and write into `.venv/`, which the sandbox blocks):

  ```bash
  <SKILL_ROOT>/.venv/bin/python <SKILL_ROOT>/scripts/main.py ...
  ```

- **It is missing** → either ask the user to build it once with `uv sync --project <SKILL_ROOT>`, **or** — because this skill is stdlib-only — simply run it with any Python **3.9+** interpreter, with no `pip install` step needed:

  ```bash
  python3 <SKILL_ROOT>/scripts/main.py ...
  ```

`SKILL.md`, `pyproject.toml`, `uv.lock`, and `scripts/` live at the root; the toolkit code is under `scripts/nmx_log_tools/`.

## Workflow

### Step 1: Identify the input

The input is **one** NVOS / NMX-C tech dump, given as either:

- a **directory** that contains a `log/nmx/nmx-c` subtree, or
- a **`.tar.gz`** of such a dump.

The tool validates that `log/nmx` exists *before* doing any heavy work. For a tarball it scans the member list first and extracts **only** the `log/nmx` subtree to a temporary directory (never the full dump), then cleans it up afterward. If `log/nmx` is missing it refuses to run.

### Step 2: Run the analysis

```bash
<SKILL_ROOT>/.venv/bin/python <SKILL_ROOT>/scripts/main.py <dump_dir_or.tar.gz> -o <output_dir> [--name <basename>] [--since WHEN] [--until WHEN]
```

Arguments:

- `input` (positional): NVOS dump directory or `.tar.gz`.
- `-o, --output-dir` (required): directory for the reports (created if missing).
- `--name` (optional): report base filename without extension (default: `nmx_log_analysis`).
- `--since` / `--start-time` (optional): keep records at or after this time (`YYYY-MM-DD` or `YYYY-MM-DD HH:MM[:SS]`). Date-only is `00:00:00`.
- `--until` / `--end-time` (optional): keep records at or before this time. Date-only includes that entire day.

Writes these files in the output directory:

- `<name>.md` — Markdown report.
- `<name>.html` — self-contained HTML report (same content; the better viewer for wide aggregated tables).
- `<name>_<node>_fm_pre_nvlsm.csv` / `<name>_<node>_fm_after_nvlsm.csv` — only when that unassigned FM table exceeds `FM_UNASSIGNED_EMBED_THRESHOLD` (1200). Those rows are omitted from Markdown; HTML gzip-embeds them.

Example:

```bash
<SKILL_ROOT>/.venv/bin/python <SKILL_ROOT>/scripts/main.py /path/to/nvos_dump.tar.gz -o /path/to/reports --name rack3_nmxc
# writes /path/to/reports/rack3_nmxc.md and /path/to/reports/rack3_nmxc.html
```

`main.py` prints the number of `log/nmx/nmx-c` root(s) found, the chosen log time range when one was passed, and each output path; any non-fatal parse issues are listed as `Warnings:` on stderr while the report is still written.

#### Optional `--since` / `--until` (do this before `correlation-xid`)

These flags filter **every** NVLSM and Fabric Manager record *before* grouping, FNM matching, GPU mapping, and report tables. They are optional: omitting them parses the full dump (same as before).

The filter is whole-report, not per-section. When a window is set, both Markdown and HTML put a **Log coverage** line at the top of the document (`This analysis only covers related logs from …`). Do not bury an equivalent note inside a single node section.

`FM_UNASSIGNED_EMBED_THRESHOLD` (`AnalysisConfig.fm_unassigned_embed_threshold`, default 1200) is a per-table **inline-vs-embed threshold** for the two unassigned Fabric Manager tables (before earliest NVLSM event; after earliest NVLSM event but outside event-group windows), judged independently. It is not a drop-cap; rows are never omitted.

- At or below 1200 rows: inline in HTML (so the report search box works) and in Markdown.
- Above 1200 rows: HTML stores gzip(JSON)+base64 and lazy-decodes on expand; Markdown writes a sibling `.csv` and only a pointer in the `.md`.

Passing a `--since` / `--until` window around the incident is still the way to keep a large dump focused so `correlation-xid` can read those FM rows from Markdown (it does not consume the CSV sidecars).

Build `--since` / `--until` with this single procedure. Do **not** add scripts; do **not** convert hours or apply a timezone offset here (`correlation-xid` is what aligns the two clocks later).

1. If the user already gave `--since` / `--until` or an incident day, use that verbatim and stop.
2. If there is no `analyze-nv-bug-report` Markdown yet, omit the flags (full dump).
3. Collect event-group timestamps from those reports:
   - Prefer `cross-node-report.md` §4 **Xid Unified Timeline** headings (`Event Group N: <ts_first> ~ <ts_last>`).
   - Otherwise each `*-nv-bug-report-analysis-report.md`: §7.3 **Xid Raw Logs** headings (`Event Group N: <ts>` or `<ts_first> ~ <ts_last>`), plus §6 **IMEX** headings if present.
   - Year comes from §1 `Date`. If a stamp is December and `Date` is January, use year − 1.
   - Do **not** use §1 `Boot Time` or §7 `Message Log Start`.
4. If the collection is empty (no Xid and no IMEX event groups), omit the flags and tell the user `correlation-xid` may have little to join.
5. Convert every collected stamp to a calendar date. Let `Dmin` be the earliest date and `Dmax` the latest. Always expand one calendar day on each side:
   - `--since` = `Dmin − 1 day`
   - `--until` = `Dmax + 1 day`
   Date-only `--until` already includes that whole day. Example: event groups on `2026-03-02` only → `--since 2026-03-01 --until 2026-03-03`.
6. Print that window in chat, then run:

   ```bash
   <SKILL_ROOT>/.venv/bin/python <SKILL_ROOT>/scripts/main.py /path/to/nvos_dump.tar.gz \
       -o /path/to/reports --name rack3_nmxc \
       --since 2026-03-01 --until 2026-03-03
   ```

   The report header **Log coverage** line must match the flags you passed.

The ±1 day is a fixed buffer so switch-side logs still fall in the window when the two hosts print local time in different zones (up to about 12 hours). Do not compute or apply `--tz-offset-minutes` at this step.

### Step 3: What the report covers

- **NVLSM** (`checks/nvlsm` patterns): invalid topology, invalid UTF-8, and port-state events with adaptive time clustering.
- **NVLSM forensics** (`nvlsm.log`): INIT / Unlink state-change counts (FNM ports 73 / 74 by default).
- **Fabric Manager**: full parse with **-vvv-equivalent** settings — all log files, no age cutoff, all health polls.
- **Aggregated detail tables**: repeated log lines grouped by pattern (count + first/last timestamp); NVLSM port transitions are listed in full inside each time cluster.

### Configuration

Defaults live in `scripts/nmx_log_tools/config.py`. The NVLSM port-state clustering knobs (tuned so an `ACTIVE→DOWN` and the following `DOWN→INIT` usually land in the same group):

- `nvlsm_event_group_gap_seconds` (120) — idle gap between events that starts a new group.
- `nvlsm_event_group_max_seconds` (600) — maximum span per group.
- `nvlsm_port_wave_gap_seconds` (120) — same-port previous-episode-end → next-anchor gap.
- `fm_fnm_nvlsm_match_window_seconds` (300) — FM FNM port-loss ↔ NVLSM `osm_spst` match window (matched on Switch GUID + port).

Edit these only to retune grouping; the defaults mirror `nvos_parser` FM `-vvv` behavior.

## Layout

```
<SKILL_ROOT>/
├── SKILL.md            # this file
├── pyproject.toml      # uv project (stdlib only — empty dependency list)
├── uv.lock             # pinned (interpreter only; no packages)
└── scripts/
    ├── main.py         # CLI entry point
    └── nmx_log_tools/
        ├── discovery.py        # log/nmx validation + file discovery
        ├── time_range.py       # --since/--until parsing and record filtering
        ├── platform_identity.py
        ├── config.py           # AnalysisConfig defaults
        ├── sources/            # directory vs tarball input
        ├── parsers/            # nvlsm_health, nvlsm_forensics, fabric_manager
        ├── event_grouping/     # adaptive time clustering
        ├── analyze/            # pipeline orchestration
        └── report/             # markdown + html renderers
```

## Notes

- The tool extracts only the `log/nmx` subtree from a tarball into a temp directory and removes it on exit — it never unpacks the full dump.
- The HTML report embeds its own CSS/JS (no CDN, opens offline) and is the recommended viewer for the wide aggregated tables. It shares the correlation-xid instrument-panel styling: a sticky sidebar TOC with scroll-spy, sortable/filterable tables, expand/collapse-all, mono-set machine data, and **severity rails** on `<details>` — fatal (Xid / nvl_fatal) event groups and suspect hardware-swap slots get a red rail, nvl_non_fatal groups an amber one — so fatal fabric faults are visible at a glance in both the body and the sidebar. Light/dark auto, keyboard focus and reduced-motion respected. The Markdown report is unchanged.
- Platform: Linux and macOS. Python **3.9+**, standard library only (no third-party dependencies).
