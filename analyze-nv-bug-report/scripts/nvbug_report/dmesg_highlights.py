"""Classify merged dmesg/syslog lines into ``system_errors`` / ``other_warnings`` for §7.5."""

import re

from nvbug_report.extractors_dmesg_sections import _DMESG_MSG_NORMALIZE

_DMESG_BOOT_WHITELIST = re.compile(
    r"Booting Linux|Linux version |KASLR |efi:|esrt:|"
    r"ACPI:\s+(RSDP|XSDT|FACP|DSDT|SSDT|APIC|MCFG|SPCR|GTDT|IORT|"
    r"DBG2|PPTT|SLIT|SRAT|HEST|BERT|ERST|EINJ|PCCT|HMAT|AGDI|SDEI|"
    r"Table |Early table|Core revision|Interpreter|"
    r"\d+ \d+ \d+ \d+)",
    re.IGNORECASE,
)

_DMESG_GPU_PATTERNS = re.compile(
    r"NVRM[:\s]|Xid\b|"
    r"AER|PCIe Bus Error|link down|link reset|"
    r"fallen off|fell off|"
    r"nv_peer|gdrdrv|"
    r"3D controller.*NVIDIA",
    re.IGNORECASE,
)

_DMESG_SYS_ERROR_PATTERNS = re.compile(
    r"panic|Oops|BUG:|RIP:|Call Trace|"
    r"Out of memory|oom[_-]|OOM|Killed process|"
    r"MCE|machine check|Hardware Error|hardware error|"
    r"I/O error|ext4.*error|xfs.*error|nvme.*error|"
    r"segfault|general protection fault",
    re.IGNORECASE,
)

_DMESG_OTHER_WARN_PATTERNS = re.compile(
    r"error|fail|warn|critical|fault|timeout|"
    r"hung_task|blocked for more than|"
    r"thermal|temperature|throttl|"
    r"power|voltage|"
    r"mlx5|ib_|infiniband|rdma|roce|"
    r"drop|reset|exception|"
    r"kill|oom|memory",
    re.IGNORECASE,
)

_DMESG_OTHER_NOISE = re.compile(
    r"^(pci\s+\d|ACPI|acpi|PM:|clocksource|"
    r"smpboot|CPU\d|Brought up|numa:|NUMA:|node\s+\d|Initmem|"
    r"zone\s|DMA|Normal|Movable|pcieport|pci_bus|"
    r"audit:|SELinux|systemd|Freeing|Memory:|"
    r"random:|urandom|input:|usb |USB |hub |"
    r"Serial:|console \[|Kernel command line|"
    r"Built \d+ zonelists|Policy zone|"
    r"TCP:|UDP:|NET:|IPv[46]|bridge:|"
    r"registered taskstats|RPC:|NFS|nfs|"
    r"mlx5_core|ib_|__ib_|"
    r"power_meter|thermal_sys:|thermal zone|"
    r"nvme\s+nvme\d|SVE:|SMP:|"
    r"svcrdma:|xprtrdma:|Adding to iommu|"
    r"enabling device|firmware version|iommu:|"
    r"Rate limit:)",
)


def _condense_category(items, keep_first=3, keep_last=2, max_total=2000):
    """Group identical messages, keep first/last few per group, hard-cap output."""
    if len(items) <= keep_first + keep_last + 1:
        return items

    groups = []
    group_map = {}
    for line_num, line_text in items:
        sig = _DMESG_MSG_NORMALIZE.sub("", line_text).strip()
        if sig not in group_map:
            group_map[sig] = len(groups)
            groups.append({"items": []})
        groups[group_map[sig]]["items"].append((line_num, line_text))

    condensed = []
    for g in groups:
        entries = g["items"]
        total = len(entries)
        if total <= keep_first + keep_last + 1:
            condensed.extend(entries)
        else:
            condensed.extend(entries[:keep_first])
            omitted = total - keep_first - keep_last
            mid_line = entries[keep_first][0]
            condensed.append(
                (mid_line, f"  ... ({omitted} identical messages omitted, {total} total) ...")
            )
            condensed.extend(entries[-keep_last:])

    condensed.sort(key=lambda x: x[0])
    if len(condensed) > max_total:
        kept = condensed[:max_total]
        kept.append(
            (
                condensed[max_total][0],
                f"  ... (output truncated: showing {max_total} of {len(condensed)} condensed lines) ...",
            )
        )
        return kept
    return condensed


def filter_dmesg_highlights(dmesg_lines):
    """Pre-filter dmesg lines into system_errors and other_warnings categories.

    GPU/NVIDIA related lines (NVRM, Xid, AER, PCIe) are excluded here —
    they are already covered by sections 7.3 (Xid Raw Logs) and 7.4 (Other GPU Related).
    """
    system_errors = []
    other_warnings = []

    for line_num, raw in dmesg_lines:
        stripped = raw.strip()
        if not stripped:
            continue
        content = stripped
        if content.startswith("[") and "]" in content:
            content = content[content.index("]") + 1 :].strip()

        if _DMESG_BOOT_WHITELIST.search(content):
            continue

        if _DMESG_GPU_PATTERNS.search(stripped):
            continue

        if _DMESG_SYS_ERROR_PATTERNS.search(stripped):
            system_errors.append((line_num, stripped))
            continue

        if _DMESG_OTHER_WARN_PATTERNS.search(stripped):
            if _DMESG_OTHER_NOISE.search(content):
                continue
            other_warnings.append((line_num, stripped))

    return {
        "system_errors": _condense_category(system_errors),
        "other_warnings": _condense_category(other_warnings),
    }
