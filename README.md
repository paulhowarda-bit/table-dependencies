# mfdep — DB2 table dependency finder for mainframe artifact libraries

Finds every dependency of a DB2 table across 50,000–100,000 exported mainframe
artifacts on a network drive: JCL, PROCs, COBOL, copybooks, DCLGENs, control
cards and stored procedures.

Pure Python 3.9+ standard library — no dependencies, and nothing needs
installing to use it.

```bash
python mfdep.py index \\fileserv\mfarchive\PROD --db prod.db
python mfdep.py query PRODDB.CUSTOMER --db prod.db --html cust.html
```

It is also a library. `pip install -e .` additionally puts an `mfdep` command
on PATH and makes it importable from anywhere — see
[Using it from Python](#using-it-from-python):

```python
import mfdep
res = mfdep.query("PRODDB.CUSTOMER", db="prod.db")
print(len(res.refs), "references,", len(res.jobs), "jobs at risk")
```

---

## Why it is built this way

**Index once, query many.** A single grep pass over 100k members on an SMB
share takes minutes to hours — and you need one pass *per table*. `index`
extracts facts into a local SQLite database once; every `query` after that
answers in well under a second. Re-indexing is incremental (size + mtime), so
a nightly refresh only reads what changed.

**Reference by grammar, not by name.** Searching for the string `CUSTOMER`
fails in both directions:

| Failure | Example | Handled by |
|---|---|---|
| Comment | `* UPDATE PRODDB.GHOST` in col 7 | COBOL column/indicator handling |
| Ident field | `CUSTOMER` in cols 73–80 | fixed-format column extraction |
| Literal | `WHERE N = 'FROM PRODDB.GHOST'` | literal masking before scan |
| Prefix collision | `CUSTOMER_HIST` ≠ `CUSTOMER` | exact identifier boundaries |
| CTE | `WITH CUSTOMER AS (...)` | CTE name subtraction |
| Table function | `FROM TABLE(fn(:x))` | keyword blocklist |
| Continuation | `PRODDB.CUS` / `-TOMER` split at col 72 | continuation rejoining |
| Via a view | program only names `V_ACTIVE_CUST` | view/synonym/alias following |
| Via JCL | job names `IKJEFT01`, not the program | SYSTSIN `RUN PROGRAM()` |
| Via utility | `LOAD DATA INTO TABLE` — no SQL at all | DSNUTILB control-card parser |
| Via a cataloged deck | `//SYSIN DD DSN=PROD.CNTL(LOADCUST)` | DSN → member resolution |
| Via shared data | sort deck names no table, but reads the unload | dataset lineage |

Instead of searching *for* the table, mfdep enumerates *every* table reference
in every artifact by the SQL keyword that introduces it (`FROM`, `INSERT INTO`,
`JOIN`, `REFERENCES`, …). That gives exact name boundaries **and** classifies
each hit as read / write / DDL / utility in the same pass — which is what turns
"what mentions this string" into "what breaks if I change this table".

---

## What it finds

**Direct SQL** — `SELECT`, `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `TRUNCATE`,
cursors, `LOCK TABLE`, `CREATE/DROP/ALTER TABLE`, `CREATE INDEX ... ON`,
`REFERENCES` (foreign keys), `GRANT`, `DECLARE ... TABLE` (DCLGEN), in COBOL
`EXEC SQL` blocks, `.sql` stored procedures and DDL scripts.

**Transitive JCL chain** — the answer to *which jobs fail at 03:00*:

```
DCLGEN copybook → copied by → program → called by → program
                → run by → PROC step → invoked by → JCL step → the job
```

Including the two idioms that hide the link from grep entirely:
- `PGM=IKJEFT01` with the real program in SYSTSIN `RUN PROGRAM(MYPGM)`
- `PGM=DSNUTILB` with the table in SYSIN control cards

**Utilities and control cards** — `LOAD`, `UNLOAD`, `REORG`, `RUNSTATS`,
`COPY`, `RECOVER`, `CHECK DATA`, `REPAIR`, `LISTDEF`, `TEMPLATE`, and BIND
decks. Tablespace-level utilities are linked back to the table through
`CREATE TABLE ... IN db.ts`, so a `REORG TABLESPACE` shows up as a dependency
of the table inside it.

**Cataloged control decks.** Most shops do not inline their utility cards; they
catalog them and point at them:

```jcl
//LOADSTEP EXEC PGM=DSNUTILB,PARM='DB2P,LOADCUST'
//SYSIN    DD DSN=PROD.CNTL(LOADCUST),DISP=SHR     <- the LOAD lives here
```

The DSN is resolved to the member on the share, so the job shows up even though
its JCL never names the table. Resolution matches on the member name and then
*verifies the dataset name*, because member names collide constantly — every
shop has a dozen members called `LOAD01`, and wiring a job to the wrong deck is
worse than not wiring it. A member-name hit whose library does not match is
reported as `UNRESOLVED-DECK-REF` rather than silently accepted or dropped.

**Dataset lineage** (`--data-hops`, default 1). A sort or merge deck names no
table at all, so it is not a SQL dependency — but it reads the dataset the
`UNLOAD` wrote, and its `SORT FIELDS=(1,10,CH,A)` hard-codes the table's
physical layout exactly as much as the LOAD deck's `POSITION(1) CHAR(10)`.
Widen a column and both break silently, with no SQL anywhere to search for.
Steps sharing a dataset with a table-touching step are reported in their own
**DATA-LINKED STEPS** section, kept separate from real table dependencies.
Datasets referenced by more than 40 steps are treated as shared plumbing and
not followed; that is stated in the notes rather than silently skipped.

**Object indirection** — views, synonyms and aliases are followed
transitively, so a program that only ever reads `V_ACTIVE_CUST` is still
reported as depending on `CUSTOMER`.

**Copybook fan-out** — who copies the DCLGEN, and who copies *those*.

---

## Commands

```bash
# Build or refresh the index (incremental by default)
python mfdep.py index \\srv\share\PROD \\srv\share\TEST --db prod.db

# Dependencies of one table
python mfdep.py query PRODDB.CUSTOMER --db prod.db
python mfdep.py query CUSTOMER --db prod.db          # every schema
python mfdep.py query PRODDB.CUSTOMER --db prod.db --writers-only
python mfdep.py query PRODDB.CUSTOMER --db prod.db \
       --csv c.csv --json c.json --html c.html --quiet

# What tables exist, ranked by how heavily used
python mfdep.py tables --db prod.db --like 'CUST%'

# Batch: one CSV row per table/dependency, for every table
python mfdep.py impact --db prod.db --all-tables --csv everything.csv

# What the index actually covers
python mfdep.py stats --db prod.db
```

Useful `index` flags:

| Flag | Purpose |
|---|---|
| `-j 32` | more workers; raise above CPU count for a high-latency share |
| `--chunksize 64` | more files per worker batch; helps on slow SMB |
| `--include '*.CBL'` | restrict by glob (repeatable) |
| `--exclude '*.LOG'` | skip by glob (repeatable) |
| `--full` | re-parse everything, ignoring size/mtime |
| `--max-file-mb 512` | raise the per-file read cap |

Useful `query` flags:

| Flag | Purpose |
|---|---|
| `--data-hops 0` | turn off dataset lineage (sort/merge chain) |
| `--data-hops 2` | follow the data two steps out instead of one |
| `--writers-only` | hide read-only references |
| `--confidence certain` | drop heuristic dynamic-SQL leads |
| `--vendor-file v.txt` | add site-specific runtime stubs to the vendor filter |
| `-v` | every hit, plus every DD dataset per step |

---

## Using it from Python

The CLI is a thin wrapper; everything it does is importable.

```bash
pip install -e .        # or just run from the repo root without installing
```

```python
import mfdep

if __name__ == "__main__":                       # see the note below
    mfdep.index([r"\\fileserv\mfarchive\PROD"], db="prod.db")

    res = mfdep.query("PRODDB.CUSTOMER", db="prod.db")
    for r in res.refs:
        print(r.access, r.stmt, f"{r.member}:{r.line}", r.path)

    for job, steps in res.jobs.items():
        print(job, [s.step for s in steps])

    for path, line, kind, detail in res.blind_spots:
        print("GAP", kind, path, line, detail)
```

| Call | Returns |
|---|---|
| `mfdep.index(roots, db=..., workers=, full=, include=, exclude=)` | dict of counts |
| `mfdep.query(table, db=..., min_confidence=, include_read=, data_hops=)` | `Result` |
| `mfdep.open_index(db)` | `Store`, usable as a context manager |
| `mfdep.tables(db=..., like=, min_refs=)` | list of dicts |

`Result` carries `.refs`, `.programs`, `.jobs`, `.steps`, `.data_links`,
`.blind_spots`, `.notes`, `.targets` — all plain dataclasses. To reuse the
formatters, `mfdep.report` has `text_report`, `csv_report`, `json_report` and
`html_report`.

**The `__main__` guard matters on Windows.** `index()` uses a process pool, and
Windows spawns workers by re-importing the calling module — without the guard
each worker re-runs your script's top level and spawns its own. If you are
embedding mfdep somewhere that makes a guard awkward (a web request, a
notebook, a module imported by something else), pass `workers=1` to parse
in-process with no pool at all:

```python
import mfdep
stats = mfdep.index("/mnt/mfarchive/PROD", db="prod.db", workers=1)
```

Reuse one open index when asking about many tables — reopening per call throws
away the page cache:

```python
with mfdep.open_index("prod.db") as store:
    for name in every_table:
        res = mfdep.query(name, db=store)
```

`open_index` raises `FileNotFoundError` for a missing index rather than
creating an empty one, because an empty index answers every question with
"nothing depends on this table".

**For anything the API doesn't cover, query the index directly** — it is a
plain SQLite database and `store.conn` is a normal connection. The schema is
documented in `store.py`; `table_refs`, `steps`, `dds`, `calls`, `copy_refs`
and `objects` are the interesting tables.

```python
with mfdep.open_index("prod.db") as store:
    rows = store.conn.execute("""
        SELECT f.member, COUNT(*) n
          FROM table_refs r JOIN files f ON f.id = r.file_id
         WHERE r.access = 'WRITE' AND r.table_name = ?
         GROUP BY 1 ORDER BY n DESC""", ("CUSTOMER",)).fetchall()
```

---

## Performance

Measured on this machine (16 workers, local SSD, 20,000 files / 356 MB of
synthetic artifacts producing 303k table references):

| Phase | Time |
|---|---|
| Directory walk | < 1 s |
| Parse + index | **18 s** (~1,100 files/s) |
| Incremental re-run, nothing changed | < 1 s |
| `query` against the built index | **0.05 s** |

Extrapolating to 100k files, expect roughly **1–2 minutes of CPU**, plus
however long the share takes to hand over the bytes — on a network drive that
read time dominates, which is exactly why the work is spread across a process
pool.

Index size is roughly **4–5 KB per source file** (88 MB for 20k files), mostly
the evidence snippets that let the report quote the matching line.

Two design choices carry most of that speed, both worth keeping if you modify
the code:

- **Indexes are dropped during a bulk load and rebuilt once at the end.**
  Maintaining 21 indexes per inserted row cost 50 s of the original 60 s
  runtime — more than four times the parsing itself.
- **`os.scandir` carries size and mtime from the directory entry.** On Windows
  those come free with the directory listing; calling `os.stat()` per file
  instead roughly doubles the walk time on a share.

Large members (dozens of MB) are parsed in overlapping 20,000-line windows
rather than being read whole, so worker memory stays flat regardless of file
size. The overlap preserves statements that straddle a window boundary, and
the file is flagged `CHUNKED` in the report.

---

## What it cannot see

Every report ends with a **BLIND SPOTS** section. This is deliberate: a
dependency report that hides its gaps reads as "nothing else touches this
table" when the truth is "something touches it in a way we cannot parse."

| Blind spot | Meaning |
|---|---|
| `DYNAMIC-SQL` | The program builds its statement at run time. mfdep recovers table names from the string literals and reports them at **heuristic** confidence — leads, not facts. |
| `DYNAMIC-CALL` | `CALL WS-PROGRAM` — the callee is a variable, so the call graph stops there. |
| `UNRESOLVED-CALL` | A static `CALL 'PGMX'` where no source for `PGMX` is on the share. The chain continues into a program mfdep cannot see. IBM/vendor runtime stubs are excluded — see below. |
| `UNRESOLVED-DECK-REF` | A `DD DSN=` names a member that exists on the share, but in a library the DSN does not match. Not linked — check whether the export layout differs from the dataset naming. |
| `UNRESOLVED-SYMBOLIC` | A `DD DSN=` still contains an unresolved `&SYM`, so any control deck it points at is unlinked. |
| `UNRESOLVED-TSO-STEP` | An `IKJEFT01` step with no `RUN PROGRAM()` found in SYSTSIN. |
| `BIND` | A BIND card was found. Its `QUALIFIER(...)` is what resolves unqualified table names at run time. |
| `TRUNCATED` / `CHUNKED` | A file hit the size cap, or was parsed in windows. |
| `UNREADABLE` | Permission denied, binary content, or an I/O error. |

### Vendor runtime modules

An unresolved `CALL` is a real hole in the answer. But every translated CICS
program calls `DFHEI1`, so reporting them all buries the one that matters —
in a 100k-artifact shop that is thousands of noise entries against a handful
of real gaps.

`vendor.py` filters them **by prefix, not by a list of names**. That
distinction is the whole point: a list containing `DFHEI1`, `DFHEI` and
`DFHPC` looks complete right up until a program calls `DFHNCTR`, which falls
straight through and gets reported as a missing application program. Every
`DFH*` module is CICS by construction, so the prefix covers the family
permanently — likewise `CEE*` (Language Environment), `DSN*` (Db2), `DFS*`
(IMS), `IGZ*`/`ILB*` (COBOL runtime), `CSQ*` (MQ), `EZA*` (TCP/IP) and the
rest.

The opposite error is just as bad. A three-letter `ICE*` rule for DFSORT would
classify an application called `ICEBERG` as a sort utility and silently drop
its dependencies, so DFSORT is matched by its short set of real entry points
instead, and PL/I is `IBMB*`/`IBMS*` rather than a bare `IBM*`. There is a
regression test for each direction.

Because the filter can hide a real program, it is **auditable**: the report
names every module it suppressed rather than just counting them.

```
NOTES
  * Ignored these call targets as vendor runtime modules (matched by prefix) -
    IBM CICS: DFHEI1, DFHNCTR; IBM Language Environment: CEEDATE. If any of
    those is actually one of your programs, its dependencies are being missed.
```

Add site-specific stubs — in-house wrappers, third-party products — without
touching code or re-indexing:

```bash
python mfdep.py query PRODDB.CUSTOMER --db prod.db --vendor-file vendors.txt
```

```
# vendors.txt - trailing * makes it a prefix rule
XPED*     Compuware Xpediter
CAL2*     CA-7
MYSTUB    in-house stub, no source exported
```

Filtering happens at query time, so the list can be corrected and re-run
immediately against the existing index.

Known limits beyond those:

- **Unqualified table names.** `FROM CUSTOMER` with no schema resolves at run
  time from the package `QUALIFIER`. Those hits are reported under schema
  `(none)` and counted in the notes rather than guessed at.
- **Scheduler dependencies.** CA-7 / Control-M / TWS job triggers are not in
  these files, so job-to-job ordering is out of scope.
- **Load modules and DBRMs** are binary and skipped.
- **Catalog-only objects.** An alias or view created directly in DB2 and never
  captured in a DDL member cannot be followed.
- **Anything not exported to the share.** The index covers what is on the
  drive, nothing more — check `mfdep stats` before trusting an empty result.

**Cross-check with the catalog.** `SYSIBM.SYSPACKDEP` / `SYSTABAUTH` are
authoritative for *bound packages* and will catch dynamic SQL that mfdep can
only guess at. mfdep covers what the catalog cannot: utility control cards,
JCL, unbound source, retired-but-still-present members, and everything in
libraries that were never compiled. Run both and reconcile; the difference
between the two lists is itself informative.

---

## Layout

```
pyproject.toml        packaging; `pip install -e .` gives an `mfdep` command
mfdep.py              launcher, for running without installing
mfdep/
  api.py              library entry points: index/query/open_index/tables
  cli.py              argument parsing, subcommands
  scan.py             directory walk + process pool
  extract.py          per-file dispatch, chunked reads for large members
  classify.py         artifact-kind detection (extension, path hint, content)
  sqlscan.py          the SQL table-reference grammar  ← precision core
  parsers/cobol.py    fixed-format columns, continuation, EXEC SQL, COPY
  parsers/jcl.py      statements, continuation, instream data, IKJEFT01
  parsers/control.py  DB2 utility control cards, BIND, sort/merge decks
  parsers/sqlfile.py  DDL and stored procedures
  vendor.py           IBM/vendor module recognition, by prefix
  store.py            SQLite schema and bulk-load write path
  graph.py            transitive closure  ← the "which jobs break" answer
  report.py           text / CSV / JSON / HTML
tests/
  make_fixtures.py    generates a column-exact sample library
  test_mfdep.py       37 regression tests, one per failure mode
```

Run the tests:

```bash
python -m unittest discover -s tests -t tests -v
```

## Tuning it to your shop

The pieces most likely to need local adjustment:

- `config.py` — `EXT_KIND` and `PATH_HINT_KIND` map your library naming
  convention onto artifact kinds. If members have no extension, the path hints
  are what classify them; add your own library-name fragments there.
- `parsers/jcl.py` — `TSO_DRIVERS`, `UTILITY_DRIVER`, `SQL_PROCESSORS` list the
  wrapper programs whose instream data hides the real target. Add any
  site-specific driver or in-house wrapper program here.
- `vendor.py` — `PREFIXES` and `EXACT` decide what counts as a runtime
  stub rather than a missing program. Prefer a prefix over a name, but
  keep it long enough not to swallow an application program.
- `sqlscan.py` — `_DIRECT` is the pattern table. Adding a statement form is one
  line: a regex whose group 1 is the table name, plus its access class.
