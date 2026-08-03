"""Field-level dependency lookups for the tracer_agent, layered on mfdep.

TEMPLATE - copy this to ``src/tracer_agent/field_dependencies.py`` in the CAST
project, next to ``table_dependencies.py``. Like that module it is kept in this
repo only so the call path can be tested against a local index.

mfdep's role here is **discovery only**: ``mfdep.query()`` says which files
reference the table (programs, DCLGENs, DDL scripts, utility decks), and the
index resolves copybook member names to paths. Everything at the field level -
which columns a statement touches, which host variable each lands in, what the
DCLGEN binds each column to - is parsed *here*, from the files mfdep pointed
at. mfdep itself stays a table-level tool.

What this reports, per column of the target table:

  * column definitions from CREATE TABLE DDL and DCLGEN DECLARE TABLE,
    in declared order (order matters: positional INSERTs and FETCHes use it)
  * the DCLGEN binding column <-> COBOL host field <-> PIC clause
  * every static-SQL use: SELECT list, INTO pairing, SET, INSERT column list,
    VALUES pairing, WHERE/ON predicates, GROUP/ORDER BY, cursor DECLARE and
    the FETCH ... INTO pairing, CREATE VIEW definitions, CREATE INDEX
  * LOAD/UNLOAD utility decks that hard-code the column layout
  * which host variable a column is read into / written from, and where that
    host variable is defined (the program or a copybook it COPYs)

What it deliberately does NOT do, and says so instead of guessing:

  * no data flow beyond the SQL statement - MOVEs, REDEFINES and group moves
    are not traced, so this is column *usage*, not full value lineage
  * dynamic SQL and instream JCL SQL are surfaced as notes, never parsed
  * a column that cannot be attributed (unqualified name in a multi-table
    statement with no known definition) becomes a blind spot, not a fact
"""

from __future__ import annotations

import bisect
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent

#: Same bootstrap as table_dependencies.py - the two templates are copied into
#: ``src/tracer_agent/`` independently, so each must locate mfdep on its own.
_SEARCH = [
    os.environ.get("MFDEP_HOME"),
    _HERE.parent / "network_drive",                                  # at work
    _HERE.parent,                                                    # local copy
    _HERE.parent.parent / "mainframe-tracer" / "src" / "network_drive",
]

for _dir in [Path(d) for d in _SEARCH if d]:
    if (_dir / "mfdep" / "__init__.py").is_file():
        sys.path.insert(0, str(_dir))
        if _dir.name == "network_drive":
            sys.path.insert(0, str(_dir.parent))
        # Drop a shim that got imported first; otherwise the path edit is
        # moot. But an mfdep already loaded from this very directory is left
        # alone - re-importing it would orphan every object the host already
        # holds (isinstance checks, multiprocessing pickling by identity).
        _loaded = getattr(sys.modules.get("mfdep"), "__file__", None)
        if _loaded is None or \
                Path(_loaded).resolve() != (_dir / "mfdep" / "__init__.py").resolve():
            for _name in [n for n in sys.modules
                          if n == "mfdep" or n.startswith("mfdep.")]:
                del sys.modules[_name]
        break


import mfdep  # noqa: E402 - deliberately after the path bootstrap

try:
    from network_drive import DB_DIR

    MFDEP_DB_PATH: str = str(DB_DIR / "mfdep.db")
except Exception:  # pragma: no cover - network_drive is absent off the work box
    MFDEP_DB_PATH = os.environ.get("MFDEP_DB", "mfdep.db")


# ---------------------------------------------------------------- result model

@dataclass
class ColumnDef:
    """One column of the table, from CREATE TABLE DDL or a DCLGEN declare."""
    name: str
    seq: int                 # 0-based position in the declaration
    sql_type: str
    nullable: bool
    origin: str              # CREATE TABLE / DECLARE TABLE / ALTER TABLE
    member: str
    path: str
    line: int


@dataclass
class HostBinding:
    """The DCLGEN's column <-> COBOL host field pairing."""
    column: str
    host_field: str
    pic: str
    member: str
    path: str
    line: int
    table: str = ""


@dataclass
class FieldUse:
    """One column touched by one statement in one file."""
    column: str
    table: str               # the target object the statement named (may be a view)
    access: str              # READ / WRITE
    context: str             # SELECT / INTO / SET / WHERE / ON / INSERT / FETCH /
                             # ORDER BY / GROUP BY / LOAD / UNLOAD / INDEX / ...
    stmt: str                # SELECT, UPDATE, CREATE VIEW, ...
    host_var: str            # ':CUST-ID' without the colon, '' when none
    member: str
    path: str
    line: int
    host_def_path: str = ""  # where the host variable's data item is defined
    host_def_line: int = 0
    host_def_pic: str = ""


@dataclass
class FieldResult:
    spec: str
    columns: List[ColumnDef] = field(default_factory=list)
    uses: List[FieldUse] = field(default_factory=list)
    bindings: List[HostBinding] = field(default_factory=list)
    #: member -> why (programs that COPY a DCLGEN/copybook for the table, so
    #: they depend on the whole record layout even with no SQL of their own)
    layout_dependents: Dict[str, str] = field(default_factory=dict)
    #: table -> table-level access set (program mode only) - a DELETE touches
    #: the table with no column list, and must not vanish from the boundary
    table_access: Dict[str, List[str]] = field(default_factory=dict)
    blind_spots: List[Tuple[str, int, str, str]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    #: the underlying table-level mfdep Result, for callers that need both
    table_result: Any = None


# ---------------------------------------------------------------- SQL lexing
#
# Deliberately self-contained: this template depends on mfdep's *interface*
# (query / open_index), never on its parser internals, so an mfdep refactor
# cannot silently break the copy of this file living in the tracer.

_IDENT = r"[A-Za-z@#$][A-Za-z0-9@#$_]*"
_QNAME = rf"{_IDENT}(?:\s*\.\s*{_IDENT}){{0,2}}"
_CERTAIN = 100               # mfdep's CERTAIN confidence tier

_HOSTVAR_RE = re.compile(r":\s*([A-Za-z][A-Za-z0-9-]*)")
_BARE_COL_RE = re.compile(rf"^(?:({_IDENT})\s*\.\s*)?({_IDENT})$")
_EXEC_SQL_RE = re.compile(r"\bEXEC\s+SQL\b(.*?)(?:\bEND-EXEC\b|\Z)", re.I | re.S)
_FREE_DIRECTIVE_RE = re.compile(r">>\s*SOURCE\s+FORMAT\s+(?:IS\s+)?FREE", re.I)

# Words that can sit where a column name could, but never are one.
_NOT_COLS = frozenset("""
ALL AND ANY AS ASC AVG BETWEEN BY CASE CAST CHAR CHARACTER CHECK COALESCE
CONCAT CONSTRAINT COUNT CROSS CURRENT CURRENT_DATE CURRENT_TIME
CURRENT_TIMESTAMP DATE DAY DAYS DECIMAL DEFAULT DELETE DESC DIGITS DISTINCT
DOUBLE ELSE END ESCAPE EXISTS FETCH FIRST FLOAT FOR FOREIGN FROM GRAPHIC GROUP
HAVING IN INDICATOR INNER INSERT INTEGER INTO IS JOIN KEY LEFT LENGTH LIKE
LOWER LTRIM MATCHED MAX MERGE MIN MONTH NOT NULL NULLIF ON ONLY OPTIMIZE OR
ORDER OUTER PRIMARY REAL REFERENCES RIGHT ROW ROWS RTRIM SELECT SESSION_USER
SET SMALLINT SOME SQLCODE SQLSTATE SUBSTR SUM THEN TIME TIMESTAMP TRIM UNION
UNIQUE UPDATE UPPER UR USER USING VALUE VALUES VARCHAR WHEN WHERE WITH YEAR
""".split())

# Clause keywords that structure a SELECT at parenthesis depth 0.
_SEL_KW_RE = re.compile(
    r"\b(SELECT|INTO|FROM|WHERE|GROUP|ORDER|HAVING|FETCH|FOR|OPTIMIZE|WITH|UNION)\b",
    re.I)
_WHERE_RE = re.compile(r"\bWHERE\b", re.I)
_DML_RE = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE)\b", re.I)

_OP = r"(?:<=|>=|<>|!=|=|<|>|\bIN\b|\bLIKE\b|\bBETWEEN\b|\bIS\b)"
_PRED_RE = re.compile(
    rf"(?<![:@#$\w.])(?:({_IDENT})\s*\.\s*)?({_IDENT})\s*({_OP})", re.I)

_TABLE_DEF_RE = re.compile(
    rf"\b(?:CREATE\s+(?:GLOBAL\s+TEMPORARY\s+|TEMPORARY\s+)?TABLE\s+({_QNAME})"
    rf"|DECLARE\s+({_QNAME})\s+TABLE)\s*\(", re.I)
_ALTER_ADD_RE = re.compile(
    rf"\bALTER\s+TABLE\s+({_QNAME})\s+ADD\s+(?:COLUMN\s+)?({_IDENT})\s+([^,;]*)",
    re.I)
_INDEX_RE = re.compile(
    rf"\bCREATE\s+(?:UNIQUE\s+(?:WHERE\s+NOT\s+NULL\s+)?)?INDEX\s+{_QNAME}\s+"
    rf"ON\s+({_QNAME})\s*\(([^)]*)\)", re.I)
_VIEW_RE = re.compile(rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+{_QNAME}\b", re.I)

_LOAD_INTO_RE = re.compile(rf"\bINTO\s+TABLE\s+({_QNAME})", re.I)
_UNLOAD_FROM_RE = re.compile(rf"\bFROM\s+TABLE\s+({_QNAME})", re.I)

_NOT_A_COLDEF = frozenset(
    ("PRIMARY", "FOREIGN", "CONSTRAINT", "UNIQUE", "CHECK", "KEY", "PERIOD",
     "LIKE", "INCLUDE"))

_DML_STMTS = frozenset(("SELECT", "INSERT", "UPDATE", "DELETE", "MERGE"))


def _split_spec(spec: str) -> Tuple[str, str]:
    parts = [p.strip() for p in spec.replace('"', "").split(".") if p.strip()]
    table = parts[-1].upper() if parts else ""
    schema = parts[-2].upper() if len(parts) > 1 else ""
    return schema, table


def _last_ident(qname: str) -> str:
    return qname.replace('"', "").split(".")[-1].strip().upper()


def _mask_sql(text: str) -> str:
    """Blank string literals and comments, preserving offsets and newlines.

    Without this, a comment saying ``-- drop CUST_NAME`` or a literal holding
    SQL text would generate phantom column uses.
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "'":
            j = i + 1
            while j < n:
                if text[j] == "'" and j + 1 < n and text[j + 1] == "'":
                    j += 2
                    continue
                if text[j] == "'":
                    break
                j += 1
            for k in range(i + 1, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j + 1
        elif c == "-" and text[i:i + 2] == "--":
            j = text.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c == "/" and text[i:i + 2] == "/*":
            j = text.find("*/", i)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)


def _depths(text: str) -> List[int]:
    """Parenthesis depth at each position (depth of the char itself)."""
    d = [0] * len(text)
    cur = 0
    for i, ch in enumerate(text):
        d[i] = cur
        if ch == "(":
            cur += 1
        elif ch == ")":
            cur = max(0, cur - 1)
    return d


def _match_paren(text: str, open_pos: int) -> int:
    """Index of the ')' closing the '(' at ``open_pos``, or len(text)."""
    depth = 0
    for i in range(open_pos, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return len(text)


def _split_depth0(text: str, sep: str = ",") -> List[Tuple[str, int]]:
    items: List[Tuple[str, int]] = []
    depth, start = 0, 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == sep and depth == 0:
            items.append((text[start:i], start))
            start = i + 1
    items.append((text[start:], start))
    return items


def _top_spans(text: str, regex: re.Pattern) -> List[Tuple[str, int, int]]:
    d = _depths(text)
    return [(m.group(1).upper(), m.start(), m.end())
            for m in regex.finditer(text) if d[m.start()] == 0]


def _find_top(text: str, regex: re.Pattern, pos: int = 0) -> Optional[re.Match]:
    d = _depths(text)
    for m in regex.finditer(text, pos):
        if d[m.start()] == 0:
            return m
    return None


def _blank_subselects(text: str) -> str:
    """Blank parenthesised subqueries so predicate scans stay in this scope."""
    out = list(text)
    i = 0
    while i < len(text):
        if text[i] == "(":
            j = i + 1
            while j < len(text) and text[j].isspace():
                j += 1
            if text[j:j + 6].upper() == "SELECT":
                close = _match_paren(text, i)
                for k in range(i + 1, close):
                    if out[k] != "\n":
                        out[k] = " "
                i = close
        i += 1
    return "".join(out)


def _first_hostvar(text: str) -> str:
    m = _HOSTVAR_RE.search(text)
    return m.group(1).upper() if m else ""


def _line_starts(text: str) -> List[int]:
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


# ---------------------------------------------------------------- COBOL text
#
# Fixed-format handling is re-implemented here (compactly) rather than imported
# from mfdep.parsers - the interface boundary again. The rules are the ones
# that make naive scans wrong: code in 8-72 only, '*' in column 7 is a
# comment, '-' in column 7 splices onto the previous line.

class _LineMap:
    def __init__(self) -> None:
        self._offs: List[int] = []
        self._lines: List[int] = []

    def add(self, off: int, line: int) -> None:
        self._offs.append(off)
        self._lines.append(line)

    def line_at(self, off: int) -> int:
        i = bisect.bisect_right(self._offs, off) - 1
        return self._lines[i] if i >= 0 else 1


def _is_free_format(lines: List[str]) -> bool:
    for ln in lines[:200]:
        if _FREE_DIRECTIVE_RE.search(ln):
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


def _cobol_blob(lines: List[str]) -> Tuple[str, _LineMap]:
    free = _is_free_format(lines)
    parts: List[str] = []
    lmap = _LineMap()
    off = 0
    for idx, raw in enumerate(lines, start=1):
        if free:
            if raw.lstrip().startswith("*>"):
                continue
            indicator, code = " ", raw
        else:
            indicator = raw[6] if len(raw) > 6 else " "
            if indicator in ("*", "/", "D", "d"):
                continue
            code = raw[7:72] if len(raw) > 7 else ""
        code = code.rstrip()
        if not code.strip():
            continue
        if indicator == "-" and parts:
            code = code.lstrip()
            lmap.add(off, idx)
            parts.append(code)
            off += len(code)
        else:
            if parts:
                parts.append("\n")
                off += 1
            lmap.add(off, idx)
            parts.append(code)
            off += len(code)
    return "".join(parts), lmap


# A data item: `10  CUST-ID  PIC X(10).` - level, name, and the clause tail.
_DATA_ITEM_RE = re.compile(
    r"(?:^|\n|\.\s)\s*(\d{1,2})\s+([A-Za-z][A-Za-z0-9-]*)((?:[^.\n]|\.(?=\d))*)",
    re.S)
_PIC_RE = re.compile(r"\bPIC(?:TURE)?(?:\s+IS)?\s+([^\s.]+(?:\.\d+)?)", re.I)
_USAGE_RE = re.compile(
    r"\b(COMP(?:-[1-5])?|COMPUTATIONAL(?:-[1-5])?|BINARY|PACKED-DECIMAL|DISPLAY)\b",
    re.I)


def _data_items(blob: str, lmap: _LineMap) -> List[Tuple[int, str, str, int]]:
    """``(level, NAME, pic, line)`` for every data item, in source order.

    Only the region before PROCEDURE DIVISION is scanned, so a numeric literal
    at the start of a procedure statement can never masquerade as a level.
    """
    stop = len(blob)
    m = re.search(r"\bPROCEDURE\s+DIVISION\b", blob, re.I)
    if m:
        stop = m.start()
    out: List[Tuple[int, str, str, int]] = []
    for m in _DATA_ITEM_RE.finditer(blob, 0, stop):
        try:
            level = int(m.group(1))
        except ValueError:
            continue
        if level in (66, 88) or not (1 <= level <= 77):
            continue
        name = m.group(2).upper()
        if name == "FILLER":
            continue
        tail = m.group(3) or ""
        pm = _PIC_RE.search(tail)
        pic = pm.group(1).upper() if pm else ""
        um = _USAGE_RE.search(tail)
        if um and pic:
            pic = f"{pic} {um.group(1).upper()}"
        elif um:
            pic = um.group(1).upper()
        out.append((level, name, pic, lmap.line_at(m.start(2))))
    return out


# ---------------------------------------------------------------- definitions

def _parse_table_columns(masked: str, member: str, path: str,
                         line_at: Callable[[int], int]
                         ) -> Dict[str, List[ColumnDef]]:
    """Columns of every CREATE TABLE / DECLARE TABLE in a masked blob."""
    out: Dict[str, List[ColumnDef]] = {}
    for m in _TABLE_DEF_RE.finditer(masked):
        table = _last_ident(m.group(1) or m.group(2))
        origin = "CREATE TABLE" if m.group(1) else "DECLARE TABLE"
        open_pos = m.end() - 1
        close = _match_paren(masked, open_pos)
        body = masked[m.end():close]
        cols: List[ColumnDef] = []
        for item, ioff in _split_depth0(body):
            words = re.findall(rf"{_IDENT}", item)
            if not words or words[0].upper() in _NOT_A_COLDEF:
                continue
            name = words[0].upper()
            head = re.search(rf"{_IDENT}", item)
            type_ = " ".join(item[head.end():].split()) if head else ""
            cols.append(ColumnDef(
                name=name, seq=len(cols), sql_type=type_,
                nullable="NOT NULL" not in item.upper(), origin=origin,
                member=member, path=path,
                line=line_at(m.end() + ioff)))
        if cols:
            existing = out.get(table)
            # A CREATE TABLE in the same file beats a DECLARE for the same name.
            if not existing or (origin == "CREATE TABLE"
                                and existing[0].origin != "CREATE TABLE"):
                out[table] = cols
    for m in _ALTER_ADD_RE.finditer(masked):
        table = _last_ident(m.group(1))
        cols = out.setdefault(table, [])
        cols.append(ColumnDef(
            name=m.group(2).upper(), seq=len(cols),
            sql_type=" ".join(m.group(3).split()), nullable=True,
            origin="ALTER TABLE", member=member, path=path,
            line=line_at(m.start(2))))
    return out


def _dclgen_bindings(blob: str, lmap: _LineMap, columns: List[ColumnDef],
                     table: str, member: str, path: str, res: FieldResult
                     ) -> List[HostBinding]:
    """Pair the DCLGEN's host structure with the declared columns.

    DCLGEN emits host fields in column order with names that are the column
    name with underscores turned to hyphens - so match by normalised name
    first and fall back to position, and report a residue rather than pairing
    nonsense.
    """
    items = _data_items(blob, lmap)
    field_levels = [lv for lv, _n, _p, _l in items if 1 < lv < 49]
    if not field_levels or not columns:
        return []
    top = min(field_levels)
    fields = [(name, pic, line, i) for i, (lv, name, pic, line)
              in enumerate(items) if lv == top]

    def child_pic(item_index: int) -> str:
        # A VARCHAR column becomes a group with 49-level -LEN/-TEXT children;
        # the -TEXT child carries the PIC a caller actually cares about.
        best = ""
        for lv, name, pic, _line in items[item_index + 1:]:
            if lv <= top:
                break
            if pic and (name.endswith("-TEXT") or not best):
                best = pic
                if name.endswith("-TEXT"):
                    break
        return best

    by_norm = {name.replace("-", "_"): (name, pic, line, idx)
               for name, pic, line, idx in fields}
    bindings: List[HostBinding] = []
    for i, col in enumerate(columns):
        hit = by_norm.get(col.name)
        if hit is None and i < len(fields):
            hit = fields[i]
        if hit is None:
            res.blind_spots.append((
                path, col.line, "UNBOUND-COLUMN",
                f"{col.name} has no host field in the DCLGEN structure"))
            continue
        name, pic, line, idx = hit
        bindings.append(HostBinding(
            column=col.name, host_field=name, pic=pic or child_pic(idx),
            member=member, path=path, line=line, table=table))
    return bindings


# ---------------------------------------------------------------- statements

@dataclass
class _Ctx:
    target: str                    # the target object this statement names
    aliases: Dict[str, str]        # correlation name -> table
    multi: bool                    # more than one table in scope


def _from_tables(region: str) -> Tuple[List[str], Dict[str, str]]:
    """Tables and correlation names out of a FROM clause."""
    tables: List[str] = []
    aliases: Dict[str, str] = {}
    parts = re.split(r"\b(?:INNER|LEFT|RIGHT|FULL|CROSS)?\s*(?:OUTER\s+)?JOIN\b",
                     region, flags=re.I)
    for part in parts:
        part = re.split(r"\bON\b", part, flags=re.I)[0]
        for item, _off in _split_depth0(part):
            item = item.strip()
            if not item:
                continue
            if item.startswith("("):
                close = _match_paren(item, 0)
                m = re.match(rf"\s*(?:AS\s+)?({_IDENT})", item[close + 1:], re.I)
                if m:
                    aliases[m.group(1).upper()] = "(subquery)"
                continue
            m = re.match(rf"({_QNAME})(?:\s+(?:AS\s+)?({_IDENT}))?\s*$", item, re.I)
            if not m:
                continue
            tbl = _last_ident(m.group(1))
            if tbl in _NOT_COLS:
                continue
            tables.append(tbl)
            alias = m.group(2)
            if alias and alias.upper() not in _NOT_COLS:
                aliases[alias.upper()] = tbl
    return tables, aliases


class _FileScanner:
    """Scans one file's SQL statements for column uses of the target table."""

    def __init__(self, member: str, path: str, target_names: frozenset,
                 known_map: Dict[str, List[ColumnDef]], res: FieldResult,
                 line_at: Callable[[int], int]):
        self.member = member
        self.path = path
        self.target_names = target_names
        self.known_map = known_map
        self.res = res
        self.line_at = line_at
        self.stmt_label = ""
        #: cursor name -> (target table, ordered select-item columns)
        self.cursors: Dict[str, Tuple[str, List[Optional[str]]]] = {}

    # ------------------------------------------------------------ plumbing

    def _known_names(self, table: str) -> frozenset:
        return frozenset(c.name for c in self.known_map.get(table, ()))

    def _known_cols(self, table: str) -> List[ColumnDef]:
        return self.known_map.get(table, [])

    def _blind(self, off: int, kind: str, detail: str) -> None:
        self.res.blind_spots.append((self.path, self.line_at(off), kind, detail))

    def _emit(self, col: str, table: str, access: str, context: str,
              off: int, host_var: str = "") -> FieldUse:
        use = FieldUse(column=col, table=table, access=access, context=context,
                       stmt=self.stmt_label, host_var=host_var,
                       member=self.member, path=self.path,
                       line=self.line_at(off))
        self.res.uses.append(use)
        return use

    def _col(self, qual: Optional[str], name: str, ctx: _Ctx,
             off: int, strict: bool = False) -> Optional[str]:
        """``strict`` is for expression positions (a SET/WHERE right-hand
        side, an arithmetic select item) where a bare identifier is just as
        likely a routine parameter or special register as a column - there it
        must match a known definition or it is not counted at all."""
        u = name.upper()
        if u in _NOT_COLS:
            return None
        if qual:
            owner = ctx.aliases.get(qual.upper(), qual.upper())
            if owner != ctx.target:
                return None
        elif strict:
            if u not in self._known_names(ctx.target):
                return None
        elif ctx.multi:
            known = self._known_names(ctx.target)
            if known:
                if u not in known:
                    return None
            else:
                self._blind(off, "AMBIGUOUS-COLUMN",
                            f"{u} in a multi-table statement and no column "
                            f"definition for {ctx.target} was found")
                return None
        return u

    # ------------------------------------------------------------ dispatch

    def scan_stmt(self, text: str, base: int, label: str = "") -> None:
        m = re.search(r"[A-Za-z]+", text)
        if not m:
            return
        word = m.group(0).upper()
        self.stmt_label = label or word
        if word == "SELECT":
            self._h_select(text, base)
        elif word == "INSERT":
            self._h_insert(text, base)
        elif word == "UPDATE":
            self._h_update(text, base)
        elif word == "DELETE":
            self._h_delete(text, base)
        elif word == "MERGE":
            self._h_merge(text, base)
        elif word == "DECLARE":
            self._h_declare(text, base)
        elif word == "FETCH":
            self._h_fetch(text, base)
        elif word == "CREATE":
            self._h_create(text, base)
        else:
            # SQL/PL wraps DML in IF/ELSE/BEGIN blocks; find the first real
            # statement at depth 0 rather than giving up on the whole line.
            dml = _find_top(text, _DML_RE, m.end())
            if dml:
                self.scan_stmt(text[dml.start():], base + dml.start())

    # ------------------------------------------------------------ SELECT

    def _h_select(self, text: str, base: int,
                  label: str = "") -> Optional[Tuple[str, List[Optional[str]]]]:
        if label:
            self.stmt_label = label
        spans = _top_spans(text, _SEL_KW_RE)
        sel_i = next((i for i, (k, _s, _e) in enumerate(spans)
                      if k == "SELECT"), None)
        if sel_i is None:
            return None
        # A UNION's later branches are scanned as their own SELECT below.
        union_i = next((i for i in range(sel_i + 1, len(spans))
                        if spans[i][0] == "UNION"), None)
        if union_i is not None:
            self.scan_stmt(text[spans[union_i][2]:], base + spans[union_i][2],
                           label=self.stmt_label)
            spans = spans[:union_i]

        def region(kw: str) -> Optional[Tuple[str, int]]:
            for i in range(sel_i, len(spans)):
                if spans[i][0] == kw:
                    start = spans[i][2]
                    end = spans[i + 1][1] if i + 1 < len(spans) else len(text)
                    return text[start:end], start
            return None

        frm = region("FROM")
        if frm is None:
            return None
        tables, aliases = _from_tables(frm[0])
        target = next((t for t in tables if t in self.target_names), None)
        if target is None:
            return None
        ctx = _Ctx(target=target, aliases=aliases, multi=len(tables) > 1)

        sel = region("SELECT")
        slots: List[Optional[FieldUse]] = []
        for item, ioff in _split_depth0(sel[0]):
            slots.extend(self._sel_item(item, base + sel[1] + ioff, ctx))

        into = region("INTO")
        if into:
            hvs = [_first_hostvar(item) for item, _o in _split_depth0(into[0])]
            for use, hv in zip(slots, hvs):
                if use is not None and hv:
                    use.host_var = hv

        where = region("WHERE")
        if where:
            self._preds(where[0], base + where[1], ctx, "WHERE")
        if re.search(r"\bJOIN\b", frm[0], re.I):
            self._preds(frm[0], base + frm[1], ctx, "ON")

        for kw, context in (("GROUP", "GROUP BY"), ("ORDER", "ORDER BY")):
            reg = region(kw)
            if reg:
                body = re.sub(r"^\s*BY\b", "", reg[0], flags=re.I)
                pad = len(reg[0]) - len(body)
                for item, ioff in _split_depth0(body):
                    it = re.sub(r"\b(?:ASC|DESC)\b\s*$", "", item.strip(),
                                flags=re.I).strip()
                    m = _BARE_COL_RE.match(it)
                    if not m:
                        continue
                    col = self._col(m.group(1), m.group(2), ctx,
                                    base + reg[1] + pad + ioff)
                    if col:
                        self._emit(col, ctx.target, "READ", context,
                                   base + reg[1] + pad + ioff)

        return target, [u.column if u is not None else None for u in slots]

    def _sel_item(self, item: str, off: int, ctx: _Ctx
                  ) -> List[Optional[FieldUse]]:
        it = item.strip()
        if not it:
            return [None]
        star = re.fullmatch(rf"(?:({_IDENT})\s*\.\s*)?\*", it)
        if star:
            qual = star.group(1)
            if qual and ctx.aliases.get(qual.upper(), qual.upper()) != ctx.target:
                return [None]
            known = self._known_cols(ctx.target)
            if not known:
                self._blind(off, "SELECT-STAR",
                            f"SELECT * on {ctx.target} and no column "
                            f"definition was found - columns unknown")
                return [None]
            return [self._emit(c.name, ctx.target, "READ", "SELECT", off)
                    for c in known]
        m = _BARE_COL_RE.match(it)
        if m:
            col = self._col(m.group(1), m.group(2), ctx, off)
            if col:
                return [self._emit(col, ctx.target, "READ", "SELECT", off)]
            return [None]
        # An expression: harvest any column it mentions, but the INTO slot
        # binds to the expression, not to a single column - so slot None.
        for em in re.finditer(
                rf"(?<![:@#$\w.])(?:({_IDENT})\s*\.\s*)?({_IDENT})\b(?!\s*\()", it):
            col = self._col(em.group(1), em.group(2), ctx, off, strict=True)
            if col:
                self._emit(col, ctx.target, "READ", "SELECT", off)
        return [None]

    def _preds(self, region: str, base: int, ctx: _Ctx, context: str) -> None:
        blanked = _blank_subselects(region)
        for m in _PRED_RE.finditer(blanked):
            off = base + m.start(2)
            col = self._col(m.group(1), m.group(2), ctx, off)
            rest = blanked[m.end():]
            hv = ""
            hm = re.match(r"\s*(?:NOT\s+)?:\s*([A-Za-z][A-Za-z0-9-]*)", rest)
            if hm:
                hv = hm.group(1).upper()
            if col:
                self._emit(col, ctx.target, "READ", context, off, host_var=hv)
            # The right-hand side can be a column too (ON A.C1 = B.C1).
            rm = re.match(
                rf"\s*(?:NOT\s+)?(?:({_IDENT})\s*\.\s*)?({_IDENT})\b(?!\s*\()",
                rest)
            if rm and not hm:
                rcol = self._col(rm.group(1), rm.group(2), ctx,
                                 base + m.end() + rm.start(2), strict=True)
                if rcol:
                    self._emit(rcol, ctx.target, "READ", context,
                               base + m.end() + rm.start(2))

    # ------------------------------------------------------------ writes

    def _h_update(self, text: str, base: int) -> None:
        m = re.match(rf"\s*UPDATE\s+({_QNAME})(?:\s+(?:AS\s+)?({_IDENT}))?\s+SET\b",
                     text, re.I | re.S)
        if not m:
            return
        tbl = _last_ident(m.group(1))
        if tbl not in self.target_names:
            return
        aliases = {m.group(2).upper(): tbl} if m.group(2) else {}
        ctx = _Ctx(target=tbl, aliases=aliases, multi=False)
        wm = _find_top(text, _WHERE_RE, m.end())
        set_text = text[m.end():wm.start()] if wm else text[m.end():]
        self._set_entries(set_text, base + m.end(), ctx)
        if wm:
            self._preds(text[wm.end():], base + wm.end(), ctx, "WHERE")

    def _set_entries(self, set_text: str, base: int, ctx: _Ctx) -> None:
        for item, ioff in _split_depth0(set_text):
            m = re.match(rf"\s*(?:({_IDENT})\s*\.\s*)?({_IDENT})\s*=", item)
            if not m:
                continue
            off = base + ioff + m.start(2)
            col = self._col(m.group(1), m.group(2), ctx, off)
            if not col:
                continue
            rhs = item[m.end():]
            self._emit(col, ctx.target, "WRITE", "SET", off,
                       host_var=_first_hostvar(rhs))
            # `SET BAL = BAL + :X` also *reads* the columns on the right.
            for em in re.finditer(
                    rf"(?<![:@#$\w.])(?:({_IDENT})\s*\.\s*)?({_IDENT})\b(?!\s*\()",
                    rhs):
                rcol = self._col(em.group(1), em.group(2), ctx, off,
                                 strict=True)
                if rcol:
                    self._emit(rcol, ctx.target, "READ", "SET-EXPR", off)

    def _h_insert(self, text: str, base: int) -> None:
        m = re.match(rf"\s*INSERT\s+INTO\s+({_QNAME})\s*", text, re.I | re.S)
        if not m:
            return
        tbl = _last_ident(m.group(1))
        if tbl not in self.target_names:
            return
        ctx = _Ctx(target=tbl, aliases={}, multi=False)
        pos = m.end()
        cols: List[Tuple[str, int]] = []
        if pos < len(text) and text[pos] == "(":
            close = _match_paren(text, pos)
            for item, ioff in _split_depth0(text[pos + 1:close]):
                cm = re.search(rf"{_IDENT}", item)
                if cm and cm.group(0).upper() not in _NOT_COLS:
                    cols.append((cm.group(0).upper(), pos + 1 + ioff))
            pos = close + 1

        source = _find_top(text, re.compile(r"\b(VALUES|SELECT)\b", re.I), pos)
        values: List[str] = []
        if source and source.group(1).upper() == "VALUES":
            vp = text.find("(", source.end())
            if vp >= 0:
                vclose = _match_paren(text, vp)
                values = [_first_hostvar(item)
                          for item, _o in _split_depth0(text[vp + 1:vclose])]
        elif source:  # INSERT ... SELECT - scan the SELECT against ITS tables
            self.scan_stmt(text[source.start():], base + source.start(),
                           label=self.stmt_label)

        if not cols:
            known = self._known_cols(tbl)
            if known:
                cols = [(c.name, m.start(1)) for c in known]
            else:
                self._blind(base + m.start(1), "INSERT-NO-COLUMNS",
                            f"INSERT into {tbl} names no columns and no "
                            f"definition was found - column set unknown")
                return
        for i, (col, coff) in enumerate(cols):
            hv = values[i] if i < len(values) else ""
            self._emit(col, tbl, "WRITE", "INSERT", base + coff, host_var=hv)

    def _h_delete(self, text: str, base: int) -> None:
        m = re.match(rf"\s*DELETE\s+FROM\s+({_QNAME})(?:\s+(?:AS\s+)?({_IDENT}))?",
                     text, re.I | re.S)
        if not m:
            return
        tbl = _last_ident(m.group(1))
        if tbl not in self.target_names:
            return
        aliases = {m.group(2).upper(): tbl} if m.group(2) else {}
        ctx = _Ctx(target=tbl, aliases=aliases, multi=False)
        wm = _find_top(text, _WHERE_RE, m.end())
        if wm:
            self._preds(text[wm.end():], base + wm.end(), ctx, "WHERE")

    def _h_merge(self, text: str, base: int) -> None:
        m = re.match(rf"\s*MERGE\s+INTO\s+({_QNAME})(?:\s+(?:AS\s+)?({_IDENT}))?",
                     text, re.I | re.S)
        if not m:
            return
        tbl = _last_ident(m.group(1))
        if tbl not in self.target_names:
            return
        aliases = {m.group(2).upper(): tbl} if m.group(2) else {}
        um = _find_top(text, re.compile(r"\bUSING\b", re.I), m.end())
        if um:
            after = text[um.end():]
            am = re.match(
                rf"\s*(?:\(|({_QNAME}))", after, re.I)
            if am and am.group(1):
                alias_m = re.match(rf"\s*{_QNAME}\s+(?:AS\s+)?({_IDENT})",
                                   after, re.I)
                if alias_m:
                    aliases[alias_m.group(1).upper()] = "(other)"
            else:
                close = _match_paren(after, after.find("("))
                alias_m = re.match(rf"\s*(?:AS\s+)?({_IDENT})",
                                   after[close + 1:], re.I)
                if alias_m:
                    aliases[alias_m.group(1).upper()] = "(other)"
        ctx = _Ctx(target=tbl, aliases=aliases, multi=True)

        onm = _find_top(text, re.compile(r"\bON\b", re.I), m.end())
        if onm:
            endm = _find_top(text, re.compile(r"\bWHEN\b", re.I), onm.end())
            on_text = text[onm.end():endm.start() if endm else len(text)]
            self._preds(on_text, base + onm.end(), ctx, "ON")

        for sm in re.finditer(r"\bUPDATE\s+SET\b", text, re.I):
            endm = _find_top(text, re.compile(r"\bWHEN\b|\bELSE\b", re.I), sm.end())
            self._set_entries(text[sm.end():endm.start() if endm else len(text)],
                              base + sm.end(), ctx)
        for im in re.finditer(r"\bINSERT\s*\(", text, re.I):
            open_pos = im.end() - 1
            close = _match_paren(text, open_pos)
            values: List[str] = []
            vm = re.match(r"\s*VALUES\s*\(", text[close + 1:], re.I)
            if vm:
                vopen = close + 1 + vm.end() - 1
                vclose = _match_paren(text, vopen)
                values = [_first_hostvar(item)
                          for item, _o in _split_depth0(text[vopen + 1:vclose])]
            for i, (item, ioff) in enumerate(
                    _split_depth0(text[open_pos + 1:close])):
                cm = re.search(rf"{_IDENT}", item)
                if not cm or cm.group(0).upper() in _NOT_COLS:
                    continue
                hv = values[i] if i < len(values) else ""
                self._emit(cm.group(0).upper(), tbl, "WRITE", "INSERT",
                           base + open_pos + 1 + ioff, host_var=hv)

    # ------------------------------------------------------------ cursors

    def _h_declare(self, text: str, base: int) -> None:
        m = re.match(r"\s*DECLARE\s+([A-Za-z][A-Za-z0-9-]*)\s+"
                     r"(?:[A-Za-z0-9-]+\s+)*?CURSOR\b.*?\bFOR\b",
                     text, re.I | re.S)
        if not m:
            return
        self.stmt_label = "DECLARE CURSOR"
        result = self._h_select(text[m.end():], base + m.end(),
                                label="DECLARE CURSOR")
        if result:
            self.cursors[m.group(1).upper()] = result

    def _h_fetch(self, text: str, base: int) -> None:
        m = re.match(
            r"\s*FETCH\s+(?:(?:NEXT|PRIOR|FIRST|LAST|BEFORE|AFTER|CURRENT|"
            r"ROWSET|SENSITIVE|INSENSITIVE)\s+)*(?:FROM\s+)?"
            r"([A-Za-z][A-Za-z0-9-]*)\s+INTO\b", text, re.I | re.S)
        if not m:
            return
        known = self.cursors.get(m.group(1).upper())
        if known is None:
            # Only worth reporting when a target cursor exists in this file at
            # all - otherwise this is a fetch on some unrelated table's cursor.
            return
        target, slots = known
        hvs = [_first_hostvar(item)
               for item, _o in _split_depth0(text[m.end():])]
        for col, hv in zip(slots, hvs):
            if col and hv:
                self._emit(col, target, "READ", "FETCH", base + m.start(1),
                           host_var=hv)

    # ------------------------------------------------------------ DDL

    def _h_create(self, text: str, base: int) -> None:
        vm = _VIEW_RE.search(text)
        if vm:
            am = _find_top(text, re.compile(r"\bAS\b", re.I), vm.end())
            if am:
                body = text[am.end():]
                boff = am.end()
                bs = body.lstrip()
                if bs.startswith("("):
                    open_pos = boff + (len(body) - len(bs))
                    close = _match_paren(text, open_pos)
                    body = text[open_pos + 1:close]
                    boff = open_pos + 1
                self.stmt_label = "CREATE VIEW"
                self._h_select(body, base + boff, label="CREATE VIEW")
            return
        im = _INDEX_RE.search(text)
        if im:
            tbl = _last_ident(im.group(1))
            if tbl not in self.target_names:
                return
            self.stmt_label = "CREATE INDEX"
            for item, ioff in _split_depth0(im.group(2)):
                cm = re.search(rf"{_IDENT}", item)
                if cm and cm.group(0).upper() not in _NOT_COLS:
                    self._emit(cm.group(0).upper(), tbl, "READ", "INDEX",
                               base + im.start(2) + ioff)


# ---------------------------------------------------------------- file drivers

def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _scan_cobolish(path: str, member: str, target_names: frozenset,
                   known_map: Dict[str, List[ColumnDef]], res: FieldResult,
                   store, copy_defs_cache: Dict[str, Dict]) -> None:
    text = _read(path)
    if text is None:
        res.blind_spots.append((path, 0, "UNREADABLE",
                                "could not read the file for field analysis"))
        return
    blob, lmap = _cobol_blob(text.splitlines())
    scanner = _FileScanner(member, path, target_names, known_map, res,
                           lmap.line_at)
    before = len(res.uses)
    for m in _EXEC_SQL_RE.finditer(blob):
        body = m.group(1)
        head = re.search(r"[A-Za-z]+", body)
        if not head:
            continue
        first = head.group(0).upper()
        if first in ("INCLUDE", "WHENEVER", "COMMIT", "ROLLBACK", "OPEN",
                     "CLOSE", "CONNECT", "SET", "BEGIN", "END", "PREPARE",
                     "EXECUTE", "DESCRIBE"):
            continue
        base = m.start(1)
        scanner.scan_stmt(_mask_sql(body), base)

    new_uses = res.uses[before:]
    if not any(u.host_var for u in new_uses):
        return
    # Where is each host variable defined? Its data item lives in this program
    # or in a copybook it COPYs - and the *index* already knows which files
    # those members are, so ask it rather than crawling anything.
    defs = dict(_field_defs(blob, lmap, path))
    for cpath in _copybook_paths(store, path):
        if cpath == path:
            continue
        if cpath not in copy_defs_cache:
            ctext = _read(cpath)
            if ctext is None:
                copy_defs_cache[cpath] = {}
            else:
                cblob, clmap = _cobol_blob(ctext.splitlines())
                copy_defs_cache[cpath] = dict(_field_defs(cblob, clmap, cpath))
        for name, d in copy_defs_cache[cpath].items():
            defs.setdefault(name, d)
    for u in new_uses:
        d = defs.get(u.host_var)
        if d:
            u.host_def_path, u.host_def_line, u.host_def_pic = d


def _field_defs(blob: str, lmap: _LineMap, path: str):
    for _lv, name, pic, line in _data_items(blob, lmap):
        yield name, (path, line, pic)


def _copybook_paths(store, path: str) -> List[str]:
    """Copybook/DCLGEN files a program COPYs, resolved through the index."""
    conn = store.conn
    row = conn.execute("SELECT id FROM files WHERE path=? OR path=?",
                       (path, "\\\\?\\" + path)).fetchone()
    if not row:
        return []
    out: List[str] = []
    seen_ids = {row["id"]}
    frontier = [row["id"]]
    for _depth in range(3):                      # copybooks copying copybooks
        if not frontier:
            break
        marks = ",".join("?" * len(frontier))
        members = [r["member"] for r in conn.execute(
            f"SELECT DISTINCT member FROM copy_refs WHERE file_id IN ({marks})",
            frontier)]
        frontier = []
        if not members:
            break
        marks = ",".join("?" * len(members))
        for r in conn.execute(
                f"SELECT id, path FROM files WHERE member IN ({marks}) "
                f"AND kind IN ('COPYBOOK','DCLGEN')", members):
            if r["id"] in seen_ids:
                continue
            seen_ids.add(r["id"])
            out.append(r["path"])
            frontier.append(r["id"])
    return out


def _scan_sqlfile(path: str, member: str, target_names: frozenset,
                  known_map: Dict[str, List[ColumnDef]],
                  res: FieldResult) -> None:
    text = _read(path)
    if text is None:
        res.blind_spots.append((path, 0, "UNREADABLE",
                                "could not read the file for field analysis"))
        return
    masked = _mask_sql(text)
    starts = _line_starts(masked)
    line_at = lambda off: bisect.bisect_right(starts, off)  # noqa: E731
    scanner = _FileScanner(member, path, target_names, known_map, res, line_at)
    for stmt, off in _split_depth0(masked, sep=";"):
        scanner.scan_stmt(stmt, off)


def _scan_utility(path: str, member: str, target_names: frozenset,
                  known_map: Dict[str, List[ColumnDef]],
                  res: FieldResult) -> None:
    """LOAD/UNLOAD decks hard-code the column layout with no SQL grammar."""
    text = _read(path)
    if text is None:
        return
    starts = _line_starts(text)
    line_at = lambda off: bisect.bisect_right(starts, off)  # noqa: E731

    def collist(after: int) -> Optional[List[Tuple[str, int]]]:
        p = after
        while p < len(text) and text[p].isspace():
            p += 1
        if p >= len(text) or text[p] != "(":
            return None
        close = _match_paren(text, p)
        cols = []
        for item, ioff in _split_depth0(text[p + 1:close]):
            cm = re.search(rf"{_IDENT}", item)
            if cm and cm.group(0).upper() not in _NOT_COLS:
                cols.append((cm.group(0).upper(), p + 1 + ioff))
        return cols

    for regex, access, context, stmt in (
            (_LOAD_INTO_RE, "WRITE", "LOAD", "LOAD INTO TABLE"),
            (_UNLOAD_FROM_RE, "READ", "UNLOAD", "UNLOAD FROM TABLE")):
        for m in regex.finditer(text):
            tbl = _last_ident(m.group(1))
            if tbl not in target_names:
                continue
            cols = collist(m.end())
            if cols is None:
                known = known_map.get(tbl)
                if known:
                    cols = [(c.name, m.start(1)) for c in known]
                    res.notes.append(
                        f"{member} {stmt.split()[0]}s {tbl} with no column "
                        f"list - every column, positionally, per the DDL.")
                else:
                    res.blind_spots.append((
                        path, line_at(m.start(1)), "NO-COLUMN-LIST",
                        f"{stmt} {tbl} names no columns and no definition "
                        f"was found - column set unknown"))
                    continue
            for col, coff in cols:
                res.uses.append(FieldUse(
                    column=col, table=tbl, access=access, context=context,
                    stmt=stmt, host_var="", member=member, path=path,
                    line=line_at(coff)))


# ---------------------------------------------------------------- assembly

def _layout_dependents(store, dclgen_paths: List[str]) -> Dict[str, str]:
    """Members that COPY one of the table's DCLGEN/copybook members."""
    conn = store.conn
    members: List[str] = []
    for path in dclgen_paths:
        row = conn.execute("SELECT member FROM files WHERE path=? OR path=?",
                           (path, "\\\\?\\" + path)).fetchone()
        if row and row["member"]:
            members.append(row["member"])
    if not members:
        return {}
    marks = ",".join("?" * len(members))
    out: Dict[str, str] = {}
    for r in conn.execute(
            f"SELECT DISTINCT f.member, c.member AS copied FROM copy_refs c "
            f"JOIN files f ON f.id=c.file_id WHERE c.member IN ({marks})",
            members):
        if r["member"] not in members:
            out.setdefault(r["member"], f"copies {r['copied']}")
    return out


_COBOLISH = frozenset(("COBOL", "COPYBOOK", "DCLGEN", "UNKNOWN"))
_UTILITY = frozenset(("JCL", "PROC", "CONTROL", "SORT", "BIND", "UNKNOWN"))


def _parse_definition_file(path: str, member: str, kind: str, has_declare: bool,
                           target_names: frozenset,
                           known_map: Dict[str, List[ColumnDef]],
                           dclgen_paths: List[str], res: FieldResult) -> None:
    """Fold one CREATE TABLE / DCLGEN file into the column-definition map."""
    text = _read(path)
    if text is None:
        res.blind_spots.append((path, 0, "UNREADABLE",
                                "could not read the definition file"))
        return
    lmap: Optional[_LineMap] = None
    if kind in _COBOLISH:
        blob, lmap = _cobol_blob(text.splitlines())
        line_at = lmap.line_at
    else:
        blob = text
        starts = _line_starts(text)
        line_at = lambda off, s=starts: bisect.bisect_right(s, off)  # noqa: E731
    found = _parse_table_columns(_mask_sql(blob), member, path, line_at)
    for tbl, cols in found.items():
        existing = known_map.get(tbl)
        if not existing or (cols[0].origin == "CREATE TABLE"
                            and existing[0].origin != "CREATE TABLE"):
            known_map[tbl] = cols
    declared = [t for t in found if t in target_names]
    # Host-structure bindings only make sense for COBOL-shaped files - the
    # host fields ARE COBOL data items.
    if declared and has_declare and lmap is not None:
        dclgen_paths.append(path)
        for tbl in declared:
            res.bindings.extend(_dclgen_bindings(
                blob, lmap, found[tbl], tbl, member, path, res))


def _load_definitions_for_tables(store, tables: List[str],
                                 target_names: frozenset,
                                 known_map: Dict[str, List[ColumnDef]],
                                 dclgen_paths: List[str],
                                 res: FieldResult) -> None:
    """Find and parse the definition files for a set of tables via the index."""
    if not tables:
        return
    marks = ",".join("?" * len(tables))
    rows = store.conn.execute(
        f"SELECT DISTINCT r.stmt, f.path, f.member, f.kind FROM table_refs r "
        f"JOIN files f ON f.id=r.file_id WHERE r.table_name IN ({marks}) "
        f"AND r.stmt IN ('CREATE TABLE','DECLARE TABLE','ALTER TABLE') "
        f"AND r.confidence >= ?", (*tables, _CERTAIN)).fetchall()
    per: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        e = per.setdefault(r["path"], {"member": r["member"], "kind": r["kind"],
                                       "declare": False})
        if r["stmt"] == "DECLARE TABLE":
            e["declare"] = True
    # DDL first: a CREATE TABLE is the authoritative column order.
    for path, e in sorted(per.items(),
                          key=lambda kv: (0 if kv[1]["kind"] == "SQL" else 1,
                                          kv[0])):
        _parse_definition_file(path, e["member"], e["kind"], e["declare"],
                               target_names, known_map, dclgen_paths, res)


def _finalize_uses(res: FieldResult, include_read: bool) -> None:
    """Dedupe (the same fact found twice is one fact), order, filter."""
    seen = set()
    uses: List[FieldUse] = []
    for u in res.uses:
        key = (u.path, u.line, u.table, u.column, u.access, u.context,
               u.host_var)
        if key in seen:
            continue
        seen.add(key)
        uses.append(u)
    uses.sort(key=lambda u: (u.member, u.line, u.column))
    if not include_read:
        uses = [u for u in uses if u.access == "WRITE"]
    res.uses = uses


def get_field_dependencies(
    table: str,
    *,
    db_path: str = MFDEP_DB_PATH,
    include_read: bool = True,
) -> FieldResult:
    """Field-level dependency report for one table.

    Runs the table-level ``mfdep.query()`` first (available on the result as
    ``.table_result``), then parses the files it pointed at for column-level
    facts. Raises ``FileNotFoundError`` if the index has not been built.
    """
    with mfdep.open_index(db_path) as store:
        # data_hops=0: dataset lineage cannot be attributed to columns, and
        # the table-level report already covers it.
        tres = mfdep.query(table, db=store, include_read=True, data_hops=0)
        res = FieldResult(spec=table, table_result=tres)

        target_names = frozenset(t.upper() for _s, t, _w in tres.targets)
        _schema, base_table = _split_spec(table)

        by_path: Dict[str, list] = {}
        dynamic_members: set = set()
        for r in tres.refs:
            if r.confidence < _CERTAIN:
                dynamic_members.add(r.member)
                continue
            by_path.setdefault(r.path, []).append(r)

        # ---- pass 1: definitions (DDL first, then DCLGEN), so later passes
        # know each table's columns and their order
        known_map: Dict[str, List[ColumnDef]] = {}
        dclgen_paths: List[str] = []
        def_paths = [(p, refs) for p, refs in by_path.items()
                     if any(r.stmt in ("CREATE TABLE", "DECLARE TABLE",
                                       "ALTER TABLE") for r in refs)]
        def_paths.sort(key=lambda pr: 0 if pr[1][0].kind == "SQL" else 1)
        for path, refs in def_paths:
            _parse_definition_file(
                path, refs[0].member, refs[0].kind,
                any(r.stmt == "DECLARE TABLE" for r in refs),
                target_names, known_map, dclgen_paths, res)

        res.columns = known_map.get(base_table, [])

        # ---- pass 2: statement-level uses
        copy_defs_cache: Dict[str, Dict] = {}
        instream_members: set = set()
        for path, refs in sorted(by_path.items()):
            member, kind = refs[0].member, refs[0].kind
            if kind in ("COBOL", "DCLGEN", "COPYBOOK"):
                _scan_cobolish(path, member, target_names, known_map, res,
                               store, copy_defs_cache)
            elif kind == "SQL":
                _scan_sqlfile(path, member, target_names, known_map, res)
            elif kind == "UNKNOWN":
                _scan_cobolish(path, member, target_names, known_map, res,
                               store, copy_defs_cache)
            if kind in _UTILITY:
                _scan_utility(path, member, target_names, known_map, res)
            if kind in ("JCL", "PROC") and any(
                    r.stmt in _DML_STMTS for r in refs):
                instream_members.add(member)

        res.layout_dependents = _layout_dependents(store, dclgen_paths)

    # ---- honesty notes
    for member in sorted(dynamic_members):
        res.notes.append(
            f"{member} builds its SQL dynamically; its column usage cannot be "
            f"determined statically and is NOT included here.")
    for member in sorted(instream_members):
        res.notes.append(
            f"{member} runs instream SQL inside JCL; that SQL was not "
            f"analysed at field level (utility LOAD/UNLOAD decks were).")
    if any(t not in (base_table,) and w.startswith("view over")
           for _s, t, w in tres.targets):
        res.notes.append(
            "Programs reading a view over this table are reported against the "
            "view's column names; the view definition's own reads of the base "
            "table are what map them back.")

    _finalize_uses(res, include_read)
    return res


def get_program_field_dependencies(
    program: str,
    *,
    db_path: str = MFDEP_DB_PATH,
    include_read: bool = True,
) -> FieldResult:
    """Field-level dependencies of one PROGRAM, across every table it touches.

    The inverse view of :func:`get_field_dependencies`: instead of "who
    touches this table's columns", this answers "which columns does this
    program touch, on which tables, through which host variables". This is
    the view the COBOL -> XState pipeline consumes (see
    :func:`xstate_db2_boundary`).

    mfdep's index supplies the discovery, exactly as in table mode: which
    files carry this program, which tables those files reference, where those
    tables' DDL/DCLGEN definitions live, and which copybooks resolve to which
    paths. All parsing happens here.

    Raises ``LookupError`` when the index has no source for the program - an
    empty boundary for a program we cannot see would be the dangerous kind of
    wrong answer.
    """
    prog = program.upper()
    with mfdep.open_index(db_path) as store:
        conn = store.conn
        res = FieldResult(spec=prog)

        # Files carrying the program: PROGRAM-ID hits, plus the member-name
        # convention for kinds where the member IS the load-module name, plus
        # SQL members (a stored procedure has no PROGRAM-ID at all).
        files: Dict[int, Tuple[str, str, str]] = {}
        for r in conn.execute(
                "SELECT DISTINCT f.id, f.path, f.member, f.kind "
                "FROM programs p JOIN files f ON f.id=p.file_id "
                "WHERE p.name=?", (prog,)):
            files[r["id"]] = (r["path"], r["member"], r["kind"])
        for r in conn.execute(
                "SELECT id, path, member, kind FROM files WHERE member=? "
                "AND kind IN ('COBOL','UNKNOWN','SQL')", (prog,)):
            files.setdefault(r["id"], (r["path"], r["member"], r["kind"]))
        if not files:
            raise LookupError(
                f"no source for program {prog!r} in the index at {db_path!r}")

        # Tables the program references, with their table-level access - a
        # DELETE names no columns but must still appear on the boundary.
        tables: set = set()
        for fid, (path, _member, _kind) in files.items():
            for r in conn.execute(
                    "SELECT line, table_name, access, confidence "
                    "FROM table_refs WHERE file_id=?", (fid,)):
                if r["confidence"] < _CERTAIN:
                    res.blind_spots.append((
                        path, r["line"], "DYNAMIC-SQL",
                        f"{r['table_name']} is referenced from dynamically "
                        f"built SQL - column usage cannot be determined "
                        f"statically"))
                    continue
                tbl = r["table_name"].upper()
                tables.add(tbl)
                acc = res.table_access.setdefault(tbl, [])
                if r["access"] not in acc:
                    acc.append(r["access"])

        target_names = frozenset(tables)
        known_map: Dict[str, List[ColumnDef]] = {}
        dclgen_paths: List[str] = []
        _load_definitions_for_tables(store, sorted(tables), target_names,
                                     known_map, dclgen_paths, res)

        copy_defs_cache: Dict[str, Dict] = {}
        for fid, (path, member, kind) in sorted(files.items()):
            if kind == "SQL":
                _scan_sqlfile(path, member, target_names, known_map, res)
            else:
                _scan_cobolish(path, member, target_names, known_map, res,
                               store, copy_defs_cache)

    for a in res.table_access.values():
        a.sort()
    _finalize_uses(res, include_read)
    return res


#: Contexts where data flows INTO the program (Db2 -> host variable).
_IN_CONTEXTS = frozenset(("SELECT", "INTO", "FETCH"))
#: Contexts where the column is compared/ordered, not transferred; a bound
#: host variable flows out to Db2 as the comparison value.
_FILTER_CONTEXTS = frozenset(("WHERE", "ON", "ORDER BY", "GROUP BY", "INDEX",
                              "SET-EXPR"))


def xstate_db2_boundary(
    program: str, *, db_path: str = MFDEP_DB_PATH
) -> Dict[str, Any]:
    """The program's Db2 external-I/O boundary, shaped for the XState pipeline.

    The COBOL -> XState rewrite flow (the ``ibm-cobol`` and
    ``xstate-cobol-contract`` skills) recovers control flow and statement
    logic from the source, but the machine also has to carry its *external
    boundary* - the typed Db2 endpoints with the fields that cross them - so
    the statechart renderer can draw input/output arrows and the rewrite team
    knows the data contract. This emits exactly that piece, following the
    contract's own integrity rules: every field is provenance-tagged to file
    and line, and anything unanalyzable lands in ``unmodeledConstructs``
    rather than being silently approximated.

    Embed the returned object in the machine's ``_meta`` (e.g. as
    ``_meta.externalIO.db2``); it is plain JSON-serializable data.

    Field directions, relative to the program:
      * ``in``     - Db2 -> host variable (SELECT INTO, FETCH)
      * ``out``    - host variable -> Db2 (INSERT, UPDATE SET)
      * ``filter`` - the column is compared or ordered on; a bound host
        variable is the comparison value the program sends
    """
    res = get_program_field_dependencies(program, db_path=db_path)

    pics = {(b.table, b.column): b.pic for b in res.bindings}
    endpoints: Dict[str, Dict[str, Any]] = {}

    def endpoint(table: str) -> Dict[str, Any]:
        return endpoints.setdefault(table, {
            "type": "Db2",
            "object": table,
            "access": sorted(res.table_access.get(table, [])),
            "fields": {},
        })

    for table in res.table_access:
        endpoint(table)
    for u in res.uses:
        ep = endpoint(u.table)
        if u.access == "WRITE":
            direction = "out"
        elif u.context in _FILTER_CONTEXTS:
            direction = "filter"
        else:
            direction = "in"
        f = ep["fields"].setdefault((u.column, direction), {
            "column": u.column,
            "direction": direction,
            "hostVars": set(),
            "pic": pics.get((u.table, u.column), ""),
            "provenance": [],
        })
        if u.host_var:
            f["hostVars"].add(u.host_var)
        f["provenance"].append({
            "path": u.path, "line": u.line,
            "stmt": u.stmt, "context": u.context,
        })

    out_endpoints = []
    for table in sorted(endpoints):
        ep = endpoints[table]
        fields = []
        for (_col, _direction), f in sorted(ep["fields"].items()):
            f["hostVars"] = sorted(f["hostVars"])
            fields.append(f)
        ep["fields"] = fields
        out_endpoints.append(ep)

    return {
        "program": res.spec,
        "generatedBy": "field_dependencies.xstate_db2_boundary",
        "endpoints": out_endpoints,
        "unmodeledConstructs": [
            {"construct": kind, "reason": detail,
             "provenance": {"path": path, "line": line}}
            for path, line, kind, detail in res.blind_spots],
        "notes": res.notes,
    }


def field_dependency_summary(
    table: str, *, db_path: str = MFDEP_DB_PATH
) -> Dict[str, Any]:
    """A compact, already-serializable per-column rollup."""
    res = get_field_dependencies(table, db_path=db_path)
    cols: Dict[str, Dict[str, Any]] = {}

    def slot(name: str) -> Dict[str, Any]:
        return cols.setdefault(name, {
            "type": "", "read_by": set(), "written_by": set(),
            "host_fields": {}})

    for c in res.columns:
        slot(c.name)["type"] = c.sql_type
    for b in res.bindings:
        # Keyed by field name so a host variable seen in a statement and the
        # same field's DCLGEN binding collapse to one entry, PIC form winning.
        slot(b.column)["host_fields"][b.host_field] = \
            f"{b.host_field} PIC {b.pic}".strip()
    for u in res.uses:
        s = slot(u.column)
        (s["read_by"] if u.access == "READ" else s["written_by"]).add(u.member)
        if u.host_var:
            s["host_fields"].setdefault(u.host_var, u.host_var)
    return {
        "table": res.spec,
        "columns": {name: {k: sorted(v.values() if isinstance(v, dict) else v)
                           if not isinstance(v, str) else v
                           for k, v in data.items()}
                    for name, data in cols.items()},
        "layout_dependents": dict(sorted(res.layout_dependents.items())),
        "blind_spots": len(res.blind_spots),
        "notes": res.notes,
    }


def field_dependencies_json(
    table: str, *, db_path: str = MFDEP_DB_PATH, **kwargs: Any
) -> str:
    """The full result as a JSON string, for serialization into the pipeline.

    The table-level ``table_result`` is not embedded - callers wanting both
    should serialize it separately via ``mfdep.report.json_report``.
    """
    res = get_field_dependencies(table, db_path=db_path, **kwargs)
    return json.dumps({
        "table": res.spec,
        "columns": [asdict(c) for c in res.columns],
        "uses": [asdict(u) for u in res.uses],
        "bindings": [asdict(b) for b in res.bindings],
        "layout_dependents": res.layout_dependents,
        "blind_spots": [list(b) for b in res.blind_spots],
        "notes": res.notes,
    }, indent=2)
