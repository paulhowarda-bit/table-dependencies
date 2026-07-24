"""Db2 utility control cards and BIND decks.

Utilities never issue SQL, so a pure SQL scanner is blind to them - yet a LOAD
REPLACE is the single most destructive thing that happens to a table, and a
REORG is the most common reason a job is scheduled against one. These decks
appear both as standalone .CTL members and as instream SYSIN under DSNUTILB.
"""

from __future__ import annotations

import bisect
import re

from ..config import CERTAIN, UTILITY, WRITE, READ
from ..facts import FileFacts
from ..sqlscan import QNAME
from ..util import snippet, split_qualified

# (regex, access, label). Group 1 is the object name.
_TABLE_PATTERNS = [
    (re.compile(rf"\bINTO\s+TABLE\s+({QNAME})", re.I), WRITE, "LOAD INTO TABLE"),
    (re.compile(rf"\bFROM\s+TABLE\s+({QNAME})", re.I), READ, "UNLOAD FROM TABLE"),
    (re.compile(rf"\bTABLE\s*\(\s*({QNAME})\s*\)", re.I), UTILITY, "UTILITY TABLE()"),
    (re.compile(rf"\bREPAIR\b[^;]{{0,200}}?\bTABLE\s+({QNAME})", re.I), WRITE, "REPAIR"),
]

_UTILITIES = (r"REORG|COPY|RECOVER|QUIESCE|RUNSTATS|MODIFY|LOAD|UNLOAD|"
              r"CHECK\s+DATA|CHECK\s+INDEX|CHECK\s+LOB|REPAIR|REBUILD|"
              r"MERGECOPY|LISTDEF|EXCHANGE")
_TABLESPACE = re.compile(
    rf"\b({_UTILITIES})\b[^\n]{{0,120}}?\bTABLESPACE\s+({QNAME})", re.I)
_INCLUDE_TS = re.compile(rf"\bINCLUDE\s+TABLESPACE\s+({QNAME})", re.I)
# Db2 utilities embed SQL as `EXEC SQL <stmt> ENDEXEC`.
_EXEC_SQL = re.compile(r"\bEXEC\s+SQL\b(.*?)\bENDEXEC\b", re.I | re.S)

_UTIL_SHAPE = re.compile(
    rf"\b(?:{_UTILITIES}|INTO\s+TABLE|FROM\s+TABLE|TEMPLATE|OPTIONS)\b", re.I)

_WRITERS = {"LOAD", "RECOVER", "REPAIR", "REBUILD", "EXCHANGE", "MODIFY"}


def _newline_index(text: str) -> list[int]:
    return [m.start() for m in re.finditer("\n", text)]


def _line_of(nl: list[int], offset: int, base: int) -> int:
    return base + bisect.bisect_right(nl, offset)


def scan_text(facts: FileFacts, text: str, base_line: int,
              confidence: int = CERTAIN, via: str = "") -> int:
    """Scan a utility control deck, appending refs to ``facts``."""
    if not text.strip() or not _UTIL_SHAPE.search(text):
        return 0

    # Utility decks comment with '--' or a leading '*'; blank those out but
    # keep offsets stable so line numbers stay exact.
    masked = re.sub(r"(?m)^\s*(?:--|\*)[^\n]*",
                    lambda m: " " * len(m.group(0)), text)
    nl = _newline_index(text)
    count = 0

    for pattern, access, label in _TABLE_PATTERNS:
        for m in pattern.finditer(masked):
            schema, table = split_qualified(m.group(1))
            if not table:
                continue
            facts.add_ref(_line_of(nl, m.start(1), base_line), schema, table,
                          access, label, confidence,
                          snippet(text, m.start(1)), via=via or "utility")
            count += 1

    for m in _TABLESPACE.finditer(masked):
        util = re.sub(r"\s+", " ", m.group(1)).upper()
        db, ts = split_qualified(m.group(2))
        access = WRITE if util in _WRITERS else UTILITY
        facts.add_ref(_line_of(nl, m.start(2), base_line), db, ts, access,
                      f"{util} TABLESPACE", confidence,
                      snippet(text, m.start(2)), via="tablespace")
        count += 1

    for m in _INCLUDE_TS.finditer(masked):
        db, ts = split_qualified(m.group(1))
        facts.add_ref(_line_of(nl, m.start(1), base_line), db, ts, UTILITY,
                      "LISTDEF INCLUDE", confidence,
                      snippet(text, m.start(1)), via="tablespace")
        count += 1

    for m in _EXEC_SQL.finditer(masked):
        from . import sqlfile
        sub = FileFacts(path=facts.path, kind=facts.kind)
        sqlfile.scan_text(sub, m.group(1), _line_of(nl, m.start(1), base_line),
                          confidence, via=via or "utility EXEC SQL")
        facts.table_refs.extend(sub.table_refs)
        facts.objects.extend(sub.objects)
        count += len(sub.table_refs)

    return count


def parse(path: str, lines: list[str], kind: str) -> FileFacts:
    facts = FileFacts(path=path, kind=kind, line_count=len(lines))
    text = "\n".join(lines)
    scan_text(facts, text, base_line=1)

    # A standalone deck may also be plain SQL (a DDL member in a CNTL library).
    from . import sqlfile
    sqlfile.scan_text(facts, text, base_line=1, guarded=True)
    return facts
