# integration/ — consumer template (not part of the mfdep package)

`table_dependencies.py` here is a **template for the CAST project**, not code
that ships with mfdep. It is kept in this repo only so its mfdep-call path can
be exercised against a local test index.

## What it is

The dependency-finding logic is no longer maintained in the CAST project. The
consumer (`src/tracer_agent/table_dependencies.py`) should be a thin adapter
that calls the standalone `mfdep` library — the same shape as
`network_drive.mf_fetch.fetch_artifact` being imported instead of reimplemented.

## To use at work

1. Install the `mfdep` package into the work environment (`pip install mfdep`,
   or `pip install -e .` from this repo's checkout — whichever the work build
   uses). The adapter imports it by package name, so the work-installed copy is
   what binds; the copy in this repo is only for local testing.
2. Copy `table_dependencies.py` to `src/tracer_agent/table_dependencies.py`.
3. Ensure `network_drive` exposes `DB_DIR` (per the reorg plan). The adapter
   reads `DB_DIR / "mfdep.db"` for the index location.

## To test off the work machine

`network_drive` won't import, so the adapter falls back to `$MFDEP_DB` (else
`./mfdep.db`). Build a local index and point at it:

```bash
python mfdep.py index tests/fixtures --db test.db
MFDEP_DB=test.db python -c "import sys; sys.path.insert(0,'integration'); \
  import table_dependencies as td; print(td.table_dependency_summary('PRODDB.CUSTOMER'))"
```

## Note on the function names

This is a fresh template — the original `table_dependencies.py` is not in this
repo, so its exact function name/signature could not be preserved. If the
tracer_agent's callers expect a specific entry point, align `get_table_dependencies`
(and friends) to it, or paste the current file and it can be matched exactly.
