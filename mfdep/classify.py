"""Decide what kind of artifact a file is.

Extension is the weakest signal here. PDS members exported to a share usually
have no extension at all - the library name in the path is the only hint, and
even that is unreliable once someone drops a copybook into a JCL library. So:
extension, then path hint, then content sniff, with content winning whenever it
is confident.
"""

from __future__ import annotations

import os
import re

from .config import (BIND, COBOL, CONTROL, COPYBOOK, DCLGEN, EXT_KIND, JCL,
                     PATH_HINT_KIND, PROC, SORT, SQL, UNKNOWN)

_JOB_STMT = re.compile(r"^//\S*\s+JOB\b", re.M)
_PROC_STMT = re.compile(r"^//\S*\s+PROC\b", re.M)
_EXEC_STMT = re.compile(r"^//\S*\s+EXEC\b", re.M)
_DD_STMT = re.compile(r"^//\S*\s+DD\b", re.M)
_ID_DIV = re.compile(r"\bIDENTIFICATION\s+DIVISION\b|\bPROGRAM-ID\s*\.", re.I)
_PROC_DIV = re.compile(r"\bPROCEDURE\s+DIVISION\b", re.I)
_DECLARE_TABLE = re.compile(r"\bEXEC\s+SQL\s+DECLARE\b.*?\bTABLE\b", re.I | re.S)
# A COBOL level-01 must begin the statement (after the optional sequence
# number) and be followed by a data name and then a clause or a period.
# A loose version of this matches `SELECT 1 FROM ...` and misfiles whole SQL
# libraries as copybooks, which then parse to nothing at all.
_LEVEL_01 = re.compile(
    r"^(?:\d{6})?[ \t]*0?1[ \t]+[A-Za-z][A-Za-z0-9-]*[ \t]*"
    r"(?:\.|(?:PIC|PICTURE|REDEFINES|OCCURS|USAGE|COMP|COMP-3|BINARY|VALUE)\b)",
    re.M | re.I)
_CREATE_ROUTINE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:PROCEDURE|FUNCTION|TRIGGER|TABLE|VIEW|INDEX)\b",
    re.I)
_UTILITY = re.compile(
    r"\b(?:LOAD\s+DATA|UNLOAD\b|REORG\s+TABLESPACE|RUNSTATS\s+TABLESPACE|"
    r"COPY\s+TABLESPACE|RECOVER\s+TABLESPACE|CHECK\s+DATA|LISTDEF|TEMPLATE)\b", re.I)
_BIND_CARD = re.compile(r"\bBIND\s+(?:PLAN|PACKAGE)\s*\(", re.I)
# DFSORT / SYNCSORT / ICETOOL. These decks name no table, but they sit in the
# middle of every UNLOAD -> sort -> LOAD chain, so identifying them keeps a
# cataloged member from being filed as UNKNOWN and losing its link to the step.
_SORT_DECK = re.compile(
    r"^\s*(?:SORT|MERGE)\s+(?:FIELDS|FORMAT)\s*=|"
    r"^\s*OPTION\s+COPY\b|"
    r"^\s*(?:INREC|OUTREC|OUTFIL|JOINKEYS)\s+\w+[=(]|"
    r"^\s*SUM\s+FIELDS\s*=",
    re.I | re.M)
_SQL_ANY = re.compile(r"\bEXEC\s+SQL\b|\bSELECT\b.*\bFROM\b", re.I | re.S)


def kind_from_name(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in EXT_KIND:
        return EXT_KIND[ext]
    upper = path.upper().replace("/", "\\")
    for fragment, kind in PATH_HINT_KIND:
        if fragment in upper:
            return kind
    return UNKNOWN


def kind_from_content(head: str) -> str:
    """Classify from the first few KB of text. Returns UNKNOWN if unsure."""
    if _JOB_STMT.search(head):
        return JCL
    if _PROC_STMT.search(head):
        return PROC
    # JCL fragment with no JOB card: an INCLUDE member or a PROC without its
    # PROC statement. Still parsed with the JCL grammar.
    if _EXEC_STMT.search(head) or _DD_STMT.search(head):
        return PROC

    if _ID_DIV.search(head):
        return COBOL
    if _DECLARE_TABLE.search(head):
        return DCLGEN
    if _BIND_CARD.search(head):
        return BIND
    if _SORT_DECK.search(head):
        return SORT
    if _UTILITY.search(head):
        return CONTROL
    if _CREATE_ROUTINE.search(head):
        return SQL
    if _LEVEL_01.search(head) and not _PROC_DIV.search(head):
        return COPYBOOK
    if _SQL_ANY.search(head):
        return SQL
    return UNKNOWN


def classify(path: str, head: str) -> str:
    """Final kind, preferring content when it disagrees with the file name."""
    by_content = kind_from_content(head)
    by_name = kind_from_name(path)

    if by_content == UNKNOWN:
        return by_name
    if by_name == UNKNOWN:
        return by_content

    # Content wins on the distinctions that change which parser runs.
    if {by_name, by_content} <= {COBOL, COPYBOOK, DCLGEN}:
        return by_content if by_content in (DCLGEN, COPYBOOK) else by_name
    if {by_name, by_content} <= {JCL, PROC}:
        return by_content
    return by_content
