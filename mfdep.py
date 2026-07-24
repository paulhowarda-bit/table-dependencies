"""Launcher so the tool runs as `python mfdep.py ...` with no install step.

The multiprocessing guard matters on Windows: workers are spawned, not forked,
so without it each worker would re-execute this file and fork-bomb the machine.
"""

import multiprocessing
import sys

from mfdep.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
