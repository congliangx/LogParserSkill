"""Shared literals and small compiled patterns used across extractors."""

import re

C2C_GPU_KEYWORDS = ["GB200", "GB300", "VR200", "VR300"]

# Quick NVRM+Xid probe (used by NVRM error filtering and Xid context scan)
XID_PATTERN_QUICK = re.compile(r"NVRM:.*Xid", re.IGNORECASE)
