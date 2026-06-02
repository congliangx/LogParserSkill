# nvos_tech_dump_processing_toools_for_nmx-c

Standalone toolkit to analyze **NVLSM** and **Fabric Manager** logs under `log/nmx/nmx-c` in NVOS dumps.

## Features

- Early validation: refuses to run if `log/nmx` is missing (tar member scan before extract).
- Tar.gz: extracts only the `log/nmx` subtree to a temp directory (not the full dump).
- **NVLSM** (`checks/nvlsm` patterns): invalid topology, invalid UTF-8, port state events + clustering.
- **NVLSM forensics** (`nvlsm_forensics.py`): INIT/Unlink state change counts from `nvlsm.log`.
- **Fabric Manager** (`fabric_manager` parser): full parse with **-vvv defaults** (all log files, no age filter, all health polls).
- Reports: **Markdown** and **HTML** to a user-specified output directory.
- **Aggregated detail tables**: repeated log lines grouped by pattern (count + first/last time); NVLSM port transitions are shown only inside each time cluster (full list per cluster).

## Usage

```bash
python main.py /path/to/nvos_dump_or.tar.gz -o /path/to/reports [--name my_report]
```

Produces `my_report.md` and `my_report.html`.

## Configuration

Defaults in `nmx_log_tools/config.py` include NVLSM port-state clustering: **120 s** gap between events in one cluster, **300 s** maximum span per cluster (tuned so `ACTIVE→DOWN` and the following `DOWN→INIT` usually land together).

## Layout

```
nmx_log_tools/
  discovery.py       # log/nmx validation, file discovery
  sources/           # directory vs tarball
  parsers/           # nvlsm_health, nvlsm_forensics, fabric_manager
  cluster/           # adaptive time clustering
  analyze/           # pipeline orchestration
  report/            # markdown + html
main.py              # CLI
```

## Requirements

Python 3.9+ stdlib only.
