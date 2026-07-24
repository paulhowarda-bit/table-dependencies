"""Recognise IBM and vendor-supplied modules by prefix.

Why prefixes rather than a list of names: a translated CICS program calls
DFHEI1, and a slightly different one calls DFHNCTR, and next release there is
another. A list of specific names is wrong the moment IBM ships a module that
is not on it - the new name falls through and gets reported as a missing
application program. Every DFH* module is CICS by construction, so match the
prefix and the whole family is covered forever.

The same reasoning applies to CEE* (Language Environment), DSN* (Db2), DFS*
(IMS), IGZ* (COBOL runtime) and the rest. These are IBM-reserved prefixes;
application programs do not use them.

This is applied at *query* time, not at index time, so extending the table
below - or pointing --vendor-file at a site list - takes effect immediately
without re-crawling the share.
"""

from __future__ import annotations

import os

# prefix -> (product, subsystem)
PREFIXES: dict[str, tuple[str, str]] = {
    "DFH": ("IBM CICS", "cics"),
    "DFS": ("IBM IMS", "ims"),
    "CEE": ("IBM Language Environment", "le"),
    "DSN": ("IBM Db2", "db2"),
    "IGZ": ("IBM COBOL runtime", "cobol-runtime"),
    "ILB": ("IBM OS/VS COBOL runtime", "cobol-runtime"),
    "IGY": ("IBM COBOL compiler", "compiler"),
    "IEL": ("IBM PL/I", "pli"),
    # IBMB*/IBMS* are the PL/I runtime families. A bare "IBM" prefix would
    # swallow any application program a shop chose to name IBMRPT.
    "IBMB": ("IBM PL/I runtime", "pli"),
    "IBMS": ("IBM PL/I runtime", "pli"),
    "AFH": ("IBM Fortran", "fortran"),
    "CBC": ("IBM C/C++", "c"),
    "ASMA": ("IBM HLASM", "assembler"),
    "IKJ": ("IBM TSO/E", "tso"),
    "IRX": ("IBM REXX", "rexx"),
    "ISP": ("IBM ISPF", "ispf"),
    "ISR": ("IBM ISPF", "ispf"),
    "IEB": ("IBM data-set utility", "utility"),
    "IEF": ("IBM scheduler service", "utility"),
    "IEH": ("IBM system utility", "utility"),
    "IDC": ("IBM Access Method Services", "idcams"),
    # No "ICE" prefix: DFSORT's callable modules are a short known set, and a
    # three-letter prefix would classify an application called ICEBERG as a
    # sort utility. Over-broad prefixes hide real dependencies just as surely
    # as a too-narrow name list misses vendor ones.
    "EZA": ("IBM TCP/IP", "tcpip"),
    "EZB": ("IBM TCP/IP", "tcpip"),
    "BPX": ("IBM z/OS UNIX", "uss"),
    "CSQ": ("IBM MQ", "mq"),
    "IGG": ("IBM VSAM", "vsam"),
    "IRR": ("IBM RACF", "security"),
    "ICH": ("IBM RACF", "security"),
    "IGD": ("IBM SMS", "sms"),
    "IEA": ("IBM supervisor service", "system"),
    "IGC": ("IBM supervisor service", "system"),
    "IAT": ("IBM JES", "jes"),
    "IAZ": ("IBM JES", "jes"),
    "EQA": ("IBM z/OS Debugger", "debug"),
    "IDI": ("IBM Fault Analyzer", "fault-analyzer"),
    "FMN": ("IBM File Manager", "file-manager"),
    "GXL": ("IBM XML Toolkit", "xml"),
}

# Names with no useful prefix - API entry points and standalone utilities.
# MQ verbs are listed individually rather than as an MQ* prefix, because a real
# application program can plausibly be called MQTEST or MQRPT.
EXACT: dict[str, tuple[str, str]] = {}
for _name in ("CBLTDLI", "PLITDLI", "ASMTDLI", "CEETDLI", "AIBTDLI"):
    EXACT[_name] = ("IBM IMS", "ims")
for _name in ("MQCONN", "MQCONNX", "MQDISC", "MQOPEN", "MQCLOSE", "MQPUT",
              "MQPUT1", "MQGET", "MQINQ", "MQSET", "MQCMIT", "MQBACK",
              "MQSUB", "MQBEGIN", "MQSUBRQ", "MQSTAT", "MQCB", "MQCTL"):
    EXACT[_name] = ("IBM MQ", "mq")
for _name in ("SORT", "MERGE", "ICEMAN", "ICETOOL", "ICEGENER", "ICEDFSRT",
              "DFSORT", "SORTD"):
    EXACT[_name] = ("IBM DFSORT", "sort")
for _name in ("SYNCSORT", "SYNCTOOL", "SYNCSRT"):
    EXACT[_name] = ("Syncsort", "sort")
for _name in ("SNAP", "ABEND", "PGMTRACE", "CANCEL"):
    EXACT[_name] = ("system service", "system")

# Longest-first, so ASMA is tested before AS-anything and IGY before IG.
_SORTED_PREFIXES = sorted(PREFIXES, key=len, reverse=True)


def load_overrides(path: str) -> int:
    """Merge a site list of vendor modules. Returns how many entries were added.

    One entry per line. A trailing ``*`` makes it a prefix rule::

        XPED*      Compuware Xpediter
        PANV*      Panvalet
        MYSTUB     an in-house stub with no source on the share

    Anything after the first whitespace is a free-text product label.
    """
    if not path or not os.path.exists(path):
        return 0
    added = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            name, _, label = line.partition(" ")
            label = label.strip() or "site-defined"
            name = name.strip().upper()
            if name.endswith("*"):
                PREFIXES[name[:-1]] = (label, "site")
            else:
                EXACT[name] = (label, "site")
            added += 1
    _SORTED_PREFIXES[:] = sorted(PREFIXES, key=len, reverse=True)
    return added


def classify_module(name: str) -> tuple[str, str] | None:
    """Return ``(product, subsystem)`` for a vendor module, else ``None``."""
    if not name:
        return None
    upper = name.strip().upper()
    hit = EXACT.get(upper)
    if hit:
        return hit
    for prefix in _SORTED_PREFIXES:
        if upper.startswith(prefix):
            return PREFIXES[prefix]
    return None


def is_vendor(name: str) -> bool:
    return classify_module(name) is not None
