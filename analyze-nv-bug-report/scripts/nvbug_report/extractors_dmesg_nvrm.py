"""BDF ↔ board serial mapping from NVRM lines in the full log."""

import re

from nvbug_report.basics import normalize_bdf


def extract_dmesg_gpu_serials(lines):
    """Extract BDF-to-serial-number mapping from dmesg NVRM lines.

    Parses pairs like:
      NVRM: GPU at PCI:0000:5c:00: GPU-xxxxx
      NVRM: GPU Board Serial Number: 1325024042738
    """
    bdf_to_sn = {}
    pending_bdf = None
    for line in lines:
        m = re.search(r"NVRM: GPU at PCI:([0-9a-fA-F:]+\.[0-9a-fA-F]):", line)
        if not m:
            m = re.search(r"NVRM: GPU at PCI:([0-9a-fA-F:]+):", line)
        if m:
            raw = m.group(1)
            if "." not in raw:
                raw += ".0"
            pending_bdf = normalize_bdf(raw)
            continue
        if pending_bdf and "GPU Board Serial Number:" in line:
            sn = line.split("GPU Board Serial Number:", 1)[1].strip()
            if pending_bdf not in bdf_to_sn:
                bdf_to_sn[pending_bdf] = sn
            pending_bdf = None
            continue
        if pending_bdf and not line.strip().startswith("NVRM:"):
            pending_bdf = None
    return bdf_to_sn
