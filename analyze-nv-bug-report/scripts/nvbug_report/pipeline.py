"""Orchestrate read → extract → Xid context → decode → markdown for one bugreport log."""

import os
import sys
import tracemalloc

from nvbug_report.basics import compute_boot_time, parse_log_date, read_log
from nvbug_report.dmesg_highlights import filter_dmesg_highlights
from nvbug_report.extractors_dmesg_nvrm import extract_dmesg_gpu_serials
from nvbug_report.extractors_dmesg_sections import extract_dmesg_and_messages, extract_pcie_fallen_off
from nvbug_report.extractors_kernel_logs import (
    extract_message_start_time,
    extract_nvrm_errors,
    extract_xid_errors,
)
from nvbug_report.extractors_nvlink import extract_nvlink_errors, extract_nvlink_status
from nvbug_report.extractors_pci_gpu import (
    extract_lspci_nn,
    extract_lspci_verbose,
    extract_nvidia_smi_query,
    extract_proc_gpu_info,
)
from nvbug_report.extractors_system import (
    _supplement_system_info,
    extract_dmidecode_serial,
    extract_system_info,
)
from nvbug_report.imex import extract_imex_status
from nvbug_report.report import generate_report, group_xids_into_bursts
from nvbug_report.section_artifacts import write_node_intermediate_artifacts
from nvbug_report.sections import SectionRangeCache, _get_dmesg_range, _get_syslog_ranges
from nvbug_report.timing import (
    _analyze_stat_line,
    _analyze_timing_enabled,
    _phase_end,
    _phase_start,
)
from nvbug_report.xid_analyzer_runner import run_xid_analyzer
from nvbug_report.xid_context import collect_xid_context_lines

_MEM_PROFILING = os.environ.get("NV_BUG_REPORT_ANALYZE_MEM") == "1"


def _mem_snap(label):
    snap = tracemalloc.take_snapshot()
    total = sum(s.size for s in snap.statistics("filename"))
    top = snap.statistics("lineno")[:8]
    print(f"\n[mem-prof] ── {label} ── total tracked: {total / 1024**2:.1f} MB", file=sys.stderr)
    for s in top:
        print(f"  {s}", file=sys.stderr)


def analyze_single_file(filepath, artifact_root_dir=None):
    """Analyze a single nv-bug-report log file.

    Returns a dict with all extracted data for downstream use (batch comparison, etc.).
    If ``artifact_root_dir`` is set, per-section extracts and Xid helper files are written
    there after analysis (``write_node_intermediate_artifacts``).

    Performance diagnostics: set environment variable NV_BUG_REPORT_ANALYZE_TIMING=1
    to print per-phase timings and key counts to stderr (read_log, each of
    extract_xid_errors / extract_nvlink_* / imex / pcie_fallen_off, context collection,
    xid analyzer subprocess, generate_report, etc.). Section header lookups are memoized
    via ``SectionRangeCache`` so repeated markers are not rescanned from line 0.
    """
    bn = os.path.basename(filepath)
    if _analyze_timing_enabled():
        print(f"[analyze-timing] {bn} | ----- begin single-file analysis -----", file=sys.stderr)

    if _MEM_PROFILING:
        tracemalloc.start()

    print(f"Reading file: {filepath} ...", file=sys.stderr)
    t = _phase_start()
    lines = read_log(filepath)
    _phase_end(filepath, "read_log (decompress+split_lines)", t)
    print(f"Total {len(lines)} lines, starting analysis...", file=sys.stderr)
    if _MEM_PROFILING:
        _mem_snap("after read_log")
    try:
        fbytes = os.path.getsize(filepath)
    except OSError:
        fbytes = "?"
    _analyze_stat_line(filepath, log_lines=len(lines), file_bytes_on_disk=fbytes)

    # One memoized index for all section headers — avoids dozens of repeated O(n)
    # find_section_range scans across extract_* (major cost in extract_xid_* bundle).
    section_cache = SectionRangeCache(lines)

    t = _phase_start()
    sys_info = extract_system_info(lines)
    _supplement_system_info(lines, sys_info)
    sys_info["system_sn"] = extract_dmidecode_serial(lines, section_cache)
    sys_info["message_start_time"] = extract_message_start_time(lines, section_cache)

    _dt = parse_log_date(sys_info.get("date", ""))
    sys_info["date_short"] = _dt.strftime("%Y-%m-%d %H:%M") if _dt else sys_info.get("date", "N/A")
    sys_info["boot_time"] = compute_boot_time(sys_info.get("date", ""), sys_info.get("uptime", ""))
    _phase_end(filepath, "system_info+dmidecode+message_start_time", t)

    t = _phase_start()
    lspci_gpus = extract_lspci_nn(lines, section_cache)
    gpu_bdfs = [g["bdf"] for g in lspci_gpus]
    lspci_detail = extract_lspci_verbose(lines, gpu_bdfs, section_cache)
    smi_gpus = extract_nvidia_smi_query(lines, section_cache)
    if not smi_gpus:
        smi_gpus = extract_proc_gpu_info(lines)
        dmesg_serials = extract_dmesg_gpu_serials(lines)
        for gpu in smi_gpus:
            bdf = gpu.get("bdf", "")
            if bdf in dmesg_serials:
                gpu["sn"] = dmesg_serials[bdf]
    if smi_gpus:
        sys_info["chassis_sn"] = smi_gpus[0].get("chassis_sn", "N/A")
        sys_info["slot_number"] = smi_gpus[0].get("slot_number", "N/A")
        sys_info["tray_index"] = smi_gpus[0].get("tray_index", "N/A")
    _phase_end(filepath, "lspci+nvidia_smi_query+proc_gpu_fallback", t)

    t = _phase_start()
    xids, raw_xid_lines, raw_xid_source_line_numbers = extract_xid_errors(lines, section_cache)
    _phase_end(filepath, "extract_xid_errors", t)

    t = _phase_start()
    nvlink = extract_nvlink_errors(lines, section_cache)
    _phase_end(filepath, "extract_nvlink_errors", t)

    t = _phase_start()
    nvlink_status = extract_nvlink_status(lines, section_cache)
    _phase_end(filepath, "extract_nvlink_status", t)

    t = _phase_start()
    fallen_off = extract_pcie_fallen_off(lines, section_cache)
    _phase_end(filepath, "extract_pcie_fallen_off", t)

    t = _phase_start()
    imex = extract_imex_status(lines, section_cache)
    _phase_end(filepath, "extract_imex_status", t)
    if _MEM_PROFILING:
        _mem_snap("after all extractors (xid/nvlink/imex/lspci/smi)")

    if _analyze_timing_enabled():
        _analyze_stat_line(filepath, section_cache_entries=section_cache.filled())

    t = _phase_start()
    bursts = group_xids_into_bursts(xids, gap_seconds=60)
    _phase_end(filepath, "group_xids_into_bursts", t)

    t = _phase_start()
    # Pre-load only the syslog/dmesg sections needed for context scanning.
    # Cache is already warm from prior extractor calls, so _get_syslog_ranges
    # and _get_dmesg_range return cached ranges without rescanning.
    # Passing pre-loaded section lists instead of the full LineStore means
    # collect_xid_context_lines holds no reference to the full file content;
    # the temporary section str objects are freed when _context_sections is deleted.
    _context_sections = [lines[s:e] for s, e in _get_syslog_ranges(lines, section_cache)]
    _ds, _de = _get_dmesg_range(lines, section_cache)
    if _ds >= 0:
        _context_sections.append(lines[_ds:_de])
    context_by_burst, xid_associated_payloads = collect_xid_context_lines(
        _context_sections, bursts
    )
    del _context_sections
    _phase_end(filepath, "collect_xid_context_lines (syslog+dmesg sections only)", t)
    if _MEM_PROFILING:
        _mem_snap("after collect_xid_context_lines")

    ctx_total = sum(len(v) for v in context_by_burst.values())
    _analyze_stat_line(
        filepath,
        xids=len(xids),
        raw_xid_lines=len(raw_xid_lines),
        bursts=len(bursts),
        context_events=ctx_total,
        xid_nvrm_payloads_tracked=len(xid_associated_payloads),
    )

    t = _phase_start()
    nvrm_errors = extract_nvrm_errors(
        lines, exclude_payloads=xid_associated_payloads, cache=section_cache
    )
    _phase_end(filepath, "extract_nvrm_errors", t)

    xid_decoded = []
    xid_analyzer_error = ""
    xid_analyzer_stdout = ""
    xid_analyzer_stderr = ""
    if raw_xid_lines:
        print(
            f"Found {len(raw_xid_lines)} Xid errors, invoking nvidia_xid_analyzer for decoding...",
            file=sys.stderr,
        )
        xid_decoded, _, xid_analyzer_error, xid_analyzer_stdout, xid_analyzer_stderr = run_xid_analyzer(
            raw_xid_lines, timing_label=bn
        )
        if xid_analyzer_error:
            print(f"nvidia_xid_analyzer error: {xid_analyzer_error}", file=sys.stderr)
        else:
            print(f"Decoding complete, {len(xid_decoded)} results", file=sys.stderr)
    if _MEM_PROFILING:
        _mem_snap("after run_xid_analyzer")

    t = _phase_start()
    dmesg_lines = extract_dmesg_and_messages(lines, section_cache)
    dmesg_highlights = (
        filter_dmesg_highlights(dmesg_lines)
        if dmesg_lines
        else {"system_errors": [], "other_warnings": []}
    )
    _phase_end(filepath, "dmesg_messages_extract+filter_highlights", t)
    if _analyze_timing_enabled() and dmesg_lines:
        _analyze_stat_line(filepath, dmesg_section_lines=len(dmesg_lines))

    t = _phase_start()
    report = generate_report(
        filepath,
        sys_info,
        lspci_gpus,
        lspci_detail,
        smi_gpus,
        xids,
        nvrm_errors,
        nvlink,
        fallen_off,
        xid_decoded=xid_decoded,
        dmesg_highlights=dmesg_highlights,
        nvlink_status=nvlink_status,
        imex=imex,
        xid_analyzer_error=xid_analyzer_error,
        bursts=bursts,
        context_by_burst=context_by_burst,
    )
    _phase_end(filepath, "generate_report (markdown)", t)
    _analyze_stat_line(filepath, report_chars=len(report))
    if _MEM_PROFILING:
        _mem_snap("after generate_report")
        tracemalloc.stop()

    if _analyze_timing_enabled():
        print(f"[analyze-timing] {bn} | ----- end single-file analysis -----", file=sys.stderr)

    if artifact_root_dir:
        art_path = write_node_intermediate_artifacts(
            artifact_root_dir,
            filepath,
            lines,
            section_cache,
            raw_xid_lines=raw_xid_lines,
            raw_xid_source_line_numbers=raw_xid_source_line_numbers,
            xid_analyzer_stdout=xid_analyzer_stdout,
            xid_analyzer_stderr=xid_analyzer_stderr,
        )
        if art_path:
            print(f"Intermediate artifacts written under: {art_path}", file=sys.stderr)

    return {
        "filepath": filepath,
        "basename": os.path.basename(filepath),
        "sys_info": sys_info,
        "smi_gpus": smi_gpus,
        "xids": xids,
        "xid_decoded": xid_decoded,
        "report": report,
        "imex": imex,
    }
