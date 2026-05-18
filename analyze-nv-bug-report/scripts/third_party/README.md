# Third-party assets (drop-in required)

This directory is intentionally empty in the git tree. The skill's Xid
deep-decode pipeline (`scripts/nvbug_report/xid_analyzer_runner.py`) shells
out to two NVIDIA-provided assets that are not redistributed with this
repo. You must place them here before running `analyze.py` on logs that
contain Xid errors.

## Required files

| File | Purpose |
|---|---|
| `nvidia_xid_analyzer.py` | NVIDIA Xid decoder. Invoked as a subprocess with `--find-resolutions` / `--decode-xid-message`. |
| `Server-RAS-Catalog.xlsx` | RAS catalog consumed by `nvidia_xid_analyzer.py` for mnemonic / severity / HW-SW / resolution lookup. |

After dropping them in, the directory should look like:

```
scripts/third_party/
├── README.md                 (this file)
├── nvidia_xid_analyzer.py
└── Server-RAS-Catalog.xlsx
```

## Where to get them

Both assets ship inside NVIDIA's internal Xid analyzer release bundle.
Download the latest bundle, extract it, and copy the two files above into
this directory. Do not rename them — `xid_analyzer_runner.py` resolves
them by exact filename.

## Why they are not committed to git

NVIDIA's redistribution policy on these assets prevents bundling them in
this repo. Keeping the directory empty (rather than vendored) also lets
each user pull the version that matches their own support contract /
internal release.

## Sanity check

After dropping the files in, from the repo root run:

```bash
python3 - <<'PY'
from pathlib import Path
d = Path("analyze-nv-bug-report/scripts/third_party")
for f in ("nvidia_xid_analyzer.py", "Server-RAS-Catalog.xlsx"):
    print(f, "OK" if (d / f).exists() else "MISSING")
PY
```

Both lines must print `OK`. If either prints `MISSING`, `analyze.py` will
still run, but section §7.2 (Xid Detailed Decode) of the generated report
will be skipped with a "Required assets missing" warning, and `--decode-xid-message`
/ `--find-resolutions` manual invocations (see `SKILL.md` Step 4) will fail.
