"""mfdep - DB2 table dependency finder for mainframe artifact libraries.

Two-phase design:
  1. `mfdep index`  - one parallel crawl of the network drive, extracting facts
                      into a local SQLite index. Incremental on re-run.
  2. `mfdep query`  - answer "what depends on table X" from the local index,
                      including the transitive JCL -> PROC -> program chain.
"""

__version__ = "1.0.0"
