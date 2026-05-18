"""Header / uname / driver metadata and dmidecode system serial."""

import re

from nvbug_report.sections import find_section_range


def _parse_summary_table_line(line):
    """Parse nv-bug-report summary table line: '<component> | <key> : <value>'."""
    if "|" not in line:
        return "", ""
    rhs = line.split("|", 1)[1].strip()
    if ":" not in rhs:
        return "", ""
    key, val = rhs.split(":", 1)
    return key.strip().lower(), val.strip().strip('" ')


def extract_system_info(lines):
    info = {}
    in_os_details = False
    for line in lines:
        stripped = line.strip()
        if stripped == "NVIDIA Driver Type Detection:":
            # Header summary ended; avoid scanning the full file.
            break
        if line.startswith("Date:"):
            info["date"] = line.split("Date:", 1)[1].strip()
        elif line.startswith("uname:"):
            parts = line.split()
            if len(parts) >= 3:
                info["hostname"] = parts[2]
                info["kernel"] = parts[3] if len(parts) > 3 else "N/A"
                info["arch"] = parts[-3] if len(parts) > 6 else "N/A"
        elif stripped.startswith("OS Details"):
            in_os_details = True

        key, value = _parse_summary_table_line(line)
        if key == "driver version":
            info["driver"] = value
        elif key == "cuda version":
            info["cuda"] = value
        elif key == "distribution":
            info["os"] = value
        elif key == "hostname" and "hostname" not in info:
            info["hostname"] = value
        elif key == "kernel" and "kernel" not in info:
            info["kernel"] = value
        elif key == "architecture" and "arch" not in info:
            info["arch"] = value
        elif key == "uptime":
            info["uptime"] = value

        if in_os_details and stripped.startswith("-" * 20):
            in_os_details = False
            # OS Details is the canonical place for uptime in this summary block.
            if "uptime" in info:
                break
    return info


def _supplement_system_info(lines, info):
    """Broaden system info extraction when summary parsing misses fields.

    Searches nvidia-smi --query --unit for Driver/CUDA Version,
    /etc/issue for OS, and dmesg NVRM loading line for driver version.
    """
    need_driver = "driver" not in info
    need_cuda = "cuda" not in info
    need_os = "os" not in info

    if not (need_driver or need_cuda or need_os):
        return

    in_etc_issue = False
    for line in lines:
        stripped = line.strip()
        if need_driver and stripped.startswith("Driver Version") and ":" in stripped:
            info["driver"] = stripped.split(":", 1)[1].strip()
            need_driver = False
        elif need_cuda and stripped.startswith("CUDA Version") and ":" in stripped:
            info["cuda"] = stripped.split(":", 1)[1].strip()
            need_cuda = False
        elif need_os and "*** /etc/issue" in stripped:
            in_etc_issue = True
            continue
        elif in_etc_issue:
            if stripped.startswith("***"):
                continue
            if stripped and not stripped.startswith("____"):
                val = stripped.replace("\\n", "").replace("\\l", "").strip()
                if val:
                    info["os"] = val
                    need_os = False
            in_etc_issue = False
        if need_driver and "NVRM: loading NVIDIA" in line:
            m = re.search(r"Kernel Module\s+([\d.]+)", line)
            if m:
                info["driver"] = m.group(1)
                need_driver = False

        if not (need_driver or need_cuda or need_os):
            break


def extract_dmidecode_serial(lines, cache=None):
    """Extract System Serial Number from the dmidecode section."""
    start, end = find_section_range(lines, "/sbin/dmidecode", cache=cache)
    if start < 0:
        return "N/A"
    in_sys_info = False
    for i in range(start, end):
        stripped = lines[i].strip()
        if stripped == "System Information":
            in_sys_info = True
            continue
        if in_sys_info:
            if stripped.startswith("Handle ") or (stripped == "" and i > start + 2):
                break
            if stripped.startswith("Serial Number:"):
                return stripped.split(":", 1)[1].strip()
    return "N/A"
