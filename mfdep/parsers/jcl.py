"""JCL and PROC parser.

The transitive chain lives here. A table is rarely named in JCL; the JCL names
a *program*, and the program holds the SQL. Two mainframe idioms make the link
invisible to grep and both are handled explicitly:

  IKJEFT01  the step runs the TSO monitor, and the real program name is buried
            in the SYSTSIN instream data as ``RUN PROGRAM(MYPGM)``. Grepping
            for the program finds nothing; every DB2 batch shop does this.
  DSNUTILB  the step runs the utility driver, and the table is named in SYSIN
            control cards (``LOAD DATA ... INTO TABLE X``), not in any SQL.

Also handles JCL statement continuation, quoted operands containing commas,
instream data with custom DLM, symbolic parameter substitution, and PROC
invocation by both keyword and positional form.
"""

from __future__ import annotations

import re

from ..config import CERTAIN, LIKELY
from ..facts import FileFacts
from ..util import parse_dsn, squash

# Programs that are wrappers - the interesting name is inside instream data.
TSO_DRIVERS = {"IKJEFT01", "IKJEFT1A", "IKJEFT1B", "IKJEFT01A"}
UTILITY_DRIVER = {"DSNUTILB", "DSNUTIL"}
SQL_PROCESSORS = {"DSNTEP2", "DSNTEP4", "DSNTIAD", "DSNTIAUL", "DSNTIAP"}

_RUN_PROGRAM = re.compile(r"\bRUN\s+PROGRAM\s*\(\s*([A-Za-z0-9$#@]+)\s*\)", re.I)
_RUN_PLAN = re.compile(r"\bPLAN\s*\(\s*([A-Za-z0-9$#@]+)\s*\)", re.I)
_DSN_SYSTEM = re.compile(r"\bDSN\s+SYSTEM\s*\(\s*([A-Za-z0-9$#@]+)\s*\)", re.I)
_BIND = re.compile(
    r"\bBIND\s+(PLAN|PACKAGE)\s*\(([^)]*)\)(.*?)(?=\bBIND\b|\bEND\b|$)", re.I | re.S)
_BIND_MEMBER = re.compile(r"\bMEMBER\s*\(\s*([A-Za-z0-9$#@]+)\s*\)", re.I)
_BIND_QUALIFIER = re.compile(r"\bQUALIFIER\s*\(\s*([A-Za-z0-9$#@_]+)\s*\)", re.I)

_SYMBOL = re.compile(r"&([A-Za-z@#$][A-Za-z0-9@#$]{0,7})\.?")


class _Stmt:
    __slots__ = ("line", "name", "op", "operands", "raw")

    def __init__(self, line: int, name: str, op: str, operands: str, raw: str):
        self.line = line
        self.name = name
        self.op = op
        self.operands = operands
        self.raw = raw


class _Instream:
    __slots__ = ("line", "dd", "text")

    def __init__(self, line: int, dd: str, text: str):
        self.line = line
        self.dd = dd
        self.text = text


# ---------------------------------------------------------------- lexing

def _scan_operands(text: str) -> tuple[str, bool]:
    """Return the operand field and whether the statement continues.

    The operand field ends at the first blank that is not inside quotes;
    everything after is a comment. A trailing comma means continuation - so
    ``PARM='A,B'  SOME COMMENT`` must not be read as continuing.
    """
    out = []
    in_quote = False
    for ch in text:
        if ch == "'":
            in_quote = not in_quote
            out.append(ch)
        elif ch == " " and not in_quote:
            break
        else:
            out.append(ch)
    operands = "".join(out)
    return operands, operands.endswith(",")


def _lex(lines: list[str]) -> tuple[list[_Stmt], list[_Instream]]:
    """Fold continuations into logical statements and pull out instream data."""
    stmts: list[_Stmt] = []
    instream: list[_Instream] = []

    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.rstrip()

        if not stripped.startswith("//"):
            i += 1
            continue
        if stripped.startswith("//*"):                      # comment
            i += 1
            continue
        if stripped == "//":                                # end of job
            i += 1
            continue

        start_line = i + 1
        body = stripped[2:]
        m = re.match(r"([A-Za-z0-9$#@]*)\s+(\S+)\s*(.*)$", body)
        if not m:
            i += 1
            continue

        name, op, rest = m.group(1).upper(), m.group(2).upper(), m.group(3)
        operands, cont = _scan_operands(rest)
        raw_all = [stripped]

        while cont and i + 1 < n:
            nxt = lines[i + 1].rstrip()
            if not nxt.startswith("//") or nxt.startswith("//*"):
                break
            tail = nxt[2:].lstrip()
            if not tail:
                break
            more, cont = _scan_operands(tail)
            operands += more
            raw_all.append(nxt)
            i += 1

        stmts.append(_Stmt(start_line, name, op, operands, " ".join(raw_all)))

        # ---- instream data: //DD DD * or DD DATA
        if op == "DD" and re.match(r"^\*(,|$)|^DATA(,|$)", operands, re.I):
            dlm_m = re.search(r"\bDLM=(?:'([^']{1,2})'|(\S{1,2}))", operands, re.I)
            dlm = (dlm_m.group(1) or dlm_m.group(2)) if dlm_m else "/*"
            data, i = _read_instream(lines, i + 1, dlm, bool(dlm_m))
            instream.append(_Instream(start_line, name or "SYSIN", data))
            continue

        i += 1

    return stmts, instream


def _read_instream(lines: list[str], start: int, dlm: str,
                   custom_dlm: bool) -> tuple[str, int]:
    """Collect instream data until the delimiter or the next JCL statement."""
    out = []
    i = start
    n = len(lines)
    while i < n:
        raw = lines[i]
        if custom_dlm:
            if raw.startswith(dlm):
                i += 1
                break
        else:
            if raw.startswith("/*") or raw.startswith("//"):
                if raw.startswith("/*"):
                    i += 1
                break
        out.append(raw)
        i += 1
    return "\n".join(out), i


# ---------------------------------------------------------------- symbolics

def _substitute(text: str, symbols: dict[str, str]) -> tuple[str, bool]:
    """Resolve ``&SYM`` from SET/PROC defaults. Returns (text, fully_resolved)."""
    unresolved = False

    def repl(m: re.Match) -> str:
        nonlocal unresolved
        key = m.group(1).upper()
        if key in symbols:
            return symbols[key]
        unresolved = True
        return m.group(0)

    return _SYMBOL.sub(repl, text), not unresolved


def _kv(operands: str) -> dict[str, str]:
    """Split ``A=1,B='x,y',C`` into a dict, respecting quotes and nesting."""
    out: dict[str, str] = {}
    cur = []
    depth = 0
    in_quote = False
    parts = []
    for ch in operands:
        if ch == "'":
            in_quote = not in_quote
            cur.append(ch)
        elif ch == "(" and not in_quote:
            depth += 1
            cur.append(ch)
        elif ch == ")" and not in_quote:
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0 and not in_quote:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))

    for p in parts:
        if "=" in p:
            k, _, v = p.partition("=")
            out[k.strip().upper()] = v.strip().strip("'")
    return out


# ---------------------------------------------------------------- parsing

def parse(path: str, lines: list[str], kind: str) -> FileFacts:
    from . import control as control_mod          # local: avoids import cycle
    from . import sqlfile as sql_mod

    facts = FileFacts(path=path, kind=kind, line_count=len(lines))
    stmts, instream = _lex(lines)
    by_line = {s.line: s for s in stmts}

    symbols: dict[str, str] = {}
    current_job = ""
    current_step = ""
    step_seq = 0
    steps_by_line: dict[int, tuple] = {}

    # Instream keyed by the DD statement's line, so a step can find its own.
    instream_by_line = {b.line: b for b in instream}

    for idx, s in enumerate(stmts):
        if s.op == "JOB":
            current_job = s.name
            facts.jobs.append((s.line, s.name))
            continue

        if s.op == "PROC":
            # A PROC's own statement carries the symbolic defaults.
            symbols.update(_kv(s.operands))
            if s.name:
                facts.jobs.append((s.line, s.name))
                current_job = current_job or s.name
            continue

        if s.op == "SET":
            symbols.update(_kv(s.operands))
            continue

        if s.op == "INCLUDE":
            member = _kv(s.operands).get("MEMBER", "")
            if member:
                facts.copy_refs.append((s.line, member.upper(), "JCL-INCLUDE"))
            continue

        if s.op == "EXEC":
            step_seq += 1
            current_step = s.name or f"STEP{step_seq:03d}"
            resolved, ok = _substitute(s.operands, symbols)
            kv = _kv(resolved)

            pgm = kv.get("PGM", "").upper()
            proc = kv.get("PROC", "").upper()
            if not pgm and not proc:
                # Positional PROC invocation: //S1 EXEC MYPROC,PARM=...
                first = resolved.split(",")[0].strip().upper()
                if first and "=" not in first:
                    proc = first
            symbols.update({k: v for k, v in kv.items()
                            if k not in ("PGM", "PROC")})

            steps_by_line[s.line] = (current_job, current_step, step_seq,
                                     pgm, proc, ok)
            continue

        if s.op == "DD":
            resolved, ok = _substitute(s.operands, symbols)
            kv = _kv(resolved)
            dsn = kv.get("DSN", kv.get("DSNAME", ""))
            if dsn:
                dsname, key, is_member = parse_dsn(dsn)
                facts.dds.append((s.line, current_step, s.name, dsn.upper(),
                                  dsname, key, int(is_member), kv.get("DISP", "")))
                if not ok:
                    facts.blind_spots.append(
                        (s.line, "UNRESOLVED-SYMBOLIC",
                         f"{current_step} DD {s.name} DSN={dsn} - symbolic not "
                         f"resolved, so any control deck it points at is unlinked"))

    # ---- resolve each step's real program using its instream data
    step_lines = sorted(steps_by_line)
    for pos, sline in enumerate(step_lines):
        job, step, seq, pgm, proc, ok = steps_by_line[sline]
        end = step_lines[pos + 1] if pos + 1 < len(step_lines) else 10 ** 9
        blocks = {b.dd: b for b in instream if sline < b.line < end}

        resolved_pgm = pgm
        confidence = CERTAIN if ok else LIKELY

        if pgm in TSO_DRIVERS:
            tso = blocks.get("SYSTSIN")
            if tso:
                rp = _RUN_PROGRAM.search(tso.text)
                if rp:
                    resolved_pgm = rp.group(1).upper()
                _harvest_bind(facts, tso)
            if resolved_pgm == pgm:
                facts.blind_spots.append(
                    (sline, "UNRESOLVED-TSO-STEP",
                     f"{job}/{step} runs {pgm} but no RUN PROGRAM() found in SYSTSIN"))

        facts.steps.append((sline, job, step, seq, pgm, proc, resolved_pgm))

        if proc and not pgm:
            facts.copy_refs.append((sline, proc, "PROC-CALL"))

        # ---- route instream data to the right sub-parser
        driver = resolved_pgm or pgm
        for dd, block in blocks.items():
            if dd in ("SYSTSIN", "SYSTSPRT", "SYSPRINT", "SYSREC", "SYSPUNCH"):
                continue
            if not block.text.strip():
                continue
            ctx = f"{job}/{step} DD {dd}"

            if pgm in UTILITY_DRIVER or driver in UTILITY_DRIVER:
                control_mod.scan_text(facts, block.text, block.line + 1,
                                      confidence, via=ctx)
            elif driver in SQL_PROCESSORS:
                sql_mod.scan_text(facts, block.text, block.line + 1,
                                  confidence, via=ctx)
            else:
                # Unknown driver: the deck could be a sort card, a report parm
                # list, anything. Both scanners gate on their own grammar shape
                # first, so a non-match costs nothing and never invents a ref.
                control_mod.scan_text(facts, block.text, block.line + 1,
                                      confidence, via=ctx)
                sql_mod.scan_text(facts, block.text, block.line + 1,
                                  confidence, via=ctx, guarded=True)

    return facts


def _harvest_bind(facts: FileFacts, block: _Instream) -> None:
    """Record BIND cards: they tie a DBRM/package to its default qualifier.

    The qualifier is what turns an unqualified ``FROM CUSTOMER`` in a program
    into a real schema at run time, so it is the missing half of every
    unqualified reference in the codebase.
    """
    for m in _BIND.finditer(block.text):
        body = m.group(0)
        member = _BIND_MEMBER.search(body)
        qual = _BIND_QUALIFIER.search(body)
        detail = squash(body)[:160]
        facts.blind_spots.append(
            (block.line, "BIND",
             f"{m.group(1).upper()}({squash(m.group(2))[:40]})"
             + (f" MEMBER={member.group(1).upper()}" if member else "")
             + (f" QUALIFIER={qual.group(1).upper()}" if qual else "")
             + f" | {detail}"))
