"""Convenience API for using mfdep from another Python program.

The CLI is a thin wrapper over these; anything it can do is available here.
For questions this does not cover, the index is a plain SQLite database and
``open_index(...).conn`` is a normal connection - see the module docstring in
store.py for the schema.

    from mfdep import index, query

    if __name__ == "__main__":                  # see note below
        index([r"\\\\fileserv\\mfarchive\\PROD"], db="prod.db")
        res = query("PRODDB.CUSTOMER", db="prod.db")
        for r in res.refs:
            print(r.access, r.member, r.line, r.stmt)

**The __main__ guard matters on Windows.** ``index()`` uses a process pool, and
Windows spawns workers by re-importing the calling module. Without the guard,
each worker re-runs your script's top level and spawns its own workers. Pass
``workers=1`` to run entirely in-process and avoid the issue - see below.
"""

from __future__ import annotations

import os

from .graph import Analyzer, Result
from .scan import ScanOptions, run_index
from .store import Store
from .vendor import load_overrides

__all__ = ["index", "query", "open_index", "tables", "Result", "Store", "Analyzer"]


def index(roots, db: str = "mfdep.db", *, workers: int = 0, full: bool = False,
          max_file_mb: int = 256, include=(), exclude=(), quiet: bool = True,
          prune_missing: bool = True) -> dict:
    """Crawl ``roots`` and build (or incrementally refresh) the index at ``db``.

    Returns a dict of counts: files, table_refs, steps, blind_spots, seconds...

    ``workers=1`` parses serially in the calling process, with no process pool.
    Use it when embedding mfdep somewhere a pool is awkward - inside a web
    request, a notebook, or any module without a ``__main__`` guard.
    """
    if isinstance(roots, (str, os.PathLike)):
        roots = [roots]
    return run_index(ScanOptions(
        roots=[os.path.abspath(os.fspath(r)) for r in roots],
        db_path=os.fspath(db), workers=workers, full=full,
        max_file_mb=max_file_mb, include=tuple(include), exclude=tuple(exclude),
        quiet=quiet, prune_missing=prune_missing))


def query(table: str, db="mfdep.db", *, min_confidence: int = 0,
          include_read: bool = True, data_hops: int = 1,
          vendor_file: str | None = None) -> Result:
    """Return the full dependency :class:`~mfdep.graph.Result` for one table.

    ``db`` may be a path or an already-open :class:`Store`. Pass an open Store
    when asking about many tables - reopening it per call re-reads the SQLite
    header and throws away the page cache.
    """
    if vendor_file:
        load_overrides(vendor_file)

    if isinstance(db, Store):
        return Analyzer(db).analyze(table, min_confidence=min_confidence,
                                    include_read=include_read,
                                    data_hops=data_hops)
    with open_index(db) as store:
        return Analyzer(store).analyze(table, min_confidence=min_confidence,
                                       include_read=include_read,
                                       data_hops=data_hops)


def open_index(db: str = "mfdep.db") -> Store:
    """Open an existing index for querying. Usable as a context manager.

    Raises FileNotFoundError if the index does not exist, rather than silently
    creating an empty one and reporting that nothing depends on the table.
    """
    path = os.fspath(db)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"no mfdep index at {path!r} - run mfdep.index([...], db={path!r}) first")
    store = Store(path)
    if store.stale_schema:
        store.close()
        raise RuntimeError(
            f"{path!r} was built by an older version of mfdep and its layout has "
            f"changed; rebuild it with mfdep.index([...], db={path!r}, full=True)")
    return store


def tables(db="mfdep.db", *, like: str = "%", min_refs: int = 1,
           limit: int = 1000) -> list[dict]:
    """List indexed tables with reference counts, most-referenced first."""
    def _run(store: Store) -> list[dict]:
        return [dict(r) for r in store.conn.execute(
            "SELECT schema, table_name, COUNT(*) refs, "
            "SUM(access='WRITE') writes, SUM(access='READ') reads, "
            "COUNT(DISTINCT file_id) files "
            "FROM table_refs WHERE table_name LIKE ? "
            "GROUP BY schema, table_name HAVING refs >= ? "
            "ORDER BY refs DESC LIMIT ?", (like.upper(), min_refs, limit))]

    if isinstance(db, Store):
        return _run(db)
    with open_index(db) as store:
        return _run(store)
