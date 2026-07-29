"""Resolve ``import mfdep`` before the tests are collected.

mfdep is not shipped from this repo. It is maintained separately and lands in
the using system inside the tracer's ``src/network_drive/`` package. The copy
in ``mfdep/`` here is gitignored and exists only so these tests can exercise
the consumer template against a real package.

The search order mirrors ``_ensure_src_on_path`` in
``integration/table_dependencies.py`` on purpose: if the template's bootstrap
would fail to find mfdep in a given layout, collection here fails the same way
rather than quietly succeeding through some other route. Rewriting these tests
to import ``network_drive.mfdep`` directly would defeat that - the consumer
imports mfdep by package name, so the tests have to as well.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

#: A submodule that only the real package has. Used to tell it apart from a
#: shim - the tracer installs one at src/mfdep/__init__.py that re-exports
#: __version__ and nothing else.
_PROBE_SUBMODULE = "mfdep.store"

#: How far up to look for a tracer checkout sitting beside this file's tree.
#: Bounded because that step lists directories, and walking to the filesystem
#: root is slow on a synced or network-mounted profile.
_SIBLING_SCAN_DEPTH = 4


def _candidate_dirs(start: Path):
    """Directories that could hold ``mfdep/``, nearest first.

    Deliberately does not hard-code the tracer checkout's directory name - it
    is spelled differently on different machines - and matches on the
    ``src/network_drive`` shape instead.
    """
    for depth, parent in enumerate(start.parents):
        yield parent                                  # <dir>/mfdep/
        yield parent / "network_drive"                # <dir>/network_drive/mfdep/
        if depth < _SIBLING_SCAN_DEPTH:
            try:                                      # <sibling>/src/network_drive/mfdep/
                siblings = sorted(parent.glob("*/src/network_drive"))
            except OSError:                           # unreadable dir on the way up
                siblings = []
            for network_drive in siblings:
                yield network_drive


def _put_on_path(pkg_parent: Path) -> None:
    sys.path.insert(0, str(pkg_parent))
    if pkg_parent.name == "network_drive":
        sys.path.insert(0, str(pkg_parent.parent))
    importlib.invalidate_caches()


def _real_mfdep_importable() -> bool:
    """True only if a plain ``import mfdep`` lands on the *usable* package.

    Import success alone is not enough. The tracer is editable-installed and
    ships a top-level ``mfdep`` shim that re-exports ``__version__`` and has no
    submodules, so the import succeeds and every ``from mfdep.graph import ...``
    afterwards fails. Probing for a submodule distinguishes the two.
    """
    try:
        import mfdep  # noqa: F401
    except ImportError:
        return False
    try:
        return importlib.util.find_spec(_PROBE_SUBMODULE) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def _forget_mfdep() -> None:
    """Drop a shim from ``sys.modules`` so the search result imports cleanly.

    Without this, putting the real package on ``sys.path`` changes nothing:
    ``import mfdep`` returns the already-cached shim rather than re-resolving.
    """
    for name in [n for n in sys.modules
                 if n == "mfdep" or n.startswith("mfdep.")]:
        del sys.modules[name]


def _resolve_mfdep() -> None:
    if _real_mfdep_importable():
        return
    # A shim answered the import. Forget it, or the search below is pointless.
    _forget_mfdep()

    override = os.environ.get("MFDEP_HOME")
    if override and (Path(override) / "mfdep" / "__init__.py").is_file():
        _put_on_path(Path(override).resolve())
        return

    for candidate in _candidate_dirs(Path(__file__).resolve()):
        if (candidate / "mfdep" / "__init__.py").is_file():
            _put_on_path(candidate)
            return

    raise ImportError(
        "Cannot find the mfdep package. These tests need it in a local mfdep/ "
        "directory, inside a network_drive package above them, in a tracer "
        "checkout beside this repo at <tracer>/src/network_drive/mfdep, or on "
        "$MFDEP_HOME. Set MFDEP_HOME to the directory *containing* mfdep/."
    )


_resolve_mfdep()
