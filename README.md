# table-dependencies — tracer_agent consumer template for mfdep

This repository holds the **consumer side** of the mfdep integration: the
adapter the tracer_agent uses to ask "what depends on this DB2 table", plus the
regression suite that proves the call path still works.

**mfdep itself is not shipped from here.** It is maintained as its own package
and lands in the using system at:

```
mainframe_tracer/src/network_drive/mfdep/
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

Both the template and the test suite resolve mfdep the same way, so a layout
that breaks the consumer breaks the tests too rather than passing by luck.

An ordinary `import mfdep` is tried first, so anything already installed or
already on the path wins and no bootstrapping happens. Only when that fails does
the search run, checking each directory from the file's own location upward:

| Layout | Where it applies |
| --- | --- |
| `<dir>/mfdep/` | a local copy sitting beside the template |
| `<dir>/network_drive/mfdep/` | the tracer at work, once the template has been copied to `src/tracer_agent/` |
| `<dir>/mainframe_tracer/src/network_drive/mfdep/` | a tracer checkout beside this repo rather than above it |

The nearest match wins. The third case matters because walking up the tree never
reaches it on its own — a sibling checkout is a different branch of the tree, not
an ancestor.

Set `MFDEP_HOME` to the directory *containing* `mfdep/` to override the search
entirely, for any layout the three above don't cover.

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
