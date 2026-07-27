"""Resolve ``import mfdep`` before the tests are collected.

mfdep is not shipped from this repo. It is maintained separately and lands in
the using system at ``mainframe_tracer/src/network_drive/mfdep``. The copy in
``mfdep/`` here is gitignored and exists only so these tests can exercise the
consumer template against a real package.

The search order mirrors ``_ensure_src_on_path`` in
``integration/table_dependencies.py`` on purpose: if the template's bootstrap
would fail to find mfdep in a given layout, collection here fails the same way
rather than quietly succeeding through some other route.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_WORK_SUBPATH = Path("mainframe_tracer") / "src" / "network_drive"


def _put_on_path(pkg_parent: Path) -> None:
    sys.path.insert(0, str(pkg_parent))
    if pkg_parent.name == "network_drive":
        sys.path.insert(0, str(pkg_parent.parent))


def _resolve_mfdep() -> None:
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

    raise ImportError(
        "Cannot find the mfdep package. These tests need it either beside this "
        "repo at ../mainframe_tracer/src/network_drive/mfdep, in a local "
        "mfdep/ directory, or on $MFDEP_HOME."
    )


_resolve_mfdep()
