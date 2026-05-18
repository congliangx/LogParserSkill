"""Persist raw bugreport sections (nvidia-smi, dmidecode, dmesg, journalctl, IMEX, etc.) per node."""

import gzip
import json
import os

from nvbug_report.sections import find_section_range

_DEFAULT_ARTIFACT_MAX_PLAIN_MB = 5.0
_ENV_ARTIFACT_MAX_PLAIN_MB = "NV_BUG_REPORT_ARTIFACT_MAX_PLAIN_MEGABYTES"


def _artifact_max_plain_bytes():
    """Max UTF-8 byte size for a plain ``.txt`` artifact before writing ``.txt.gz`` instead."""
    raw = os.environ.get(_ENV_ARTIFACT_MAX_PLAIN_MB)
    if raw is None or not str(raw).strip():
        mb = _DEFAULT_ARTIFACT_MAX_PLAIN_MB
    else:
        try:
            mb = float(str(raw).strip())
        except ValueError:
            mb = _DEFAULT_ARTIFACT_MAX_PLAIN_MB
    if mb <= 0:
        mb = _DEFAULT_ARTIFACT_MAX_PLAIN_MB
    return int(mb * 1024 * 1024)


def _write_text_artifact(dest_dir, basename_txt, payload):
    """Write ``payload`` under ``dest_dir`` as ``basename_txt`` or ``basename_txt + '.gz'`` if oversized.

    ``payload`` may be ``str`` or any bytes-like (``bytes``/``bytearray``/``memoryview``).
    The bytes-like path skips UTF-8 round-tripping — used by section slicing to write a
    ``LineStore`` byte-range straight to disk without materializing a decoded copy.
    Returns the basename actually written (``*.txt`` or ``*.txt.gz``).
    """
    if isinstance(payload, (bytes, bytearray, memoryview)):
        data = payload
    else:
        data = payload.encode("utf-8")
    if len(data) > _artifact_max_plain_bytes():
        out_name = basename_txt + ".gz"
        with gzip.open(os.path.join(dest_dir, out_name), "wb", compresslevel=9) as gz:
            gz.write(data)
        return out_name
    out_path = os.path.join(dest_dir, basename_txt)
    with open(out_path, "wb") as f:
        f.write(data)
    return basename_txt


def _sanitize_component(name):
    if not name:
        return ""
    name = os.path.basename(str(name))
    for ch in '\0/\\<>:"|?*':
        name = name.replace(ch, "_")
    return name.strip(" .")


def node_artifact_dir_name(filepath, prefix_parent_dir=True):
    """Directory name for one node's intermediate dumps (aligned with per-node report naming)."""
    basename = os.path.basename(filepath or "")
    for suffix in (".log.gz", ".log"):
        if basename.endswith(suffix):
            stem = basename[: -len(suffix)]
            break
    else:
        stem = basename
    stem = _sanitize_component(stem)
    if not stem:
        stem = "unknown-log"
    if prefix_parent_dir and filepath:
        parent = _sanitize_component(os.path.basename(os.path.dirname(os.path.abspath(filepath))))
        if parent:
            return f"{parent}_{stem}"
    return stem


def _slice_text(lines, start, end):
    """Return ``lines[start:end]`` as a zero-copy ``memoryview`` (LineStore-backed)
    or ``str`` (fallback for plain ``list[str]``). ``None`` for empty/invalid ranges.
    """
    if start < 0 or end < 0 or start >= end:
        return None
    if hasattr(lines, "get_bytes"):
        mv = lines.get_bytes(start, end)
        return mv if mv else None
    return "".join(lines[start:end])


def _payload_nonempty(payload):
    """Match the previous ``text and text.strip()`` semantics across str/bytes/memoryview
    without forcing a full-buffer copy on huge byte slices.
    """
    if not payload:
        return False
    if isinstance(payload, str):
        return bool(payload.strip())
    # bytes-like: avoid copying multi-hundred-MB buffers just to check for non-whitespace.
    # For tiny slices keep the exact strip()-semantics; for larger ones any non-empty range
    # from a valid line span is real content.
    if len(payload) <= 256:
        return bool(bytes(payload).strip())
    return True


# Main bugreport command sections: (disk basename, first-line marker, exact match for that marker).
# Each slice is written to the node artifact root when non-empty; used for report-side debugging only.
_SECTION_SPECS = [
    ("nvidia-smi--query.txt", "nvidia-smi --query", False),
    ("lspci-nn.txt", "lspci -nn", True),
    ("lspci-nnDvvvxxxx.txt", "lspci -nnDvvvxxxx", False),
    ("dmidecode.txt", "/sbin/dmidecode", False),
    ("nvidia-smi-nvlink-errorcounters.txt", "nvidia-smi nvlink --errorcounters", False),
    ("nvidia-smi-nvlink-status.txt", "nvidia-smi nvlink --status", False),
    ("systemctl-nvidia-imex-service.txt", "systemctl status nvidia-imex.service", False),
    ("var-log-nvidia-imex.log.txt", "/var/log/nvidia-imex.log", False),
    ("var-log-nvidia-imex-verbose.log.txt", "/var/log/nvidia-imex-verbose.log", False),
    ("var-log-nvidia-imex-stats.log.txt", "/var/log/nvidia-imex-stats.log", False),
    ("nvidia-imex-ctl-N.txt", "nvidia-imex-ctl -N", False),
]

_KERNEL_SCAN_MARKER = "Scanning kernel log files"
# Subsection markers to split out from inside "Scanning kernel log files" only (output under
# kernel-log-scan-parts/ and listed under kernel-log-scan.txt -> parts in manifest). Not written at artifact root.
_KERNEL_LOG_SCAN_PART_SPECS = [
    ("journalctl-b-0.txt", "journalctl -b -0:"),
    ("journalctl-b-1.txt", "journalctl -b -1:"),
    ("var-log-messages.txt", "/var/log/messages"),
    ("var-log-kern.log.txt", "/var/log/kern.log"),
    ("var-log-dmesg.txt", "/var/log/dmesg"),
]


def write_node_intermediate_artifacts(
    artifact_root_dir,
    filepath,
    lines,
    cache,
    raw_xid_lines=None,
    raw_xid_source_line_numbers=None,
    xid_analyzer_stdout=None,
    xid_analyzer_stderr=None,
    prefix_parent_dir=True,
):
    """Write one subdirectory under ``artifact_root_dir`` with split text files for each section.

    Xid lines are written as ``<1-based line>: <text>`` in ``xid-nvrm-lines.txt`` (or
    ``xid-nvrm-lines.txt.gz`` if the UTF-8 payload exceeds the plain-text size threshold). The files
    ``xid-nvrm-lines.*``, ``nvidia-xid-analyzer-stdout.*``, and
    ``nvidia-xid-analyzer-stderr.*`` are not listed in ``manifest.json`` (not bugreport slices).

    Returns the path to the node directory, or None if nothing was written and directory not needed.
    """
    if not artifact_root_dir or not lines:
        return None

    node_dir = node_artifact_dir_name(filepath, prefix_parent_dir=prefix_parent_dir)
    dest = os.path.join(artifact_root_dir, node_dir)
    os.makedirs(dest, exist_ok=True)

    manifest = {
        "source_log": os.path.basename(filepath or ""),
        "source_path": os.path.abspath(filepath) if filepath else "",
        "files": {},
    }

    for fname, marker, exact in _SECTION_SPECS:
        start, end = find_section_range(lines, marker, exact=exact, cache=cache)
        text = _slice_text(lines, start, end)
        if _payload_nonempty(text):
            written = _write_text_artifact(dest, fname, text)
            manifest["files"][written] = {"section_marker": marker, "line_range": [start, end]}

    # dmesg: only when a real `dmesg:` section exists (do not dump whole log)
    ds, de = find_section_range(lines, "dmesg:", cache=cache)
    if ds >= 0:
        text = _slice_text(lines, ds, de)
        if _payload_nonempty(text):
            fname = "dmesg.txt"
            written = _write_text_artifact(dest, fname, text)
            manifest["files"][written] = {"section_marker": "dmesg:", "line_range": [ds, de]}

    ks_s, ks_e = find_section_range(lines, _KERNEL_SCAN_MARKER, exact=False, cache=cache)
    parts_meta = {}
    if ks_s >= 0:
        ks_text = _slice_text(lines, ks_s, ks_e)
        if _payload_nonempty(ks_text):
            embedded = []
            for fname, marker in _KERNEL_LOG_SCAN_PART_SPECS:
                cs, ce = find_section_range(lines, marker, exact=False, cache=cache)
                if cs < 0 or not (ks_s <= cs < ks_e):
                    continue
                ce2 = min(ce, ks_e)
                if cs >= ce2:
                    continue
                embedded.append((fname, marker, cs, ce2))
            embedded.sort(key=lambda x: x[2])
            if embedded:
                sub_dir = os.path.join(dest, "kernel-log-scan-parts")
                os.makedirs(sub_dir, exist_ok=True)
                for fname, marker, cs, ce2 in embedded:
                    t = _slice_text(lines, cs, ce2)
                    if not _payload_nonempty(t):
                        continue
                    wpart = _write_text_artifact(sub_dir, fname, t)
                    parts_meta[wpart] = {
                        "section_marker": marker,
                        "line_range": [cs, ce2],
                    }
            wks = _write_text_artifact(dest, "kernel-log-scan.txt", ks_text)
            entry = {
                "section_marker": _KERNEL_SCAN_MARKER,
                "line_range": [ks_s, ks_e],
            }
            if parts_meta:
                entry["parts"] = parts_meta
            manifest["files"][wks] = entry

    if raw_xid_lines:
        fname = "xid-nvrm-lines.txt"
        nums = raw_xid_source_line_numbers or []
        out_lines = []
        for idx, text in enumerate(raw_xid_lines):
            n = nums[idx] if idx < len(nums) else None
            if n is not None:
                out_lines.append(f"{n}: {text}")
            else:
                out_lines.append(text)
        body = "\n".join(out_lines)
        if body.strip():
            if not body.endswith("\n"):
                body = body + "\n"
            _write_text_artifact(dest, fname, body)

    if xid_analyzer_stdout and xid_analyzer_stdout.strip():
        fname = "nvidia-xid-analyzer-stdout.txt"
        _write_text_artifact(dest, fname, xid_analyzer_stdout)

    if xid_analyzer_stderr and xid_analyzer_stderr.strip():
        fname = "nvidia-xid-analyzer-stderr.txt"
        _write_text_artifact(dest, fname, xid_analyzer_stderr)

    manifest_path = os.path.join(dest, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")

    return dest
