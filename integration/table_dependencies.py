"""Table-dependency lookups for the tracer_agent, backed by the mfdep library.

TEMPLATE - copy this to ``src/tracer_agent/table_dependencies.py`` in the CAST
project. It is kept in the mfdep repo only so the mfdep-call path can be tested
against a local index; it is not part of the mfdep package.

The dependency-finding logic is no longer maintained in the CAST project. This
module is a thin adapter over the standalone ``mfdep`` package: it resolves the
index location and calls ``mfdep.query()``, the same way the tracer_agent calls
``network_drive.mf_fetch.fetch_artifact`` instead of reimplementing the fetch.

Two rules make "test here, run at work" work cleanly:

  * Import mfdep by PACKAGE NAME (``import mfdep``), never by a path into a
    checkout. Whatever ``mfdep`` is installed in the environment wins - the work
    build at work, the dev copy in a test venv. This file does not care which.
  * Take the index PATH from the ``network_drive`` package, which owns where the
    shared ``mfdep.db`` lives at work. Off the work machine (no network_drive
    installed) it falls back to ``$MFDEP_DB`` or ``./mfdep.db`` so the adapter
    still runs against a local test index.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import mfdep

try:
    # At work: the dedicated network-share package owns the db location, so a
    # move of the share is a one-line change there, not here.
    from network_drive import DB_DIR

    MFDEP_DB_PATH: str = str(DB_DIR / "mfdep.db")
except Exception:  # pragma: no cover - network_drive is absent off the work box
    # Local / testing: point at $MFDEP_DB, else mfdep.db in the working dir.
    MFDEP_DB_PATH = os.environ.get("MFDEP_DB", "mfdep.db")


def get_table_dependencies(
    table: str,
    *,
    db_path: str = MFDEP_DB_PATH,
    include_read: bool = True,
    data_hops: int = 1,
):
    """Return the full mfdep ``Result`` for one table.

    ``.refs`` (every read/write/DDL/utility reference), ``.programs``,
    ``.jobs``, ``.steps``, ``.data_links``, ``.blind_spots`` and ``.notes`` are
    all on the returned object; see the mfdep README. Raises
    ``FileNotFoundError`` if the index has not been built at ``db_path``.
    """
    return mfdep.query(
        table, db=db_path, include_read=include_read, data_hops=data_hops
    )


def get_table_dependencies_json(
    table: str, *, db_path: str = MFDEP_DB_PATH, **kwargs: Any
) -> str:
    """The same result as a JSON string, for serialization into the pipeline."""
    from mfdep.report import json_report

    return json_report(get_table_dependencies(table, db_path=db_path, **kwargs))


def table_dependency_summary(
    table: str, *, db_path: str = MFDEP_DB_PATH
) -> Dict[str, Any]:
    """A compact, already-serializable summary for a caller that only needs
    counts and the at-risk job/program lists rather than every reference."""
    res = get_table_dependencies(table, db_path=db_path)
    writers = sorted({r.member for r in res.refs if r.access == "WRITE"})
    return {
        "table": res.spec,
        "references": len(res.refs),
        "writers": writers,
        "programs": sorted(res.programs),
        "jobs": sorted(res.jobs),
        "blind_spots": len(res.blind_spots),
        "notes": res.notes,
    }
