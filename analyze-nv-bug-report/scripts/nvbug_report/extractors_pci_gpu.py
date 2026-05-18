"""lspci and nvidia-smi --query (and /proc fallback) GPU extractors."""

import re

from nvbug_report.basics import normalize_bdf
from nvbug_report.sections import find_section_range


def extract_lspci_nn(lines, cache=None):
    """Extract GPU devices from lspci -nn section."""
    start, end = find_section_range(lines, "lspci -nn", exact=True, cache=cache)
    if start < 0:
        return []
    gpus = []
    for i in range(start + 1, end):
        line = lines[i].strip()
        if "3D controller" in line and "NVIDIA" in line:
            parts = line.split()
            if not parts:
                continue
            raw_bdf = parts[0]
            bdf = normalize_bdf(raw_bdf)
            gpus.append({"bdf": bdf, "lspci_line": line})
    return gpus


def extract_lspci_verbose(lines, gpu_bdfs, cache=None):
    """Extract detailed lspci info for each GPU from lspci -nnDvvvxxxx."""
    start, end = find_section_range(lines, "lspci -nnDvvvxxxx", exact=False, cache=cache)
    if start < 0:
        return {}

    result = {}
    for bdf in gpu_bdfs:
        info = {
            "lnk_cap": "N/A",
            "lnk_sta": "N/A",
            "dev_sta": "N/A",
            "ue_sta": "N/A",
            "ce_sta": "N/A",
            "lnk_ok": True,
            "lnk_cap_short": "N/A",
            "lnk_sta_short": "N/A",
            "lnk_cap2": "N/A",
            "lnk_sta2": "N/A",
            "retimer": "",
            "equalization": "",
            "regions": [],
            "rev_ff": False,
            "unknown_header": False,
            "lane_errors": "",
        }

        found = False
        for i in range(start, end):
            line = lines[i]
            stripped = line.strip()

            if stripped.startswith(bdf) and "3D controller" in stripped:
                found = True
                if "(rev ff)" in stripped:
                    info["rev_ff"] = True
                    info["lnk_ok"] = False
                continue

            if found:
                if not line[0].isspace() and not line.startswith("\t") and i > start + 5:
                    if re.match(r"^[0-9a-fA-F]{4}:", stripped):
                        break

                if "Unknown header type" in stripped:
                    info["unknown_header"] = True
                    info["lnk_ok"] = False

                elif stripped.startswith("LaneErrStat:"):
                    lane_val = stripped.split("LaneErrStat:", 1)[1].strip()
                    if lane_val and lane_val != "0":
                        info["lane_errors"] = lane_val

                elif stripped.startswith("LnkCap:") and info["lnk_cap"] == "N/A":
                    info["lnk_cap"] = stripped
                    m = re.search(r"Speed (\S+),\s*Width (\S+)", stripped)
                    if m:
                        width = m.group(2).rstrip(",")
                        info["lnk_cap_short"] = f"{m.group(1)} / {width}"

                elif stripped.startswith("LnkSta:") and info["lnk_sta"] == "N/A":
                    info["lnk_sta"] = stripped
                    if "downgraded" in stripped:
                        info["lnk_ok"] = False
                    m = re.search(r"Speed (\S+)\s*\([^)]*\),\s*Width (\S+)\s*\(([^)]*)\)", stripped)
                    if m:
                        info["lnk_sta_short"] = f"{m.group(1)} / {m.group(2)}"
                        if "downgraded" in stripped:
                            info["lnk_sta_short"] += " ⚠"

                elif stripped.startswith("DevSta:") and info["dev_sta"] == "N/A":
                    info["dev_sta"] = stripped.split("DevSta:", 1)[1].strip()

                elif stripped.startswith("UESta:") and info["ue_sta"] == "N/A":
                    info["ue_sta"] = stripped.split("UESta:", 1)[1].strip()

                elif stripped.startswith("CESta:") and info["ce_sta"] == "N/A":
                    info["ce_sta"] = stripped.split("CESta:", 1)[1].strip()

                elif stripped.startswith("LnkCap2:") and info["lnk_cap2"] == "N/A":
                    info["lnk_cap2"] = stripped
                    retimer_parts = []
                    if "Retimer+" in stripped:
                        retimer_parts.append("Retimer+")
                    elif "Retimer-" in stripped:
                        retimer_parts.append("Retimer-")
                    if "2Retimers+" in stripped:
                        retimer_parts.append("2Retimers+")
                    elif "2Retimers-" in stripped:
                        retimer_parts.append("2Retimers-")
                    info["retimer"] = " ".join(retimer_parts) if retimer_parts else "N/A"

                elif stripped.startswith("LnkSta2:") and info["lnk_sta2"] == "N/A":
                    info["lnk_sta2"] = stripped
                    eq_parts = []
                    for token in stripped.split():
                        if token.startswith("EqualizationComplete"):
                            eq_parts.append(token)
                    info["equalization"] = eq_parts[0] if eq_parts else "N/A"

                elif stripped.startswith("Region "):
                    m_region = re.match(
                        r"Region\s+(\d+):\s+Memory\s+at\s+([0-9a-fA-F]+)\s+"
                        r"\(([^)]*)\)\s+\[([^\]]*)\]",
                        stripped,
                    )
                    if m_region:
                        reg_num = int(m_region.group(1))
                        reg_addr = m_region.group(2)
                        reg_type = m_region.group(3)
                        reg_flags = m_region.group(4)
                        size_m = re.search(r"size=(\S+)", reg_flags)
                        reg_size = size_m.group(1) if size_m else reg_flags
                        disabled = "disabled" in reg_flags.lower()
                        virtual = "virtual" in reg_flags.lower()
                        info["regions"].append(
                            {
                                "num": reg_num,
                                "addr": reg_addr,
                                "type": reg_type,
                                "size": reg_size,
                                "disabled": disabled,
                                "virtual": virtual,
                                "raw": stripped,
                            }
                        )

        result[bdf] = info
    return result


def extract_nvidia_smi_query(lines, cache=None):
    """Extract GPU info from nvidia-smi --query section."""
    start, end = find_section_range(lines, "nvidia-smi --query", exact=False, cache=cache)
    if start < 0:
        return []

    gpus = []
    current_gpu = None
    in_ecc_volatile = False
    ecc_volatile_done = False
    in_remapped = False

    for i in range(start, end):
        line = lines[i]
        stripped = line.strip()

        m = re.match(r"^GPU\s+([0-9a-fA-F]{4,}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.\d)", stripped)
        if m:
            if current_gpu:
                gpus.append(current_gpu)
            current_gpu = {
                "bdf": normalize_bdf(m.group(1)),
                "bdf_long": m.group(1),
                "ecc_vol": {},
                "ecc_agg": {},
                "temp": {},
                "power": {},
                "remapped": {},
            }
            in_ecc_volatile = False
            ecc_volatile_done = False
            in_remapped = False
            continue

        if current_gpu is None:
            continue

        if re.match(r"\s{4}Product Name\s+:", line):
            current_gpu["name"] = line.split(":", 1)[1].strip()
        elif re.match(r"\s{4}Serial Number\s+:", line):
            current_gpu["sn"] = line.split(":", 1)[1].strip()
        elif re.match(r"\s{4}GPU UUID\s+:", line):
            current_gpu["uuid"] = line.split(":", 1)[1].strip()
        elif re.match(r"\s{4}VBIOS Version\s+:", line):
            current_gpu["vbios"] = line.split(":", 1)[1].strip()

        elif re.match(r"\s+Chassis Serial Number\s+:", line):
            current_gpu["chassis_sn"] = line.split(":", 1)[1].strip()
        elif re.match(r"\s+Slot Number\s+:", line):
            current_gpu["slot_number"] = line.split(":", 1)[1].strip()
        elif re.match(r"\s+Tray Index\s+:", line):
            current_gpu["tray_index"] = line.split(":", 1)[1].strip()

        elif stripped == "Volatile":
            in_ecc_volatile = True
            ecc_volatile_done = False
        elif stripped == "Aggregate" and in_ecc_volatile:
            in_ecc_volatile = False
            ecc_volatile_done = True
        elif in_ecc_volatile:
            if "SRAM Correctable" in stripped and ":" in stripped:
                current_gpu["ecc_vol"]["sram_ce"] = stripped.split(":", 1)[1].strip()
            elif "SRAM Uncorrectable Parity" in stripped:
                current_gpu["ecc_vol"]["sram_ue_parity"] = stripped.split(":", 1)[1].strip()
            elif "SRAM Uncorrectable SEC-DED" in stripped:
                current_gpu["ecc_vol"]["sram_ue_secded"] = stripped.split(":", 1)[1].strip()
            elif "DRAM Correctable" in stripped:
                current_gpu["ecc_vol"]["dram_ce"] = stripped.split(":", 1)[1].strip()
            elif "DRAM Uncorrectable" in stripped:
                current_gpu["ecc_vol"]["dram_ue"] = stripped.split(":", 1)[1].strip()
        elif ecc_volatile_done and not in_ecc_volatile:
            if "SRAM Correctable" in stripped and ":" in stripped and "sram_ce" not in current_gpu["ecc_agg"]:
                current_gpu["ecc_agg"]["sram_ce"] = stripped.split(":", 1)[1].strip()
            elif "SRAM Uncorrectable Parity" in stripped and "sram_ue_parity" not in current_gpu["ecc_agg"]:
                current_gpu["ecc_agg"]["sram_ue_parity"] = stripped.split(":", 1)[1].strip()
            elif "SRAM Uncorrectable SEC-DED" in stripped and "sram_ue_secded" not in current_gpu["ecc_agg"]:
                current_gpu["ecc_agg"]["sram_ue_secded"] = stripped.split(":", 1)[1].strip()
            elif "DRAM Correctable" in stripped and "dram_ce" not in current_gpu["ecc_agg"]:
                current_gpu["ecc_agg"]["dram_ce"] = stripped.split(":", 1)[1].strip()
            elif "DRAM Uncorrectable" in stripped and "dram_ue" not in current_gpu["ecc_agg"]:
                current_gpu["ecc_agg"]["dram_ue"] = stripped.split(":", 1)[1].strip()
            elif "SRAM Threshold Exceeded" in stripped:
                current_gpu["ecc_agg"]["threshold"] = stripped.split(":", 1)[1].strip()
                ecc_volatile_done = False

        if stripped.startswith("Remapped Rows"):
            in_remapped = True
        elif in_remapped:
            if re.match(r"\s+Correctable Error\s+:", line):
                current_gpu["remapped"]["ce"] = line.split(":", 1)[1].strip()
            elif re.match(r"\s+Uncorrectable Error\s+:", line):
                current_gpu["remapped"]["ue"] = line.split(":", 1)[1].strip()
            elif "Pending" in stripped and ":" in stripped and "pending" not in current_gpu["remapped"]:
                current_gpu["remapped"]["pending"] = stripped.split(":", 1)[1].strip()
            elif "Remapping Failure" in stripped:
                current_gpu["remapped"]["failure"] = stripped.split(":", 1)[1].strip()
                in_remapped = False

        if re.match(r"\s+GPU Current Temp\s+:", line):
            current_gpu["temp"]["gpu"] = line.split(":", 1)[1].strip()
        elif re.match(r"\s+Memory Current Temp\s+:", line):
            current_gpu["temp"]["mem"] = line.split(":", 1)[1].strip()

        if "GPU Power Readings" in stripped:
            current_gpu["_in_gpu_power"] = True
        elif current_gpu.get("_in_gpu_power"):
            if "Average Power Draw" in stripped and "gpu_power" not in current_gpu["power"]:
                current_gpu["power"]["gpu_power"] = stripped.split(":", 1)[1].strip()
            elif "Current Power Limit" in stripped and "power_limit" not in current_gpu["power"]:
                current_gpu["power"]["power_limit"] = stripped.split(":", 1)[1].strip()
                current_gpu["_in_gpu_power"] = False

        if "HW Slowdown" in stripped and "HW Thermal" not in stripped and "HW Power" not in stripped:
            if ":" in stripped and "hw_slowdown" not in current_gpu:
                current_gpu["hw_slowdown"] = stripped.split(":", 1)[1].strip()
        elif "HW Thermal Slowdown" in stripped and ":" in stripped:
            if "hw_thermal" not in current_gpu:
                current_gpu["hw_thermal"] = stripped.split(":", 1)[1].strip()

        if re.match(r"\s+Link Width", stripped):
            current_gpu["_in_link_width"] = True
        elif current_gpu.get("_in_link_width"):
            if "Max" in stripped and ":" in stripped and "pcie_max_w" not in current_gpu:
                current_gpu["pcie_max_w"] = stripped.split(":", 1)[1].strip()
            elif "Current" in stripped and ":" in stripped and "pcie_cur_w" not in current_gpu:
                current_gpu["pcie_cur_w"] = stripped.split(":", 1)[1].strip()
                current_gpu["_in_link_width"] = False

    if current_gpu:
        gpus.append(current_gpu)

    for g in gpus:
        g.pop("_in_gpu_power", None)
        g.pop("_in_link_width", None)

    return gpus


def extract_proc_gpu_info(lines):
    """Fallback: extract GPU info from /proc/driver/nvidia/gpus/*/information sections.

    Used when nvidia-smi --query fails (e.g. dead GPU causes entire command to error out).
    """
    gpus = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if "/proc/driver/nvidia/" in line and "/information" in line:
            m = re.search(
                r"/gpus/([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.\d)/information",
                line,
            )
            if m:
                bdf = normalize_bdf(m.group(1))
                gpu = {"bdf": bdf, "ecc_vol": {}, "ecc_agg": {}, "temp": {}, "power": {}, "remapped": {}}
                j = i + 1
                while j < n and j < i + 20:
                    gl = lines[j].strip()
                    if gl.startswith("____"):
                        break
                    if gl.startswith("Model:"):
                        gpu["name"] = gl.split(":", 1)[1].strip()
                    elif gl.startswith("GPU UUID:"):
                        gpu["uuid"] = gl.split(":", 1)[1].strip()
                    elif gl.startswith("Video BIOS:"):
                        gpu["vbios"] = gl.split(":", 1)[1].strip()
                    elif gl.startswith("Bus Type:"):
                        gpu["bus_type"] = gl.split(":", 1)[1].strip()
                    j += 1
                gpus.append(gpu)
                i = j
                continue
        i += 1
    return gpus
