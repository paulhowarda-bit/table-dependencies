"""Optional per-stage wall-clock timing for one run (diagnostic only).

No-op by design: when nothing consumes the numbers, ``stage()`` is a bare yield,
``start()`` returns ``None``, and nothing is emitted. It never touches the index
database or any report file - timing is delivered only to stderr and/or a caller's
sink, so turning it on cannot change what a query or an index build produces.

The collected spans can be delivered two INDEPENDENT ways:

  * ``enabled=True`` (the ``--timing`` flag) logs a formatted breakdown to stderr.
  * ``sink=<callable>`` hands the same spans to the caller as structured rows -
    ``[{"stage": "parse", "ms": 12.3}, ...]``, in call order - so a calling Python
    program can route them into its own timing log. Passing a sink turns collection
    on by itself, so an embedding caller needs no ``--timing`` in argv and gets no
    stderr noise unless it also asks for it.

The durations are inherently non-reproducible (real wall-clock); they never reach
the index or a report.
"""

from __future__ import annotations

import contextlib
import time
from typing import Callable, Dict, List, Optional, Tuple

# What a sink receives: the spans of one run, in call order.
TimingRows = List[Dict[str, object]]
TimingSink = Callable[[TimingRows], None]


class StageTimer:
    """Accumulate labelled wall-clock spans, then report them once at the end.

    Two ways to time a span, both no-ops when nothing will consume them:
      * ``with timer.stage("parse"): ...``  - a self-contained block
      * ``t0 = timer.start(); ...; timer.since("closure", t0)`` - when wrapping the
        block in a ``with`` would force an awkward re-indent (e.g. a long loop that
        already owns its indentation).
    """

    def __init__(self, log, enabled: bool, source_name: str,
                 sink: Optional[TimingSink] = None) -> None:
        self._log = log
        self._echo = bool(enabled)          # log to stderr (the --timing flag)
        self._sink = sink
        # Collect whenever ANYONE will consume the numbers - the flag, a sink, or both.
        self._enabled = bool(enabled) or sink is not None
        self._src = source_name
        self._rows: List[Tuple[str, float]] = []

    @property
    def enabled(self) -> bool:
        """True when anything will consume the spans - the flag, a sink, or both.
        Callers gate optional measurement work on this rather than on the flag, so a
        sink-only caller measures the same stages the flag reports."""
        return self._enabled

    def _record(self, name: str, seconds: float) -> None:
        self._rows.append((name, seconds * 1000.0))

    @contextlib.contextmanager
    def stage(self, name: str):
        if not self._enabled:
            yield
            return
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self._record(name, time.perf_counter() - t0)

    def start(self) -> Optional[float]:
        """A perf_counter timestamp, or None when disabled (pairs with since())."""
        return time.perf_counter() if self._enabled else None

    def since(self, name: str, t0: Optional[float]) -> None:
        if self._enabled and t0 is not None:
            self._record(name, time.perf_counter() - t0)

    def timings(self) -> TimingRows:
        """The collected spans as structured rows, in call order. This is the shape a
        ``sink`` receives, and is safe to hand to a caller (a fresh list each call)."""
        return [{"stage": name, "ms": ms} for name, ms in self._rows]

    def report(self) -> None:
        """Deliver the run's spans: log to stderr if asked, hand to the sink if given.

        Called once at the end of a completed run; a run that fails before reaching it
        delivers nothing.
        """
        if not self._enabled or not self._rows:
            return
        if self._echo:
            width = max(len(name) for name, _ in self._rows)
            measured = sum(ms for _, ms in self._rows)
            self._log.info(f"[{self._src}] timing (ms):")
            for name, ms in self._rows:
                self._log.info(f"  {name:<{width}}  {ms:9.1f}")
            # "measured", not "total": cheap glue between the timed stages is
            # deliberately unmeasured - the point is to locate the dominant stage,
            # not to reconcile to wall-clock.
            self._log.info(f"  {'measured':<{width}}  {measured:9.1f}")
        if self._sink is not None:
            try:
                self._sink(self.timings())
            except Exception as exc:
                # A caller's sink is a diagnostic hook: it must never fail a run
                # whose real work (the index, or the answer) is already done.
                self._log.warning(
                    f"[{self._src}] timing sink raised {type(exc).__name__}: {exc}")
