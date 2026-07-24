"""Answer 'what depends on this table' by closing over the index.

The direct SQL hits are the easy half. The half that matters operationally is
the transitive chain, because the thing that breaks when you alter a table is a
*job*, and no job ever mentions the table:

    DCLGEN copybook  -> copied by ->  program
    program          -> called by ->  program
    program          -> run by    ->  PROC step
    PROC             -> invoked by->  JCL step
    JCL              -> is        ->  the job that fails at 03:00

Views, synonyms and aliases are followed on the object side, so a program that
only ever selects from V_CUSTOMER is still reported as depending on CUSTOMER.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .config import HEURISTIC, READ
from .store import Store
from .util import display_path, dsn_path_score, split_qualified
from .vendor import classify_module

MAX_DEPTH = 12


@dataclass
class Ref:
    """One concrete piece of evidence in a file."""
    path: str
    member: str
    kind: str
    line: int
    schema: str
    table: str
    access: str
    stmt: str
    confidence: int
    snippet: str
    via: str


@dataclass
class ProgramNode:
    name: str
    files: list[str] = field(default_factory=list)
    depth: int = 0
    reason: str = ""


@dataclass
class StepNode:
    job: str
    step: str
    seq: int
    pgm: str
    resolved_pgm: str
    path: str
    line: int
    reason: str
    kind: str = "JCL"        # JCL (a real job) vs PROC (a cataloged procedure)
    file_id: int = 0
    # (dd, dsn, disp) for this step, and (dd, dsn, deck_kind, path) for the
    # DD references that resolve to a real member on the share.
    datasets: list = field(default_factory=list)
    decks: list = field(default_factory=list)


@dataclass
class DataLink:
    """A step that shares a dataset with a step that touches the table.

    Not a SQL dependency, and deliberately reported separately - but a sort
    deck's ``FIELDS=(1,10,CH,A)`` and a LOAD deck's ``POSITION(1) CHAR(10)``
    both hard-code the table's physical layout. Widen a column and they break
    silently, with no SQL anywhere to find by searching.
    """
    job: str
    step: str
    pgm: str
    dataset: str
    direction: str        # reads / writes
    via_job: str          # the table-touching step it shares the dataset with
    path: str
    line: int
    kind: str
    decks: list = field(default_factory=list)


@dataclass
class Result:
    spec: str
    targets: list[tuple[str, str, str]] = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    programs: dict[str, ProgramNode] = field(default_factory=dict)
    copybooks: dict[str, str] = field(default_factory=dict)
    steps: list[StepNode] = field(default_factory=list)
    jobs: dict[str, list[StepNode]] = field(default_factory=dict)
    blind_spots: list[tuple[str, int, str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    data_links: list[DataLink] = field(default_factory=list)


def _chunks(seq, n=400):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class Analyzer:
    def __init__(self, store: Store):
        self.db: sqlite3.Connection = store.conn

    # ------------------------------------------------------------ targets

    def resolve_targets(self, spec: str) -> list[tuple[str, str, str]]:
        """Expand a table name into every object that stands in for it.

        Returns ``(schema, table, why)``. An unqualified spec matches the table
        in every schema, which is correct on a mainframe where the same table
        exists in DEV/TEST/PROD qualifiers - the report groups by schema so the
        ambiguity is visible rather than silently resolved.
        """
        schema, table = split_qualified(spec)
        targets: dict[tuple[str, str], str] = {}
        seen_names: set[tuple[str, str]] = set()

        if schema:
            targets[(schema, table)] = "target table"
        else:
            rows = self.db.execute(
                "SELECT DISTINCT schema, table_name FROM table_refs "
                "WHERE table_name=?", (table,)).fetchall()
            for r in rows:
                targets[(r["schema"] or "", table)] = "target table"
            if not rows:
                targets[("", table)] = "target table"

        # Follow views / synonyms / aliases outward, transitively.
        frontier = list(targets)
        depth = 0
        while frontier and depth < MAX_DEPTH:
            depth += 1
            nxt = []
            for sch, tbl in frontier:
                if (sch, tbl) in seen_names:
                    continue
                seen_names.add((sch, tbl))

                # synonym / alias pointing at this object
                for r in self.db.execute(
                        "SELECT schema, name, obj_type FROM objects "
                        "WHERE parent_name=? AND (parent_schema=? OR parent_schema='' "
                        "OR ?='') AND obj_type IN ('SYNONYM','ALIAS')",
                        (tbl, sch, sch)):
                    key = (r["schema"] or "", r["name"])
                    if key not in targets:
                        targets[key] = f"{r['obj_type'].lower()} for {sch}.{tbl}".strip(".")
                        nxt.append(key)

                # views whose definition file reads this object
                for r in self.db.execute(
                        "SELECT DISTINCT o.schema, o.name FROM objects o "
                        "JOIN table_refs t ON t.file_id=o.file_id "
                        "WHERE o.obj_type='VIEW' AND t.table_name=? "
                        "AND (t.schema=? OR t.schema='' OR ?='')",
                        (tbl, sch, sch)):
                    key = (r["schema"] or "", r["name"])
                    if key not in targets:
                        targets[key] = f"view over {sch}.{tbl}".strip(".")
                        nxt.append(key)

                # the tablespace the table lives in, so REORG/COPY jobs surface
                for r in self.db.execute(
                        "SELECT parent_schema, parent_name FROM objects "
                        "WHERE obj_type='IN-TABLESPACE' AND name=? "
                        "AND (schema=? OR schema='' OR ?='')", (tbl, sch, sch)):
                    key = (r["parent_schema"] or "", r["parent_name"])
                    if key not in targets:
                        targets[key] = f"tablespace holding {sch}.{tbl}".strip(".")
                        nxt.append(key)
            frontier = nxt

        return [(s, t, why) for (s, t), why in targets.items()]

    # ------------------------------------------------------------ refs

    def refs_for(self, targets, min_confidence: int = 0) -> list[Ref]:
        out: list[Ref] = []
        by_table: dict[str, set[str]] = {}
        for sch, tbl, _ in targets:
            by_table.setdefault(tbl, set()).add(sch)

        names = list(by_table)
        for batch in _chunks(names):
            marks = ",".join("?" * len(batch))
            rows = self.db.execute(
                f"SELECT r.*, f.path, f.member, f.kind FROM table_refs r "
                f"JOIN files f ON f.id=r.file_id "
                f"WHERE r.table_name IN ({marks}) AND r.confidence >= ?",
                (*batch, min_confidence)).fetchall()
            for r in rows:
                wanted = by_table[r["table_name"]]
                got = r["schema"] or ""
                # '' on either side means unqualified - keep it and let the
                # report flag the ambiguity rather than dropping a real hit.
                if wanted != {""} and got and got not in wanted and "" not in wanted:
                    continue
                out.append(Ref(display_path(r["path"]), r["member"], r["kind"],
                               r["line"], got, r["table_name"], r["access"],
                               r["stmt"], r["confidence"], r["snippet"] or "",
                               r["via"] or ""))
        out.sort(key=lambda x: (-x.confidence, x.path, x.line))
        return out

    # ------------------------------------------------------------ closure

    # Kinds where the PDS member name is also the load-module name, so a JCL
    # step can EXEC PGM=<member>. For JCL, PROC, CONTROL and DDL members it is
    # not - treating those as programs fills the report with things that are
    # plainly not programs (a DDL script listed as running under a step).
    _MEMBER_IS_PROGRAM = {"COBOL", "UNKNOWN"}

    def _programs_of_file(self, file_id: int, member: str, kind: str) -> set[str]:
        names = {r["name"] for r in self.db.execute(
            "SELECT name FROM programs WHERE file_id=?", (file_id,))}
        if kind in self._MEMBER_IS_PROGRAM:
            names.add(member)
        return {n for n in names if n}

    def _file_ids_for_refs(self, targets, min_confidence: int) -> dict[int, str]:
        ids: dict[int, str] = {}
        names = list({t for _, t, _ in targets})
        for batch in _chunks(names):
            marks = ",".join("?" * len(batch))
            for r in self.db.execute(
                    f"SELECT DISTINCT file_id, access FROM table_refs "
                    f"WHERE table_name IN ({marks}) AND confidence >= ?",
                    (*batch, min_confidence)):
                ids.setdefault(r["file_id"], r["access"])
        return ids

    def analyze(self, spec: str, min_confidence: int = 0,
                include_read: bool = True, data_hops: int = 1) -> Result:
        res = Result(spec=spec)
        res.targets = self.resolve_targets(spec)
        res.refs = self.refs_for(res.targets, min_confidence)

        if not include_read:
            res.refs = [r for r in res.refs if r.access != READ]

        seed_files = self._file_ids_for_refs(res.targets, min_confidence)

        # ---- copybook / DCLGEN fan-out
        dclgen_members: set[str] = set()
        for fid in seed_files:
            row = self.db.execute(
                "SELECT member, kind FROM files WHERE id=?", (fid,)).fetchone()
            if row and row["kind"] in ("DCLGEN", "COPYBOOK"):
                dclgen_members.add(row["member"])

        copy_files: dict[int, str] = {}
        frontier = set(dclgen_members)
        depth = 0
        while frontier and depth < MAX_DEPTH:
            depth += 1
            nxt: set[str] = set()
            for batch in _chunks(sorted(frontier)):
                marks = ",".join("?" * len(batch))
                for r in self.db.execute(
                        f"SELECT c.file_id, f.member, f.kind, c.member AS copied "
                        f"FROM copy_refs c JOIN files f ON f.id=c.file_id "
                        f"WHERE c.member IN ({marks})", batch):
                    if r["file_id"] in copy_files:
                        continue
                    copy_files[r["file_id"]] = (
                        f"copies {r['copied']} (depth {depth})")
                    res.copybooks[r["copied"]] = r["copied"]
                    if r["kind"] in ("DCLGEN", "COPYBOOK"):
                        nxt.add(r["member"])
            frontier = nxt

        # ---- program set: seeds + copybook consumers + reverse call graph
        programs: dict[str, ProgramNode] = {}

        def add_file_programs(fid: int, reason: str, depth: int) -> set[str]:
            row = self.db.execute(
                "SELECT path, member, kind FROM files WHERE id=?", (fid,)).fetchone()
            if not row:
                return set()
            added = set()
            for name in self._programs_of_file(fid, row["member"], row["kind"]):
                node = programs.get(name)
                if node is None:
                    node = ProgramNode(name=name, depth=depth, reason=reason)
                    programs[name] = node
                    added.add(name)
                if display_path(row["path"]) not in node.files:
                    node.files.append(display_path(row["path"]))
            return added

        frontier_names: set[str] = set()
        for fid, access in seed_files.items():
            frontier_names |= add_file_programs(fid, f"{access} on the table", 0)
        for fid, why in copy_files.items():
            frontier_names |= add_file_programs(fid, why, 1)

        depth = 0
        while frontier_names and depth < MAX_DEPTH:
            depth += 1
            nxt: set[str] = set()
            for batch in _chunks(sorted(frontier_names)):
                marks = ",".join("?" * len(batch))
                for r in self.db.execute(
                        f"SELECT DISTINCT c.file_id, c.callee FROM calls c "
                        f"WHERE c.callee IN ({marks})", batch):
                    nxt |= add_file_programs(
                        r["file_id"], f"calls {r['callee']} (depth {depth})", depth)
            frontier_names = nxt

        res.programs = programs

        # ---- JCL: steps running any of those programs, then PROC expansion
        step_rows: list[StepNode] = []
        seen_steps: set[tuple[int, int]] = set()
        proc_frontier: set[str] = set()

        def collect_steps(names: set[str], reason_fmt: str, depth: int) -> None:
            for batch in _chunks(sorted(names)):
                marks = ",".join("?" * len(batch))
                for r in self.db.execute(
                        f"SELECT s.*, f.path, f.member, f.kind FROM steps s "
                        f"JOIN files f ON f.id=s.file_id "
                        f"WHERE s.resolved_pgm IN ({marks}) "
                        f"   OR s.pgm IN ({marks})", (*batch, *batch)):
                    key = (r["file_id"], r["line"])
                    if key in seen_steps:
                        continue
                    seen_steps.add(key)
                    node = StepNode(
                        job=r["job"] or r["member"], step=r["step"] or "",
                        seq=r["seq"] or 0, pgm=r["pgm"] or "",
                        resolved_pgm=r["resolved_pgm"] or "",
                        path=display_path(r["path"]), line=r["line"],
                        reason=reason_fmt.format(
                            pgm=r["resolved_pgm"] or r["pgm"]),
                        kind=r["kind"], file_id=r["file_id"])
                    step_rows.append(node)
                    if r["kind"] in ("PROC",):
                        proc_frontier.add(r["member"])

        collect_steps(set(programs), "runs {pgm}", 0)

        # A JCL member can name the table directly, in utility control cards or
        # instream SQL, without running any program we would recognise. That is
        # the LOAD REPLACE job - the single most destructive thing that happens
        # to the table - so it must never be missing from the job list.
        self._steps_from_direct_refs(seed_files, res.targets, min_confidence,
                                     seen_steps, step_rows, proc_frontier)

        # Most shops do not inline their utility decks; they catalog them and
        # point at them: //SYSIN DD DSN=PROD.CNTL(LOADCUST). Without resolving
        # that reference the LOAD card is found but the job that runs it is
        # not, so the report says the table has no jobs at all.
        self._steps_from_control_decks(seed_files, seen_steps, step_rows,
                                       proc_frontier, res)

        depth = 0
        while proc_frontier and depth < MAX_DEPTH:
            depth += 1
            current = proc_frontier
            proc_frontier = set()
            for batch in _chunks(sorted(current)):
                marks = ",".join("?" * len(batch))
                for r in self.db.execute(
                        f"SELECT s.*, f.path, f.member, f.kind FROM steps s "
                        f"JOIN files f ON f.id=s.file_id "
                        f"WHERE s.proc IN ({marks})", batch):
                    key = (r["file_id"], r["line"])
                    if key in seen_steps:
                        continue
                    seen_steps.add(key)
                    step_rows.append(StepNode(
                        job=r["job"] or r["member"], step=r["step"] or "",
                        seq=r["seq"] or 0, pgm=r["pgm"] or "",
                        resolved_pgm=r["proc"] or "",
                        path=display_path(r["path"]), line=r["line"],
                        reason=f"invokes PROC {r['proc']} (depth {depth})",
                        kind=r["kind"], file_id=r["file_id"]))
                    if r["kind"] == "PROC":
                        proc_frontier.add(r["member"])

        res.steps = sorted(step_rows, key=lambda s: (s.job, s.seq, s.step))
        for s in res.steps:
            res.jobs.setdefault(s.job, []).append(s)

        self.attach_datasets(res)
        self.attach_decks(res)
        # extend, not assign: the deck resolver already recorded the references
        # it could not confirm, and those must not be thrown away here.
        res.notes = self._notes(res)
        self.trace_data(res, hops=data_hops)
        self._unresolved_calls(programs, res)
        res.blind_spots.extend(self._blind_spots(seed_files, programs))
        return res

    def _steps_from_direct_refs(self, seed_files, targets, min_confidence,
                                seen_steps, step_rows, proc_frontier) -> None:
        """Attribute a table reference inside a JCL member to its own step.

        Each ref is assigned to the nearest preceding EXEC, which is how JCL
        actually scopes instream data, so the report names the failing step
        rather than just the member.
        """
        names = list({t for _, t, _ in targets})
        if not names:
            return

        for fid in seed_files:
            row = self.db.execute(
                "SELECT path, member, kind FROM files WHERE id=?", (fid,)).fetchone()
            if not row or row["kind"] not in ("JCL", "PROC"):
                continue

            steps = self.db.execute(
                "SELECT * FROM steps WHERE file_id=? ORDER BY line", (fid,)).fetchall()

            ref_rows = []
            for batch in _chunks(names):
                marks = ",".join("?" * len(batch))
                ref_rows += self.db.execute(
                    f"SELECT line, access, stmt FROM table_refs WHERE file_id=? "
                    f"AND table_name IN ({marks}) AND confidence >= ?",
                    (fid, *batch, min_confidence)).fetchall()

            for ref in ref_rows:
                owner = None
                for s in steps:
                    if s["line"] <= ref["line"]:
                        owner = s
                    else:
                        break

                line = owner["line"] if owner else ref["line"]
                key = (fid, line)
                if key in seen_steps:
                    continue
                seen_steps.add(key)
                step_rows.append(StepNode(
                    job=(owner["job"] if owner else "") or row["member"],
                    step=(owner["step"] if owner else "") or "(no step)",
                    seq=(owner["seq"] if owner else 0),
                    pgm=(owner["pgm"] if owner else ""),
                    resolved_pgm=(owner["resolved_pgm"] if owner else ""),
                    path=display_path(row["path"]), line=line,
                    reason=f"{ref['stmt']} on the table in this step's cards",
                    kind=row["kind"], file_id=fid))
                if row["kind"] == "PROC":
                    proc_frontier.add(row["member"])

    def _steps_from_control_decks(self, seed_files, seen_steps, step_rows,
                                  proc_frontier, res) -> None:
        """Link a JCL step to a cataloged control deck it points at by DSN.

        Matching is on the member name (indexed) and then verified against the
        dataset name, because member names collide constantly across libraries
        - every shop has a dozen members called LOAD01, and wiring a job to the
        wrong deck is worse than not wiring it at all. A member-name hit whose
        library does not match is reported as a blind spot rather than dropped
        silently or accepted.
        """
        for fid in list(seed_files):
            row = self.db.execute(
                "SELECT path, member, kind FROM files WHERE id=?", (fid,)).fetchone()
            if not row or row["kind"] in ("JCL", "PROC") or not row["member"]:
                continue

            for d in self.db.execute(
                    "SELECT d.*, f.path AS jpath, f.member AS jmember, "
                    "       f.kind AS jkind, f.id AS jid "
                    "FROM dds d JOIN files f ON f.id=d.file_id "
                    "WHERE d.lookup_key=?", (row["member"],)):

                score = dsn_path_score(d["dsname"], row["path"])

                # score 3 = full dataset name in the path, 2 = last two
                # qualifiers, 1 = last qualifier only. A score of 1 is almost
                # meaningless on its own ("CNTL" matches every control library),
                # so it is only trusted when no other member of that name
                # exists - which is the case for a flattened export where the
                # library structure was not preserved.
                if score == 1:
                    twins = self.db.execute(
                        "SELECT COUNT(*) c FROM files WHERE member=?",
                        (row["member"],)).fetchone()["c"]
                    if twins > 1:
                        score = 0

                if score < 2:
                    res.blind_spots.append((
                        display_path(d["jpath"]), d["line"], "UNRESOLVED-DECK-REF",
                        f"{d['step']} DD {d['dd']} DSN={d['dsn']} - member name "
                        f"matches {display_path(row['path'])} but the library "
                        f"does not, so the two were not linked"))
                    continue

                step = self.db.execute(
                    "SELECT * FROM steps WHERE file_id=? AND step=? "
                    "ORDER BY line LIMIT 1", (d["jid"], d["step"])).fetchone()

                line = step["line"] if step else d["line"]
                key = (d["jid"], line)
                if key in seen_steps:
                    continue
                seen_steps.add(key)

                deck = "control deck" if row["kind"] != "SORT" else "sort/merge deck"
                certainty = "" if score >= 2 else " (library match is weak)"
                step_rows.append(StepNode(
                    job=(step["job"] if step else "") or d["jmember"],
                    step=d["step"] or (step["step"] if step else "") or "(no step)",
                    seq=(step["seq"] if step else 0),
                    pgm=(step["pgm"] if step else ""),
                    resolved_pgm=(step["resolved_pgm"] if step else ""),
                    path=display_path(d["jpath"]), line=line,
                    reason=f"DD {d['dd']} reads {deck} {d['dsn']}{certainty}",
                    kind=d["jkind"], file_id=d["jid"]))

                if d["jkind"] == "PROC":
                    proc_frontier.add(d["jmember"])

    def attach_datasets(self, res: Result, limit: int = 12) -> None:
        """Record the datasets each dependent step reads and writes.

        This is what makes an UNLOAD -> sort -> LOAD chain visible: the sort
        deck itself never names a table, but the dataset flowing through it is
        right there on the step's DD statements.
        """
        for s in res.steps:
            if not s.file_id or not s.step:
                continue
            rows = self.db.execute(
                "SELECT dd, dsn, disp FROM dds WHERE file_id=? AND step=? "
                "ORDER BY line LIMIT ?", (s.file_id, s.step, limit)).fetchall()
            s.datasets = [(r["dd"], r["dsn"], r["disp"] or "") for r in rows
                          if r["dd"] not in ("STEPLIB", "JOBLIB", "SYSPRINT",
                                             "SYSOUT", "SYSUDUMP", "SYSTSPRT")]

    def attach_decks(self, res: Result) -> None:
        """Resolve every dependent step's DD references to files on the share.

        Shows what each step's cards actually are - a LOAD deck, a sort deck, a
        BIND deck - so a chain that runs through cataloged members is readable
        end to end instead of stopping at a dataset name.
        """
        for s in res.steps:
            if not s.file_id or not s.step:
                continue
            found = []
            for d in self.db.execute(
                    "SELECT dd, dsn, dsname, lookup_key FROM dds "
                    "WHERE file_id=? AND step=? AND lookup_key != ''",
                    (s.file_id, s.step)):
                for f in self.db.execute(
                        "SELECT path, kind FROM files WHERE member=?",
                        (d["lookup_key"],)):
                    if dsn_path_score(d["dsname"], f["path"]) >= 2:
                        found.append((d["dd"], d["dsn"], f["kind"],
                                      display_path(f["path"])))
                        break
            s.decks = found

    # Datasets this common are plumbing, not lineage - a control file or a
    # master parm library used by half the shop. Following them turns the
    # report into a listing of every job that exists.
    DATA_FANOUT_CAP = 40

    def trace_data(self, res: Result, hops: int = 1) -> None:
        """Follow datasets out of the table-touching steps, `hops` levels.

        This is what makes the UNLOAD -> sort -> LOAD chain visible: the sort
        step names no table, but it reads the dataset the UNLOAD wrote.
        """
        if hops <= 0:
            return

        seen_steps = {(s.file_id, s.line) for s in res.steps}
        seen_dsn: set[str] = set()
        frontier: dict[str, str] = {}
        for s in res.steps:
            for _dd, dsn, _disp in s.datasets:
                base = dsn.split("(")[0].strip()
                if base:
                    frontier.setdefault(base, f"{s.job}/{s.step}")

        for hop in range(1, hops + 1):
            nxt: dict[str, str] = {}
            for base, origin in frontier.items():
                if base in seen_dsn:
                    continue
                seen_dsn.add(base)

                rows = self.db.execute(
                    "SELECT d.*, f.path, f.member, f.kind, f.id AS fid "
                    "FROM dds d JOIN files f ON f.id=d.file_id "
                    "WHERE d.dsname=? LIMIT ?",
                    (base, self.DATA_FANOUT_CAP + 1)).fetchall()

                if len(rows) > self.DATA_FANOUT_CAP:
                    res.notes.append(
                        f"Dataset {base} is referenced by more than "
                        f"{self.DATA_FANOUT_CAP} steps and was not followed - "
                        f"it looks like shared plumbing, not lineage.")
                    continue

                for r in rows:
                    step = self.db.execute(
                        "SELECT * FROM steps WHERE file_id=? AND step=? "
                        "ORDER BY line LIMIT 1", (r["fid"], r["step"])).fetchone()
                    line = step["line"] if step else r["line"]
                    if (r["fid"], line) in seen_steps:
                        continue
                    seen_steps.add((r["fid"], line))

                    disp = (r["disp"] or "").upper()
                    writes = disp.startswith("(NEW") or "CATLG" in disp or \
                        disp.startswith("(MOD") or disp.startswith("NEW")
                    link = DataLink(
                        job=(step["job"] if step else "") or r["member"],
                        step=r["step"] or "(no step)",
                        pgm=(step["resolved_pgm"] or step["pgm"]) if step else "",
                        dataset=r["dsn"], direction="writes" if writes else "reads",
                        via_job=origin, path=display_path(r["path"]),
                        line=line, kind=r["kind"])
                    res.data_links.append(link)

                    if hop < hops:
                        for d2 in self.db.execute(
                                "SELECT dsname FROM dds WHERE file_id=? AND step=?",
                                (r["fid"], r["step"])):
                            if d2["dsname"]:
                                nxt.setdefault(d2["dsname"], f"{link.job}/{link.step}")
            frontier = nxt

        self._attach_link_decks(res)

    def _attach_link_decks(self, res: Result) -> None:
        """Resolve DD deck references for the data-linked steps too, so a sort
        deck shows up by name rather than as an anonymous SORT step."""
        for link in res.data_links:
            row = self.db.execute(
                "SELECT id FROM files WHERE path=? OR path=?",
                (link.path, "\\\\?\\" + link.path)).fetchone()
            if not row:
                continue
            found = []
            for d in self.db.execute(
                    "SELECT dd, dsn, dsname, lookup_key FROM dds "
                    "WHERE file_id=? AND step=? AND lookup_key != ''",
                    (row["id"], link.step)):
                for f in self.db.execute(
                        "SELECT path, kind FROM files WHERE member=?",
                        (d["lookup_key"],)):
                    if dsn_path_score(d["dsname"], f["path"]) >= 2:
                        found.append((d["dd"], d["dsn"], f["kind"],
                                      display_path(f["path"])))
                        break
            link.decks = found

    def _unresolved_calls(self, programs: dict[str, ProgramNode],
                          res: Result) -> None:
        """Report CALL targets that are neither on the share nor vendor stubs.

        An unresolved call is a real hole: the dependency chain continues into
        a program we cannot see, so the job list may be incomplete. But every
        translated CICS program calls DFHEI1, so reporting all unresolved calls
        without filtering buries the one that matters under thousands that do
        not. Vendor modules are matched by prefix (see vendor.py) rather than
        by a list of names, because a list is wrong as soon as IBM ships a
        module that is not on it.
        """
        names = sorted(programs)
        if not names:
            return

        file_ids: set[int] = set()
        for batch in _chunks(names):
            marks = ",".join("?" * len(batch))
            for r in self.db.execute(
                    f"SELECT DISTINCT file_id FROM programs WHERE name IN ({marks})",
                    batch):
                file_ids.add(r["file_id"])

        callees: dict[str, list[tuple[str, int]]] = {}
        for batch in _chunks(sorted(file_ids)):
            marks = ",".join("?" * len(batch))
            for r in self.db.execute(
                    f"SELECT c.callee, c.dynamic, f.path, c.line FROM calls c "
                    f"JOIN files f ON f.id=c.file_id "
                    f"WHERE c.file_id IN ({marks}) AND c.dynamic=0", batch):
                callees.setdefault(r["callee"], []).append(
                    (display_path(r["path"]), r["line"]))

        vendor_hits: dict[str, int] = {}
        for callee, sites in sorted(callees.items()):
            if callee in programs:
                continue                              # already in the closure
            product = classify_module(callee)
            if product:
                vendor_hits.setdefault(product[0], []).append(callee)
                continue
            if self.db.execute("SELECT 1 FROM programs WHERE name=? LIMIT 1",
                               (callee,)).fetchone():
                continue
            if self.db.execute("SELECT 1 FROM files WHERE member=? LIMIT 1",
                               (callee,)).fetchone():
                continue
            path, line = sites[0]
            more = f" (+{len(sites) - 1} more site(s))" if len(sites) > 1 else ""
            res.blind_spots.append((
                path, line, "UNRESOLVED-CALL",
                f"CALL '{callee}' - no source for {callee} on the share, so "
                f"anything it does to the table is invisible{more}"))

        if vendor_hits:
            # Name them rather than just counting. A prefix rule that is too
            # broad would silently swallow a real application program, and the
            # only way anyone would notice is by seeing the name listed here.
            summary = "; ".join(
                f"{product}: {', '.join(sorted(mods))}"
                for product, mods in sorted(vendor_hits.items()))
            res.notes.append(
                f"Ignored these call targets as vendor runtime modules "
                f"(matched by prefix) - {summary}. If any of those is actually "
                f"one of your programs, its dependencies are being missed.")

    # ------------------------------------------------------------ blind spots

    def _blind_spots(self, seed_files: dict[int, str],
                     programs: dict[str, ProgramNode]) -> list[tuple[str, int, str, str]]:
        """Report what the scan could NOT resolve.

        A dependency report that hides its gaps is worse than no report: it
        reads as 'nothing else touches this table' when the truth is 'a program
        builds the name at run time and we cannot see it'.
        """
        out: list[tuple[str, int, str, str]] = []
        ids = set(seed_files)

        names = sorted(programs)
        for batch in _chunks(names):
            marks = ",".join("?" * len(batch))
            for r in self.db.execute(
                    f"SELECT DISTINCT file_id FROM programs WHERE name IN ({marks})",
                    batch):
                ids.add(r["file_id"])

        for batch in _chunks(sorted(ids)):
            marks = ",".join("?" * len(batch))
            for r in self.db.execute(
                    f"SELECT b.kind, b.line, b.detail, f.path FROM blind_spots b "
                    f"JOIN files f ON f.id=b.file_id "
                    f"WHERE b.file_id IN ({marks})", batch):
                out.append((display_path(r["path"]), r["line"], r["kind"],
                            r["detail"] or ""))

        # Global gaps: anything unreadable at all is a hole in the answer.
        for r in self.db.execute(
                "SELECT path, error FROM files WHERE error != '' LIMIT 200"):
            out.append((display_path(r["path"]), 0, "UNREADABLE", r["error"]))

        return out

    def _notes(self, res: Result) -> list[str]:
        notes: list[str] = []
        unqualified = sum(1 for r in res.refs if not r.schema)
        if unqualified:
            notes.append(
                f"{unqualified} reference(s) are unqualified in the source; their "
                f"schema is set at BIND time by the package QUALIFIER, so they "
                f"may or may not resolve to this table. Listed under schema '(none)'.")
        heur = sum(1 for r in res.refs if r.confidence <= HEURISTIC)
        if heur:
            notes.append(
                f"{heur} reference(s) came from dynamic-SQL string literals, not "
                f"from parsed statements. Treat as leads, not facts.")
        # Tablespace refs carry a database name, not a schema - counting them
        # here would report a schema ambiguity that does not exist.
        schemas = {r.schema for r in res.refs if r.schema and r.via != "tablespace"}
        if len(schemas) > 1:
            notes.append(
                f"The name exists under {len(schemas)} schemas: "
                f"{', '.join(sorted(schemas))}. Qualify the query to narrow it.")
        return notes
