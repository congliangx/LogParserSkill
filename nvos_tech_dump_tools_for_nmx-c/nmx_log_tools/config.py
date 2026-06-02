"""Default analysis configuration."""

from dataclasses import dataclass


@dataclass
class AnalysisConfig:
    """Runtime options aligned with nvos_parser FM -vvv behavior."""

    # Fabric Manager: no file cap, no age cutoff (equivalent to -vvv)
    fm_max_log_files: int = 0  # 0 = unlimited
    fm_max_age_days: int = 0  # 0 = disabled
    fm_verbosity: int = 3  # emit all health polls

    # NVLSM port-state event grouping: gap = global idle; max = row AD→INIT pair window;
    # port_wave_gap = same-port previous episode end → next episode anchor.
    nvlsm_event_group_gap_seconds: int = 120
    nvlsm_event_group_max_seconds: int = 600
    nvlsm_port_wave_gap_seconds: int = 120

    # NVLSM forensics
    nvlsm_fnm_ports: tuple = (73, 74)

    # FM FNM port loss ↔ nvlsm osm_spst match (Switch GUID + port)
    fm_fnm_nvlsm_match_window_seconds: int = 300
    fm_fnm_init_follow_gap_seconds: int = 600

    # Report
    report_basename: str = "nmx_log_analysis"
