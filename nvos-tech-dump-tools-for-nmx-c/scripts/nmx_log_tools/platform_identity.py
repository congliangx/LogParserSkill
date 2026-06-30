"""Load switch identity (chassis, hostname, IPs) from NVOS dump layout files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class NodeIdentity:
    chassis_sn: str = ""
    slot_number: str = ""
    hostname: str = ""
    eth0_ip: str = ""
    eth1_ip: str = ""


def dump_root_from_nmx_c(nmx_c: Path) -> Path:
    """Return dump root: parent of ``log/`` (…/log/nmx/nmx-c -> dump root)."""
    return nmx_c.resolve().parent.parent.parent


def parse_chassis_location(text: str) -> dict[str, str]:
    """Parse ``dump/platform.chassis-location`` key/value table."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or set(line) <= {"-", " "}:
            continue
        if line.lower().startswith("operational"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0].rstrip(":")] = parts[-1]
    return out


def parse_ip_addr(text: str) -> dict[str, str]:
    """Parse ``dump/ip.addr`` (``ip addr`` output); return eth0/eth1 IPv4 CIDR."""
    out: dict[str, str] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        m_iface = re.match(r"^\d+:\s+(eth[01]):", line)
        if m_iface:
            current = m_iface.group(1)
            continue
        if current and current not in out:
            m_inet = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+/\d+)\b", line)
            if m_inet:
                out[current] = m_inet.group(1)
    return out


def load_node_identity(dump_root: Path) -> NodeIdentity:
    """Read chassis / hostname / IP files under ``dump_root`` if present."""
    ident = NodeIdentity()
    chassis_fp = dump_root / "dump" / "platform.chassis-location"
    if chassis_fp.is_file():
        try:
            parsed = parse_chassis_location(chassis_fp.read_text(encoding="utf-8", errors="replace"))
            ident.chassis_sn = parsed.get("chassis-sn", "")
            ident.slot_number = parsed.get("slot-number", "")
        except OSError:
            pass

    hostname_fp = dump_root / "etc" / "hostname"
    if hostname_fp.is_file():
        try:
            ident.hostname = hostname_fp.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            pass

    ip_fp = dump_root / "dump" / "ip.addr"
    if ip_fp.is_file():
        try:
            ips = parse_ip_addr(ip_fp.read_text(encoding="utf-8", errors="replace"))
            ident.eth0_ip = ips.get("eth0", "")
            ident.eth1_ip = ips.get("eth1", "")
        except OSError:
            pass

    return ident


def format_node_title(ident: NodeIdentity, *, fallback_label: str = "nmx-c") -> str:
    """Format ``<Chassis SN-Slot X>: <Host Name> - eth0 / eth1 IP``."""
    hostname = ident.hostname or fallback_label
    eth0 = ident.eth0_ip or "-"
    eth1 = ident.eth1_ip or "-"
    ip_part = f"{eth0} / {eth1}"

    if ident.chassis_sn and ident.slot_number:
        return f"{ident.chassis_sn}-Slot {ident.slot_number}: {hostname} - {ip_part}"
    if ident.chassis_sn:
        return f"{ident.chassis_sn}: {hostname} - {ip_part}"
    return f"{hostname} - {ip_part}"
