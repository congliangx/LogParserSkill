"""nvidia-smi nvlink --errorcounters / --status sections."""

import re
from collections import defaultdict

from nvbug_report.sections import find_section_range


def extract_nvlink_errors(lines, cache=None):
    """Extract NVLink error counters (only actual error fields, not traffic stats)."""
    start, end = find_section_range(lines, "nvidia-smi nvlink --errorcounters", exact=False, cache=cache)
    if start < 0:
        return []

    error_fields = {
        "Malformed packet Errors",
        "Buffer overrun Errors",
        "Rx Errors",
        "Rx remote Errors",
        "Rx General Errors",
        "Local link integrity Errors",
        "Tx discards",
        "Link recovery successful events",
        "Link recovery failed events",
        "Total link recovery events",
        "Effective Errors",
        "Symbol Errors",
        "Replay Errors",
        "Recovery Errors",
        "CRC Errors",
    }
    fec_pattern = re.compile(r"FEC Errors - (\d+):\s*(\d+)")
    ber_fields = {"Effective BER", "Symbol BER"}

    results = []
    current_gpu = None
    for i in range(start + 1, end):
        line = lines[i].strip()
        m = re.match(r"^GPU (\d+):\s*(.*)", line)
        if m:
            current_gpu = {
                "gpu_idx": int(m.group(1)),
                "name": line,
                "link_errors": defaultdict(dict),
                "fec_errors": defaultdict(lambda: defaultdict(int)),
                "ber": defaultdict(dict),
            }
            results.append(current_gpu)
            continue
        if current_gpu is None:
            continue

        link_m = re.match(r"Link (\d+):\s*(.*)", line)
        if link_m:
            link_id = int(link_m.group(1))
            rest = link_m.group(2)

            for field in error_fields:
                if rest.startswith(field + ":"):
                    val_str = rest.split(":", 1)[1].strip()
                    try:
                        val = int(val_str)
                        current_gpu["link_errors"][link_id][field] = val
                    except ValueError:
                        pass
                    break

            for bf in ber_fields:
                if rest.startswith(bf + ":"):
                    val_str = rest.split(":", 1)[1].strip()
                    current_gpu["ber"][link_id][bf] = val_str
                    break

            fec_m = fec_pattern.search(rest)
            if fec_m:
                bin_idx = int(fec_m.group(1))
                count = int(fec_m.group(2))
                current_gpu["fec_errors"][link_id][bin_idx] = count

    return results


def extract_nvlink_status(lines, cache=None):
    """Extract NVLink link status (speed per link) from nvidia-smi nvlink --status."""
    start, end = find_section_range(lines, "nvidia-smi nvlink --status", exact=False, cache=cache)
    if start < 0:
        return []

    results = []
    current_gpu = None
    for i in range(start + 1, end):
        line = lines[i].strip()
        if not line:
            continue
        m = re.match(r"^GPU (\d+):\s*(.*)", line)
        if m:
            current_gpu = {"gpu_idx": int(m.group(1)), "name": line, "links": {}}
            results.append(current_gpu)
            continue
        if current_gpu is None:
            continue
        link_m = re.match(r"Link (\d+):\s*(.*)", line)
        if link_m:
            link_id = int(link_m.group(1))
            status_str = link_m.group(2).strip()
            active = "inactive" not in status_str.lower()
            current_gpu["links"][link_id] = {"raw": status_str, "active": active}

    return results
