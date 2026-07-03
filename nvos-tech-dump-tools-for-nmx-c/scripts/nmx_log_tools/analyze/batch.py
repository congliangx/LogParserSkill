"""Batch mode: expand inputs into dump units and analyze each in its own process.

Real parallelism needs processes (the parsing is CPU-bound pure-Python, so the
GIL would serialize threads). Each dump is fully independent, so this is an
embarrassingly parallel fan-out over a ``ProcessPoolExecutor``. Linux uses the
``fork`` start method (fast startup); other platforms use the default (``spawn``)
for safety. The per-dump worker lives in this module (not ``__main__``) so it is
importable under ``spawn``.
"""

from __future__ import annotations

import concurrent.futures
from concurrent.futures.process import BrokenProcessPool
import multiprocessing as mp
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List

from .run import analyze_and_write_one, dump_basename

_TARBALL_SUFFIXES = (".tar.gz", ".tgz", ".tar")


def _is_tarball(p: Path) -> bool:
    n = p.name.lower()
    return p.is_file() and any(n.endswith(s) for s in _TARBALL_SUFFIXES)


def _is_dump_dir(p: Path) -> bool:
    """Top-level dump-dir check (NON-recursive): ``p/log/nmx`` exists directly.

    Deliberately not recursive — ``discovery.dir_has_log_nmx`` rglobs, so it
    would report a *parent* of several dumps as itself a dump. Here we need to
    tell "this dir is one dump" from "this dir contains dumps to scan".
    """
    return p.is_dir() and (p / "log" / "nmx").is_dir()


def expand_inputs(paths) -> List[Path]:
    """Expand user inputs into individual dump units (tarballs / dump dirs).

    - a ``.tar.gz`` / ``.tgz`` / ``.tar`` file -> one unit
    - a directory that is itself a dump (top-level ``log/nmx``) -> one unit
    - any other directory -> scan its immediate children for tarballs / dump
      dirs; if none are found but a ``log/nmx`` exists somewhere below, treat the
      whole directory as one (deeply-nested) dump.
    """
    from ..discovery import dir_has_log_nmx

    units: List[Path] = []
    seen = set()

    def add(u: Path) -> None:
        resolved = u.resolve()
        if resolved not in seen:
            seen.add(resolved)
            units.append(u)

    for raw in paths:
        p = Path(raw)
        if not p.exists():
            print(f"Warning: input not found, skipping: {p}", file=sys.stderr)
            continue
        if _is_tarball(p):
            add(p)
        elif p.is_file():
            print(f"Warning: not a dump (directory or .tar.gz expected), skipping: {p}", file=sys.stderr)
        elif _is_dump_dir(p):
            add(p)
        elif p.is_dir():
            found = False
            for child in sorted(p.iterdir()):
                if _is_tarball(child) or _is_dump_dir(child):
                    add(child)
                    found = True
            if not found:
                if dir_has_log_nmx(p):
                    add(p)
                else:
                    print(f"Warning: no dumps (.tar.gz or log/nmx dirs) found under: {p}", file=sys.stderr)
    return units


def _resolve_names(units: List[Path]) -> List[str]:
    """Per-unit report basenames, de-duplicated by suffixing ``_2``, ``_3`` …"""
    names: List[str] = []
    used: Dict[str, int] = {}
    for u in units:
        base = dump_basename(u)
        n = used.get(base, 0)
        used[base] = n + 1
        names.append(base if n == 0 else f"{base}_{n + 1}")
    return names


def _worker(task) -> Dict[str, Any]:
    """Analyze one dump; never raise — return an error dict so one bad dump does
    not abort the whole batch."""
    input_path, output_dir, name = task
    print(f"[batch] analyzing {input_path}", file=sys.stderr)
    try:
        return analyze_and_write_one(input_path, output_dir, name)
    except SystemExit as e:  # open_source validation aborts with sys.exit(2)
        return {"input": str(input_path), "name": name, "nodes": [],
                "error": f"validation failed (exit {e.code}) — missing log/nmx/nmx-c?"}
    except Exception:  # noqa: BLE001 — isolate any parse failure to this dump
        return {"input": str(input_path), "name": name, "nodes": [],
                "error": traceback.format_exc(limit=4)}


def run_batch(inputs, output_dir, jobs: int = 0) -> List[Dict[str, Any]]:
    """Expand ``inputs`` into dumps and analyze them in parallel.

    Returns one result dict per dump (each is the slim summary from
    ``analyze_and_write_one`` or an ``error`` dict). ``jobs`` overrides the
    worker count; 0 means ``min(#dumps, os.cpu_count())``.
    """
    output_dir = Path(output_dir)
    units = expand_inputs(inputs)
    if not units:
        print("Error: no analyzable dumps found in the given input(s).", file=sys.stderr)
        return []

    names = _resolve_names(units)
    output_dir.mkdir(parents=True, exist_ok=True)
    tasks = [(str(u), str(output_dir), nm) for u, nm in zip(units, names)]

    n = len(tasks)
    cpu = os.cpu_count() or 1
    workers = min(jobs, n) if jobs and jobs > 0 else max(1, min(n, cpu))
    print(f"Batch mode: {n} dump(s), {workers} worker process(es) (os.cpu_count()={cpu}).",
          file=sys.stderr)

    if n == 1:
        # One dump: skip the pool entirely (no fork/spawn overhead).
        return [_worker(tasks[0])]

    ctx = None
    if sys.platform.startswith("linux"):
        try:
            ctx = mp.get_context("fork")
        except ValueError:
            ctx = None
    pool_kw: Dict[str, Any] = {"max_workers": workers}
    if ctx is not None:
        pool_kw["mp_context"] = ctx
    try:
        with concurrent.futures.ProcessPoolExecutor(**pool_kw) as ex:
            return list(ex.map(_worker, tasks))
    except BrokenProcessPool as e:
        print(f"Error: worker pool crashed ({e}); falling back to sequential.", file=sys.stderr)
        return [_worker(t) for t in tasks]
