"""SQLite fact store.

Why an index at all: a single grep pass over 100k members on an SMB share is
minutes to hours, and you need one pass *per table*. Extracting facts once into
a local database turns every subsequent question into a millisecond lookup, and
lets the transitive closure run at all - you cannot follow JCL -> program ->
table by grepping, because you do not know what to grep for until the previous
hop resolves.

The write path is built for bulk load. Measured on 20k files / 356 MB, the
naive version (live indexes, a SELECT per file, nine executemany calls per
file) spent 50 of 60 seconds writing and only 9 seconds parsing. So: indexes
are dropped during the load and rebuilt once at the end, file ids are assigned
in Python instead of read back, and rows are batched across files.
"""

from __future__ import annotations

import os
import sqlite3
import time

from .facts import FileFacts

SCHEMA_VERSION = 2
FLUSH_ROWS = 200_000

_TABLES = """
CREATE TABLE IF NOT EXISTS files (
    id         INTEGER PRIMARY KEY,
    path       TEXT NOT NULL UNIQUE,
    member     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    size       INTEGER NOT NULL DEFAULT 0,
    mtime_ns   INTEGER NOT NULL DEFAULT 0,
    line_count INTEGER NOT NULL DEFAULT 0,
    truncated  INTEGER NOT NULL DEFAULT 0,
    error      TEXT NOT NULL DEFAULT '',
    scanned_at INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS programs (
    file_id INTEGER NOT NULL, line INTEGER, name TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS table_refs (
    file_id INTEGER NOT NULL, line INTEGER, schema TEXT, table_name TEXT NOT NULL,
    access TEXT, stmt TEXT, confidence INTEGER, snippet TEXT, via TEXT);

CREATE TABLE IF NOT EXISTS copy_refs (
    file_id INTEGER NOT NULL, line INTEGER, member TEXT NOT NULL, kind TEXT);

CREATE TABLE IF NOT EXISTS calls (
    file_id INTEGER NOT NULL, line INTEGER, callee TEXT NOT NULL, dynamic INTEGER);

CREATE TABLE IF NOT EXISTS jobs (
    file_id INTEGER NOT NULL, line INTEGER, name TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS steps (
    file_id INTEGER NOT NULL, line INTEGER, job TEXT, step TEXT, seq INTEGER,
    pgm TEXT, proc TEXT, resolved_pgm TEXT);

CREATE TABLE IF NOT EXISTS dds (
    file_id INTEGER NOT NULL, line INTEGER, step TEXT, dd TEXT, dsn TEXT,
    dsname TEXT, lookup_key TEXT, is_member INTEGER, disp TEXT);

CREATE TABLE IF NOT EXISTS objects (
    file_id INTEGER NOT NULL, line INTEGER, schema TEXT, name TEXT NOT NULL,
    obj_type TEXT, parent_schema TEXT, parent_name TEXT);

CREATE TABLE IF NOT EXISTS blind_spots (
    file_id INTEGER NOT NULL, line INTEGER, kind TEXT, detail TEXT);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Kept separate so a bulk load can drop them and rebuild once at the end.
_INDEXES = [
    ("ix_files_member", "files(member)"),
    ("ix_files_kind", "files(kind)"),
    ("ix_prog_name", "programs(name)"),
    ("ix_prog_file", "programs(file_id)"),
    ("ix_ref_table", "table_refs(table_name)"),
    ("ix_ref_qual", "table_refs(schema, table_name)"),
    ("ix_ref_file", "table_refs(file_id)"),
    ("ix_copy_member", "copy_refs(member)"),
    ("ix_copy_file", "copy_refs(file_id)"),
    ("ix_call_callee", "calls(callee)"),
    ("ix_call_file", "calls(file_id)"),
    ("ix_job_file", "jobs(file_id)"),
    ("ix_step_pgm", "steps(resolved_pgm)"),
    ("ix_step_proc", "steps(proc)"),
    ("ix_step_file", "steps(file_id)"),
    ("ix_dd_file", "dds(file_id)"),
    ("ix_dd_lookup", "dds(lookup_key)"),
    ("ix_dd_dsname", "dds(dsname)"),
    ("ix_obj_name", "objects(name)"),
    ("ix_obj_parent", "objects(parent_name)"),
    ("ix_obj_file", "objects(file_id)"),
    ("ix_blind_kind", "blind_spots(kind)"),
    ("ix_blind_file", "blind_spots(file_id)"),
]

_CHILD_TABLES = ("programs", "table_refs", "copy_refs", "calls", "jobs",
                 "steps", "dds", "objects", "blind_spots")

_INSERTS = {
    "programs": "INSERT INTO programs(file_id,line,name) VALUES(?,?,?)",
    "table_refs": "INSERT INTO table_refs(file_id,line,schema,table_name,access,"
                  "stmt,confidence,snippet,via) VALUES(?,?,?,?,?,?,?,?,?)",
    "copy_refs": "INSERT INTO copy_refs(file_id,line,member,kind) VALUES(?,?,?,?)",
    "calls": "INSERT INTO calls(file_id,line,callee,dynamic) VALUES(?,?,?,?)",
    "jobs": "INSERT INTO jobs(file_id,line,name) VALUES(?,?,?)",
    "steps": "INSERT INTO steps(file_id,line,job,step,seq,pgm,proc,resolved_pgm) "
             "VALUES(?,?,?,?,?,?,?,?)",
    "dds": "INSERT INTO dds(file_id,line,step,dd,dsn,dsname,lookup_key,is_member,disp) VALUES(?,?,?,?,?,?,?,?,?)",
    "objects": "INSERT INTO objects(file_id,line,schema,name,obj_type,"
               "parent_schema,parent_name) VALUES(?,?,?,?,?,?,?)",
    "blind_spots": "INSERT INTO blind_spots(file_id,line,kind,detail) "
                   "VALUES(?,?,?,?)",
    "files": "INSERT INTO files(id,path,member,kind,size,mtime_ns,line_count,"
             "truncated,error,scanned_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
}


def member_name(path: str) -> str:
    """PDS member name for a file - how JCL and COPY statements refer to it."""
    return os.path.splitext(os.path.basename(path))[0].upper()


class Store:
    def __init__(self, db_path: str, fast: bool = False):
        self.db_path = db_path
        # timeout: an analyst querying the index must not make a concurrent
        # re-index fail outright.
        self.conn = sqlite3.connect(db_path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row

        # An index built by an older version has a different column layout.
        # Reading it would silently produce wrong answers, which is worse than
        # refusing, so the caller is told to re-index instead.
        self.stale_schema = self._detect_stale_schema()

        self.conn.executescript(_TABLES)
        self.conn.execute("PRAGMA journal_mode=WAL")
        if fast:
            # Index builds are re-runnable; durability during the crawl is not
            # worth the ~10x write cost on 100k files.
            self.conn.execute("PRAGMA synchronous=OFF")
            self.conn.execute("PRAGMA cache_size=-262144")
            self.conn.execute("PRAGMA temp_store=MEMORY")
        else:
            self.create_indexes()
        self.set_meta("schema_version", str(SCHEMA_VERSION))
        # Without this commit the open write transaction from set_meta is held
        # for the life of the connection, and every read-only Store blocks all
        # writers - so opening a query session would break indexing.
        self.conn.commit()

        self._buf: dict[str, list[tuple]] = {t: [] for t in _INSERTS}
        self._buffered = 0
        self._next_id = (self.conn.execute(
            "SELECT COALESCE(MAX(id),0) FROM files").fetchone()[0]) + 1

    def _detect_stale_schema(self) -> bool:
        try:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        except sqlite3.OperationalError:
            return False                       # brand new database
        if row is None:
            return bool(self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='files'").fetchone())
        return row["value"] != str(SCHEMA_VERSION)

    def rebuild_schema(self) -> None:
        """Drop everything and start clean, for a schema-version upgrade."""
        self.drop_indexes()
        for table in ("files", *_CHILD_TABLES):
            self.conn.execute(f"DROP TABLE IF EXISTS {table}")
        self.conn.executescript(_TABLES)
        self.set_meta("schema_version", str(SCHEMA_VERSION))
        self.conn.commit()
        self.stale_schema = False
        self._next_id = 1

    # ------------------------------------------------------------ indexes

    def create_indexes(self) -> None:
        for name, spec in _INDEXES:
            self.conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {spec}")
        self.conn.commit()

    def drop_indexes(self) -> None:
        for name, _ in _INDEXES:
            self.conn.execute(f"DROP INDEX IF EXISTS {name}")
        self.conn.commit()

    # ------------------------------------------------------------ meta

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # ------------------------------------------------------------ incremental

    def existing_signatures(self) -> dict[str, tuple[int, int]]:
        """``{path: (size, mtime_ns)}`` so an unchanged file is never re-read.

        This is what makes a re-index over SMB take minutes instead of hours.
        Files recorded with an error are omitted, so they get retried.
        """
        return {r["path"]: (r["size"], r["mtime_ns"])
                for r in self.conn.execute(
                    "SELECT path, size, mtime_ns FROM files WHERE error=''")}

    def all_paths(self) -> set[str]:
        return {r["path"] for r in self.conn.execute("SELECT path FROM files")}

    def delete_paths(self, paths: list[str]) -> int:
        """Remove files and all their facts. Returns how many were removed."""
        removed = 0
        for batch in (paths[i:i + 400] for i in range(0, len(paths), 400)):
            marks = ",".join("?" * len(batch))
            ids = [r[0] for r in self.conn.execute(
                f"SELECT id FROM files WHERE path IN ({marks})", batch)]
            self._delete_ids(ids)
            self.conn.execute(f"DELETE FROM files WHERE path IN ({marks})", batch)
            removed += len(ids)
        self.conn.commit()
        return removed

    def _delete_ids(self, ids: list[int]) -> None:
        for batch in (ids[i:i + 400] for i in range(0, len(ids), 400)):
            marks = ",".join("?" * len(batch))
            for table in _CHILD_TABLES:
                self.conn.execute(
                    f"DELETE FROM {table} WHERE file_id IN ({marks})", batch)

    # ------------------------------------------------------------ bulk write

    def add(self, facts: FileFacts) -> None:
        """Buffer one file's facts. Caller must have removed any prior row."""
        file_id = self._next_id
        self._next_id += 1

        self._buf["files"].append((
            file_id, facts.path, member_name(facts.path), facts.kind, facts.size,
            facts.mtime_ns, facts.line_count, int(facts.truncated), facts.error,
            int(time.time())))
        n = 1

        for table, rows in (("programs", facts.programs),
                            ("table_refs", facts.table_refs),
                            ("copy_refs", facts.copy_refs),
                            ("calls", facts.calls),
                            ("jobs", facts.jobs),
                            ("steps", facts.steps),
                            ("dds", facts.dds),
                            ("objects", facts.objects),
                            ("blind_spots", facts.blind_spots)):
            if rows:
                buf = self._buf[table]
                for r in rows:
                    buf.append((file_id, *r))
                n += len(rows)

        self._buffered += n
        if self._buffered >= FLUSH_ROWS:
            self.flush()

    def flush(self) -> None:
        # files first: child rows reference its id.
        for table in ("files", *_CHILD_TABLES):
            rows = self._buf[table]
            if rows:
                self.conn.executemany(_INSERTS[table], rows)
                rows.clear()
        self._buffered = 0
        self.conn.commit()

    def commit(self) -> None:
        self.flush()

    def optimize(self) -> None:
        self.flush()
        self.create_indexes()
        self.conn.execute("ANALYZE")
        self.conn.commit()

    def close(self) -> None:
        self.flush()
        self.conn.close()

    # ------------------------------------------------------------ stats

    def stats(self) -> dict[str, int]:
        out = {}
        for t in ("files",) + _CHILD_TABLES:
            out[t] = self.conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        out["distinct_tables"] = self.conn.execute(
            "SELECT COUNT(DISTINCT schema||'.'||table_name) c FROM table_refs"
        ).fetchone()["c"]
        return out

    def kind_counts(self) -> list[tuple[str, int]]:
        return [(r["kind"], r["c"]) for r in self.conn.execute(
            "SELECT kind, COUNT(*) c FROM files GROUP BY kind ORDER BY c DESC")]
