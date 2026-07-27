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
    At work the package ships inside network_drive, at
    ``src/network_drive/mfdep/``; ``_ensure_src_on_path`` below puts that
    directory on ``sys.path`` so the bare import still resolves with no install.
  * Take the index PATH from the ``network_drive`` package, which owns where the
    shared ``mfdep.db`` lives at work. Off the work machine (no network_drive
    installed) it falls back to ``$MFDEP_DB`` or ``./mfdep.db`` so the adapter
    still runs against a local test index.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict


#: Where mfdep sits inside a tracer checkout, relative to that checkout's parent.
_WORK_SUBPATH = Path("mainframe_tracer") / "src" / "network_drive"


def _put_on_path(pkg_parent: Path) -> None:
    """Add the directory that holds ``mfdep/`` to ``sys.path``.

    When that directory is the network_drive package itself, its parent goes on
    too, so ``from network_drive import DB_DIR`` keeps working alongside the
    bare ``import mfdep``.
    """
    sys.path.insert(0, str(pkg_parent))
    if pkg_parent.name == "network_drive":
        sys.path.insert(0, str(pkg_parent.parent))


def _ensure_src_on_path() -> None:
    """Make ``import mfdep`` work straight from a download, with no install.

    Tries a normal import first, so a pip-installed mfdep (or an already
    configured path) wins and this does nothing. Only when that fails does it
    look for a vendored copy, in the three layouts that occur in practice. At
    each level going up from this file:

      * ``<dir>/mfdep/`` - the package sits at the root beside the template,
        which is the local test checkout.
      * ``<dir>/network_drive/mfdep/`` - the work layout, where mfdep ships
        inside the network_drive package. This is the one that matches when the
        template has been copied to ``src/tracer_agent/`` at work.
      * ``<dir>/mainframe_tracer/src/network_drive/mfdep/`` - a tracer checkout
        sitting beside this repo rather than above it. Walking up alone never
        finds this, because it is a sibling branch of the tree, not an ancestor.

    The nearest match wins, so a local copy takes precedence over a sibling
    checkout. ``$MFDEP_HOME``, if set to the directory containing ``mfdep/``,
    overrides the search entirely - the escape hatch for a layout none of the
    three cover.
    """
    try:
        import mfdep  # noqa: F401
        return
    except ImportError:
        pass

    override = os.environ.get("MFDEP_HOME")
    if override and (Path(override) / "mfdep" / "__init__.py").is_file():
        _put_on_path(Path(override).resolve())
        return

    for parent in Path(__file__).resolve().parents:
        for candidate in (parent, parent / "network_drive", parent / _WORK_SUBPATH):
            if (candidate / "mfdep" / "__init__.py").is_file():
                _put_on_path(candidate)
                return
    # Not found anywhere; the import below raises a clear ImportError.


_ensure_src_on_path()

import mfdep  # noqa: E402 - deliberately after the path bootstrap

try:
    # At work: the dedicated network-share package owns the db location, so a
    # move of the share is a one-line change there, not here. (Importable via
    # the same src root the bootstrap above put on the path.)
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
