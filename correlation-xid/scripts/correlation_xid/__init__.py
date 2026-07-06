"""Time-correlate compute-tray (nv-bug-report) and NVOS/NMX-C (nvos dump) reports.

Reads the Markdown reports produced by the ``analyze-nv-bug-report`` and
``nvos-tech-dump-tools-for-nmx-c`` skills, extracts time-stamped event groups
from each (compute-tray Xid / IMEX vs switch port-state / Fabric Manager), and
correlates events that fall in the same time window — accounting for a possible
timezone offset between the two capture sources.
"""
