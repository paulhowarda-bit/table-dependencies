"""Static configuration: file-kind mapping, size limits, tuning knobs."""

from __future__ import annotations

# ---------------------------------------------------------------- file kinds

# Canonical artifact kinds. Anything unrecognised becomes UNKNOWN and is still
# text-scanned at low confidence rather than silently dropped.
JCL = "JCL"
PROC = "PROC"
COBOL = "COBOL"
COPYBOOK = "COPYBOOK"
DCLGEN = "DCLGEN"
SQL = "SQL"
CONTROL = "CONTROL"
SORT = "SORT"
BIND = "BIND"
UNKNOWN = "UNKNOWN"

ALL_KINDS = (JCL, PROC, COBOL, COPYBOOK, DCLGEN, SQL, CONTROL, SORT,
             BIND, UNKNOWN)

# Extension -> kind. Lower-case, leading dot.
EXT_KIND = {
    ".jcl": JCL, ".job": JCL, ".jbl": JCL, ".jb": JCL,
    ".prc": PROC, ".proc": PROC,
    ".cbl": COBOL, ".cob": COBOL, ".cobol": COBOL, ".ccp": COBOL,
    ".sqb": COBOL, ".pco": COBOL, ".cb2": COBOL,
    ".cpy": COPYBOOK, ".copy": COPYBOOK, ".cpb": COPYBOOK, ".inc": COPYBOOK,
    ".dcl": DCLGEN, ".dclgen": DCLGEN,
    ".sql": SQL, ".ddl": SQL, ".db2": SQL, ".spsql": SQL, ".sp": SQL, ".prm": SQL,
    ".ctl": CONTROL, ".ctrl": CONTROL, ".card": CONTROL, ".sysin": CONTROL,
    ".util": CONTROL, ".parm": CONTROL, ".cntl": CONTROL,
    ".bnd": BIND, ".bind": BIND,
    ".srt": SORT, ".sort": SORT,
}

# PDS members exported to a share usually have NO extension - the *library
# name* in the path is the only hint. Matched as upper-cased path fragments.
# Ordered: first match wins, so put the specific ones first.
PATH_HINT_KIND = (
    ("DCLGEN", DCLGEN),
    ("COPYLIB", COPYBOOK), ("COPYBOOK", COPYBOOK), (".COPY", COPYBOOK),
    ("MACLIB", COPYBOOK),
    ("PROCLIB", PROC), (".PROC", PROC), ("PROCS", PROC),
    ("JOBLIB", JCL), ("JCLLIB", JCL), (".JCL", JCL), ("JOBS", JCL),
    ("COBOL", COBOL), (".CBL", COBOL), ("SOURCE", COBOL), (".SRC", COBOL),
    ("STOREDPROC", SQL), ("SPROC", SQL), (".SQL", SQL), (".DDL", SQL),
    ("BIND", BIND),
    ("CNTL", CONTROL), ("CONTROL", CONTROL), ("SYSIN", CONTROL),
    ("PARMLIB", CONTROL),
)

# ---------------------------------------------------------------- limits

# Files larger than this are still indexed, but reported in the blind-spot
# section so a skipped 400 MB member never masquerades as "no dependencies".
DEFAULT_MAX_FILE_MB = 256

# Directories never worth crawling on a mainframe export share.
SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__",
    "$recycle.bin", "system volume information",
}

# Binary sniff: if this fraction of the first block is non-text, skip the file.
BINARY_NUL_THRESHOLD = 0.01
SNIFF_BYTES = 8192

# ---------------------------------------------------------------- tuning

DEFAULT_WORKERS = 0          # 0 = os.cpu_count()
DEFAULT_CHUNKSIZE = 24       # files per IPC round-trip; SMB latency amortiser
BULK_LOAD_FILES = 5000       # at/above this, drop indexes and rebuild after

# ---------------------------------------------------------------- SQL model

# Access classes attached to every table reference.
READ = "READ"
WRITE = "WRITE"
DDL = "DDL"
LOCK = "LOCK"
DECLARE = "DECLARE"
UTILITY = "UTILITY"

# Confidence tiers. Anything below CERTAIN is surfaced separately in reports so
# a heuristic hit is never presented as a parsed fact.
CERTAIN = 100      # parsed from a real SQL/JCL/utility grammar
LIKELY = 70        # parsed, but the statement had unresolved symbolics
HEURISTIC = 40     # literal-string match inside dynamic SQL, comments, etc.
