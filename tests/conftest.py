"""Put mfdep on the path before the tests are collected.

mfdep is not shipped from this repo. It lives at
``../mainframe-tracer/src/network_drive`` relative to this repo, or in a local
``mfdep/`` copy used only for testing here. ``$MFDEP_HOME`` overrides both.

The first directory that contains ``mfdep/`` wins, and it goes on the front of
``sys.path`` - so it beats anything installed under the same name, including
the tracer's version shim.
"""

import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

_SEARCH = [
    os.environ.get("MFDEP_HOME"),
    _REPO,                                                          # local test copy
    _REPO.parent / "mainframe-tracer" / "src" / "network_drive",    # deployed
]

for _dir in [Path(d) for d in _SEARCH if d]:
    if (_dir / "mfdep" / "__init__.py").is_file():
        sys.path.insert(0, str(_dir))
        # Drop a shim that got imported first; otherwise the path edit is moot.
        for _name in [n for n in sys.modules
                      if n == "mfdep" or n.startswith("mfdep.")]:
            del sys.modules[_name]
        break
else:
    raise ImportError(
        "mfdep not found. Looked in:\n"
        + "\n".join("  " + str(d) for d in _SEARCH if d)
        + "\nSet MFDEP_HOME to the directory *containing* mfdep/."
    )
