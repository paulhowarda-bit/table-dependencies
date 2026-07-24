"""The record a worker process returns for one file.

Deliberately plain lists of tuples: this crosses a multiprocessing pickle
boundary 100k times, so it stays small and cheap to serialise.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FileFacts:
    path: str
    kind: str
    size: int = 0
    mtime_ns: int = 0
    line_count: int = 0
    error: str = ""
    truncated: bool = False

    # (line, name)
    programs: list[tuple[int, str]] = field(default_factory=list)
    # (line, schema, table, access, stmt, confidence, snippet, via)
    table_refs: list[tuple] = field(default_factory=list)
    # (line, member, kind)  kind = COPY | SQL-INCLUDE | JCL-INCLUDE
    copy_refs: list[tuple] = field(default_factory=list)
    # (line, callee, is_dynamic)
    calls: list[tuple] = field(default_factory=list)
    # (line, job_name)
    jobs: list[tuple] = field(default_factory=list)
    # (line, job, step, seq, pgm, proc, resolved_pgm)
    steps: list[tuple] = field(default_factory=list)
    # (line, step, dd_name, dsn, dsname, lookup_key, is_member, disp)
    dds: list[tuple] = field(default_factory=list)
    # (line, schema, name, obj_type, parent_schema, parent_name)
    objects: list[tuple] = field(default_factory=list)
    # (line, kind, detail) - anything the parser could not resolve
    blind_spots: list[tuple] = field(default_factory=list)

    def add_ref(self, line: int, schema: str, table: str, access: str,
                stmt: str, confidence: int, snippet: str, via: str = "") -> None:
        self.table_refs.append(
            (line, schema, table, access, stmt, confidence, snippet[:200], via))
