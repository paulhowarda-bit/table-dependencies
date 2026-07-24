"""Dispatch one file to the right parser and return its facts.

This is the function the worker pool calls, so it must never raise and never
grow unbounded in memory. Members of a few dozen MB are common in these
libraries (generated DDL, concatenated listings, giant control decks) and a
pool of eight workers each materialising one would take the machine down.
Those are parsed in overlapping line windows instead - same parsers, bounded
memory, and the overlap keeps statements that straddle a window boundary.
"""

from __future__ import annotations

import os

from .classify import classify
from .config import (BIND, COBOL, CONTROL, COPYBOOK, DCLGEN, JCL, PROC, SORT,
                     SQL, UNKNOWN)
from .facts import FileFacts
from .parsers import cobol as cobol_parser
from .parsers import control as control_parser
from .parsers import jcl as jcl_parser
from .parsers import sqlfile as sql_parser
from .reader import iter_lines

HEAD_LINES = 200
CHUNK_LINES = 20_000
CHUNK_OVERLAP = 300
CHUNK_THRESHOLD_BYTES = 8 << 20      # parse in one pass below this

_PARSER = {
    COBOL: cobol_parser.parse,
    COPYBOOK: cobol_parser.parse,
    DCLGEN: cobol_parser.parse,
    JCL: jcl_parser.parse,
    PROC: jcl_parser.parse,
    SQL: sql_parser.parse,
    CONTROL: control_parser.parse,
    SORT: control_parser.parse,
    BIND: control_parser.parse,
}


def _parse_unknown(path: str, lines: list[str], kind: str) -> FileFacts:
    """Last resort: try every grammar, each of which gates on its own shape."""
    facts = control_parser.parse(path, lines, kind)
    sub = cobol_parser.parse(path, lines, kind)
    facts.table_refs.extend(sub.table_refs)
    facts.copy_refs.extend(sub.copy_refs)
    facts.programs.extend(sub.programs)
    facts.objects.extend(sub.objects)
    return facts


def _shift(facts: FileFacts, offset: int) -> None:
    """Rebase 1-based line numbers from a window onto the whole file."""
    if offset == 0:
        return

    def bump(rows: list[tuple], idx: int = 0) -> list[tuple]:
        return [tuple(v + offset if i == idx and isinstance(v, int) else v
                      for i, v in enumerate(row)) for row in rows]

    facts.programs = bump(facts.programs)
    facts.table_refs = bump(facts.table_refs)
    facts.copy_refs = bump(facts.copy_refs)
    facts.calls = bump(facts.calls)
    facts.jobs = bump(facts.jobs)
    facts.steps = bump(facts.steps)
    facts.dds = bump(facts.dds)
    facts.objects = bump(facts.objects)
    facts.blind_spots = bump(facts.blind_spots)


def _merge(dest: FileFacts, src: FileFacts) -> None:
    dest.programs.extend(src.programs)
    dest.table_refs.extend(src.table_refs)
    dest.copy_refs.extend(src.copy_refs)
    dest.calls.extend(src.calls)
    dest.jobs.extend(src.jobs)
    dest.steps.extend(src.steps)
    dest.dds.extend(src.dds)
    dest.objects.extend(src.objects)
    dest.blind_spots.extend(src.blind_spots)


def _dedupe(facts: FileFacts) -> None:
    """Drop duplicates produced by window overlap, preserving order."""
    def uniq(rows: list[tuple]) -> list[tuple]:
        seen = set()
        out = []
        for row in rows:
            if row in seen:
                continue
            seen.add(row)
            out.append(row)
        return out

    facts.programs = uniq(facts.programs)
    facts.table_refs = uniq(facts.table_refs)
    facts.copy_refs = uniq(facts.copy_refs)
    facts.calls = uniq(facts.calls)
    facts.jobs = uniq(facts.jobs)
    facts.steps = uniq(facts.steps)
    facts.dds = uniq(facts.dds)
    facts.objects = uniq(facts.objects)
    facts.blind_spots = uniq(facts.blind_spots)


def extract_file(path: str, size: int = 0, mtime_ns: int = 0,
                 max_bytes: int | None = None) -> FileFacts:
    """Parse one artifact. Always returns facts; failures land in ``.error``."""
    try:
        return _extract(path, size, mtime_ns, max_bytes)
    except Exception as exc:                      # a bad member must not stop the crawl
        return FileFacts(path=path, kind=UNKNOWN, size=size, mtime_ns=mtime_ns,
                         error=f"{type(exc).__name__}: {exc}"[:300])


def _extract(path: str, size: int, mtime_ns: int,
             max_bytes: int | None) -> FileFacts:
    it, result = iter_lines(path, max_bytes=max_bytes)

    head: list[str] = []
    for line in it:
        head.append(line)
        if len(head) >= HEAD_LINES:
            break

    if not result.ok:
        return FileFacts(path=path, kind=UNKNOWN, size=size, mtime_ns=mtime_ns,
                         error=result.reason)
    if not head:
        return FileFacts(path=path, kind=UNKNOWN, size=size, mtime_ns=mtime_ns,
                         line_count=0)

    kind = classify(path, "\n".join(head)[:8192])
    parser = _PARSER.get(kind, _parse_unknown)

    if size and size > CHUNK_THRESHOLD_BYTES:
        facts = _extract_chunked(path, kind, parser, head, it)
    else:
        lines = head + list(it)
        facts = parser(path, lines, kind)

    facts.size = size or result.size
    facts.mtime_ns = mtime_ns
    facts.line_count = result.line_count or facts.line_count
    facts.truncated = result.truncated
    if result.truncated:
        facts.blind_spots.append(
            (0, "TRUNCATED",
             f"stopped after {max_bytes} bytes - file is {size} bytes"))
    if not result.ok and result.reason:
        facts.error = result.reason
    return facts


def _extract_chunked(path: str, kind: str, parser, head: list[str], it) -> FileFacts:
    """Parse a large file in overlapping windows, bounding worker memory."""
    merged = FileFacts(path=path, kind=kind)
    window = list(head)
    start = 0                                     # 0-based line offset of window[0]
    windows = 0

    def flush(final: bool) -> None:
        nonlocal window, start, windows
        if not window:
            return
        sub = parser(path, window, kind)
        _shift(sub, start)
        _merge(merged, sub)
        windows += 1
        if final:
            window = []
            return
        keep = window[-CHUNK_OVERLAP:]
        start += len(window) - len(keep)
        window = keep

    for line in it:
        window.append(line)
        if len(window) >= CHUNK_LINES:
            flush(final=False)
    flush(final=True)

    _dedupe(merged)
    merged.blind_spots.append(
        (0, "CHUNKED",
         f"parsed in {windows} overlapping windows of {CHUNK_LINES} lines "
         f"({CHUNK_OVERLAP}-line overlap) because the file exceeds "
         f"{CHUNK_THRESHOLD_BYTES >> 20} MB"))
    return merged
