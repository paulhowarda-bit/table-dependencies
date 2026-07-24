"""SQL files: DDL scripts, Db2 stored procedures, and instream SQL decks."""

from __future__ import annotations

import bisect
import re

from ..config import CERTAIN
from ..facts import FileFacts
from ..sqlscan import QNAME, scan_sql
from ..util import mask_sql_noise, snippet, split_qualified

_CREATE_ROUTINE = re.compile(
    rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?(PROCEDURE|FUNCTION|TRIGGER)\s+({QNAME})", re.I)
# `CREATE TABLE T (...) IN DBNAME.TSNAME` - the link that lets a REORG of a
# tablespace be reported as a dependency of the table inside it.
_TABLE_IN_TS = re.compile(
    rf"\bCREATE\s+(?:GLOBAL\s+TEMPORARY\s+)?TABLE\s+({QNAME})[^;]{{0,6000}}?"
    rf"\bIN\s+({QNAME})\b", re.I | re.S)
# Cheap gate before scanning arbitrary instream decks as if they were SQL.
_SQL_SHAPE = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|DECLARE|GRANT|"
    r"LOCK\s+TABLE|TRUNCATE)\b", re.I)


def _newline_index(text: str) -> list[int]:
    return [m.start() for m in re.finditer("\n", text)]


def _line_of(nl: list[int], offset: int, base: int) -> int:
    return base + bisect.bisect_right(nl, offset)


def scan_text(facts: FileFacts, text: str, base_line: int,
              confidence: int = CERTAIN, via: str = "",
              guarded: bool = False) -> int:
    """Scan a SQL blob and append its refs to ``facts``. Returns ref count.

    ``guarded`` requires the text to actually look like SQL first. Instream DD
    data for an unknown program can be anything - a sort deck, a report parm
    list - and blindly scanning it for ``FROM x`` invents dependencies.
    """
    if not text.strip():
        return 0
    if guarded and not _SQL_SHAPE.search(text):
        return 0

    masked = mask_sql_noise(text)
    refs, objects = scan_sql(masked, confidence)
    nl = _newline_index(text)

    for r in refs:
        facts.add_ref(_line_of(nl, r.offset, base_line), r.schema, r.table,
                      r.access, r.stmt, r.confidence,
                      snippet(text, r.offset), via=via)

    for o in objects:
        facts.objects.append((_line_of(nl, o.offset, base_line), o.schema,
                              o.name, o.obj_type, o.parent_schema, o.parent_name))

    for m in _TABLE_IN_TS.finditer(masked):
        tschema, tname = split_qualified(m.group(1))
        db, ts = split_qualified(m.group(2))
        facts.objects.append((_line_of(nl, m.start(2), base_line), tschema,
                              tname, "IN-TABLESPACE", db, ts))

    return len(refs)


def parse(path: str, lines: list[str], kind: str) -> FileFacts:
    facts = FileFacts(path=path, kind=kind, line_count=len(lines))
    text = "\n".join(lines)

    masked = mask_sql_noise(text)
    nl = _newline_index(text)
    for m in _CREATE_ROUTINE.finditer(masked):
        schema, name = split_qualified(m.group(2))
        facts.objects.append((_line_of(nl, m.start(2), 1), schema, name,
                              m.group(1).upper(), "", ""))
        facts.programs.append((_line_of(nl, m.start(2), 1), name))

    scan_text(facts, text, base_line=1, confidence=CERTAIN)
    return facts
