# table-dependencies — tracer_agent consumer template for mfdep

This repository holds the **consumer side** of the mfdep integration: the
adapter the tracer_agent uses to ask "what depends on this DB2 table", plus the
regression suite that proves the call path still works.

**mfdep itself is not shipped from here.** It is maintained as its own package
and lands in the using system inside the tracer's `network_drive` package:

```
<tracer checkout>/src/network_drive/mfdep/
```

## What's in it

```
integration/
  table_dependencies.py   the template - copy to src/tracer_agent/ in the tracer
  README.md               what the template is and how to wire it up
tests/
  conftest.py             resolves `import mfdep` before collection
  make_fixtures.py        generates a column-exact sample library
  test_mfdep.py           regression tests, one per failure mode
```

## How mfdep is found

Both the template and the test suite use the same short list of directories, in
order. The first one containing `mfdep/` goes on the front of `sys.path`:

| # | Directory | Why |
| --- | --- | --- |
| 1 | `$MFDEP_HOME` | explicit override, wins over everything |
| 2 | `../mainframe-tracer/src/network_drive` | where the package actually lives |
| 3 | `./mfdep` | local testing copy, gitignored |

The template's list differs only in that it also checks `../network_drive`
relative to itself, because at work it has been copied into `src/tracer_agent/`
and the package is one level up. When the chosen directory is `network_drive`,
its parent goes on the path too, so `from network_drive import DB_DIR` keeps
working.

**This is a list, not a search.** Nothing walks the tree, globs for candidates,
or infers a layout. If none of the three is right on a given machine, set
`MFDEP_HOME` to the directory *containing* `mfdep/` — that is the only knob, and
it is meant to be used rather than worked around. When nothing matches, the
error names every directory that was tried.

Going on the *front* of `sys.path` matters: it beats anything installed under
the same name, including the version shim the tracer installs, which exports
only `__version__`. A shim already imported is dropped from `sys.modules` so the
path change takes effect.

## Running the tests

```bash
python -m pytest tests/test_mfdep.py -q
```

They need mfdep reachable by one of the routes above. Two of them
(`TestZeroInstallConsumer`, `TestZeroInstallConsumerNested`) build a throwaway
project tree, vendor the package into it, and load the template in a subprocess
run with `-S -E` — no site-packages, no `PYTHON*` env — so an installed mfdep
cannot mask a broken bootstrap. They cover both the sibling and nested layouts.

## The local `mfdep/` directory

If present, it is a **local testing copy only**: gitignored here, and its own
independent git repository with no remote. It exists so the suite above can run
against a real package. Nothing in it is published from this repo, and git does
not recurse into a nested repository, so it cannot ride along on a push.

Its own README documents the package — the CLI, the library API, what it can and
cannot see.
