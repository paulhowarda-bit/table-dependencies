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

#: Marks a package that is new enough to be usable as a library. Deliberately
#: not store.py, which exists in the older CLI-only builds too - probing for
#: that accepts a stale copy and defers the failure to 20-odd broken tests.
_PROBE_SUBMODULE = "mfdep.api"

#: The library entry points the consumer template and these tests both call.
_REQUIRED_ATTRS = ("query", "index", "open_index", "tables")


def _why_unusable(module) -> str:
    """Empty string if this mfdep is usable, else the reason it is not."""
    missing = [a for a in _REQUIRED_ATTRS if not hasattr(module, a)]
    if missing:
        return "missing " + ", ".join("mfdep." + a + "()" for a in missing)
    try:
        if importlib.util.find_spec(_PROBE_SUBMODULE) is None:
            return "no " + _PROBE_SUBMODULE + " submodule"
    except (ImportError, AttributeError, ValueError):
        return "no " + _PROBE_SUBMODULE + " submodule"
    return ""

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


#: Anything importable but unusable, recorded so the error can name it.
_rejected: list = []


def _real_mfdep_importable() -> bool:
    """True only if a plain ``import mfdep`` lands on a *usable* package.

    Import success alone is not enough. Two things answer to the name and are
    not the package this repo needs:

      * the version shim the tracer installs at ``src/mfdep/__init__.py``,
        which re-exports ``__version__`` and has no submodules at all;
      * a stale build from before mfdep grew a library API, which has the
        parsers and the store but no ``api``, ``profiling`` or
        ``logging_setup``, so ``mfdep.query()`` does not exist.

    Both fail far from the cause if accepted here, so reject and keep looking.
    """
    try:
        import mfdep  # noqa: F401
    except ImportError:
        return False
    reason = _why_unusable(mfdep)
    if reason:
        _rejected.append((getattr(mfdep, "__file__", "?"),
                          getattr(mfdep, "__version__", "unknown"), reason))
        return False
    return True


def _forget_mfdep() -> None:
    """Drop a shim from ``sys.modules`` so the search result imports cleanly.

    Without this, putting the real package on ``sys.path`` changes nothing:
    ``import mfdep`` returns the already-cached shim rather than re-resolving.
    """
    for name in [n for n in sys.modules
                 if n == "mfdep" or n.startswith("mfdep.")]:
        del sys.modules[name]


def _has_library_api(pkg_dir: Path) -> bool:
    """Cheap staleness screen for a candidate, done on the filesystem.

    api.py is what the library entry points live in, so a build without it is
    the pre-library CLI-only mfdep. Checked by looking rather than importing,
    so a stale copy is skipped without leaving half-imported state behind.
    """
    return (pkg_dir / "__init__.py").is_file() and (pkg_dir / "api.py").is_file()


def _all_candidates():
    override = os.environ.get("MFDEP_HOME")
    if override:
        yield Path(override)
    yield from _candidate_dirs(Path(__file__).resolve())


def _not_found_message(stale) -> str:
    lines = ["Cannot find a usable mfdep package."]
    for path, version, reason in _rejected:
        lines.append("  rejected (importable): %s [%s] - %s"
                     % (path, version, reason))
    for path in stale:
        lines.append("  rejected (stale, no api.py): %s" % path)
    if _rejected or stale:
        lines.append("")
        lines.append("Those are older or partial builds. Deploy the current "
                     "mfdep package over them rather than adding exports by "
                     "hand - query/index/open_index/tables come from api.py, "
                     "which those copies do not have.")
    lines.append("")
    lines.append("Looked for mfdep/ in: a local directory beside this repo, a "
                 "network_drive package above it, <tracer>/src/network_drive/ "
                 "beside it, and $MFDEP_HOME. Set MFDEP_HOME to the directory "
                 "*containing* mfdep/.")
    return "\n".join(lines)


def _resolve_mfdep() -> None:
    if _real_mfdep_importable():
        return
    # Something unusable answered the import. Forget it, or the search below
    # is pointless: `import mfdep` would just return the cached module.
    _forget_mfdep()

    stale = []
    for candidate in _all_candidates():
        pkg = candidate / "mfdep"
        if not (pkg / "__init__.py").is_file():
            continue
        if not _has_library_api(pkg):
            stale.append(str(pkg))
            continue
        _put_on_path(candidate.resolve())
        return

    raise ImportError(_not_found_message(stale))


_resolve_mfdep()
