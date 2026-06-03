"""Fabric Manager log parser (ported from nvos_parser)."""
from __future__ import annotations

from typing import Any
from typing import Dict
from typing import Iterable
from typing import List
from typing import Optional
from pathlib import Path
from collections import defaultdict, deque
from datetime import datetime
import os
import re


class _Peekable:
    """Tiny line iterator with arbitrary pushback.

    The FM parser needs one-line lookahead to detect multi-line NVL / general-info
    blocks, and the general-info path may need to *un-consume* an arbitrary
    number of lines if the field check ultimately fails (to preserve the
    original "advance by 1 and reprocess" semantics).
    """

    __slots__ = ("_src", "_buf")

    def __init__(self, source: Iterable[str]) -> None:
        self._src = iter(source)
        self._buf: "deque[str]" = deque()

    def __iter__(self) -> "_Peekable":
        return self

    def __next__(self) -> str:
        if self._buf:
            return self._buf.popleft()
        return next(self._src)

    def push(self, value: str) -> None:
        self._buf.appendleft(value)

    def push_many(self, values: Iterable[str]) -> None:
        # ``values`` are pushed back so that iterating yields them in original order.
        self._buf.extendleft(reversed(list(values)))



# Error code and subcode meanings
ERROR_CODE_MEANINGS = {
    '0x02': 'RLW - Xid 145',
    '0x06': 'NETIR - Xid 149',
}

# keys normalized to lowercase 0x.. at import time
ERROR_SUBCODE_MEANINGS = {
    '0x06': 'RLW_RXPIPE',
    '0x07': 'RLW_SRC_TRACK',
    '0x0e': 'NETIR_LINK_EVENT',
}

PORT_STATUS_MEANINGS = {
    '1': 'Down',
    '2': 'Up',
}

# Placeholder for future port-down reason code mappings
PORT_DOWN_REASON_MEANINGS = {}

ERROR_STATUS_MEANINGS = {
    '0x00000001': 'System SW Error',
    '0x00000002': 'Correctable HW Error',
    '0x00000004': 'Packet Loss on Read',
    '0x00000008': 'Packet Loss on Write',
    '0x00000010': 'NVLink Port SRAM Error',
    '0x00000020': 'NVLink Port SRAM Error',
    '0x80000000': 'System SW Error',
}

# Pre-compiled regex patterns for FM log parsing (compiled once at module load)
# This significantly improves performance for parsing large log files
_RE_ISO_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)\s+"
    r"(?P<lvl>[A-Z]+)\s+(?P<comp>[^:]+):?\s+(?P<msg>.*)$"
)
_RE_HEADER_BLOCK = re.compile(
    r"^\[(?P<mon>\w{3})\s+(?P<day>\d{1,2})\s+(?P<year>\d{4})\s+"
    r"(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})\]\s+\[(?P<lvl>[A-Z]+)\]"
)
_RE_KV_PATTERN = re.compile(r"^(?P<key>\w[\w-]*)\s*:\s*(?P<val>.*)$")

# General info response patterns - combined for efficiency
_RE_GENERAL_INFO_FIELDS = re.compile(
    r"(?:gpuGuid\s+(0x[0-9a-fA-F]+))|"
    r"(?:nodeID\s+(\d+))|"
    r"(?:chassisPhySlotNo\s+(\d+))|"
    r"(?:computeSlotIndex\s+(\d+))|"
    r"(?:computeNodeIndex\s+(\d+))|"
    r"(?:moduleId\s+(\d+))|"
    r"(?:fmMajorVer\s+(\d+))|"
    r"(?:fmMinorVer\s+(\d+))|"
    r"(?:capabilityMask\s+(0x[0-9a-fA-F]+))|"
    r"(?:chipId\s+(0x[0-9a-fA-F]+))|"
    r"(?:toplogyId\s+(0x[0-9a-fA-F]+))|"
    r"(?:computeTrayType\s+(0x[0-9a-fA-F]+))|"
    r"(?:discoveredLinkMask\s+(0x[0-9a-fA-F]+))|"
    r"(?:enabledLinkMask\s+(0x[0-9a-fA-F]+))|"
    r"(?:rackGuid\s+(\d+))",
    re.IGNORECASE
)

# Lost connection patterns
_RE_LOST_CONNECTION = re.compile(
    r"lost connection to\s+GPU\s+on\s+"
    r"nodeId\s+(?P<nodeId>\d+),\s*"
    r"systemGuid\s+(?P<systemGuid>0x[0-9a-fA-F]+),\s*"
    r"gpuGUID\s+(?P<gpuGUID>0x[0-9a-fA-F]+),\s*"
    r"port LID\s+(?P<portLID>0x[0-9a-fA-F]+),\s*"
    r"GPU state\s+(?P<gpuState>\w+)",
    re.IGNORECASE
)
# Switch-level connection loss: "Lost connection to switch on chassisId X,
# hostId Y, slotNumber Z, systemGuid 0x..., switchGUID 0x...". Distinct from
# the GPU-level pattern above because the fields are different (no nodeId,
# no GPU state) -- handled before the generic fallback so the structured
# fields land in the FM event row's columns.
_RE_LOST_CONNECTION_SWITCH = re.compile(
    r"lost connection to\s+switch\s+on\s+"
    r"chassisId\s+(?P<chassisId>\d+),\s*"
    r"hostId\s+(?P<hostId>\d+),\s*"
    r"slotNumber\s+(?P<slotNumber>\d+),\s*"
    r"systemGuid\s+(?P<systemGuid>0x[0-9a-fA-F]+),\s*"
    r"switchGUID\s+(?P<switchGuid>0x[0-9a-fA-F]+)",
    re.IGNORECASE,
)
_RE_LOST_CONNECTION_GENERIC = re.compile(r"lost connection to\s+(?P<target>.+)", re.IGNORECASE)

# Health patterns -- match both "is set to STATUS" and "is STATUS" (API variant)
_RE_GPU_HEALTH = re.compile(
    r"GPU\s+Health\s+for\s+"
    r"chassisId\s+(?P<chassisId>\d+),\s*"
    r"slotNumber\s+(?P<slotNumber>\d+),\s*"
    r"hostId\s+(?P<hostId>\d+),\s*"
    r"gpuId\s+(?P<gpuId>\d+)\s+"
    r"is\s+(?:set\s+to\s+)?(?P<healthStatus>\w+)",
    re.IGNORECASE
)
_RE_COMPUTE_NODE_HEALTH = re.compile(
    r"Compute\s+Node\s+Health\s+for\s+"
    r"chassisId\s+(?P<chassisId>\d+),\s*"
    r"slotNumber\s+(?P<slotNumber>\d+),\s*"
    r"hostId\s+(?P<hostId>\d+)\s+"
    r"is\s+(?:set\s+to\s+)?(?P<healthStatus>\w+)",
    re.IGNORECASE
)

_RE_FNM_PORT_LOSS = re.compile(
    r"node manager detected a fabric network management \(FNM\) port loss on switch "
    r"with node GUID\s+(?P<nodeGuid>0x[0-9a-fA-F]+),\s*"
    r"port GUID\s+(?P<portGuid>0x[0-9a-fA-F]+),\s*"
    r"and port num\s+(?P<portNum>\d+)",
    re.IGNORECASE
)

# "Failed to get switch info for chassisId 1 slotNumber 13 hostId 1 switchId 1"
_RE_SWITCH_INFO_FAIL = re.compile(
    r"Failed to get switch info for\s+"
    r"chassisId\s+(?P<chassisId>\d+)\s+"
    r"slotNumber\s+(?P<slotNumber>\d+)\s+"
    r"hostId\s+(?P<hostId>\d+)\s+"
    r"switchId\s+(?P<switchId>\d+)",
    re.IGNORECASE
)

# "partition Id 32766 went into an unexpected error state"
_RE_PARTITION_UNEXPECTED_ERR = re.compile(
    r"partition\s+Id\s+(?P<partitionId>\d+)\s+went into an unexpected error state",
    re.IGNORECASE
)

# "The number of multicast team limit 0 has reached in partition Id 3849 for
# Multicast Team Setup request 0x648de6654b96a." -- FM has run out of multicast
# team slots in a partition; surfaced as a dedicated section in the report
# because it indicates a hard resource limit, not a transient fault.
# FM emits this message in two flavors -- with and without the limit value:
#   "The number of multicast team limit 0 has reached in partition Id ..."
#   "The number of multicast team limit has reached in partition Id ..."
# Match both (limit is optional).
_RE_MULTICAST_TEAM_LIMIT = re.compile(
    r"The number of multicast team limit\s*(?P<limit>\d+)?\s*has reached\s+"
    r"in partition Id\s+(?P<partitionId>\d+)\s+for Multicast Team Setup request\s+"
    r"(?P<requestId>0x[0-9a-fA-F]+)",
    re.IGNORECASE
)

# Month name to number lookup
_MONTH_TO_NUM = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12
}

# Accept multiple naming conventions
FM_RELATIVE_GLOBS = [
    "log/nmx/nmx-c/fabricmanager.log*",
    "log/nmx/nmx-c/fabric-manager.log*",
    "log/nmx/nmx-c/fabric_manager.log*",
]


def _normalize_hex_key(value: str) -> str:
    """Normalize a hex key value, extracting just the hex part if formatted."""
    key = (value or '').strip()
    if not key:
        return key
    # Handle formatted values like '0x06 (NETIR (Xid 149))' - extract just the hex part
    if ' ' in key:
        key = key.split()[0]
    key_l = key.lower()
    if key_l.startswith('0x'):
        return key_l
    try:
        return f"0x{int(key_l, 0):02x}"
    except (ValueError, TypeError):
        return key


def _normalize_gpu_guid(value: str) -> str:
    """Normalize a GPU GUID to 16 hex characters with 0x prefix.
    
    GPU GUIDs are 64-bit values and should always be displayed as 16 hex chars.
    E.g., 0x3532d872d519459 -> 0x03532d872d519459
    """
    key = (value or '').strip()
    if not key:
        return key
    # Handle formatted values - extract just the hex part
    if ' ' in key:
        key = key.split()[0]
    key_l = key.lower()
    # Remove 0x prefix if present
    if key_l.startswith('0x'):
        hex_part = key_l[2:]
    else:
        hex_part = key_l
    # Zero-pad to 16 characters
    try:
        # Parse as hex and format with zero-padding
        int_val = int(hex_part, 16)
        return f"0x{int_val:016x}"
    except (ValueError, TypeError):
        return key


def _extract_message_type(message: str) -> str:
    """
    Extract message type from FM error message.
    
    Extracts text between "detected" and "error on" from messages like:
    "Fabric Manager detected GPU NVL Fatal error on :"
    "Fabric Manager detected stale GPU NVL Non Fatal error on :"
    
    Returns the message type (e.g., "GPU NVL Fatal", "stale GPU NVL Non Fatal")
    or empty string if pattern not found.
    """
    if not message:
        return ''
    
    # Pattern: "detected <message_type> error on"
    # Case-insensitive search
    msg_lower = message.lower()
    detected_idx = msg_lower.find('detected')
    if detected_idx == -1:
        return ''
    
    error_on_idx = msg_lower.find('error on', detected_idx)
    if error_on_idx == -1:
        return ''
    
    start = detected_idx + len('detected')
    end = error_on_idx
    message_type = message[start:end].strip()

    # Shorten: "GPU NVL Fatal" -> "Fatal", "stale GPU NVL Fatal" -> "Stale Fatal"
    shortened = message_type
    is_stale = shortened.lower().startswith('stale ')
    if is_stale:
        shortened = shortened[6:]
    if shortened.lower().startswith('gpu nvl '):
        shortened = shortened[8:]
    if is_stale:
        shortened = f"Stale {shortened}"

    return shortened or message_type


def _parse_fabric_manager_log(
    lines: Iterable[str],
    *,
    verbosity: int = 0,
    age_cutoff_ts: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Minimal FM log parser: extract timestamp, level, component and message.
    Also supports multi-line NVL error blocks starting with bracketed datetime.
    Additionally parses 'Lost connection to' and 'health' events for event grouping.

    Health events with status ``HEALTHY`` are dropped at parse time since they
    constitute 99%+ of all events but are never displayed.  Repeated non-HEALTHY
    polls for the same (chassis, slot, host, gpu) are deduplicated -- only
    status *changes* are emitted.

    Args:
        lines: An iterable of raw FM log lines (with or without trailing ``\\n``).
            Pass ``iter_lines(path)`` to stream a gzipped log without loading the
            entire file into memory.
        age_cutoff_ts: Optional timestamp string (YYYY-MM-DD HH:MM:SS).
            All lines with timestamps older than this are skipped.

    Performance: Uses pre-compiled module-level regex patterns and consumes lines
    via a one-line lookahead iterator -- the input is never materialized as a list.
    """
    results: List[Dict[str, Any]] = []

    # Health dedup: only emit on status changes per (chassis, slot, host, gpu/node).
    # FM polls health every few seconds; identical polls are noise.
    _health_last_status: Dict[str, str] = {}

    # Field name to group index mapping for general info combined pattern
    FIELD_GROUPS = [
        ('gpuGuid', 1), ('nodeID', 2), ('chassisPhySlotNo', 3),
        ('computeSlotIndex', 4), ('computeNodeIndex', 5), ('moduleId', 6),
        ('fmMajorVer', 7), ('fmMinorVer', 8), ('capabilityMask', 9),
        ('chipId', 10), ('toplogyId', 11), ('computeTrayType', 12),
        ('discoveredLinkMask', 13), ('enabledLinkMask', 14), ('rackGuid', 15),
    ]

    def _extract_timestamp(m_hdr) -> str:
        """Extract formatted timestamp from header match."""
        try:
            mon = _MONTH_TO_NUM.get(m_hdr.group('mon').lower(), 1)
            return f"{m_hdr.group('year')}-{mon:02d}-{int(m_hdr.group('day')):02d} {m_hdr.group('h')}:{m_hdr.group('m')}:{m_hdr.group('s')}"
        except (ValueError, AttributeError):
            return f"{m_hdr.group('year')}-{m_hdr.group('mon')}-{m_hdr.group('day')} {m_hdr.group('h')}:{m_hdr.group('m')}:{m_hdr.group('s')}"

    it = _Peekable(lines)

    for raw_line in it:
        line = raw_line.rstrip()
        if not line:
            continue

        # Skip non-log lines (metadata headers)
        if line.startswith('ChassisId:'):
            continue

        # Try ISO format first (most common for simple entries)
        m_iso = _RE_ISO_LINE.match(line)
        if m_iso:
            iso_line = line
            results.append({
                'ts': m_iso.group('ts'),
                'level': m_iso.group('lvl'),
                'component': m_iso.group('comp'),
                'message': m_iso.group('msg'),
                'raw_text': iso_line,
            })
            continue

        # Check for bracketed header format
        m_hdr = _RE_HEADER_BLOCK.match(line)

        # Pre-extract timestamp and apply age cutoff to ALL events
        _hdr_ts: Optional[str] = None
        if m_hdr:
            _hdr_ts = _extract_timestamp(m_hdr)
            if age_cutoff_ts and _hdr_ts < age_cutoff_ts:
                continue

        # Only lowercase for content checks if needed
        line_lower = line.lower()
        
        # Check for NVL error blocks
        # The header line phrasings observed in real fabricmanager.log:
        #   "Fabric Manager detected GPU NVL Fatal error on : ..."         → nvl_fatal
        #   "Fabric Manager detected GPU NVL Non Fatal error on : ..."     → nvl_non_fatal
        #   "Fabric Manager detected stale GPU NVL Fatal error on : ..."   → nvl_fatal (stale=True)
        if m_hdr and 'fabric manager detected' in line_lower and 'gpu nvl' in line_lower:
            ts = _hdr_ts
            raw_lines = [line]
            # 'non fatal' must be tested first because it is a strict superset of 'fatal'.
            if 'non fatal' in line_lower:
                nvl_category = 'nvl_non_fatal'
            elif 'fatal' in line_lower:
                nvl_category = 'nvl_fatal'
            else:
                nvl_category = 'nvl_error'  # safety net for future FM wording changes
            block_fields: Dict[str, str] = {}
            if 'stale' in line_lower:
                block_fields['stale'] = 'true'
            block = {
                'ts': ts,
                'level': m_hdr.group('lvl'),
                'component': 'FabricManager',
                'message': line,
                'fields': block_fields,
                'category': nvl_category,
            }
            for nxt_raw in it:
                nxt = nxt_raw.strip()
                if not nxt:
                    # Blank line ends the block. The outer loop would skip a
                    # blank anyway, so it's safe to consume here.
                    break
                if _RE_HEADER_BLOCK.match(nxt) or _RE_ISO_LINE.match(nxt):
                    # Boundary belongs to the next log entry -- push back so
                    # the outer loop processes it as a header/ISO line.
                    it.push(nxt_raw)
                    break
                raw_lines.append(nxt_raw.rstrip())
                mkv = _RE_KV_PATTERN.match(nxt)
                if mkv:
                    block['fields'][mkv.group('key')] = mkv.group('val')
            block['raw_text'] = '\n'.join(raw_lines)
            results.append(block)
            continue

        # Check for general info response message (multiline)
        if 'received general info response message' in line_lower:
            ts_val = _extract_timestamp(m_hdr) if m_hdr else ''
            lvl_val = m_hdr.group('lvl') if m_hdr else ''

            # Collect the full message. We buffer every line we peek past the
            # trigger so we can either (a) commit them all on a successful
            # field match, or (b) push them back wholesale on failure -- which
            # mirrors the original "advance by 1 and reprocess" behavior.
            full_message = line
            peeked_lines: List[str] = []
            for nxt_raw in it:
                nxt = nxt_raw.strip()
                if not nxt:
                    peeked_lines.append(nxt_raw)
                    break
                if _RE_HEADER_BLOCK.match(nxt) or _RE_ISO_LINE.match(nxt):
                    peeked_lines.append(nxt_raw)
                    break
                peeked_lines.append(nxt_raw)
                full_message += " " + nxt

            # Extract all fields with single finditer pass
            fields = {}
            for match in _RE_GENERAL_INFO_FIELDS.finditer(full_message):
                for field_name, group_idx in FIELD_GROUPS:
                    val = match.group(group_idx)
                    if val:
                        if field_name == 'gpuGuid':
                            fields[field_name] = _normalize_gpu_guid(val)
                        else:
                            fields[field_name] = val
                        break

            # Only create mapping if we have gpuGuid (required) and at least one other field
            if 'gpuGuid' in fields and ('nodeID' in fields or 'chassisPhySlotNo' in fields or 'computeSlotIndex' in fields):
                results.append({
                    'ts': ts_val,
                    'level': lvl_val or 'INFO',
                    'component': 'FabricManager',
                    'message': full_message[:200] + '...' if len(full_message) > 200 else full_message,
                    'fields': fields,
                    'category': 'gpu_node_mapping',
                })
                # Continuation lines are consumed; push back only the final
                # boundary line (header/ISO/blank) so the outer loop picks it up.
                if peeked_lines:
                    it.push(peeked_lines[-1])
                continue
            # Failure: push back every peeked line in original order.
            it.push_many(peeked_lines)
            continue

        # Check for "Lost connection to GPU" events
        lost_conn_match = _RE_LOST_CONNECTION.search(line)
        if lost_conn_match:
            ts_val = _hdr_ts or ''
            lvl_val = m_hdr.group('lvl') if m_hdr else ''
            fields = {
                'nodeId': lost_conn_match.group('nodeId'),
                'systemGuid': _normalize_hex_key(lost_conn_match.group('systemGuid')),
                'gpuGuid': _normalize_gpu_guid(lost_conn_match.group('gpuGUID')),
                'portLID': _normalize_hex_key(lost_conn_match.group('portLID')),
                'gpuState': lost_conn_match.group('gpuState'),
            }
            results.append({
                'ts': ts_val,
                'level': lvl_val or 'WARNING',
                'component': 'FabricManager',
                'message': line,
                'fields': fields,
                'category': 'connection_lost',
                'raw_text': line,
            })
            continue

        # "Lost connection to switch on chassisId X, hostId Y, slotNumber Z,
        # systemGuid 0x..., switchGUID 0x..." -- switch-level loss, distinct
        # from the GPU-level "Lost connection to GPU on nodeId ..." pattern.
        # Routed to its own category so the report dumps it alongside the
        # "Failed to get switch info" raw block and keeps the per-event-group
        # FM event tables (which target GPU-level lifecycle) free of it.
        if 'lost connection to switch' in line_lower:
            sw_lost_match = _RE_LOST_CONNECTION_SWITCH.search(line)
            if sw_lost_match:
                ts_val = _hdr_ts or ''
                lvl_val = m_hdr.group('lvl') if m_hdr else ''
                results.append({
                    'ts': ts_val,
                    'level': lvl_val or 'WARNING',
                    'component': 'FabricManager',
                    'message': line,
                    'fields': {
                        'chassisId': sw_lost_match.group('chassisId'),
                        'hostId': sw_lost_match.group('hostId'),
                        'slotNumber': sw_lost_match.group('slotNumber'),
                        'systemGuid': _normalize_hex_key(sw_lost_match.group('systemGuid')),
                        'switchGuid': _normalize_hex_key(sw_lost_match.group('switchGuid')),
                    },
                    'category': 'switch_connection_lost',
                    'raw_text': line,
                })
                continue

        # Check for generic "Lost connection to" (fallback)
        if 'lost connection to' in line_lower:
            lost_conn_generic = _RE_LOST_CONNECTION_GENERIC.search(line)
            if lost_conn_generic:
                ts_val = _hdr_ts or ''
                lvl_val = m_hdr.group('lvl') if m_hdr else ''
                results.append({
                    'ts': ts_val,
                    'level': lvl_val or 'WARNING',
                    'component': 'FabricManager',
                    'message': line,
                    'fields': {'target': lost_conn_generic.group('target')},
                    'category': 'connection_lost',
                    'raw_text': line,
                })
                continue

        # Check for GPU Health events
        # Default: drop HEALTHY, dedup repeated non-HEALTHY polls.
        # With -vvv: emit every health event (no filtering or dedup).
        gpu_health_match = _RE_GPU_HEALTH.search(line)
        if gpu_health_match:
            chassis = gpu_health_match.group('chassisId')
            slot = gpu_health_match.group('slotNumber')
            host = gpu_health_match.group('hostId')
            gpu_id = gpu_health_match.group('gpuId')
            health_status = gpu_health_match.group('healthStatus').upper()
            emit = False
            if verbosity >= 3:
                emit = True
            elif health_status == 'HEALTHY':
                _health_last_status.pop(f"G:{chassis}:{slot}:{host}:{gpu_id}", None)
            else:
                dedup_key = f"G:{chassis}:{slot}:{host}:{gpu_id}"
                if _health_last_status.get(dedup_key) != health_status:
                    _health_last_status[dedup_key] = health_status
                    emit = True
            if emit:
                results.append({
                    'ts': _hdr_ts or '',
                    'level': (m_hdr.group('lvl') if m_hdr else '') or 'INFO',
                    'component': 'FabricManager',
                    'message': line,
                    'fields': {
                        'chassisId': chassis,
                        'slotNumber': slot,
                        'hostId': host,
                        'gpuId': gpu_id,
                        'healthStatus': gpu_health_match.group('healthStatus'),
                        'healthType': 'GPU',
                    },
                    'category': 'health',
                    'raw_text': line,
                })
            continue

        # Check for Compute Node Health events (same dedup logic)
        compute_health_match = _RE_COMPUTE_NODE_HEALTH.search(line)
        if compute_health_match:
            chassis = compute_health_match.group('chassisId')
            slot = compute_health_match.group('slotNumber')
            host = compute_health_match.group('hostId')
            health_status = compute_health_match.group('healthStatus').upper()
            emit = False
            if verbosity >= 3:
                emit = True
            elif health_status == 'HEALTHY':
                _health_last_status.pop(f"C:{chassis}:{slot}:{host}", None)
            else:
                dedup_key = f"C:{chassis}:{slot}:{host}"
                if _health_last_status.get(dedup_key) != health_status:
                    _health_last_status[dedup_key] = health_status
                    emit = True
            if emit:
                results.append({
                    'ts': _hdr_ts or '',
                    'level': (m_hdr.group('lvl') if m_hdr else '') or 'INFO',
                    'component': 'FabricManager',
                    'message': line,
                    'fields': {
                        'chassisId': chassis,
                        'slotNumber': slot,
                        'hostId': host,
                        'healthStatus': compute_health_match.group('healthStatus'),
                        'healthType': 'ComputeNode',
                    },
                    'category': 'health',
                    'raw_text': line,
                })
            continue

        # Generic health fallback -- skip lines already handled by specific regexes above
        if 'health' in line_lower and m_hdr:
            continue

        # FNM port loss on switch (node manager)
        fnm_loss_match = _RE_FNM_PORT_LOSS.search(line)
        if fnm_loss_match and m_hdr:
            results.append({
                'ts': _hdr_ts or '',
                'level': (m_hdr.group('lvl') if m_hdr else '') or 'INFO',
                'component': 'FabricManager',
                'message': line,
                'fields': {
                    'nodeGuid': _normalize_hex_key(fnm_loss_match.group('nodeGuid')),
                    'portGuid': _normalize_hex_key(fnm_loss_match.group('portGuid')),
                    'portNum': fnm_loss_match.group('portNum'),
                },
                'category': 'fnm_port_loss',
                'raw_text': line,
            })
            continue

        # "Failed to get switch info for chassisId X slotNumber Y hostId Z switchId W"
        sw_info_fail_match = _RE_SWITCH_INFO_FAIL.search(line)
        if sw_info_fail_match and m_hdr:
            results.append({
                'ts': _hdr_ts or '',
                'level': (m_hdr.group('lvl') if m_hdr else '') or 'ERROR',
                'component': 'FabricManager',
                'message': line,
                'fields': {
                    'chassisId': sw_info_fail_match.group('chassisId'),
                    'slotNumber': sw_info_fail_match.group('slotNumber'),
                    'hostId': sw_info_fail_match.group('hostId'),
                    'switchId': sw_info_fail_match.group('switchId'),
                },
                'category': 'switch_info_failed',
                'raw_text': line,
            })
            continue

        # "partition Id N went into an unexpected error state"
        part_err_match = _RE_PARTITION_UNEXPECTED_ERR.search(line)
        if part_err_match and m_hdr:
            results.append({
                'ts': _hdr_ts or '',
                'level': (m_hdr.group('lvl') if m_hdr else '') or 'ERROR',
                'component': 'FabricManager',
                'message': line,
                'fields': {
                    'partitionId': part_err_match.group('partitionId'),
                },
                'category': 'partition_error',
                'raw_text': line,
            })
            continue

        # "The number of multicast team limit N has reached in partition Id M
        # for Multicast Team Setup request 0x...". Surfaced as raw text in its
        # own section so reviewers can see partition exhaustion at a glance.
        mt_limit_match = _RE_MULTICAST_TEAM_LIMIT.search(line)
        if mt_limit_match and m_hdr:
            results.append({
                'ts': _hdr_ts or '',
                'level': (m_hdr.group('lvl') if m_hdr else '') or 'ERROR',
                'component': 'FabricManager',
                'message': line,
                'fields': {
                    # `limit` is optional in the source line; fall back to ''
                    # so downstream code never sees None.
                    'limit': mt_limit_match.group('limit') or '',
                    'partitionId': mt_limit_match.group('partitionId'),
                    'requestId': mt_limit_match.group('requestId'),
                },
                'category': 'multicast_team_limit_reached',
                'raw_text': line,
            })
            continue

        # Check for FM lifecycle events (start/stop)
        # Start: "Fabric Manager version X is running with the following configuration options"
        # Stop: "stop run thread for", "Successfully cleaned up and exiting fabric manager"
        if m_hdr:
            ts_val = _extract_timestamp(m_hdr)
            lvl_val = m_hdr.group('lvl')
            
            # FM Start event
            if 'fabric manager version' in line_lower and 'is running' in line_lower:
                # Extract version from message
                version = ''
                version_match = re.search(r'fabric manager version\s+([\d.]+)', line, re.IGNORECASE)
                if version_match:
                    version = version_match.group(1)
                results.append({
                    'ts': ts_val,
                    'level': lvl_val or 'INFO',
                    'component': 'FabricManager',
                    'message': line,
                    'fields': {'version': version, 'event_type': 'start'},
                    'category': 'fm_lifecycle',
                })
                continue
            
            # FM Restart event - GFM restart (any reason)
            if 'restarting gfm' in line_lower:
                results.append({
                    'ts': ts_val,
                    'level': lvl_val or 'INFO',
                    'component': 'FabricManager',
                    'message': line,
                    'fields': {'event_type': 'restart'},
                    'category': 'fm_lifecycle',
                })
                continue
            
            # FM Stop events
            if 'stop run thread for' in line_lower:
                results.append({
                    'ts': ts_val,
                    'level': lvl_val or 'INFO',
                    'component': 'FabricManager',
                    'message': line,
                    'fields': {'event_type': 'stop'},
                    'category': 'fm_lifecycle',
                })
                continue
            
            # Catch any "exiting fabric manager" message (clean or non-operational exit)
            if 'exiting fabric manager' in line_lower:
                results.append({
                    'ts': ts_val,
                    'level': lvl_val or 'INFO',
                    'component': 'FabricManager',
                    'message': line,
                    'fields': {'event_type': 'stop', 'is_exit': True},
                    'category': 'fm_lifecycle',
                })
                continue

    return results


parse_fabric_manager_log = _parse_fabric_manager_log

