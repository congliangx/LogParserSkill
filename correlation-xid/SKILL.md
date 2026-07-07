---
name: correlation-xid
description: Correlate compute-tray nv-bug-report reports with NVOS / NMX-C dump reports by TIME. Reads the Markdown reports the analyze-nv-bug-report and nvos-tech-dump-tools-for-nmx-c skills already produced, extracts compute-tray Xid event groups + IMEX event groups and switch-side Port state event groups + Other FabricManager Log Highlights (FNM port loss), and reports events that fall in the same time window — accounting for a timezone offset between the two capture sources. Use this skill when the user has BOTH nv-bug-report analysis report(s) AND nvos dump analysis report(s) from the same rack and wants a unified time-correlation report (replaces analyze-rack-xid). Triggers: "correlate xid", "correlation-xid", "correlate gpu and switch by time", "rack xid correlation".
---

# Correlate Compute-Tray Xid/IMEX with NVOS Switch Events (by time)

## Overview

This skill is a **meta-analysis** step: it does not read raw logs, it reads the **Markdown reports** already produced by the other two skills and joins them on a shared timeline.

- **Compute-tray side** — `analyze-nv-bug-report` reports (`# NVIDIA Bug Report Analysis`): §6 IMEX Node Disconnect **event groups**, §7.1 Xid Summary, §7.3 Xid Raw Logs **event groups** (per GPU BDF, with mnemonics), plus §1 identity (hostname, **Chassis Serial Number**, collection Date, Boot Time).
- **Switch side** — `nvos-tech-dump-tools-for-nmx-c` reports (`# NMX-C Log Analysis Report`): **Port state event groups** (ACTIVE→DOWN / DOWN→INIT with nvl_fatal / nvl_non_fatal Fabric-Manager rows) and **Other FabricManager Log Highlights** (FNM port loss).

It normalizes every timestamp to an absolute datetime (inferring the year for syslog-style Xid stamps from the report's Date), applies a **timezone offset** to align the two clocks, and correlates events whose **anchor moments fall in the same time window**. Correlation is scoped per **chassis serial** — the nv-bug-report `Chassis Serial Number` equals the leading number of the nvos node title (`## 1821425180267-Slot 9: ...`), so trays and switches in the same rack are matched together.

**Skill install paths** — this skill lives at one of:

- Cursor: `~/.cursor/skills/correlation-xid/`
- Claude Code: `~/.claude/skills/correlation-xid/` (user-level) or `.claude/skills/correlation-xid/` (project-level)
- Codex: `~/.codex/skills/correlation-xid/`

This skill ships a self-contained `uv` project (`pyproject.toml` + `uv.lock`) at its root; `uv sync` builds a `.venv/` inside the skill directory. All `scripts/...` paths below are **relative to this skill's root** — substitute `<SKILL_ROOT>` with the active tool's install root (e.g. on Claude Code: `~/.claude/skills/correlation-xid`).

> **Stdlib only.** The toolkit imports nothing outside the Python standard library, so the `uv` environment carries **no third-party packages** — `uv` is used purely to pin a Python interpreter (3.9+) and give this skill the same install flow as its siblings.

**Environment check — do this once before running, then pick the matching branch:**

```bash
ls <SKILL_ROOT>/.venv/bin/python
```

- **It exists** → run the script through that interpreter directly (NOT `uv run`, which may try to re-sync and write into `.venv/`):

  ```bash
  <SKILL_ROOT>/.venv/bin/python <SKILL_ROOT>/scripts/correlate.py ...
  ```

- **It is missing** → either ask the user to build it once with `uv sync --project <SKILL_ROOT>`, **or** — because this skill is stdlib-only — just run it with any Python **3.9+**: `python3 <SKILL_ROOT>/scripts/correlate.py ...`.

## Workflow

### Step 1: Produce the source reports first

This skill consumes the *outputs* of the other two skills. If they have not been run yet, run them first:

- `analyze-nv-bug-report` on the compute-tray `nv-bug-report.log(.gz)` files (batch mode is fine) → a `report/` directory of `*-nv-bug-report-analysis-report.md`.
- `nvos-tech-dump-tools-for-nmx-c` on the NVOS dump(s) → `<dump>.md`.

### Step 2: Run the correlation

```bash
<SKILL_ROOT>/.venv/bin/python <SKILL_ROOT>/scripts/correlate.py <inputs...> -o <output_dir> [options]
```

`<inputs...>` may be individual report `.md` files **and/or** directories (scanned recursively for `*.md`). Each file is auto-classified as nv-bug-report or NVOS by its title line; aggregate reports (`cross-node-report.md`, `rack-comparison.md`) and this skill's own output are ignored automatically. Point it at both report sets, e.g.:

```bash
<SKILL_ROOT>/.venv/bin/python <SKILL_ROOT>/scripts/correlate.py \
    /path/to/nvbugreport/report /path/to/nvosdump/nmxc_batch \
    -o /path/to/out --auto-tz
```

Options:

- `-o, --output-dir` (required): directory for the report.
- `--name` (default `correlation-xid-report`): output base filename.
- `--tz-offset-minutes N` (default `0`): minutes **added to the switch (NVOS) timestamps** to align them with the compute-tray clock. Positive = the switch clock is behind the tray clock.
- `--auto-tz`: auto-pick the offset that maximizes time-aligned event pairs (see Timezone note). Recommended for the first pass.
- `--window-seconds S` (default `120`): how close two anchor moments must be to count as "same time window".
- `--cross-chassis`: correlate across different chassis serials (default: same chassis only).

Outputs `<name>.md` and a self-contained `<name>.html` (sortable/filterable tables, expand/collapse) in the output directory.

### Step 3: Timezone alignment (important)

Neither report family records a timezone — both are **local wall-clock at their own host**, and the switch and compute trays may sit in different zones. So the correlation cannot assume the two clocks match.

- Start with `--auto-tz`. The report's **§1 Timezone Alignment** table sweeps candidate offsets (±13h, 30-min steps) and scores each by how many compute↔switch anchors line up; the auto pass applies the top one and prints it.
- Sanity-check the chosen offset against the *known* deployment (e.g. switches in UTC, compute hosts in UTC+8). If the auto pick is wrong or ambiguous, re-run with an explicit `--tz-offset-minutes` (e.g. `--tz-offset-minutes 60` if the switch is 1 h behind).
- A strong, unambiguous peak in the sweep (one offset with far more hits than the rest, with many Δ≈0 matches) is a good sign the offset is real.

### Step 4: Read the report

Sections:

1. **Timezone Alignment** — the offset sweep + which offset was applied.
2. **Correlated Events** — every compute-tray Xid/IMEX event that has ≥1 time-overlapping switch event, with a per-event details block listing the matching switch port-state / FNM events (raw time, shifted time, Δ seconds, chassis, transitions).
3. **Uncorrelated Compute-Tray Events** — Xid/IMEX groups with **no** switch correlation (an Xid with no matching fabric event is itself a signal).
4. **Switch Event Coverage** — how many switch events matched; the long tail of unmatched switch port flaps is summarized, not listed.

## Layout

```
<SKILL_ROOT>/
├── SKILL.md            # this file
├── pyproject.toml      # uv project (stdlib only — empty deps)
├── uv.lock
└── scripts/
    ├── correlate.py            # CLI entry point
    └── correlation_xid/
        ├── timeutil.py         # timestamp parse + syslog year inference + tz shift
        ├── models.py           # Event / TrayReport / SwitchReport dataclasses
        ├── parsers.py          # parse nv-bug-report + NVOS Markdown reports
        ├── engine.py           # anchor-proximity correlation + tz offset sweep
        └── render.py           # dual Markdown + self-contained HTML renderer
```

## Notes

- **Anchor-based matching**: an event contributes its `start` and (if different) its `end` as discrete anchor moments, rather than treating the whole `[start,end]` as active. This is deliberate — a single NVOS port-state group can lump an ACTIVE→DOWN and its recovery weeks apart into one group; matching on anchors correlates the down moment and the recovery moment independently instead of blanketing the gap.
- Correlation is **time-based** (the requested join). GPU identity is not used as the join key because the two sides expose different identifiers (nv-bug-report GPU UUID vs NVOS GPU GUID); chassis serial is used only to scope which trays and switches belong to the same rack.
- Syslog Xid timestamps in §7.3 carry no year; the year is inferred from the report's collection `Date` (with Dec→Jan rollover handling).
- Platform: Linux and macOS. Python **3.9+**, standard library only.
