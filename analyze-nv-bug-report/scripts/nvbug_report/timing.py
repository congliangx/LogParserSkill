"""Optional stderr phase timings (``NV_BUG_REPORT_ANALYZE_TIMING``)."""

import os
import sys
import time


def analyze_timing_enabled():
    """Set env NV_BUG_REPORT_ANALYZE_TIMING=1 to print per-phase timings to stderr."""
    v = os.environ.get("NV_BUG_REPORT_ANALYZE_TIMING", "").strip().lower()
    return v in ("1", "true", "yes", "y", "on")


def phase_start():
    return time.perf_counter()


def phase_end(filepath, phase_name, t0):
    if analyze_timing_enabled():
        dt = time.perf_counter() - t0
        print(
            f"[analyze-timing] {os.path.basename(filepath)} | {phase_name}: {dt:.3f}s",
            file=sys.stderr,
        )
    return time.perf_counter()


def stat_line(filepath, **parts):
    if not analyze_timing_enabled():
        return
    s = " ".join(f"{k}={v}" for k, v in parts.items())
    print(f"[analyze-timing] {os.path.basename(filepath)} | metrics | {s}", file=sys.stderr)


# Backward-compatible names for the monolith-style call sites
_analyze_timing_enabled = analyze_timing_enabled
_phase_start = phase_start
_phase_end = phase_end
_analyze_stat_line = stat_line
