# LogParserSkill

A collection of custom Agent Skills and related NVIDIA log-analysis tools. Directories that contain a `SKILL.md` are installable Agent Skills; other directories may be standalone tooling or documentation.

## Repository layout

```
LogParserSkill/
├── README.md                         # This file
├── LICENSE
├── requirements.txt                  # Python deps for analyze-nv-bug-report scripts
├── analyze-nv-bug-report/            # Agent Skill for nvidia-bug-report.sh logs
│   ├── SKILL.md
│   └── scripts/
│       ├── analyze.py
│       ├── nvbug_report/
│       └── third_party/              # Drop-in NVIDIA Xid analyzer assets; README only is committed
├── doc/                              # Per-skill structure / design docs
│   └── analyze-nv-bug-report-structure.md
└── nvos_tech_dump_tools_for_nmx-c/   # Standalone NVOS NMX-C tech dump log-analysis toolkit
    ├── README.md
    ├── requirements.txt
    ├── main.py
    └── nmx_log_tools/
```

## Available skills

Only directories with `SKILL.md` are listed here as Agent Skills.

| Name | Purpose |
|---|---|
| [`analyze-nv-bug-report`](analyze-nv-bug-report/) | Analyze NVIDIA `nvidia-bug-report.sh` log files — extract GPU status, Xid errors, NVLink / IMEX state, and emit a Markdown report. Supports both single-file and multi-node batch comparison. |

`nvos_tech_dump_tools_for_nmx-c/` is included in this repo as a standalone CLI toolkit for NVOS NMX-C tech dump logs. It is not auto-loaded as an Agent Skill because it does not contain a `SKILL.md`.

## Installing a skill

Cursor automatically loads `~/.cursor/skills/<skill-name>/SKILL.md`, so "installing" a skill is just copying the corresponding subdirectory into `~/.cursor/skills/` while keeping the directory name intact.

### Steps

#### 1. Clone this repo somewhere

```bash
git clone https://gitlab-master.nvidia.com/congliangx/skills.git
cd skills
```

> If you already cloned it before, just `git pull` to fetch the latest version.

#### 2. Install Python dependencies

- Python **3.9+** (the bundled NVIDIA Xid decoder uses `zoneinfo` from the standard library)
- Install the third-party Python packages required by bundled scripts:

  ```bash
  pip install -r requirements.txt
  ```

#### 3. Drop in third-party assets (analyze-nv-bug-report only)

`analyze-nv-bug-report` shells out to NVIDIA's Xid decoder, which is **not** committed to this repo. Before syncing the skill, place the two assets below into `analyze-nv-bug-report/scripts/third_party/`:

| File | Source |
|---|---|
| `nvidia_xid_analyzer.py` | NVIDIA Xid analyzer bundle |
| `Server-RAS-Catalog.xlsx` | NVIDIA Xid analyzer bundle |

Details (provenance, redistribution policy, sanity-check snippet) live in [`analyze-nv-bug-report/scripts/third_party/README.md`](analyze-nv-bug-report/scripts/third_party/README.md). If you skip this step, the skill still runs, but section §7.2 (Xid Detailed Decode) of the report will be skipped with a "Required assets missing" warning.

#### 4. Make sure the target directory exists

```bash
mkdir -p ~/.cursor/skills
```

#### 5. Sync the skill directory with `rsync -aPp`

```bash
rsync -aPp ./<skill-name> ~/.cursor/skills/
```

#### 6. Verify in Cursor / Claude Code

Open Cursor, start a new chat, and prompt something matching the skill's description (e.g. "analyze this nv-bug-report.log"). The agent will load the skill and follow its `SKILL.md` workflow.

---

## Full example: installing `analyze-nv-bug-report`

```bash
# 1. Clone (first-time install)
git clone https://gitlab-master.nvidia.com/congliangx/skills.git ~/repos/skills
cd ~/repos/skills

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Drop in NVIDIA Xid analyzer assets (see analyze-nv-bug-report/scripts/third_party/README.md)
#    Required: nvidia_xid_analyzer.py  Server-RAS-Catalog.xlsx
cp /path/to/extracted/nvidia_xid_analyzer.py    analyze-nv-bug-report/scripts/third_party/
cp /path/to/extracted/Server-RAS-Catalog.xlsx   analyze-nv-bug-report/scripts/third_party/

# 4. Prepare skills root
mkdir -p ~/.cursor/skills

# 5. Sync the skill
rsync -aPp ./analyze-nv-bug-report ~/.cursor/skills/

# 6. Verify the install
ls ~/.cursor/skills/analyze-nv-bug-report/
# Expect:  SKILL.md  scripts/

ls ~/.cursor/skills/analyze-nv-bug-report/scripts/third_party/
# Expect:  README.md  nvidia_xid_analyzer.py  Server-RAS-Catalog.xlsx

cat ~/.cursor/skills/analyze-nv-bug-report/SKILL.md | head -5
# Expect frontmatter:
# ---
# name: analyze-nv-bug-report
# description: Analyze NVIDIA nv-bug-report log files...
```

#### Using it inside Cursor

Open a new chat, drop a log file into the input (or paste its path), and prompt:

> Analyze this nv-bug-report

The agent invokes the `analyze-nv-bug-report` skill and runs `scripts/analyze.py` to produce a Markdown report (plus a self-contained HTML sidecar).

For a deeper walk-through of the internal modules, see [`doc/analyze-nv-bug-report-structure.md`](doc/analyze-nv-bug-report-structure.md).

---


## Platform support

All bundled scripts run on both **Linux** and **macOS**. See each skill's `SKILL.md` for skill-specific notes.
