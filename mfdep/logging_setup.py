"""Command-line-side logging configuration.

The LIBRARY never configures logging: :mod:`mfdep` attaches a ``NullHandler`` to
the package logger and every module logs through ``logging.getLogger(__name__)``.
Only the command-line entry point calls :func:`configure_logging`, so
``import mfdep`` leaves a host application's logging untouched - the documented
contract for a well-behaved library
(https://docs.python.org/3/howto/logging.html#configuring-logging-for-a-library).
"""
from __future__ import annotations

import logging
import sys

#: The package's top-level logger name. Every module logger (``mfdep.scan`` ...) is
#: a child of this one, so configuring it here configures the whole package.
PACKAGE_LOGGER = "mfdep"

_HANDLER_TAG = "_mfdep_cli_handler"


def level_for(verbose: int = 0, quiet: int = 0) -> int:
    """Resolve ``verbose`` / ``quiet`` counts to a logging level.

    The default is INFO - a normal CLI run stays as chatty as it always was
    (progress + warnings + errors). ``quiet`` wins over ``verbose`` when both given.
    """
    if quiet >= 2:
        return logging.ERROR      # only failures
    if quiet == 1:
        return logging.WARNING    # failures + warnings (hides progress milestones)
    if verbose >= 1:
        return logging.DEBUG      # adds swallowed tracebacks + internal detail
    return logging.INFO           # default: progress + warnings + failures


def configure_logging(verbose: int = 0, quiet: int = 0) -> logging.Logger:
    """Install a single stderr handler on the package logger and set its level.

    Idempotent: a second call replaces the handler this function added rather than
    stacking another, so repeated calls in one process (and the test suite) never
    double-print. The handler binds to the CURRENT ``sys.stderr`` each call, which
    also keeps a test harness's per-test stderr capture working.
    """
    logger = logging.getLogger(PACKAGE_LOGGER)
    logger.setLevel(level_for(verbose, quiet))
    # The CLI owns stderr, so don't also bubble records up to the root logger (which a
    # host might have configured) - that would double-print under the CLI.
    logger.propagate = False

    for handler in [h for h in logger.handlers if getattr(h, _HANDLER_TAG, False)]:
        logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    # Messages already carry their own context (``[source] ...``); a bare format keeps
    # CLI output identical to the historical ``print(..., file=sys.stderr)`` lines.
    handler.setFormatter(logging.Formatter("%(message)s"))
    setattr(handler, _HANDLER_TAG, True)
    logger.addHandler(handler)
    return logger
