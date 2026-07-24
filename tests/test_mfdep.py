"""Regression tests.

Each test pins one specific way a naive grep gets the answer wrong. Run with:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mfdep.graph import Analyzer                                    # noqa: E402
from mfdep.parsers.cobol import build_blob, is_free_format          # noqa: E402
from mfdep.scan import ScanOptions, run_index                       # noqa: E402
from mfdep.sqlscan import scan_sql                                  # noqa: E402
from mfdep.store import Store                                       # noqa: E402
from mfdep.util import mask_sql_noise, split_qualified              # noqa: E402
from make_fixtures import ROOT, build                               # noqa: E402


def tables(sql: str) -> set[tuple[str, str, str]]:
    refs, _ = scan_sql(mask_sql_noise(sql))
    return {(r.schema, r.table, r.access) for r in refs}


class TestSqlScanner(unittest.TestCase):
    """Precision: the scanner must not invent references."""

    def test_ignores_line_comment(self):
        self.assertEqual(tables("-- SELECT * FROM PROD.GHOST\nSELECT 1 FROM PROD.REAL"),
                         {("PROD", "REAL", "READ")})

    def test_ignores_block_comment(self):
        self.assertEqual(tables("/* FROM PROD.GHOST */ SELECT 1 FROM PROD.REAL"),
                         {("PROD", "REAL", "READ")})

    def test_ignores_string_literal(self):
        self.assertEqual(tables("SELECT 1 FROM PROD.REAL WHERE N='FROM PROD.GHOST'"),
                         {("PROD", "REAL", "READ")})

    def test_prefix_is_not_a_match(self):
        """CUSTOMER_HIST must never satisfy a query for CUSTOMER."""
        found = tables("SELECT 1 FROM PROD.CUSTOMER_HIST")
        self.assertEqual(found, {("PROD", "CUSTOMER_HIST", "READ")})
        self.assertNotIn(("PROD", "CUSTOMER", "READ"), found)

    def test_cte_is_not_a_table(self):
        found = tables("WITH T AS (SELECT * FROM PROD.REAL) SELECT * FROM T")
        self.assertEqual(found, {("PROD", "REAL", "READ")})

    def test_table_function_is_not_a_table(self):
        self.assertEqual(tables("SELECT * FROM TABLE(MYFN(1)) AS X"), set())

    def test_for_update_of_is_not_an_update(self):
        found = tables("SELECT A FROM PROD.T FOR UPDATE OF A")
        self.assertEqual(found, {("PROD", "T", "READ")})

    def test_delete_from_is_write_not_read(self):
        self.assertEqual(tables("DELETE FROM PROD.T WHERE A=1"),
                         {("PROD", "T", "WRITE")})

    def test_comma_join_finds_every_table(self):
        found = tables("SELECT * FROM PROD.A X, PROD.B AS Y, PROD.C WHERE X.I=Y.I")
        self.assertEqual(found, {("PROD", "A", "READ"), ("PROD", "B", "READ"),
                                 ("PROD", "C", "READ")})

    def test_access_classification(self):
        cases = {
            "INSERT INTO P.T VALUES(1)": "WRITE",
            "UPDATE P.T SET A=1": "WRITE",
            "MERGE INTO P.T AS X USING P.S AS Y ON X.I=Y.I": "WRITE",
            "CREATE TABLE P.T (A INT)": "DDL",
            "DROP TABLE P.T": "DDL",
            "ALTER TABLE P.T ADD B INT": "DDL",
            "LOCK TABLE P.T IN SHARE MODE": "LOCK",
            "DECLARE P.T TABLE (A INT)": "DECLARE",
        }
        for sql, access in cases.items():
            with self.subTest(sql=sql):
                self.assertIn(("P", "T", access), tables(sql))

    def test_three_part_name_drops_location(self):
        self.assertEqual(split_qualified("REMOTE.PROD.CUSTOMER"), ("PROD", "CUSTOMER"))

    def test_delimited_identifier(self):
        self.assertIn(("My Schema", "My Table", "READ"),
                      tables('SELECT * FROM "My Schema"."My Table"'))


class TestCobolFormat(unittest.TestCase):
    """Fixed-format column handling."""

    def test_sequence_and_ident_fields_are_stripped(self):
        line = f"{'000100':6} {'MOVE A TO B.':<65}"[:72] + "CUSTOMER"
        blob, _ = build_blob([line], free=False)
        self.assertIn("MOVE A TO B.", blob)
        self.assertNotIn("CUSTOMER", blob)   # ident field 73-80 must be dropped
        self.assertNotIn("000100", blob)     # sequence field 1-6 must be dropped

    def test_comment_line_dropped(self):
        blob, _ = build_blob(["000100* FROM PROD.GHOST"], free=False)
        self.assertEqual(blob.strip(), "")

    def test_continuation_rejoins_split_name(self):
        lines = [f"{'000100':6} {'EXEC SQL SELECT * FROM PROD.CUS':<65}"[:72],
                 f"{'000200':6}-{'TOMER END-EXEC.':<65}"[:72]]
        blob, _ = build_blob(lines, free=False)
        self.assertIn("PROD.CUSTOMER", blob)

    def test_free_format_detection(self):
        self.assertTrue(is_free_format(["IDENTIFICATION DIVISION.",
                                        "PROGRAM-ID. X.",
                                        "PROCEDURE DIVISION."]))
        self.assertFalse(is_free_format([f"{'000100':6} MOVE A TO B.",
                                         f"{'000200':6} MOVE C TO D."]))


class TestEndToEnd(unittest.TestCase):
    """Index the fixture library and interrogate it the way a user would."""

    @classmethod
    def setUpClass(cls):
        build()
        cls.tmp = tempfile.mkdtemp()
        cls.db = os.path.join(cls.tmp, "t.db")
        run_index(ScanOptions(roots=[ROOT], db_path=cls.db, quiet=True))
        cls.store = Store(cls.db)
        cls.res = Analyzer(cls.store).analyze("PRODDB.CUSTOMER")

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _members(self, access=None):
        return {r.member for r in self.res.refs
                if access is None or r.access == access}

    def test_no_phantom_from_comments_or_literals(self):
        found = {r.table for r in self.res.refs}
        self.assertNotIn("GHOST_TABLE", found)
        self.assertNotIn("NOTREAL", found)

    def test_ident_field_does_not_create_a_reference(self):
        """CUSTUPD has 'CUSTOMER' in columns 73-80 of every line."""
        custupd = [r for r in self.res.refs if r.member == "CUSTUPD"]
        self.assertEqual({r.stmt for r in custupd}, {"SELECT", "UPDATE"})

    def test_continuation_split_name_is_found(self):
        self.assertIn("CUSTCON", self._members("WRITE"))

    def test_utility_load_card_found(self):
        stmts = {r.stmt for r in self.res.refs if r.member == "CUSTLOAD"}
        self.assertIn("LOAD INTO TABLE", stmts)

    def test_reorg_reached_through_tablespace(self):
        self.assertTrue(any(r.stmt == "REORG TABLESPACE" and r.via == "tablespace"
                            for r in self.res.refs))

    def test_instream_sql_under_dsntep2_found(self):
        self.assertTrue(any(r.member == "CUSTLOAD" and r.stmt == "SELECT"
                            for r in self.res.refs))

    def test_view_consumer_is_a_dependency(self):
        self.assertIn("CUSTVW", self.res.programs)

    def test_synonym_resolved(self):
        self.assertIn("CUSTSYN", {t for _, t, _ in self.res.targets})

    def test_ikjeft01_run_program_resolved(self):
        """The PROC runs IKJEFT01; the real program is inside SYSTSIN."""
        steps = [s for s in self.res.steps if s.job == "CUSTPRC"]
        self.assertTrue(steps, "PROC step not found")
        self.assertEqual(steps[0].pgm, "IKJEFT01")
        self.assertEqual(steps[0].resolved_pgm, "CUSTUPD")

    def test_job_reaches_table_through_proc(self):
        self.assertIn("CUSTJOB1", self.res.jobs)

    def test_utility_job_listed_with_its_steps(self):
        steps = {s.step for s in self.res.jobs.get("CUSTLOAD", [])}
        self.assertEqual(steps, {"LOADSTEP", "REORGSTP", "SQLSTEP"})

    def test_call_graph_transitive(self):
        self.assertEqual(self.res.programs["CUSTDRV"].depth, 1)

    def test_dclgen_fanout(self):
        self.assertIn("CUSTRPT", self.res.programs)

    def test_dynamic_sql_recovered_but_flagged(self):
        dyn = [r for r in self.res.refs if r.member == "CUSTDYN"]
        self.assertTrue(dyn)
        self.assertEqual(dyn[0].confidence, 40)
        self.assertTrue(any(k == "DYNAMIC-SQL" for _, _, k, _ in self.res.blind_spots))

    def test_sort_deck_creates_no_dependency(self):
        """Instream data under an unknown program must not be read as SQL."""
        self.assertNotIn("SORTJOB", {r.member for r in self.res.refs})
        self.assertNotIn("SORTJOB", self.res.jobs)

    def test_unrelated_program_absent(self):
        self.assertNotIn("ORDPROC", self.res.programs)

    def test_customer_hist_is_a_different_table(self):
        self.assertNotIn("CUSTOMER_HIST", {r.table for r in self.res.refs})

    def test_jcl_comment_ignored(self):
        self.assertNotIn("GHOST_TABLE",
                         {r.table for r in self.res.refs if r.member == "CUSTJOB1"})

    # ---- cataloged control decks referenced by DSN rather than inlined

    def test_external_load_deck_links_its_job(self):
        """//SYSIN DD DSN=PROD.CNTL(LOADCUST) must reach the job."""
        self.assertIn("CUSTEXT", self.res.jobs)
        steps = {s.step for s in self.res.jobs["CUSTEXT"]}
        self.assertIn("LOADSTEP", steps)

    def test_external_unload_deck_links_its_step(self):
        self.assertIn("UNLDSTEP", {s.step for s in self.res.jobs["CUSTEXT"]})

    def test_deck_reference_names_the_resolved_member(self):
        load = [s for s in self.res.jobs["CUSTEXT"] if s.step == "LOADSTEP"][0]
        decks = {dd: (kind, dsn) for dd, dsn, kind, _ in load.decks}
        self.assertIn("SYSIN", decks)
        self.assertEqual(decks["SYSIN"][1], "PROD.CNTL(LOADCUST)")
        self.assertEqual(decks["SYSIN"][0], "CONTROL")

    def test_proc_pointing_at_external_deck_reaches_the_job(self):
        """CUSTJOB2 -> PROC CUSTLPRC -> DSN=PROD.CNTL(LOADCUST) -> table."""
        self.assertIn("CUSTLPRC", self.res.jobs)
        self.assertIn("CUSTJOB2", self.res.jobs)

    def test_member_name_collision_is_not_wired(self):
        """TEST.CNTL(LOADCUST) must not attach to a job naming PROD.CNTL."""
        for step in self.res.steps:
            for _dd, _dsn, _kind, path in step.decks:
                self.assertNotIn("TEST.CNTL", path)
        self.assertTrue(any(k == "UNRESOLVED-DECK-REF"
                            for _, _, k, _ in self.res.blind_spots),
                        "the colliding member should be reported, not dropped")

    def test_sort_deck_classified(self):
        row = self.store.conn.execute(
            "SELECT kind FROM files WHERE member='SORTCUST'").fetchone()
        self.assertEqual(row["kind"], "SORT")

    def test_sort_step_datasets_expose_the_chain(self):
        """UNLOAD writes a dataset, the sort step reads it - visible via DDs."""
        unld = [s for s in self.res.jobs["CUSTEXT"] if s.step == "UNLDSTEP"][0]
        dsns = {dsn for _dd, dsn, _disp in unld.datasets}
        self.assertTrue(any("PROD.CUST.EXTRACT" in d for d in dsns))

    def test_gdg_generation_is_not_treated_as_a_member(self):
        from mfdep.util import parse_dsn
        self.assertEqual(parse_dsn("PROD.CUST.EXTRACT(+1)"),
                         ("PROD.CUST.EXTRACT", "EXTRACT", False))
        self.assertEqual(parse_dsn("PROD.CNTL(LOADCUST)"),
                         ("PROD.CNTL", "LOADCUST", True))
        self.assertEqual(parse_dsn("'PROD.CNTL(MEM)'"), ("PROD.CNTL", "MEM", True))
        self.assertEqual(parse_dsn("&&TEMP")[2], False)

    # ---- dataset lineage: the sort/merge step names no table

    def test_sort_step_surfaced_as_data_link(self):
        steps = {(d.job, d.step) for d in self.res.data_links}
        self.assertIn(("CUSTEXT", "SORTSTEP"), steps)

    def test_data_link_names_the_sort_deck(self):
        link = [d for d in self.res.data_links
                if (d.job, d.step) == ("CUSTEXT", "SORTSTEP")][0]
        kinds = {kind for _dd, _dsn, kind, _p in link.decks}
        self.assertIn("SORT", kinds)

    def test_data_link_records_the_shared_dataset(self):
        link = [d for d in self.res.data_links
                if (d.job, d.step) == ("CUSTEXT", "SORTSTEP")][0]
        self.assertIn("PROD.CUST.EXTRACT", link.dataset)
        self.assertEqual(link.via_job, "CUSTEXT/UNLDSTEP")

    def test_data_links_are_not_counted_as_table_dependencies(self):
        """They must stay out of refs and jobs - they are lineage, not SQL."""
        self.assertNotIn("SORTJOB", self.res.jobs)
        self.assertNotIn("SORTJOB", {r.member for r in self.res.refs})

    def test_data_hops_zero_disables_lineage(self):
        res = Analyzer(self.store).analyze("PRODDB.CUSTOMER", data_hops=0)
        self.assertEqual(res.data_links, [])

    def test_incremental_reindex_skips_unchanged(self):
        stats = run_index(ScanOptions(roots=[ROOT], db_path=self.db, quiet=True))
        self.assertEqual(stats["scanned"], 0)
        self.assertGreater(stats["skipped"], 0)

    def test_writers_only_filter(self):
        res = Analyzer(self.store).analyze("PRODDB.CUSTOMER", include_read=False)
        self.assertTrue(res.refs)
        self.assertNotIn("READ", {r.access for r in res.refs})


class TestVendorModules(unittest.TestCase):
    """IBM/vendor stubs must be matched by prefix, not by a list of names.

    A list is wrong the moment IBM ships a module that is not on it: the new
    name falls through and gets reported as a missing application program.
    """

    def test_dfh_prefix_covers_the_whole_cics_family(self):
        from mfdep.vendor import classify_module
        # DFHEI1 and DFHPC would be on any hand-written list; DFHNCTR and
        # DFHCOMMAREA are the ones a list misses.
        for name in ("DFHEI1", "DFHEI", "DFHPC", "DFHNCTR", "DFHCOMMAREA",
                     "DFHBMSCA", "DFHRESP", "DFHZC9999"):
            with self.subTest(module=name):
                hit = classify_module(name)
                self.assertIsNotNone(hit, f"{name} not recognised as vendor")
                self.assertEqual(hit[1], "cics")

    def test_other_ibm_families(self):
        from mfdep.vendor import classify_module
        cases = {"CEEDATE": "le", "CEE3ABD": "le", "DSNTIAR": "db2",
                 "DSNHLI": "db2", "IGZCBSO": "cobol-runtime",
                 "ILBOABN0": "cobol-runtime", "CBLTDLI": "ims",
                 "DFSLI000": "ims", "MQPUT": "mq", "CSQBSTUB": "mq",
                 "IDCAMS": "idcams", "IEFBR14": "utility",
                 "ICETOOL": "sort", "SYNCSORT": "sort", "EZASOKET": "tcpip"}
        for name, subsystem in cases.items():
            with self.subTest(module=name):
                hit = classify_module(name)
                self.assertIsNotNone(hit, f"{name} not recognised")
                self.assertEqual(hit[1], subsystem)

    def test_application_programs_are_not_swallowed(self):
        """The prefix rules must not eat real application program names."""
        from mfdep.vendor import classify_module
        for name in ("CUSTVAL", "CUSTUPD", "PAYROLL", "MQTEST", "SORTRPT",
                     "ICEBERG", "DFHAPP"):
            with self.subTest(module=name):
                if name == "DFHAPP":
                    # An application must not squat on an IBM prefix; if it
                    # does, being classified as vendor is the correct call.
                    self.assertIsNotNone(classify_module(name))
                else:
                    self.assertIsNone(classify_module(name),
                                      f"{name} wrongly treated as vendor")

    def test_site_override_file(self):
        import tempfile
        from mfdep.vendor import classify_module, load_overrides
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "vendors.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# site stubs\nXPED*  Compuware Xpediter\nMYSTUB  in-house\n")
            self.assertIsNone(classify_module("XPEDCICS"))
            self.assertEqual(load_overrides(path), 2)
            self.assertEqual(classify_module("XPEDCICS")[0], "Compuware Xpediter")
            self.assertEqual(classify_module("MYSTUB")[0], "in-house")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            # Undo, so later tests see the stock table.
            from mfdep import vendor
            vendor.PREFIXES.pop("XPED", None)
            vendor.EXACT.pop("MYSTUB", None)


class TestUnresolvedCalls(unittest.TestCase):
    """A CALL into a program that is not on the share is a real hole - but it
    must not be buried under every CICS program's DFHEI1 stub."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        src = os.path.join(cls.tmp, "src", "PROD.COBOL.SRC")
        os.makedirs(src)

        def cob(lines, ident=""):
            out = []
            for i, code in enumerate(lines, 1):
                body = f"{i * 100:06d} {code:<65}"[:72]
                out.append(f"{body:<72}{ident[:8]:<8}".rstrip())
            return "\n".join(out) + "\n"

        with open(os.path.join(src, "CICSCUST"), "w", newline="\r\n") as fh:
            fh.write(cob([
                "IDENTIFICATION DIVISION.", "PROGRAM-ID. CICSCUST.",
                "PROCEDURE DIVISION.",
                "     CALL 'DFHEI1' USING DFHEIV0.",
                "     CALL 'DFHNCTR' USING DFHEIV0.",
                "     CALL 'CEEDATE' USING WS-DATE.",
                "     CALL 'CUSTVAL'.",
                "     EXEC SQL SELECT A INTO :A FROM PRODDB.CUSTOMER END-EXEC.",
                "     STOP RUN."], "CICSCUST"))

        # 30 unrelated CICS programs calling the same IBM stubs.
        for i in range(30):
            with open(os.path.join(src, f"CICSOT{i:02d}"), "w", newline="\r\n") as fh:
                fh.write(cob([
                    "IDENTIFICATION DIVISION.", f"PROGRAM-ID. CICSOT{i:02d}.",
                    "PROCEDURE DIVISION.",
                    "     CALL 'DFHEI1' USING DFHEIV0.",
                    "     CALL 'DFHNCTR' USING DFHEIV0.",
                    "     STOP RUN."], f"CICSOT{i:02d}"))

        cls.db = os.path.join(cls.tmp, "t.db")
        run_index(ScanOptions(roots=[os.path.join(cls.tmp, "src")],
                              db_path=cls.db, quiet=True))
        cls.store = Store(cls.db)
        cls.res = Analyzer(cls.store).analyze("PRODDB.CUSTOMER")

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _unresolved(self):
        return [d for _p, _l, k, d in self.res.blind_spots
                if k == "UNRESOLVED-CALL"]

    def test_application_call_is_reported(self):
        self.assertTrue(any("CUSTVAL" in d for d in self._unresolved()))

    def test_ibm_stubs_are_not_reported(self):
        joined = " ".join(self._unresolved())
        for stub in ("DFHEI1", "DFHNCTR", "CEEDATE"):
            self.assertNotIn(stub, joined)

    def test_exactly_one_unresolved_call(self):
        """The signal must not be diluted - 91 stub call sites, 1 real gap."""
        self.assertEqual(len(self._unresolved()), 1)

    def test_vendor_calls_summarised_in_notes(self):
        self.assertTrue(any("IBM CICS" in n for n in self.res.notes))

    def test_unrelated_cics_programs_not_pulled_in(self):
        self.assertEqual(set(self.res.programs), {"CICSCUST"})


class TestLargeFile(unittest.TestCase):
    """A member far larger than the chunk threshold must still parse."""

    def test_chunked_extraction(self):
        from mfdep.extract import CHUNK_THRESHOLD_BYTES, extract_file

        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "BIG.sql")
            filler = "SELECT 1 FROM PRODDB.FILLER;\n"
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("SELECT * FROM PRODDB.HEADTBL;\n")
                fh.write(filler * ((CHUNK_THRESHOLD_BYTES // len(filler)) + 40_000))
                fh.write("UPDATE PRODDB.TAILTBL SET A=1;\n")

            size = os.path.getsize(path)
            self.assertGreater(size, CHUNK_THRESHOLD_BYTES)

            facts = extract_file(path, size=size)
            self.assertEqual(facts.kind, "SQL")
            found = {(r[2], r[4]) for r in facts.table_refs}   # (table, stmt)
            self.assertIn(("HEADTBL", "SELECT"), found)
            self.assertIn(("TAILTBL", "UPDATE"), found)
            self.assertTrue(any(k == "CHUNKED" for _, k, _ in facts.blind_spots))
            self.assertEqual(facts.error, "")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
