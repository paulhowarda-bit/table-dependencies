"""Extract table references from SQL text by grammar context, not by name.

This is the precision core of the tool. Grepping for a table name over 100k
members produces two failure modes that make the answer useless:

  false positives - the name appears in a comment, a DD name, a literal, or as
                    a prefix of a different table (CUSTOMER vs CUSTOMER_HIST)
  false negatives - the reference is split across COBOL continuation lines, or
                    reached through a view, synonym, alias or CTE

So instead of searching for the table, we enumerate *every* table reference in
every artifact by looking at what SQL keyword introduces it (FROM, INSERT INTO,
JOIN, REFERENCES, ...). That gives exact name boundaries, and classifies the
access as read/write/DDL in the same pass - which is what makes the final
report answer "what breaks if I change this table" rather than "what mentions
this string".
"""

from __future__ import annotations

import re
from typing import Iterable, NamedTuple

from .config import CERTAIN, DDL, DECLARE, LOCK, READ, WRITE
from .util import split_qualified

# ---------------------------------------------------------------- identifiers

_ORD = r"[A-Za-z@#$][A-Za-z0-9@#$_]*"
_DELIM = r'"(?:[^"]|"")+"'
IDENT = rf"(?:{_DELIM}|{_ORD})"
# Db2 allows LOCATION.SCHEMA.TABLE; split_qualified() drops the location part.
QNAME = rf"{IDENT}(?:\s*\.\s*{IDENT}){{0,2}}"

_QNAME_RE = re.compile(QNAME)
_WORD_RE = re.compile(r"[A-Za-z@#$][A-Za-z0-9@#$_]*")

# Names that can syntactically follow a context keyword but are never tables.
# Without this, `FROM TABLE(fn(:x))` reports a table called TABLE, and
# `FOR UPDATE OF COL` reports one called OF.
_NOT_TABLES = frozenset("""
SELECT VALUES TABLE FINAL OLD NEW OLD_TABLE NEW_TABLE UNNEST LATERAL XMLTABLE
SET WHERE ORDER GROUP HAVING FETCH FOR WITH ONLY OF ON AND OR USING INTO AS
CURRENT JOIN INNER OUTER LEFT RIGHT FULL CROSS UNION EXCEPT INTERSECT
DISTINCT ALL EXISTS NOT NULL BY IS IN LIKE BETWEEN CASE WHEN THEN ELSE END
INSERT UPDATE DELETE MERGE CREATE DROP ALTER GRANT REVOKE DECLARE OPEN CLOSE
CURSOR ROWS ROW NEXT FIRST OPTIMIZE ISOLATION SKIP LOCKED DATA
""".split())

# Tokens that terminate a comma-separated table list after FROM.
_LIST_STOP = frozenset("""
WHERE GROUP ORDER HAVING SET ON JOIN INNER OUTER LEFT RIGHT FULL CROSS
UNION EXCEPT INTERSECT INTO FETCH FOR WITH OPTIMIZE VALUES USING
""".split())


class TableRef(NamedTuple):
    """One resolved reference to a table-like object."""
    offset: int          # char offset into the scanned blob
    schema: str          # '' when the source did not qualify it
    table: str
    access: str          # READ / WRITE / DDL / LOCK / DECLARE
    stmt: str            # SELECT, INSERT, CREATE TABLE, ...
    confidence: int


class ObjectDef(NamedTuple):
    """A view / synonym / alias definition, so the query can follow it."""
    offset: int
    schema: str
    name: str
    obj_type: str        # VIEW / SYNONYM / ALIAS
    parent_schema: str   # for SYNONYM/ALIAS, the object it points at
    parent_name: str


# ---------------------------------------------------------------- patterns
#
# (regex, access, statement-label). Group 1 is the table name. Ordered by
# specificity: DELETE FROM must be tried before the bare FROM sweep.

_DIRECT = [
    (re.compile(rf"\bINSERT\s+INTO\s+({QNAME})", re.I), WRITE, "INSERT"),
    (re.compile(rf"\bDELETE\s+FROM\s+({QNAME})", re.I), WRITE, "DELETE"),
    (re.compile(rf"\bMERGE\s+INTO\s+({QNAME})", re.I), WRITE, "MERGE"),
    (re.compile(rf"\bTRUNCATE\s+(?:TABLE\s+)?({QNAME})", re.I), WRITE, "TRUNCATE"),
    (re.compile(rf"\bLOCK\s+TABLE\s+({QNAME})", re.I), LOCK, "LOCK TABLE"),
    (re.compile(rf"\bCREATE\s+(?:GLOBAL\s+TEMPORARY\s+|TEMPORARY\s+)?TABLE\s+({QNAME})",
                re.I), DDL, "CREATE TABLE"),
    (re.compile(rf"\bDROP\s+TABLE\s+({QNAME})", re.I), DDL, "DROP TABLE"),
    (re.compile(rf"\bALTER\s+TABLE\s+({QNAME})", re.I), DDL, "ALTER TABLE"),
    (re.compile(rf"\bRENAME\s+(?:TABLE\s+)?({QNAME})", re.I), DDL, "RENAME TABLE"),
    (re.compile(rf"\bDROP\s+VIEW\s+({QNAME})", re.I), DDL, "DROP VIEW"),
    (re.compile(rf"\bCREATE\s+(?:UNIQUE\s+(?:WHERE\s+NOT\s+NULL\s+)?)?INDEX\s+"
                rf"{QNAME}\s+ON\s+({QNAME})", re.I), DDL, "CREATE INDEX"),
    (re.compile(rf"\bREFERENCES\s+({QNAME})", re.I), DDL, "FOREIGN KEY"),
    (re.compile(rf"\b(?:LABEL|COMMENT)\s+ON\s+TABLE\s+({QNAME})", re.I), DDL, "COMMENT"),
    (re.compile(rf"\bSET\s+INTEGRITY\s+FOR\s+({QNAME})", re.I), DDL, "SET INTEGRITY"),
    (re.compile(rf"\b(?:GRANT|REVOKE)\b[^;]{{0,400}}?\bON\s+(?:TABLE\s+)?({QNAME})\s+"
                rf"(?:TO|FROM)\b", re.I), DDL, "GRANT"),
    # DCLGEN's declaration - the canonical host-language binding for a table.
    (re.compile(rf"\bDECLARE\s+({QNAME})\s+TABLE\b", re.I), DECLARE, "DECLARE TABLE"),
]

# UPDATE needs a preceding-word guard: `FOR UPDATE OF C1` must not register.
_UPDATE_RE = re.compile(rf"\bUPDATE\s+({QNAME})", re.I)
_FROM_RE = re.compile(rf"\bFROM\s+({QNAME})", re.I)
_JOIN_RE = re.compile(rf"\bJOIN\s+({QNAME})", re.I)

# CTE names must be subtracted or every `WITH T AS (...) SELECT * FROM T`
# reports a table that does not exist. Anchored on WITH-or-comma so a
# `CREATE VIEW V AS (` is not mistaken for one.
_CTE_RE = re.compile(rf"(?:\bWITH\b|,)\s*({IDENT})\s*(?:\([^()]*\)\s*)?AS\s*\(", re.I)

_VIEW_RE = re.compile(rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+({QNAME})", re.I)
_SYNONYM_RE = re.compile(
    rf"\bCREATE\s+(SYNONYM|ALIAS|PUBLIC\s+SYNONYM)\s+({QNAME})\s+FOR\s+({QNAME})", re.I)
_TEMP_TABLE_RE = re.compile(
    rf"\bDECLARE\s+GLOBAL\s+TEMPORARY\s+TABLE\s+({QNAME})", re.I)


# ---------------------------------------------------------------- helpers

def _prev_word(text: str, pos: int) -> str:
    """Uppercased word immediately before ``pos``, or '' at the start."""
    i = pos - 1
    while i >= 0 and text[i].isspace():
        i -= 1
    end = i + 1
    while i >= 0 and (text[i].isalnum() or text[i] in "_@#$"):
        i -= 1
    return text[i + 1:end].upper()


def _is_table_name(name: str) -> bool:
    _, table = split_qualified(name)
    return bool(table) and table.upper() not in _NOT_TABLES


def _emit(name: str, offset: int, access: str, stmt: str,
          confidence: int) -> TableRef | None:
    if not _is_table_name(name):
        return None
    schema, table = split_qualified(name)
    return TableRef(offset, schema, table, access, stmt, confidence)


def _skip_ws(text: str, pos: int) -> int:
    n = len(text)
    while pos < n and text[pos].isspace():
        pos += 1
    return pos


def _walk_from_list(text: str, pos: int, confidence: int) -> Iterable[TableRef]:
    """Consume ``T1 A, T2 AS B, T3`` after a FROM, yielding each real table.

    The old-style comma join is everywhere in mainframe SQL; handling only the
    first name after FROM would silently drop every table but one.
    """
    while True:
        pos = _skip_ws(text, pos)
        m = _QNAME_RE.match(text, pos)
        if not m:
            return
        name = m.group(0)
        ref = _emit(name, m.start(), READ, "SELECT", confidence)
        if ref:
            yield ref
        pos = m.end()

        # optional correlation name: [AS] alias
        save = pos
        pos = _skip_ws(text, pos)
        w = _WORD_RE.match(text, pos)
        if w and w.group(0).upper() == "AS":
            pos = _skip_ws(text, w.end())
            w = _WORD_RE.match(text, pos)
        if w and w.group(0).upper() not in _LIST_STOP:
            pos = w.end()
        else:
            pos = save

        pos = _skip_ws(text, pos)
        if pos < len(text) and text[pos] == ",":
            pos += 1
            continue
        return


# ---------------------------------------------------------------- public API

def scan_sql(masked: str, confidence: int = CERTAIN
             ) -> tuple[list[TableRef], list[ObjectDef]]:
    """Return every table reference and object definition in a SQL blob.

    ``masked`` must already have literals and comments blanked by
    :func:`mfdep.util.mask_sql_noise` - offsets are preserved by that function,
    so every offset here maps straight back to a source line.
    """
    refs: list[TableRef] = []
    objects: list[ObjectDef] = []

    # Local names that are not real tables.
    local = {m.group(1).strip('"').upper() for m in _CTE_RE.finditer(masked)}
    local |= {split_qualified(m.group(1))[1] for m in _TEMP_TABLE_RE.finditer(masked)}

    for pattern, access, stmt in _DIRECT:
        for m in pattern.finditer(masked):
            ref = _emit(m.group(1), m.start(1), access, stmt, confidence)
            if ref and ref.table.upper() not in local:
                refs.append(ref)

    for m in _UPDATE_RE.finditer(masked):
        if _prev_word(masked, m.start()) == "FOR":     # `FOR UPDATE OF ...`
            continue
        ref = _emit(m.group(1), m.start(1), WRITE, "UPDATE", confidence)
        if ref and ref.table.upper() not in local:
            refs.append(ref)

    for m in _FROM_RE.finditer(masked):
        if _prev_word(masked, m.start()) == "DELETE":  # already counted as WRITE
            continue
        for ref in _walk_from_list(masked, m.end(0) - len(m.group(1)), confidence):
            if ref.table.upper() not in local:
                refs.append(ref)

    for m in _JOIN_RE.finditer(masked):
        ref = _emit(m.group(1), m.start(1), READ, "JOIN", confidence)
        if ref and ref.table.upper() not in local:
            refs.append(ref)

    # ---- object definitions, so queries can follow views and synonyms
    for m in _VIEW_RE.finditer(masked):
        schema, name = split_qualified(m.group(1))
        objects.append(ObjectDef(m.start(1), schema, name, "VIEW", "", ""))

    for m in _SYNONYM_RE.finditer(masked):
        schema, name = split_qualified(m.group(2))
        pschema, pname = split_qualified(m.group(3))
        kind = "ALIAS" if m.group(1).upper() == "ALIAS" else "SYNONYM"
        objects.append(ObjectDef(m.start(2), schema, name, kind, pschema, pname))

    refs.sort(key=lambda r: r.offset)
    return _dedupe(refs), objects


def _dedupe(refs: list[TableRef]) -> list[TableRef]:
    """Collapse refs that land on the same name at the same offset.

    Overlapping patterns (e.g. GRANT ... ON T and a later sweep) can hit the
    same span; keep the most specific access we assigned.
    """
    seen: dict[tuple[int, str, str], TableRef] = {}
    rank = {DDL: 5, WRITE: 4, DECLARE: 3, LOCK: 2, READ: 1}
    for r in refs:
        key = (r.offset, r.schema, r.table)
        cur = seen.get(key)
        if cur is None or rank.get(r.access, 0) > rank.get(cur.access, 0):
            seen[key] = r
    return sorted(seen.values(), key=lambda r: r.offset)
