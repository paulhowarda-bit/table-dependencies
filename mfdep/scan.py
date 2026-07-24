"""Crawl the network drive and populate the index.

Two costs dominate and both are addressed here:

  walk   - 100k directory entries over SMB. ``os.scandir`` is used because on
           Windows each entry already carries size and mtime from the
           underlying FindFirstFile, so the incremental check costs no extra
           round trip. Calling os.stat() per file instead roughly doubles the
           walk time on a share.
  read   - the files themselves. I/O-bound with a real CPU cost for the regex
           work, so a process pool with a large chunksize amortises both the
           SMB latency and the IPC overhead.

Writes are funnelled back to a single process because SQLite does not want
eight writers, and the facts coming back are tiny compared to the file bodies.
"""

from __future__ import annotations

import fnmatch
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

from .config import BULK_LOAD_FILES, DEFAULT_CHUNKSIZE, SKIP_DIRS
from .extract import extract_file
from .facts import FileFacts
from .store import Store
from .util import long_path


@dataclass
class ScanOptions:
    roots: list[str]
    db_path: str
    workers: int = 0
    chunksize: int = DEFAULT_CHUNKSIZE
    max_file_mb: int = 256
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    full: bool = False
    prune_missing: bool = True
    quiet: bool = False


@dataclass
class Candidate:
    path: str
    size: int
    mtime_ns: int


def walk(roots: list[str], include: tuple[str, ...], exclude: tuple[str, ...],
         on_progress=None) -> list[Candidate]:
    """Enumerate candidate files, carrying size/mtime from the directory entry."""
    out: list[Candidate] = []
    seen_dirs = 0

    for root in roots:
        stack = [long_path(root)]
        while stack:
            current = stack.pop()
            seen_dirs += 1
            if on_progress and seen_dirs % 250 == 0:
                on_progress(seen_dirs, len(out))
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name.lower() in SKIP_DIRS:
                                    continue
                                stack.append(entry.path)
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            name = entry.name
                            if exclude and any(fnmatch.fnmatch(name, p)
                                               for p in exclude):
                                continue
                            if include and not any(fnmatch.fnmatch(name, p)
                                                   for p in include):
                                continue
                            st = entry.stat(follow_symlinks=False)
                            out.append(Candidate(entry.path, st.st_size,
                                                 st.st_mtime_ns))
                        except OSError:
                            continue      # permission denied on one entry
            except OSError as exc:
                print(f"  ! cannot read directory {current}: {exc}",
                      file=sys.stderr)

    if on_progress:
        on_progress(seen_dirs, len(out))
    return out


def _worker(job: tuple[str, int, int, int]) -> FileFacts:
    path, size, mtime_ns, max_bytes = job
    return extract_file(path, size, mtime_ns, max_bytes)


def run_index(opts: ScanOptions) -> dict:
    started = time.time()
    workers = opts.workers or (os.cpu_count() or 4)
    max_bytes = opts.max_file_mb << 20

    def say(msg: str) -> None:
        if not opts.quiet:
            print(msg, file=sys.stderr, flush=True)

    say(f"Walking {len(opts.roots)} root(s)...")

    def walk_progress(dirs: int, files: int) -> None:
        if not opts.quiet:
            print(f"\r  {dirs:,} dirs, {files:,} files found", end="",
                  file=sys.stderr, flush=True)

    candidates = walk(opts.roots, opts.include, opts.exclude, walk_progress)
    if not opts.quiet:
        print(file=sys.stderr)
    say(f"Found {len(candidates):,} files in {time.time() - started:.1f}s")

    store = Store(opts.db_path, fast=True)
    if store.stale_schema:
        # The extracted facts changed shape, so the old rows cannot be reused.
        # Rebuild rather than mixing layouts or reporting from a stale mix.
        say("Index was built by an older version - rebuilding it from scratch")
        store.rebuild_schema()
        opts.full = True
    store.set_meta("roots", os.pathsep.join(opts.roots))

    todo = candidates
    skipped = 0
    if not opts.full:
        sigs = store.existing_signatures()
        todo = [c for c in candidates
                if sigs.get(c.path) != (c.size, c.mtime_ns)]
        skipped = len(candidates) - len(todo)
        if skipped:
            say(f"Skipping {skipped:,} unchanged files (incremental)")

    if opts.prune_missing:
        present = {c.path for c in candidates}
        gone = [p for p in store.all_paths() if p not in present]
        if gone:
            say(f"Pruning {len(gone):,} files no longer on the share")
            store.delete_paths(gone)

    # Clear the old rows for everything about to be re-parsed, in one batch.
    # Doing it up front means every write in the parse loop is a pure INSERT
    # with no read-back, which is most of why the load is fast.
    stale = [c.path for c in todo]
    if stale and not opts.full:
        store.delete_paths(stale)
    elif opts.full:
        store.delete_paths([c.path for c in candidates])

    jobs = [(c.path, c.size, c.mtime_ns, max_bytes) for c in todo]
    total = len(jobs)
    done = errors = 0
    bytes_done = 0
    t0 = time.time()

    if total:
        # Maintaining 21 indexes per inserted row costs several times the parse
        # itself, so a bulk load drops them and rebuilds once at the end. Not
        # worth it for a small incremental run, where rebuilding every index
        # over the whole database would cost more than the rows being added.
        bulk = total >= BULK_LOAD_FILES
        if bulk:
            store.drop_indexes()
        say(f"Parsing {total:,} files with {workers} workers...")
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for facts in pool.map(_worker, jobs, chunksize=opts.chunksize):
                store.add(facts)
                done += 1
                bytes_done += facts.size
                if facts.error:
                    errors += 1
                if not opts.quiet and done % 200 == 0:
                    elapsed = max(time.time() - t0, 0.001)
                    rate = done / elapsed
                    eta = (total - done) / rate if rate else 0
                    print(f"\r  {done:,}/{total:,}  {rate:,.0f} files/s  "
                          f"{bytes_done / 1e6:,.0f} MB  ETA {eta / 60:,.1f}m  "
                          f"errors {errors}", end="", file=sys.stderr, flush=True)
        if not opts.quiet:
            print(file=sys.stderr)

    store.set_meta("indexed_at", str(int(time.time())))
    store.optimize()
    stats = store.stats()
    store.close()

    elapsed = time.time() - started
    say(f"Indexed {done:,} files ({errors:,} unreadable) in {elapsed / 60:.1f}m")
    return {"scanned": done, "skipped": skipped, "errors": errors,
            "seconds": elapsed, **stats}
