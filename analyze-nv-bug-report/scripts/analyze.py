#!/usr/bin/env python3
"""
NVIDIA Bug Report Log Analyzer
Parses nv-bug-report.log and extracts key GPU diagnostic information.

Diagnostics: export NV_BUG_REPORT_ANALYZE_TIMING=1 for stderr timings per phase
(see ``nvbug_report.pipeline`` and ``nvbug_report.report``).

With ``--output-dir``, intermediate per-section extracts are written under that directory
after analysis (see ``nvbug_report.section_artifacts`` / ``nvbug_report.pipeline``).

Batch mode (``--batch``) uses ``ProcessPoolExecutor`` so each log is analyzed in
a separate process with its own GIL. Linux uses ``fork`` (faster startup);
macOS / Windows / other platforms use the default start method (``spawn`` on
Python 3.8+) for safety and forward compatibility — Apple frameworks are not
fork-safe and CPython 3.14 deprecates ``fork`` on Darwin.
"""

import sys
import os
import concurrent.futures
import multiprocessing as mp

from nvbug_report.comparison_report import generate_comparison_report
from nvbug_report.html_renderer import write_sidecar_html
from nvbug_report.pipeline import analyze_single_file
from nvbug_report.timing import _phase_end, _phase_start


def _worker_init():
    """ProcessPoolExecutor initializer — runs once per worker at startup.

    Patches ``builtins.print`` so every line written via ``print(...)`` from
    anywhere in the worker's call tree (pipeline, timing, section_artifacts, …)
    is prefixed with ``[pid <N>] ``. Lets per-PID profiling artifacts (e.g.
    ``memray run --follow-fork`` outputs ``output.<pid>.bin``) be correlated
    back to the source log file the worker was processing.
    """
    import builtins

    pid_prefix = f"[pid {os.getpid()}] "
    original = builtins.print

    def _pid_print(*args, sep=" ", end="\n", file=None, flush=False):
        text = sep.join(str(a) for a in args)
        prefixed = pid_prefix + text.replace("\n", "\n" + pid_prefix)
        original(prefixed, end=end, file=file, flush=flush)

    builtins.print = _pid_print


def _sanitize_report_filename_component(name):
    """Make a directory or file stem safe as part of a report filename."""
    if not name:
        return ""
    name = os.path.basename(str(name))
    for ch in '\0/\\<>:"|?*':
        name = name.replace(ch, "_")
    name = name.strip(" .")
    return name


def _batch_process_one(fp_and_out):
    """Analyze one log and write its report (used by batch parallel mode).

    Returns a slim dict for the parent process: cross-node generation only needs
    sys_info / xids / xid_decoded / imex / basename / filepath — not the full
    markdown ``report`` (avoids pickling megabyte strings between processes).
    """
    fp, output_dir = fp_and_out
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"[batch] {fp}", file=sys.stderr)
    tw = _phase_start()
    result = analyze_single_file(fp, artifact_root_dir=output_dir)
    report_path = save_report(result, output_dir, prefix_parent_dir=True)
    _phase_end(fp, "batch_worker_analyze+save_report", tw)
    return {
        "filepath": result["filepath"],
        "basename": result["basename"],
        "sys_info": result["sys_info"],
        "xids": result["xids"],
        "xid_decoded": result["xid_decoded"],
        "imex": result.get("imex"),
        "report_path": report_path,
    }


def save_report(result, output_dir, prefix_parent_dir=False):
    """Save a single-file analysis report to disk.

    When ``prefix_parent_dir`` is True (single-file and ``--batch`` per-node
    reports), the parent directory name of the source log is prepended to the
    report filename, e.g. ``myhost_nvidia-bug-report-analysis-report.md``.
    """
    basename = result["basename"]
    for suffix in [".log.gz", ".log"]:
        if basename.endswith(suffix):
            stem = basename[: -len(suffix)]
            break
    else:
        stem = basename
    stem = _sanitize_report_filename_component(stem)
    report_name = stem + "-analysis-report.md"
    if prefix_parent_dir:
        fp = result.get("filepath") or ""
        if fp:
            parent = _sanitize_report_filename_component(
                os.path.basename(os.path.dirname(os.path.abspath(fp))))
            if parent:
                report_name = f"{parent}_{report_name}"
    report_path = os.path.join(output_dir, report_name)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(result["report"])
    print(f"Report saved to: {report_path}", file=sys.stderr)

    write_sidecar_html(report_path, result["report"], title=stem, kind="per_node")

    return report_path



def main():
    if len(sys.argv) < 2:
        print("Usage:", file=sys.stderr)
        print("  Single file: python3 analyze.py <file.log|.log.gz> [--output-dir DIR]", file=sys.stderr)
        print("  Batch:       python3 analyze.py --batch <file1> <file2> ... [--output-dir DIR]", file=sys.stderr)
        sys.exit(1)

    output_dir = None
    explicit_output_dir = False
    if "--output-dir" in sys.argv:
        explicit_output_dir = True
        idx = sys.argv.index("--output-dir")
        if idx + 1 < len(sys.argv):
            output_dir = sys.argv[idx + 1]
            sys.argv.pop(idx)
            sys.argv.pop(idx)

    if "--batch" in sys.argv:
        batch_idx = sys.argv.index("--batch")
        files = sys.argv[batch_idx + 1:]
        if not files:
            print("Error: --batch requires a file list", file=sys.stderr)
            sys.exit(1)

        if output_dir is None and files:
            output_dir = os.path.join(os.path.dirname(os.path.abspath(files[0])), "report")

        os.makedirs(output_dir, exist_ok=True)

        ordered_files = []
        for fp in files:
            if not os.path.exists(fp):
                print(f"Warning: file not found, skipping: {fp}", file=sys.stderr)
                continue
            ordered_files.append(fp)

        n = len(ordered_files)
        if n == 0:
            all_results = []
        else:
            logical_cpus = os.cpu_count()
            cpu_cap = logical_cpus if logical_cpus and logical_cpus >= 1 else 1
            max_workers = max(1, min(n, cpu_cap))
            cpu_label = str(logical_cpus) if logical_cpus is not None else "n/a (using 1)"
            print(
                f"Batch mode: analyzing {n} file(s) with {max_workers} worker process(es) "
                f"(os.cpu_count()={cpu_label}, capped by file count; ProcessPoolExecutor).",
                file=sys.stderr,
            )
            tasks = [(fp, output_dir) for fp in ordered_files]
            # Use processes so each file runs under its own Python interpreter (own GIL).
            # On Linux prefer fork context to reduce pickling issues with __main__ workers.
            # On macOS we deliberately do NOT use fork: Apple frameworks are not fork-safe
            # and CPython 3.14+ deprecates fork on Darwin; let ProcessPoolExecutor pick
            # the platform default (spawn on macOS/Windows, Python 3.8+).
            if sys.platform.startswith("linux"):
                try:
                    pool_ctx = mp.get_context("fork")
                except ValueError:
                    pool_ctx = None
            else:
                pool_ctx = None
            pool_kw = {"max_workers": max_workers, "initializer": _worker_init}
            if pool_ctx is not None:
                pool_kw["mp_context"] = pool_ctx
            with concurrent.futures.ProcessPoolExecutor(**pool_kw) as ex:
                all_results = list(ex.map(_batch_process_one, tasks))
            if not explicit_output_dir:
                for result in all_results:
                    rp = result.get("report_path", "")
                    print(f"Report written to: {rp}", flush=True)

        if len(all_results) > 0:
            print(f"\n{'='*60}", file=sys.stderr)
            print(f"All {len(all_results)} files analyzed, generating comparison report...", file=sys.stderr)
            cross_path = generate_comparison_report(all_results, output_dir)
            if explicit_output_dir:
                print(
                    f"Batch analysis complete: {len(all_results)} per-node report(s) in {output_dir}; "
                    f"cross-node report: {cross_path}",
                    flush=True,
                )
        elif explicit_output_dir:
            print(
                "Batch analysis complete: no valid input files; no reports generated.",
                flush=True,
            )
    else:
        filepath = sys.argv[1]
        if not os.path.exists(filepath):
            print(f"Error: file not found: {filepath}", file=sys.stderr)
            sys.exit(1)

        if output_dir is None:
            output_dir = os.path.join(os.path.dirname(os.path.abspath(filepath)), "report")

        os.makedirs(output_dir, exist_ok=True)

        result = analyze_single_file(filepath, artifact_root_dir=output_dir)
        if explicit_output_dir:
            report_path = save_report(result, output_dir, prefix_parent_dir=True)
            print(f"Analysis complete: {report_path}", flush=True)
        else:
            print(result["report"])
            save_report(result, output_dir, prefix_parent_dir=True)


if __name__ == "__main__":
    main()
