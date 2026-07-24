"""Streaming file reading that stays flat in memory on multi-MB members.

Nothing here ever calls ``f.read()`` on a whole file. Some artifact members run
to dozens of MB (generated DDL, giant control decks, concatenated listings) and
a process pool that slurps those whole will thrash or die.
"""

from __future__ import annotations

import io
import os
from typing import Iterator

from .config import BINARY_NUL_THRESHOLD, SNIFF_BYTES
from .util import long_path


class ReadResult:
    """Outcome of opening a file, so callers can distinguish 'nothing found'
    from 'never actually looked'."""

    __slots__ = ("ok", "reason", "encoding", "truncated", "line_count", "size")

    def __init__(self, ok: bool, reason: str = "", encoding: str = "utf-8",
                 truncated: bool = False, line_count: int = 0, size: int = 0):
        self.ok = ok
        self.reason = reason
        self.encoding = encoding
        self.truncated = truncated
        self.line_count = line_count
        self.size = size


def looks_binary(head: bytes) -> bool:
    """Cheap binary sniff on the first block.

    Load modules, DBRMs and object decks live in the same libraries as source
    and would otherwise burn CPU producing garbage matches.
    """
    if not head:
        return False
    if b"\x00" in head:
        nul_ratio = head.count(b"\x00") / len(head)
        if nul_ratio > BINARY_NUL_THRESHOLD:
            return True
    # High-bit density is the EBCDIC / packed-binary tell.
    high = sum(1 for b in head if b >= 0x80)
    return (high / len(head)) > 0.30


def iter_lines(path: str, max_bytes: int | None = None,
               encoding: str = "utf-8") -> tuple[Iterator[str], ReadResult]:
    """Yield decoded, right-stripped lines without holding the file in memory.

    Returns ``(iterator, result)``. The result is only fully populated once the
    iterator is exhausted, so read it *after* iterating.
    """
    p = long_path(path)
    try:
        size = os.path.getsize(p)
    except OSError as exc:
        return iter(()), ReadResult(False, f"stat failed: {exc}")

    result = ReadResult(True, encoding=encoding, size=size)

    try:
        fh = open(p, "rb", buffering=1 << 20)
    except OSError as exc:
        return iter(()), ReadResult(False, f"open failed: {exc}", size=size)

    try:
        head = fh.peek(SNIFF_BYTES)[:SNIFF_BYTES]
    except OSError as exc:
        fh.close()
        return iter(()), ReadResult(False, f"read failed: {exc}", size=size)

    if looks_binary(head):
        fh.close()
        return iter(()), ReadResult(False, "binary content", size=size)

    def _gen() -> Iterator[str]:
        consumed = 0
        try:
            # errors='replace' keeps a stray byte from killing a 40 MB member.
            text = io.TextIOWrapper(fh, encoding=encoding, errors="replace",
                                    newline=None)
            for line in text:
                consumed += len(line)
                if max_bytes is not None and consumed > max_bytes:
                    result.truncated = True
                    break
                result.line_count += 1
                yield line.rstrip("\r\n")
        except OSError as exc:
            result.ok = False
            result.reason = f"read failed: {exc}"
        finally:
            try:
                fh.close()
            except OSError:
                pass

    return _gen(), result


def read_lines(path: str, max_bytes: int | None = None,
               encoding: str = "utf-8") -> tuple[list[str], ReadResult]:
    """Materialise lines for files small enough to hold.

    Used by the JCL and COBOL parsers, which need look-ahead for continuation
    handling. Guarded by ``max_bytes`` so a runaway member still can't blow the
    worker up - it gets truncated and flagged, never silently accepted.
    """
    it, result = iter_lines(path, max_bytes=max_bytes, encoding=encoding)
    lines = list(it)
    return lines, result
