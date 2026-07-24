"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import __version__
from .config import CERTAIN, HEURISTIC, LIKELY
from .graph import Analyzer
from .report import csv_report, html_report, json_report, text_report
from .scan import ScanOptions, run_index
from .store import Store
from .vendor import load_overrides

DEFAULT_DB = "mfdep.db"

_CONF = {"any": 0, "heuristic": HEURISTIC, "likely": LIKELY, "certain": CERTAIN}


def _add_db(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=DEFAULT_DB,
                   help=f"index database path (default: {DEFAULT_DB})")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mfdep",
        description="Find every dependency of a DB2 table across mainframe "
                    "JCL, PROC, COBOL, control-card and stored-procedure "
                    "libraries.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  mfdep index \\\\fileserv\\mfarchive\\PROD --db prod.db
  mfdep query PRODDB.CUSTOMER --db prod.db
  mfdep query CUSTOMER --db prod.db --html cust.html --csv cust.csv
  mfdep query PRODDB.CUSTOMER --db prod.db --writers-only
  mfdep tables --db prod.db --like 'CUST%'
  mfdep impact --db prod.db --all-tables --csv everything.csv
""")
    p.add_argument("--version", action="version", version=f"mfdep {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    # ---- index
    ix = sub.add_parser("index", help="crawl libraries and build the index")
    ix.add_argument("roots", nargs="+", help="directories to crawl (UNC ok)")
    _add_db(ix)
    ix.add_argument("-j", "--workers", type=int, default=0,
                    help="worker processes (default: CPU count)")
    ix.add_argument("--chunksize", type=int, default=24,
                    help="files per worker batch; raise for high-latency shares")
    ix.add_argument("--max-file-mb", type=int, default=256,
                    help="stop reading a single file past this size (default 256)")
    ix.add_argument("--include", action="append", default=[],
                    help="only files matching this glob (repeatable)")
    ix.add_argument("--exclude", action="append", default=[],
                    help="skip files matching this glob (repeatable)")
    ix.add_argument("--full", action="store_true",
                    help="re-parse everything, ignoring size/mtime")
    ix.add_argument("--no-prune", action="store_true",
                    help="keep index entries for files no longer on the share")
    ix.add_argument("-q", "--quiet", action="store_true")

    # ---- query
    q = sub.add_parser("query", help="report dependencies of one table")
    q.add_argument("table", help="SCHEMA.TABLE, or TABLE for every schema")
    _add_db(q)
    q.add_argument("--confidence", choices=list(_CONF), default="any",
                   help="minimum confidence to report (default: any)")
    q.add_argument("--writers-only", action="store_true",
                   help="hide read-only references")
    q.add_argument("--vendor-file", metavar="FILE",
                   help="extra IBM/vendor module names or PREFIX* rules "
                        "to treat as runtime stubs rather than missing "
                        "application programs")
    q.add_argument("--data-hops", type=int, default=1, metavar="N",
                   help="follow datasets N hops out of the dependent steps to "
                        "surface sort/merge/copy decks in the same chain "
                        "(default 1, 0 disables)")
    q.add_argument("-v", "--verbose", action="store_true",
                   help="list every hit instead of the first 40 per section")
    q.add_argument("--csv", metavar="FILE")
    q.add_argument("--json", metavar="FILE")
    q.add_argument("--html", metavar="FILE")
    q.add_argument("--quiet", action="store_true",
                   help="suppress the text report (use with --csv/--json/--html)")

    # ---- tables
    t = sub.add_parser("tables", help="list tables the index knows about")
    _add_db(t)
    t.add_argument("--like", default="%", help="SQL LIKE pattern, e.g. 'CUST%%'")
    t.add_argument("--min-refs", type=int, default=1)
    t.add_argument("--limit", type=int, default=200)

    # ---- impact
    im = sub.add_parser("impact",
                        help="batch mode: one row per table/artifact dependency")
    _add_db(im)
    im.add_argument("--all-tables", action="store_true")
    im.add_argument("--table", action="append", default=[])
    im.add_argument("--csv", metavar="FILE", required=True)
    im.add_argument("--min-refs", type=int, default=1)

    # ---- stats
    st = sub.add_parser("stats", help="show what the index contains")
    _add_db(st)

    return p


# ---------------------------------------------------------------- commands

def cmd_index(a: argparse.Namespace) -> int:
    roots = []
    for r in a.roots:
        if not os.path.isdir(r):
            print(f"error: not a directory: {r}", file=sys.stderr)
            return 2
        roots.append(os.path.abspath(r))

    opts = ScanOptions(
        roots=roots, db_path=a.db, workers=a.workers, chunksize=a.chunksize,
        max_file_mb=a.max_file_mb, include=tuple(a.include),
        exclude=tuple(a.exclude), full=a.full, prune_missing=not a.no_prune,
        quiet=a.quiet)
    stats = run_index(opts)

    if not a.quiet:
        print(f"\nIndex: {a.db}")
        print(f"  files        {stats['files']:,}")
        print(f"  table refs   {stats['table_refs']:,}")
        print(f"  distinct tbl {stats['distinct_tables']:,}")
        print(f"  jcl steps    {stats['steps']:,}")
        print(f"  blind spots  {stats['blind_spots']:,}")
    return 0


def _require_db(path: str) -> Store | None:
    if not os.path.exists(path):
        print(f"error: no index at {path}. Run 'mfdep index <root>' first.",
              file=sys.stderr)
        return None
    store = Store(path)
    if store.stale_schema:
        # Reading an older layout would give confidently wrong answers, which
        # is worse than refusing outright.
        store.close()
        print(f"error: {path} was built by an older version of mfdep and its "
              f"layout has changed.\n"
              f"       Re-index it:  python mfdep.py index <root> --db {path} --full",
              file=sys.stderr)
        return None
    return store


def cmd_query(a: argparse.Namespace) -> int:
    store = _require_db(a.db)
    if store is None:
        return 2

    if getattr(a, "vendor_file", None):
        n = load_overrides(a.vendor_file)
        print(f"loaded {n} vendor module rule(s) from {a.vendor_file}",
              file=sys.stderr)

    t0 = time.time()
    result = Analyzer(store).analyze(
        a.table, min_confidence=_CONF[a.confidence],
        include_read=not a.writers_only, data_hops=a.data_hops)
    elapsed = time.time() - t0

    if not a.quiet:
        text_report(result, verbose=a.verbose)
        print(f"(answered in {elapsed:.2f}s from {a.db})\n")

    if a.csv:
        csv_report(result, a.csv)
        print(f"wrote {a.csv}")
    if a.json:
        json_report(result, a.json)
        print(f"wrote {a.json}")
    if a.html:
        html_report(result, a.html)
        print(f"wrote {a.html}")

    store.close()
    return 0


def cmd_tables(a: argparse.Namespace) -> int:
    store = _require_db(a.db)
    if store is None:
        return 2
    rows = store.conn.execute(
        "SELECT schema, table_name, COUNT(*) refs, "
        "SUM(access='WRITE') writes, SUM(access='READ') reads, "
        "COUNT(DISTINCT file_id) files "
        "FROM table_refs WHERE table_name LIKE ? "
        "GROUP BY schema, table_name HAVING refs >= ? "
        "ORDER BY refs DESC LIMIT ?",
        (a.like.upper(), a.min_refs, a.limit)).fetchall()

    print(f"{'TABLE':<44} {'REFS':>6} {'WRITE':>6} {'READ':>6} {'FILES':>6}")
    print("-" * 74)
    for r in rows:
        name = f"{r['schema']}.{r['table_name']}" if r["schema"] else r["table_name"]
        print(f"{name:<44} {r['refs']:>6} {r['writes'] or 0:>6} "
              f"{r['reads'] or 0:>6} {r['files']:>6}")
    print(f"\n{len(rows)} table(s)")
    store.close()
    return 0


def cmd_impact(a: argparse.Namespace) -> int:
    store = _require_db(a.db)
    if store is None:
        return 2

    if a.all_tables:
        specs = [f"{r['schema']}.{r['table_name']}" if r["schema"]
                 else r["table_name"]
                 for r in store.conn.execute(
                     "SELECT schema, table_name, COUNT(*) c FROM table_refs "
                     "GROUP BY schema, table_name HAVING c >= ? ORDER BY 1,2",
                     (a.min_refs,))]
    else:
        specs = a.table

    if not specs:
        print("error: pass --all-tables or --table NAME", file=sys.stderr)
        return 2

    import csv as _csv
    analyzer = Analyzer(store)
    with open(a.csv, "w", newline="", encoding="utf-8-sig") as fh:
        wr = _csv.writer(fh)
        wr.writerow(["table", "record_type", "name", "access", "artifact_kind",
                     "path", "line", "detail"])
        for i, spec in enumerate(specs, 1):
            res = analyzer.analyze(spec)
            for r in res.refs:
                wr.writerow([spec, "REFERENCE", f"{r.schema}.{r.table}", r.access,
                             r.kind, r.path, r.line, r.stmt])
            for name, node in sorted(res.programs.items()):
                wr.writerow([spec, "PROGRAM", name, "", "", "", "", node.reason])
            for s in res.steps:
                wr.writerow([spec, "JOB", s.job, "", "JCL", s.path, s.line,
                             f"{s.step} -> {s.resolved_pgm or s.pgm}"])
            if i % 25 == 0:
                print(f"\r  {i}/{len(specs)} tables", end="", file=sys.stderr,
                      flush=True)
    print(f"\nwrote {a.csv} for {len(specs)} table(s)")
    store.close()
    return 0


def cmd_stats(a: argparse.Namespace) -> int:
    store = _require_db(a.db)
    if store is None:
        return 2
    s = store.stats()
    roots = store.get_meta("roots", "(unknown)")
    when = store.get_meta("indexed_at", "")
    print(f"Index      {a.db}")
    print(f"Roots      {roots}")
    if when:
        print(f"Indexed    {time.strftime('%Y-%m-%d %H:%M', time.localtime(int(when)))}")
    print()
    print(f"  files            {s['files']:>10,}")
    print(f"  table refs       {s['table_refs']:>10,}")
    print(f"  distinct tables  {s['distinct_tables']:>10,}")
    print(f"  programs         {s['programs']:>10,}")
    print(f"  jcl steps        {s['steps']:>10,}")
    print(f"  copy/include     {s['copy_refs']:>10,}")
    print(f"  calls            {s['calls']:>10,}")
    print(f"  objects          {s['objects']:>10,}")
    print(f"  blind spots      {s['blind_spots']:>10,}")
    print("\nBy artifact kind:")
    for kind, count in store.kind_counts():
        print(f"  {kind:<12} {count:>10,}")

    bad = store.conn.execute(
        "SELECT COUNT(*) c FROM files WHERE error != ''").fetchone()["c"]
    if bad:
        print(f"\n  {bad:,} file(s) could not be read - see 'blind spots' in "
              f"any query report")
    store.close()
    return 0


_COMMANDS = {"index": cmd_index, "query": cmd_query, "tables": cmd_tables,
             "impact": cmd_impact, "stats": cmd_stats}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
