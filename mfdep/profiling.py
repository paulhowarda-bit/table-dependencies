"""Per-stage wall-clock timing for one run (diagnostic only).

The spans are always collected and handed back to the caller, so a Python
program gets the numbers by return value - no flag, no callback:

  * ``index()`` puts them in its result dict under ``"timing"``.
  * ``query()`` puts them on ``Result.timings``.

Each is ``[{"stage": "parse", "ms": 12.3}, ...]`` in call order. The CLI
``--timing`` flag additionally logs a formatted breakdown to stderr (``echo``).

Timing never touches the index or a report - it is pure measurement, and the
durations are inherently non-reproducible, so they are kept out of every output
file and surfaced only as a return value or a stderr diagnostic.
"""

from __future__ import annotations

import contextlib
import time
from typing import Dict, List, Optional, Tuple

# The shape a caller receives: the spans of one run, in call order.
TimingRows = List[Dict[str, object]]


class StageTimer:
    """Accumulate labelled wall-clock spans and return them; optionally echo.

    Collection is always on - the overhead is a handful of ``perf_counter``
    calls per run (the stages wrap whole phases, not per-file work), which is
    lost in the noise next to the crawl or the query it measures. Two ways to
    time a span:
      * ``with timer.stage("parse"): ...``  - a self-contained block
      * ``t0 = timer.start(); ...; timer.since("closure", t0)`` - when wrapping
        the block in a ``with`` would force an awkward re-indent.
    """

    def __init__(self, log, echo: bool = False, source_name: str = "") -> None:
        self._log = log
        self._echo = bool(echo)          # log to stderr (the --timing flag)
        self._src = source_name
        self._rows: List[Tuple[str, float]] = []

    def _record(self, name: str, seconds: float) -> None:
        self._rows.append((name, seconds * 1000.0))

    @contextlib.contextmanager
    def stage(self, name: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self._record(name, time.perf_counter() - t0)

    def start(self) -> float:
        """A perf_counter timestamp to pass to since()."""
        return time.perf_counter()

    def since(self, name: str, t0: Optional[float]) -> None:
        if t0 is not None:
            self._record(name, time.perf_counter() - t0)

    def timings(self) -> TimingRows:
        """The collected spans as structured rows, in call order - a fresh list
        each call, safe to hand back to a caller as the run's return value."""
        return [{"stage": name, "ms": ms} for name, ms in self._rows]

    def report(self) -> None:
        """Log the breakdown to stderr, but only when ``echo`` (the --timing
        flag) is set. The returned timings are unaffected by this - they are
        always available via :meth:`timings`.
        """
        if not self._echo or not self._rows:
            return
        width = max(len(name) for name, _ in self._rows)
        measured = sum(ms for _, ms in self._rows)
        self._log.info(f"[{self._src}] timing (ms):")
        for name, ms in self._rows:
            self._log.info(f"  {name:<{width}}  {ms:9.1f}")
        # "measured", not "total": cheap glue between the timed stages is
        # deliberately unmeasured - the point is to locate the dominant stage,
        # not to reconcile to wall-clock.
        self._log.info(f"  {'measured':<{width}}  {measured:9.1f}")
