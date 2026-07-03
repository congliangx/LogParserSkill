---
name: nvos-tech-dump-tools-for-nmx-c
description: Analyze NVLSM and Fabric Manager logs under log/nmx/nmx-c in an NVOS / NMX-C tech-support dump (a directory or a .tar.gz) and emit a consolidated Markdown + HTML report — NVLSM topology / port-state events with time clustering, NVLSM forensics (INIT/Unlink counts), and full Fabric Manager (-vvv) parsing. Handles a single dump or, with --batch, many dumps in parallel plus a rack-level comparison report. Use this skill when the user provides an NVOS dump or tarball (or several) and asks to analyze NMX-C / NVLSM / Fabric Manager logs.
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

### Step 2: Run the analysis (single dump)

```bash
<SKILL_ROOT>/.venv/bin/python <SKILL_ROOT>/scripts/main.py <dump_dir_or.tar.gz> -o <output_dir> [--name <basename>]
```

Arguments:

- `input` (positional): NVOS dump directory or `.tar.gz`.
- `-o, --output-dir` (required): directory for the reports (created if missing).
- `--name` (optional): report base filename without extension (default: `nmx_log_analysis`).

Outputs two files in the output directory:

- `<name>.md` — Markdown report.
- `<name>.html` — self-contained HTML report (same content; the better viewer for wide aggregated tables).

Example:

```bash
<SKILL_ROOT>/.venv/bin/python <SKILL_ROOT>/scripts/main.py /path/to/nvos_dump.tar.gz -o /path/to/reports --name rack3_nmxc
# writes /path/to/reports/rack3_nmxc.md and /path/to/reports/rack3_nmxc.html
```

`main.py` prints the number of `log/nmx/nmx-c` root(s) found and the two output paths; any non-fatal parse issues are listed as `Warnings:` on stderr while the report is still written.

### Step 2b: Batch mode — multiple dumps in parallel

To analyze several dumps at once (e.g. a whole rack of switch dumps), use `--batch`. Each dump runs in **its own process** (real parallelism — the parsing is CPU-bound, so it scales with cores), and a **rack-level comparison report** is written alongside the per-dump reports.

```bash
# Explicit list of dumps (directories and/or .tar.gz, mixed is fine)
<SKILL_ROOT>/.venv/bin/python <SKILL_ROOT>/scripts/main.py --batch dump1/ dump2.tar.gz dump3/ -o <output_dir> [-j N]

# Or point at ONE parent directory and let it scan for the dumps inside
<SKILL_ROOT>/.venv/bin/python <SKILL_ROOT>/scripts/main.py --batch /path/to/rack_dumps/ -o <output_dir>
```

- **Inputs** accept both forms: an explicit list of dump dirs / tarballs, **and/or** a parent directory that is scanned for child `*.tar.gz` and `log/nmx` dump dirs. `--batch` is auto-enabled when more than one input path is given.
- `-j, --jobs N` (optional): number of worker processes. Default: `min(#dumps, CPU count)`.
- **Outputs**: one `<dump>.md` + `<dump>.html` per dump (named after each dump dir / tarball), plus **`rack-comparison.md` + `rack-comparison.html`** — a cross-dump report with a per-dump run-status table, a per-node overview (FM files / events, port-event groups, NVLSM state changes, key FM failure counts), and a Fabric-Manager category matrix (node × category).
- **Error isolation**: a dump that fails validation or parsing does **not** abort the batch — it is marked `FAILED` (with the reason) in the comparison report's Run Status table while the rest complete. The exit code is non-zero only if *every* dump failed.
- **Platform**: Linux uses the `fork` start method; macOS / others use `spawn`. Each tarball worker extracts to its own temp dir, so there are no cross-process collisions.

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
    ├── main.py         # CLI entry point (single dump + --batch)
    └── nmx_log_tools/
        ├── discovery.py        # log/nmx validation + file discovery
        ├── platform_identity.py
        ├── config.py           # AnalysisConfig defaults
        ├── sources/            # directory vs tarball input
        ├── parsers/            # nvlsm_health, nvlsm_forensics, fabric_manager
        ├── event_grouping/     # adaptive time clustering
        ├── analyze/            # pipeline.py; run.py (single-dump core); batch.py (parallel + dir scan)
        └── report/             # markdown + html renderers; comparison.py (rack-level report)
```

## Notes

- The tool extracts only the `log/nmx` subtree from a tarball into a temp directory and removes it on exit — it never unpacks the full dump.
- The HTML report embeds its own CSS/JS (no CDN, opens offline) and is the recommended viewer for the wide aggregated tables.
- Batch mode (`--batch`) analyzes each dump in a separate process (Linux `fork` / else `spawn`); this is the only way to get real parallelism since the parsing is CPU-bound pure-Python (the GIL would serialize threads). One failing dump is isolated and reported, not fatal to the batch.
- Platform: Linux and macOS. Python **3.9+**, standard library only (no third-party dependencies).
