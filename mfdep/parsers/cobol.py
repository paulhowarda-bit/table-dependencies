"""COBOL source and copybook parser.

Handles the things that make naive grepping wrong on real mainframe source:

  * fixed-format columns - code lives in 8-72; the sequence number in 1-6 and
    the ident field in 73-80 are noise that produces phantom matches
  * comment lines - '*' or '/' in column 7
  * continuation - '-' in column 7, which is how a table name ends up split as
    ``PROD`` / ``DB.CUSTOMER`` across two lines and never gets grepped
  * EXEC SQL ... END-EXEC blocks spanning any number of lines
  * dynamic SQL, where the table name only exists inside string literals
"""

from __future__ import annotations

import re

from ..config import CERTAIN, HEURISTIC
from ..facts import FileFacts
from ..sqlscan import scan_sql
from ..util import LineMap, mask_sql_noise, snippet, squash

_SEQ_COL, _IND_COL, _CODE_START, _CODE_END = 0, 6, 7, 72

_PROGRAM_ID = re.compile(r"\bPROGRAM-ID\s*\.\s*([A-Za-z0-9$#@-]+)", re.I)
_COPY = re.compile(
    r"(?:^|\.)\s*COPY\s+(?:\"([^\"]+)\"|'([^']+)'|([A-Za-z0-9$#@-]+))"
    r"(?:\s+(?:OF|IN)\s+([A-Za-z0-9$#@-]+))?", re.I | re.M)
_CALL_LIT = re.compile(r"\bCALL\s+(?:\"([^\"]+)\"|'([^']+)')", re.I)
_CALL_DYN = re.compile(r"\bCALL\s+([A-Za-z][A-Za-z0-9-]*)(?!\s*\()", re.I)
_EXEC_SQL = re.compile(r"\bEXEC\s+SQL\b(.*?)(?:\bEND-EXEC\b|\Z)", re.I | re.S)
_SQL_INCLUDE = re.compile(r"^\s*INCLUDE\s+([A-Za-z0-9$#@-]+)\s*$", re.I)
_DYNAMIC = re.compile(r"\b(?:PREPARE|EXECUTE\s+IMMEDIATE)\b", re.I)
_LITERAL = re.compile(r"'((?:[^']|'')*)'")
_FREE_DIRECTIVE = re.compile(r">>\s*SOURCE\s+FORMAT\s+(?:IS\s+)?FREE", re.I)


def is_free_format(lines: list[str]) -> bool:
    """Detect free-format source.

    The tell is code starting in columns 1-6, which in fixed format can only
    hold a sequence number (digits or blanks).
    """
    for ln in lines[:200]:
        if _FREE_DIRECTIVE.search(ln):
            return True
    checked = coded = 0
    for ln in lines:
        if not ln.strip():
            continue
        checked += 1
        if checked > 500:
            break
        prefix = ln[:6]
        if prefix.strip() and not prefix.strip().isdigit():
            coded += 1
    return checked > 0 and (coded / checked) > 0.20


def build_blob(lines: list[str], free: bool) -> tuple[str, LineMap]:
    """Flatten source into a scannable blob, keeping a line-number map.

    Continuation lines are joined without a separator so a name broken across
    columns 72/8 is reassembled into the single token it really is.
    """
    parts: list[str] = []
    lmap = LineMap()
    offset = 0

    for idx, raw in enumerate(lines, start=1):
        if free:
            code = raw
            if code.lstrip().startswith("*>"):
                continue
            indicator = " "
        else:
            if len(raw) > _IND_COL:
                indicator = raw[_IND_COL]
            else:
                indicator = " "
            if indicator in ("*", "/", "D", "d"):        # comment / debug line
                continue
            code = raw[_CODE_START:_CODE_END] if len(raw) > _CODE_START else ""

        code = code.rstrip()
        if not code.strip():
            continue

        if indicator == "-" and parts:
            # True continuation: splice directly onto the previous text.
            joined = code.lstrip()
            lmap.add(offset, idx)
            parts.append(joined)
            offset += len(joined)
        else:
            if parts:
                parts.append("\n")
                offset += 1
            lmap.add(offset, idx)
            parts.append(code)
            offset += len(code)

    return "".join(parts), lmap


def parse(path: str, lines: list[str], kind: str) -> FileFacts:
    facts = FileFacts(path=path, kind=kind, line_count=len(lines))
    free = is_free_format(lines)
    blob, lmap = build_blob(lines, free)
    upper_blob = blob  # regexes are all case-insensitive

    for m in _PROGRAM_ID.finditer(upper_blob):
        facts.programs.append((lmap.line_at(m.start(1)), m.group(1).upper()))

    for m in _COPY.finditer(upper_blob):
        member = (m.group(1) or m.group(2) or m.group(3) or "").upper()
        if member:
            facts.copy_refs.append((lmap.line_at(m.start()), member, "COPY"))

    for m in _CALL_LIT.finditer(upper_blob):
        callee = (m.group(1) or m.group(2)).upper()
        facts.calls.append((lmap.line_at(m.start()), callee, 0))

    for m in _CALL_DYN.finditer(upper_blob):
        name = m.group(1).upper()
        if name in ("USING", "BY", "REFERENCE", "CONTENT", "VALUE"):
            continue
        facts.calls.append((lmap.line_at(m.start()), name, 1))
        facts.blind_spots.append(
            (lmap.line_at(m.start()), "DYNAMIC-CALL",
             f"CALL {name} - target resolved at run time"))

    _parse_sql_blocks(facts, blob, lmap)
    return facts


def _parse_sql_blocks(facts: FileFacts, blob: str, lmap: LineMap) -> None:
    """Pull every EXEC SQL block out of the blob and scan it."""
    has_dynamic = False

    for m in _EXEC_SQL.finditer(blob):
        body = m.group(1)
        body_start = m.start(1)

        inc = _SQL_INCLUDE.match(body.strip())
        if inc:
            facts.copy_refs.append(
                (lmap.line_at(body_start), inc.group(1).upper(), "SQL-INCLUDE"))
            continue

        if _DYNAMIC.search(body):
            has_dynamic = True
            facts.blind_spots.append(
                (lmap.line_at(body_start), "DYNAMIC-SQL",
                 squash(body)[:160]))

        masked = mask_sql_noise(body)
        refs, objects = scan_sql(masked, CERTAIN)
        for r in refs:
            line = lmap.line_at(body_start + r.offset)
            facts.add_ref(line, r.schema, r.table, r.access, r.stmt,
                          r.confidence, snippet(blob, body_start + r.offset))
        for o in objects:
            facts.objects.append((lmap.line_at(body_start + o.offset), o.schema,
                                  o.name, o.obj_type, o.parent_schema, o.parent_name))

    if has_dynamic:
        _scan_dynamic_literals(facts, blob, lmap)


def _scan_dynamic_literals(facts: FileFacts, blob: str, lmap: LineMap) -> None:
    """Recover table names from a program that builds its SQL at run time.

    Such a program assembles ``'SELECT ... FROM PRODDB.CUSTOMER WHERE '`` from
    string fragments, so the name exists only inside literals. Joining the
    literals and re-running the same grammar recovers most of them - reported
    at HEURISTIC confidence so it is never mistaken for a parsed fact.
    """
    pieces: list[str] = []
    offsets: list[int] = []
    for m in _LITERAL.finditer(blob):
        text = m.group(1).replace("''", "'")
        if not text.strip():
            continue
        offsets.append(m.start(1))
        pieces.append(text)

    if not pieces:
        return

    joined = " ".join(pieces)
    # Map joined-offset back to blob-offset.
    jmap = LineMap()
    pos = 0
    for text, off in zip(pieces, offsets):
        jmap.add(pos, lmap.line_at(off))
        pos += len(text) + 1

    refs, _ = scan_sql(mask_sql_noise(joined), HEURISTIC)
    seen = set()
    for r in refs:
        key = (r.schema, r.table, r.access)
        if key in seen:
            continue
        seen.add(key)
        facts.add_ref(jmap.line_at(r.offset), r.schema, r.table, r.access,
                      r.stmt, HEURISTIC, joined[max(0, r.offset - 30):r.offset + 60],
                      via="dynamic-sql-literal")
