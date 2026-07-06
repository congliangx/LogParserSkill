# LogParserSkill

A collection of custom Agent Skills and related NVIDIA log-analysis tools. Directories that contain a `SKILL.md` are installable Agent Skills; other directories may be standalone tooling or documentation. The skill `SKILL.md` files use only relative paths for bundled scripts, so the same directory installs unchanged into **Cursor**, **Claude Code**, or **Codex**.

## Repository layout

```
LogParserSkill/
├── README.md                         # This file
├── LICENSE
├── requirements.txt                  # pip fallback deps for analyze-nv-bug-report (its uv project is the primary path)
├── analyze-nv-bug-report/            # Agent Skill for nvidia-bug-report.sh logs
│   ├── SKILL.md
│   ├── pyproject.toml                # per-skill uv project (deps); `uv sync` builds .venv/
│   ├── uv.lock                       # pinned dependency versions (committed)
│   └── scripts/
│       ├── analyze.py
│       ├── nvbug_report/
│       └── third_party/              # NVIDIA Xid analyzer assets — drop-in required; only README is committed
├── nvos-tech-dump-tools-for-nmx-c/   # Agent Skill for NVOS / NMX-C NVLSM + Fabric Manager logs
│   ├── SKILL.md
│   ├── pyproject.toml                # per-skill uv project (stdlib only — empty deps)
│   ├── uv.lock
│   └── scripts/
│       ├── main.py
│       └── nmx_log_tools/
├── correlation-xid/                  # Agent Skill: time-correlate nv-bug-report + NVOS reports
│   ├── SKILL.md
│   ├── pyproject.toml                # per-skill uv project (stdlib only — empty deps)
│   ├── uv.lock
│   └── scripts/
│       ├── correlate.py
│       └── correlation_xid/
└── doc/                              # Per-skill structure / design docs
    └── analyze-nv-bug-report-structure.md
```

## Available skills

Only directories with `SKILL.md` are listed here as Agent Skills.

| Name | Purpose |
|---|---|
| [`analyze-nv-bug-report`](analyze-nv-bug-report/) | Analyze NVIDIA `nvidia-bug-report.sh` log files — extract GPU status, Xid errors, NVLink / IMEX state, and emit a Markdown report (plus a self-contained HTML sidecar). Supports both single-file and multi-node batch comparison. |
| [`nvos-tech-dump-tools-for-nmx-c`](nvos-tech-dump-tools-for-nmx-c/) | Analyze **NVLSM** + **Fabric Manager** logs under `log/nmx/nmx-c` in an NVOS / NMX-C tech dump (directory or `.tar.gz`) — NVLSM topology / port-state clustering, NVLSM forensics, and a full Fabric Manager (`-vvv`) parse — into a Markdown + HTML report. |
| [`correlation-xid`](correlation-xid/) | **Time-correlate** the two skills' reports: fold each switch port-state fabric event to the compute-tray Xid / IMEX **cross-node event group(s)** it overlaps in time (timezone-offset aware, auto-suggested; per-chassis), with a node-deduped Xid raw-log summary, time-aligned FNM port-loss context, and the matching Fabric Manager rows. Replaces `analyze-rack-xid`. |

All three skills install the same way (see below). They differ only in dependencies: `analyze-nv-bug-report` pulls a few PyPI packages into its `uv` environment and needs two drop-in NVIDIA Xid assets (step 3); `nvos-tech-dump-tools-for-nmx-c` and `correlation-xid` are **standard-library-only**, so their `uv` environments have no third-party packages — they can even run on any Python 3.9+ without `uv`. `correlation-xid` reads the *reports* produced by the other two skills, so run those first.

## Installing a skill

Each skill installs as a directory named after the skill, containing `SKILL.md` + `scripts/`. Because the `SKILL.md` files reference bundled scripts only by relative path, the same directory installs unchanged into Cursor, Claude Code, or Codex — just pick the install root that matches your tool:

| Tool | Install root | How the agent picks the skill up |
|---|---|---|
| Cursor | `~/.cursor/skills/<skill>/` | Auto-loaded — restart chat to see new skills. |
| Claude Code | `~/.claude/skills/<skill>/` (global) or `<project>/.claude/skills/<skill>/` (project) | Auto-loaded — global skills available everywhere, project skills only inside that repo. |
| Codex | `~/.codex/skills/<skill>/` | Auto-loaded — restart Codex session if needed. |

> **Prerequisite:** [`uv`](https://docs.astral.sh/uv/) on your `PATH`. Each skill manages its own Python environment through a per-skill `uv` project — `uv` installs a pinned interpreter and builds a per-skill `.venv/`, so there is no system-Python requirement and nothing to `pip install` globally. (`nvos-tech-dump-tools-for-nmx-c` is stdlib-only and can also run without `uv` on any Python 3.9+; see step 5.)

### Steps

#### 1. Clone this repo somewhere

```bash
git clone https://github.com/congliangx/LogParserSkill.git
cd LogParserSkill
```

> If you already cloned it before, just `git pull` to fetch the latest version.

#### 2. Prepare the skills root for your tool

```bash
mkdir -p ~/.cursor/skills    # Cursor
mkdir -p ~/.claude/skills    # Claude Code (user-level)
mkdir -p ~/.codex/skills     # Codex
# Project-level Claude Code skills go to <project>/.claude/skills/ — no mkdir if you don't use them
```

#### 3. Drop in third-party assets (analyze-nv-bug-report only)

`analyze-nv-bug-report` shells out to NVIDIA's Xid decoder, which is **not** committed to this repo. Before syncing the skill, place the two assets below into `analyze-nv-bug-report/scripts/third_party/`:

| File | Source |
|---|---|
| `nvidia_xid_analyzer.py` | NVIDIA Xid analyzer bundle |
| `Server-RAS-Catalog.xlsx` | NVIDIA Xid analyzer bundle |

Provenance, redistribution policy, and a sanity-check snippet live in [`analyze-nv-bug-report/scripts/third_party/README.md`](analyze-nv-bug-report/scripts/third_party/README.md). If you skip this step the skill still runs, but section §7.2 (Xid Detailed Decode) of the report is skipped with a "Required assets missing" warning.

> `nvos-tech-dump-tools-for-nmx-c` and `correlation-xid` have no third-party assets — skip this step for them.

#### 4. Sync the skill directory with `rsync -aPp`

```bash
# Cursor
rsync -aPp ./<skill-name> ~/.cursor/skills/

# Claude Code (user-level)
rsync -aPp ./<skill-name> ~/.claude/skills/

# Codex
rsync -aPp ./<skill-name> ~/.codex/skills/
```

#### 5. Build the skill's `uv` environment at the install location

A `.venv` is not path-portable, so build it **in place** after syncing (do NOT `uv sync` in the clone and rsync it across — create it at the install root):

```bash
uv sync --project ~/.claude/skills/<skill-name>   # swap the root for Cursor / Codex
```

Afterwards the skill runs every script directly through that env — e.g. `~/.claude/skills/<skill-name>/.venv/bin/python scripts/<entry>.py ...` (each `SKILL.md` does this automatically). It calls `.venv/bin/python` directly, not `uv run`, so no write access to the skill dir is needed at run time.

> **No `uv`?**
> - `analyze-nv-bug-report` needs a few PyPI packages — install them into any Python 3.9+ interpreter and call that interpreter instead: `pip install -r requirements.txt`.
> - `nvos-tech-dump-tools-for-nmx-c` and `correlation-xid` are **stdlib-only** — skip `uv` entirely and just run them with any Python 3.9+ (e.g. `python3 ~/.claude/skills/correlation-xid/scripts/correlate.py ...`).

#### 6. (Optional) Register the NVBugs MCP server for `analyze-nv-bug-report`

Step 2.5 of `analyze-nv-bug-report/SKILL.md` can call the `nvbugs_search` tool on a MaaS-hosted MCP server (`user-MaaS NVBugs`) to attach related bugs to the report. This lookup is **disabled by default** — the skill runs it only when you explicitly ask (e.g. "search nvbugs" in chat), and even then it is optional: if the server is not registered the skill prints a one-line notice and appends no bugs section. To make the lookup available, register the server in your tool's MCP config:

- **Claude Code**: add an entry under `mcpServers` in `~/.claude.json` (or `~/.config/claude-code/mcp.json`, depending on version).
- **Cursor**: add the server under `mcpServers` in `~/.cursor/mcp.json`.
- **Codex**: add the server under `mcp_servers` in `~/.codex/config.toml`.

Use the exact server name `user-MaaS NVBugs` so the `SKILL.md` procedure matches. (`nvos-tech-dump-tools-for-nmx-c` uses no MCP server — skip this step for it.)

#### 7. Verify in the target tool

Open a new chat in the tool and prompt something matching the skill's description (e.g. "analyze this nv-bug-report.log", or "analyze this NVOS NMX-C dump"). The agent will load the skill and follow its `SKILL.md` workflow.

---

## Full example: installing `analyze-nv-bug-report` for Claude Code

```bash
# 1. Clone (first-time install)
git clone https://github.com/congliangx/LogParserSkill.git ~/repos/LogParserSkill
cd ~/repos/LogParserSkill

# 2. Prepare skills root (user-level Claude Code)
mkdir -p ~/.claude/skills

# 3. Drop in NVIDIA Xid analyzer assets (see analyze-nv-bug-report/scripts/third_party/README.md)
#    Required: nvidia_xid_analyzer.py  Server-RAS-Catalog.xlsx
cp /path/to/extracted/nvidia_xid_analyzer.py    analyze-nv-bug-report/scripts/third_party/
cp /path/to/extracted/Server-RAS-Catalog.xlsx   analyze-nv-bug-report/scripts/third_party/

# 4. Sync the skill to its install root
rsync -aPp ./analyze-nv-bug-report ~/.claude/skills/

# 5. Build the skill's uv environment AT the install location
#    (creates ~/.claude/skills/analyze-nv-bug-report/.venv ; needs `uv` on PATH)
uv sync --project ~/.claude/skills/analyze-nv-bug-report

# 6. Verify the install
ls ~/.claude/skills/analyze-nv-bug-report/
# Expect:  SKILL.md  pyproject.toml  uv.lock  scripts/  .venv/

ls ~/.claude/skills/analyze-nv-bug-report/scripts/third_party/
# Expect:  README.md  nvidia_xid_analyzer.py  Server-RAS-Catalog.xlsx

head -5 ~/.claude/skills/analyze-nv-bug-report/SKILL.md
# Expect frontmatter:
# ---
# name: analyze-nv-bug-report
# description: Analyze NVIDIA nv-bug-report log files...
```

For Cursor, swap `~/.claude/skills/` → `~/.cursor/skills/`. For Codex, swap → `~/.codex/skills/`. Everything else is identical.

#### Using it inside the tool

Open a new chat, drop a log file into the input (or paste its path), and prompt:

> Analyze this nv-bug-report

The agent invokes the `analyze-nv-bug-report` skill, runs `scripts/analyze.py` through the skill's own `.venv/bin/python` to produce a Markdown report (plus a self-contained HTML sidecar), and — only when you explicitly ask for NVBugs lookup, Xid errors are present, and the NVBugs MCP server from step 6 is registered — attaches related bugs to the report. NVBugs lookup is **disabled by default**; see [`analyze-nv-bug-report/SKILL.md`](analyze-nv-bug-report/SKILL.md) Step 2.5, and say "search nvbugs" in chat to opt in.

For a deeper walk-through of the internal modules, see [`doc/analyze-nv-bug-report-structure.md`](doc/analyze-nv-bug-report-structure.md).

## Installing `nvos-tech-dump-tools-for-nmx-c`

Same flow, minus the analyze-nv-bug-report-only bits (no step 3 assets, no step 6 MCP):

```bash
cd ~/repos/LogParserSkill
mkdir -p ~/.claude/skills
rsync -aPp ./nvos-tech-dump-tools-for-nmx-c ~/.claude/skills/
uv sync --project ~/.claude/skills/nvos-tech-dump-tools-for-nmx-c   # stdlib-only env; or skip uv and use any Python 3.9+
```

Then prompt the agent with something like *"analyze this NVOS NMX-C dump /path/to/nvos_dump.tar.gz"*. The skill runs `scripts/main.py` and writes a Markdown + HTML report to the output directory. See [`nvos-tech-dump-tools-for-nmx-c/SKILL.md`](nvos-tech-dump-tools-for-nmx-c/SKILL.md) for arguments and what the report covers.

---


## Platform support

All bundled scripts run on both **Linux** and **macOS**. See each skill's `SKILL.md` for skill-specific notes.
