"""Enable ``python -m mfdep``.

Mirrors the repo-root ``mfdep.py`` launcher, but lives *inside* the package so
it travels with it: when the ``mfdep/`` folder is vendored into another
project's ``src/`` tree, the root launcher does not come along, and this module
is what makes the package runnable there.

The ``freeze_support()`` guard is required on Windows, where the ``index``
command's process pool spawns workers by re-importing this module.
"""

import multiprocessing
import sys

from .cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
