"""Orchestrate discovery, parsing, and analysis for all nmx-c logs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import AnalysisConfig
from ..discovery import (
    collect_fabric_manager_logs,
    collect_nvlsm_log_files,
)
from ..event_grouping.adaptive import group_port_events, port_events_for_event_group_report
from ..io.gzip_text import iter_lines
from ..parsers.fabric_manager import parse_fabric_manager_log
from ..parsers.nvlsm_forensics import NvlsmForensicsAnalyzer
from ..parsers.nvlsm_health import parse_nvlsm_logs
from ..platform_identity import dump_root_from_nmx_c, format_node_title, load_node_identity
from ..sources.base import DumpSource


@dataclass
class NodeAnalysis:
    """Per nmx-c root results."""

    nmx_c_path: Path
    label: str
    node_title: str = ""
    nvlsm_health: Any = None
    nvlsm_port_event_groups: List[Dict] = field(default_factory=list)
    nvlsm_forensics: Any = None
    fm_events: List[Dict[str, Any]] = field(default_factory=list)
    fm_files_parsed: int = 0
    fm_lifecycle: List[Dict] = field(default_factory=list)
    fm_category_counts: Dict[str, int] = field(default_factory=dict)
    fm_switch_info_failures: List[Dict] = field(default_factory=list)
    fm_partition_errors: List[Dict] = field(default_factory=list)
    fm_multicast_team_limits: List[Dict] = field(default_factory=list)


@dataclass
class AnalysisBundle:
    input_path: Path
    config: AnalysisConfig = field(default_factory=AnalysisConfig)
    nodes: List[NodeAnalysis] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def analyze_source(source: DumpSource, input_path: Path, cfg: AnalysisConfig) -> AnalysisBundle:
    bundle = AnalysisBundle(input_path=input_path, config=cfg)
    unlimited_fm = cfg.fm_max_log_files == 0

    for idx, nmx_c in enumerate(source.nmx_c_roots()):
        label = nmx_c.name if len(source.nmx_c_roots()) == 1 else f"{nmx_c.parent.parent.parent.name}_{idx}"
        identity = load_node_identity(dump_root_from_nmx_c(nmx_c))
        node = NodeAnalysis(
            nmx_c_path=nmx_c,
            label=label,
            node_title=format_node_title(identity, fallback_label=label),
        )

        nvlsm_logs = collect_nvlsm_log_files(nmx_c)
        if nvlsm_logs:
            node.nvlsm_health = parse_nvlsm_logs(nvlsm_logs, extract_port_events=True)
            group_port_events_input = port_events_for_event_group_report(
                node.nvlsm_health.port_events,
            )
            node.nvlsm_port_event_groups = group_port_events(
                group_port_events_input,
                gap_seconds=cfg.nvlsm_event_group_gap_seconds,
                max_group_seconds=cfg.nvlsm_event_group_max_seconds,
                lifecycle_pair_max_seconds=cfg.nvlsm_event_group_max_seconds,
                port_wave_gap_seconds=cfg.nvlsm_port_wave_gap_seconds,
            )
            forensics = NvlsmForensicsAnalyzer()
            forensics.parse_logs(nvlsm_logs)
            node.nvlsm_forensics = forensics.finalize()

        fm_logs = collect_fabric_manager_logs(nmx_c, unlimited=unlimited_fm)
        node.fm_files_parsed = len(fm_logs)
        all_events: List[Dict[str, Any]] = []
        for fp in fm_logs:
            try:
                events = parse_fabric_manager_log(
                    iter_lines(fp),
                    verbosity=cfg.fm_verbosity,
                    age_cutoff_ts=None if cfg.fm_max_age_days == 0 else None,
                )
                for ev in events:
                    ev["file"] = str(fp)
                    ev["source"] = label
                all_events.extend(events)
            except OSError as e:
                bundle.errors.append(f"FM read failed {fp}: {e}")

        node.fm_events = all_events
        # Single pass over fm_events: tally categories AND extract the
        # interesting buckets the report needs. Avoids re-walking the list.
        counts: Dict[str, int] = {}
        lifecycle: List[Dict[str, Any]] = []
        sw_info_failures: List[Dict[str, Any]] = []
        partition_errors: List[Dict[str, Any]] = []
        multicast_team_limits: List[Dict[str, Any]] = []
        for ev in all_events:
            cat = ev.get("category") or "generic"
            counts[cat] = counts.get(cat, 0) + 1
            if cat == "fm_lifecycle":
                lifecycle.append(ev)
            elif cat == "switch_info_failed" or cat == "switch_connection_lost":
                # Both flavors of "something went wrong talking to a switch"
                # are surfaced together in the dedicated raw-log section.
                sw_info_failures.append(ev)
            elif cat == "partition_error":
                partition_errors.append(ev)
            elif cat == "multicast_team_limit_reached":
                multicast_team_limits.append(ev)
        node.fm_category_counts = counts
        node.fm_lifecycle = lifecycle
        node.fm_switch_info_failures = sw_info_failures
        node.fm_partition_errors = partition_errors
        node.fm_multicast_team_limits = multicast_team_limits

        bundle.nodes.append(node)

    return bundle
