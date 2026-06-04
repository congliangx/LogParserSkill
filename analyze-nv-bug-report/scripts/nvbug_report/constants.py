"""Shared literals and small compiled patterns used across extractors."""

import re

C2C_GPU_KEYWORDS = ["GB200", "GB300", "VR200", "VR300"]

# Quick NVRM+Xid probe (used by NVRM error filtering and Xid context scan)
XID_PATTERN_QUICK = re.compile(r"NVRM:.*Xid", re.IGNORECASE)

# Matches the trailing "caused by previous Xid <N>" annotation that NVRM kernel
# driver puts on derivative Xid lines (e.g. Xid 45 channel cleanup triggered by
# a prior Xid 145 NVLink error). Group(1) is the primary Xid number.
DERIVATIVE_CAUSED_BY_RE = re.compile(r"caused by previous Xid\s+(\d+)", re.IGNORECASE)
