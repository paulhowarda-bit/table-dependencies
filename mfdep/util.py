"""Path, offset and text helpers shared by the parsers."""

from __future__ import annotations

import bisect
import os
import re
import sys


# ---------------------------------------------------------------- long paths

def long_path(path: str) -> str:
    """Return a Windows form that survives the 260-char MAX_PATH limit.

    Mainframe export shares nest deeply (``\\\\srv\\share\\PROD\\SYS\\...``) and
    routinely blow past MAX_PATH. Without the ``\\\\?\\`` prefix those files
    raise FileNotFoundError and vanish from the scan silently.
    """
    if sys.platform != "win32":
        return path
    if path.startswith("\\\\?\\"):
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\"):                       # UNC: \\srv\share -> \\?\UNC\srv\share
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def display_path(path: str) -> str:
    """Strip any ``\\\\?\\`` prefix so reports stay readable."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


# ---------------------------------------------------------------- line index

class LineMap:
    """Maps character offsets in an assembled text blob back to source lines.

    The parsers build a normalised blob (COBOL columns stripped, JCL
    continuations joined) and then run regexes over it. Every match has to be
    reportable as a real line number in the real file, which is what this does.
    """

    __slots__ = ("_starts", "_lines")

    def __init__(self) -> None:
        self._starts: list[int] = []   # blob offset where each piece begins
        self._lines: list[int] = []    # 1-based source line for that piece

    def add(self, blob_offset: int, source_line: int) -> None:
        self._starts.append(blob_offset)
        self._lines.append(source_line)

    def line_at(self, offset: int) -> int:
        if not self._starts:
            return 0
        i = bisect.bisect_right(self._starts, offset) - 1
        return self._lines[max(i, 0)]


# ---------------------------------------------------------------- masking

_SQ_STRING = re.compile(r"'(?:[^']|'')*'")
_DQ_STRING = re.compile(r'"(?:[^"]|"")*"')
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _blank(match: re.Match) -> str:
    """Replace a span with spaces, preserving newlines so offsets AND line
    numbers both stay exact."""
    return "".join("\n" if c == "\n" else " " for c in match.group(0))


def mask_sql_noise(text: str, keep_delimited_ids: bool = True) -> str:
    """Blank out string literals and comments, preserving length and newlines.

    Length preservation is the whole point: every offset in the masked text is
    still valid in the original, so a regex match maps straight back to a line
    with no bookkeeping. Without this, ``WHERE NOTE = 'SELECT FROM CUSTOMER'``
    reports a phantom dependency on CUSTOMER.

    Delimited identifiers (``"My Table"``) are kept by default because they are
    real object names, not literals.
    """
    text = _BLOCK_COMMENT.sub(_blank, text)
    text = _LINE_COMMENT.sub(_blank, text)
    text = _SQ_STRING.sub(_blank, text)
    if not keep_delimited_ids:
        text = _DQ_STRING.sub(_blank, text)
    return text


def extract_literals(text: str) -> list[tuple[int, str]]:
    """Return ``(offset, value)`` for every single-quoted literal.

    Used for the dynamic-SQL fallback: when a program builds its statement at
    run time the table name only ever appears inside a literal, so those get
    scanned at HEURISTIC confidence rather than being lost.
    """
    return [(m.start(), m.group(0)[1:-1].replace("''", "'"))
            for m in _SQ_STRING.finditer(text)]


# ---------------------------------------------------------------- identifiers

def split_qualified(name: str) -> tuple[str, str]:
    """``'SCHEMA.TBL'`` -> ``('SCHEMA', 'TBL')``; unqualified -> ``('', 'TBL')``.

    Handles delimited parts and Db2's 3-part ``LOCATION.SCHEMA.TABLE`` form,
    where the location prefix is dropped (it names a remote subsystem, not a
    different table).
    """
    parts = [p.strip() for p in _split_dots(name)]
    parts = [p for p in parts if p]
    if not parts:
        return "", ""
    parts = [_unquote(p) for p in parts]
    if len(parts) >= 3:
        return parts[-2], parts[-1]
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", parts[0]


def _split_dots(name: str) -> list[str]:
    """Split on dots that are not inside double quotes."""
    out, cur, in_q = [], [], False
    for ch in name:
        if ch == '"':
            in_q = not in_q
            cur.append(ch)
        elif ch == "." and not in_q:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def _unquote(part: str) -> str:
    part = part.strip()
    if len(part) >= 2 and part[0] == '"' and part[-1] == '"':
        return part[1:-1].replace('""', '"')
    return part.upper()


def norm_table(name: str) -> str:
    """Canonical ``SCHEMA.TABLE`` (or bare ``TABLE`` when unqualified)."""
    schema, table = split_qualified(name)
    return f"{schema}.{table}" if schema else table


# ---------------------------------------------------------------- snippets

_WS = re.compile(r"\s+")


def squash(text: str) -> str:
    """Collapse whitespace so a multi-line statement fits on one report line."""
    return _WS.sub(" ", text).strip()


def snippet(text: str, offset: int, before: int = 40, after: int = 70) -> str:
    """A one-line excerpt of ``text`` around ``offset``, for report evidence."""
    return squash(text[max(0, offset - before): offset + after])


# ---------------------------------------------------------------- datasets

_GDG_GEN = re.compile(r"^[+-]?\d+$")


def parse_dsn(dsn: str) -> tuple[str, str, bool]:
    """Split a JCL DSN into ``(dsname, lookup_key, is_pds_member)``.

    ``PROD.CNTL(LOADCUST)`` -> ``('PROD.CNTL', 'LOADCUST', True)``. The lookup
    key is what gets matched against a file's member name on the share; for a
    sequential dataset it falls back to the last qualifier, since an exported
    sequential file is usually named after it.

    Generation numbers are not members: ``PROD.GDG(+1)`` must not be looked up
    as a member called ``+1``.
    """
    if not dsn:
        return "", "", False

    dsn = dsn.strip().strip("'").upper()
    if dsn.startswith("&&") or dsn.startswith("&"):
        return dsn, "", False                    # temporary or unresolved symbolic

    member = ""
    dsname = dsn
    if dsn.endswith(")") and "(" in dsn:
        dsname, _, inner = dsn[:-1].partition("(")
        inner = inner.strip()
        if _GDG_GEN.match(inner):
            member = ""                          # GDG relative generation
        else:
            member = inner

    dsname = dsname.strip()
    if member:
        return dsname, member, True

    last = dsname.rsplit(".", 1)[-1] if dsname else ""
    return dsname, last, False


def dsn_path_score(dsname: str, path: str) -> int:
    """How strongly a DSN matches an exported file path. 0 means no match.

    Member names collide constantly across libraries (every shop has a dozen
    members called LOAD01), so matching on the member alone would wire a job to
    the wrong control deck. The dataset name is what disambiguates.
    """
    if not dsname:
        return 0
    p = path.upper().replace("/", "\\")
    d = dsname.upper()
    if d in p:
        return 3
    quals = [q for q in d.split(".") if q]
    if len(quals) >= 2 and ".".join(quals[-2:]) in p:
        return 2
    if quals and quals[-1] in p:
        return 1
    return 0
