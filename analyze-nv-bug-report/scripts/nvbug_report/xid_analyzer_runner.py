"""Run ``third_party/nvidia_xid_analyzer.py`` as a subprocess and parse its table output."""

import os
import pathlib
import re
import subprocess
import sys
import tempfile

from nvbug_report.timing import _analyze_timing_enabled, _phase_end, _phase_start

# Parent of this package is ``scripts/``; bundled NVIDIA analyzer lives under ``third_party/``.
_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent
_XID_ANALYZER_SCRIPT = _SCRIPTS_DIR / "third_party" / "nvidia_xid_analyzer.py"
_XID_CATALOG_XLSX = _SCRIPTS_DIR / "third_party" / "Server-RAS-Catalog.xlsx"

# Public sentinel so report.py can distinguish "missing required asset"
# from "analyzer ran but failed" and render a friendlier block.
MISSING_ASSET_PREFIX = "MISSING_XID_ASSET: "


def _missing_assets():
    missing = []
    if not _XID_ANALYZER_SCRIPT.exists():
        missing.append("nvidia_xid_analyzer.py")
    if not _XID_CATALOG_XLSX.exists():
        missing.append("Server-RAS-Catalog.xlsx")
    return missing


def run_xid_analyzer(raw_xid_lines, timing_label=""):
    """Run third_party/nvidia_xid_analyzer.py on extracted Xid lines and parse results.

    Returns (decoded_entries, line_map, error_msg, stdout_text, stderr_text).
    error_msg is empty on success; on failure it describes the problem
    (e.g. missing Python packages) so the report can surface it.
    stdout_text / stderr_text are subprocess streams (for saving artifacts).

    timing_label: basename used in NV_BUG_REPORT_ANALYZE_TIMING logs.
    """
    if not raw_xid_lines:
        return [], {}, "", "", ""

    log_name = timing_label or "xid_analyzer"

    analyzer = _XID_ANALYZER_SCRIPT
    missing = _missing_assets()
    if missing:
        msg = (
            MISSING_ASSET_PREFIX
            + "Missing " + " / ".join(missing)
            + " (under " + str(_XID_ANALYZER_SCRIPT.parent) + "). "
            + "This section requires the XID analyzer script - "
            + "please contact your NVIDIA contact to obtain it."
        )
        return [], {}, msg, "", ""

    unique_lines = list(dict.fromkeys(raw_xid_lines))

    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as tmp:
        tmp.write("\n".join(unique_lines) + "\n")
        tmp_path = tmp.name

    nvparse_python = pathlib.Path.home() / "anaconda3/envs/nvparse/bin/python"
    python_exe = str(nvparse_python) if nvparse_python.exists() else sys.executable

    output = ""
    stderr_text = ""
    try:
        t_sub = _phase_start()
        result = subprocess.run(
            [python_exe, str(analyzer), "--find-resolutions", tmp_path],
            capture_output=True,
            text=True,
            timeout=120,
        )
        _phase_end(log_name, "xid_analyzer_subprocess", t_sub)
        output = result.stdout or ""
        stderr_text = result.stderr or ""
        error_msg = ""
        if result.returncode != 0:
            missing = re.findall(r"No module named '(\w+)'", stderr_text)
            if missing:
                error_msg = (
                    f"Missing Python packages: {', '.join(missing)}. "
                    f"Install with: pip install {' '.join(missing)}"
                )
            else:
                error_msg = f"nvidia_xid_analyzer exited with code {result.returncode}"
    except Exception as e:
        return [], {}, f"nvidia_xid_analyzer execution error: {e}", "", ""
    finally:
        os.unlink(tmp_path)

    t_parse = _phase_start()
    decoded_entries = _parse_analyzer_table(output, unique_lines)
    _phase_end(log_name, "xid_analyzer_parse_table", t_parse)
    if _analyze_timing_enabled():
        print(
            f"[analyze-timing] {log_name} | metrics | "
            f"raw_xid_lines={len(raw_xid_lines)} unique_xid_lines={len(unique_lines)} "
            f"decoded_rows={len(decoded_entries)} analyzer_stdout_chars={len(output)}",
            file=sys.stderr,
        )

    t_map = _phase_start()
    line_map = {}
    for entry in decoded_entries:
        ctx = entry.get("context", "")
        for raw in raw_xid_lines:
            if raw in ctx or ctx in raw:
                line_map[raw] = entry
                break
    _phase_end(log_name, "xid_analyzer_build_line_map", t_map)
    if _analyze_timing_enabled() and decoded_entries and raw_xid_lines:
        est = len(decoded_entries) * len(raw_xid_lines)
        print(
            f"[analyze-timing] {log_name} | note | build_line_map is O(decoded×raw) "
            f"~{est} string comparisons; large values explain slowness/high CPU.",
            file=sys.stderr,
        )

    return decoded_entries, line_map, error_msg, output, stderr_text


def _parse_analyzer_table(output, _unique_lines):
    """Parse the psql-style table output from nvidia_xid_analyzer into dicts.

    The psql format has +---+---+ borders at top/bottom, |---|---| after the
    header, and NO separators between data rows.  Each logical data row may
    span multiple physical lines; a new logical row starts when the Machine Id
    column (index 1) is non-empty.
    """
    lines = output.split("\n")

    header_cells = []
    data_lines = []
    phase = "before"  # before -> header -> data -> done

    for line in lines:
        stripped = line.strip()
        if phase == "before":
            if stripped.startswith("+---"):
                phase = "header"
            continue
        if phase == "header":
            if stripped.startswith("|---"):
                phase = "data"
                continue
            if stripped.startswith("|"):
                cells = [c.strip() for c in stripped.split("|")[1:-1]]
                header_cells.append(cells)
            continue
        if phase == "data":
            if stripped.startswith("+---"):
                phase = "done"
                break
            if stripped.startswith("|"):
                cells = [c.strip() for c in stripped.split("|")[1:-1]]
                data_lines.append(cells)

    if not header_cells or not data_lines:
        return []

    headers = _merge_multiline_row_to_list(header_cells)

    logical_rows = []
    current_block = []
    for cells in data_lines:
        if len(cells) > 1 and cells[1]:
            if current_block:
                logical_rows.append(current_block)
            current_block = [cells]
        else:
            current_block.append(cells)
    if current_block:
        logical_rows.append(current_block)

    field_map = {
        "Timestamp": "timestamp",
        "Machine": "machine",
        "Decoded": "decoded_xid",
        "Mnemonic": "mnemonic",
        "XID Message": "context",
        "Job": "job_severity",
        "HW/SW": "hw_sw",
        "Resolution": "resolution",
        "Investigatory": "investigatory",
        "Comments": "comments",
    }

    result = []
    for block in logical_rows:
        merged = {}
        for col_idx in range(len(headers)):
            parts = []
            for row in block:
                if col_idx < len(row) and row[col_idx]:
                    parts.append(row[col_idx])
            merged[headers[col_idx]] = " ".join(parts)

        mapped = {}
        for raw_key, val in merged.items():
            matched = False
            for prefix, target in field_map.items():
                if prefix in raw_key or raw_key in prefix:
                    mapped[target] = val
                    matched = True
                    break
            if not matched:
                mapped[raw_key] = val

        if "mnemonic" in mapped and mapped["mnemonic"]:
            mapped["mnemonic"] = mapped["mnemonic"].replace(" ", "")
        if "job_severity" in mapped and mapped["job_severity"]:
            mapped["job_severity"] = mapped["job_severity"].replace(" ", "").replace("-", "-")
            mapped["job_severity"] = re.sub(r"(\w)-(\w)", r"\1-\2", mapped["job_severity"])

        if mapped:
            result.append(mapped)

    return result


def _merge_multiline_row_to_list(cell_rows):
    """Merge multi-line header cells into a single list of header names."""
    if not cell_rows:
        return []
    n_cols = max(len(r) for r in cell_rows)
    headers = []
    for col_idx in range(n_cols):
        parts = []
        for row in cell_rows:
            if col_idx < len(row) and row[col_idx]:
                parts.append(row[col_idx])
        headers.append(" ".join(parts))
    return headers
