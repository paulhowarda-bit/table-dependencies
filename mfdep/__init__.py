"""mfdep - DB2 table dependency finder for mainframe artifact libraries.

Two-phase design:
  1. `mfdep index`  - one parallel crawl of the network drive, extracting facts
                      into a local SQLite index. Incremental on re-run.
  2. `mfdep query`  - answer "what depends on table X" from the local index,
                      including the transitive JCL -> PROC -> program chain.

Usable as a library as well as a CLI::

    from mfdep import index, query

    if __name__ == "__main__":                 # needed on Windows, see api.py
        index([r"\\\\fileserv\\mfarchive\\PROD"], db="prod.db")
        res = query("PRODDB.CUSTOMER", db="prod.db")
        print(len(res.refs), "references,", len(res.jobs), "jobs")

Pass ``workers=1`` to index() to parse in-process with no pool, which removes
the __main__ requirement entirely - use that when embedding.
"""

__version__ = "1.2.0"

import logging as _logging

# A well-behaved library configures no logging of its own: attach a NullHandler
# so `import mfdep` never touches a host application's logging, and only the CLI
# (mfdep.logging_setup.configure_logging) ever installs a real handler.
_logging.getLogger(__name__).addHandler(_logging.NullHandler())

from .api import Analyzer, Result, Store, index, open_index, query, tables

__all__ = ["index", "query", "open_index", "tables",
           "Result", "Store", "Analyzer", "__version__"]
