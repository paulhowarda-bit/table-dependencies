"""Regression tests for the field-level template (integration/field_dependencies.py).

Each test pins one way a column-level answer goes wrong: a phantom column out
of a comment, a host variable paired with the wrong column, a positional
INSERT guessed without a definition. Run with:

    python -m pytest tests/test_field_dependencies.py -q
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mfdep                                                        # noqa: E402
from make_fixtures import ROOT, build                               # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_template():
    path = os.path.join(_REPO, "integration", "field_dependencies.py")
    spec = importlib.util.spec_from_file_location("field_dependencies", path)
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec: the dataclasses in the template resolve their
    # (string) annotations through sys.modules[cls.__module__].
    sys.modules["field_dependencies"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestFieldDependencies(unittest.TestCase):
    """Index the fixture library once, then interrogate it per column."""

    @classmethod
    def setUpClass(cls):
        build()
        cls.tmp = tempfile.mkdtemp()
        cls.db = os.path.join(cls.tmp, "t.db")
        mfdep.index(ROOT, db=cls.db, workers=1, full=True)
        cls.fd = _load_template()
        cls.res = cls.fd.get_field_dependencies("PRODDB.CUSTOMER",
                                                db_path=cls.db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def uses(self, **want):
        out = []
        for u in self.res.uses:
            if all(getattr(u, k) == v for k, v in want.items()):
                out.append(u)
        return out

    # ---- column definitions

    def test_columns_come_from_the_ddl_in_declared_order(self):
        cols = [(c.name, c.seq) for c in self.res.columns]
        self.assertEqual(cols, [("CUST_ID", 0), ("CUST_NAME", 1),
                                ("BALANCE", 2)])
        self.assertEqual(self.res.columns[0].origin, "CREATE TABLE")

    def test_column_types_and_nullability(self):
        by_name = {c.name: c for c in self.res.columns}
        self.assertIn("CHAR(10)", by_name["CUST_ID"].sql_type)
        self.assertFalse(by_name["CUST_ID"].nullable)
        self.assertTrue(by_name["CUST_NAME"].nullable)
        self.assertIn("DECIMAL(11,2)", by_name["BALANCE"].sql_type)

    # ---- static SQL in COBOL

    def test_select_pairs_each_column_with_its_host_variable(self):
        got = {(u.column, u.host_var) for u in
               self.uses(member="CUSTUPD", context="SELECT", stmt="SELECT")}
        self.assertEqual(got, {("CUST_NAME", "CUST-NAME"),
                               ("BALANCE", "BALANCE")})

    def test_where_predicate_is_a_read_with_its_host_variable(self):
        got = self.uses(member="CUSTUPD", context="WHERE", column="CUST_ID")
        self.assertTrue(got)
        self.assertTrue(all(u.access == "READ" for u in got))
        self.assertIn("CUST-ID", {u.host_var for u in got})

    def test_update_set_is_a_write_of_that_column_only(self):
        got = self.uses(member="CUSTUPD", stmt="UPDATE", access="WRITE")
        self.assertEqual({u.column for u in got}, {"BALANCE"})
        self.assertEqual(got[0].host_var, "BALANCE")
        self.assertEqual(got[0].context, "SET")

    def test_no_phantom_columns_from_comments_literals_or_ident_field(self):
        cols = {u.column for u in self.res.uses if u.table == "CUSTOMER"}
        self.assertLessEqual(cols, {"CUST_ID", "CUST_NAME", "BALANCE"})

    # ---- cursors

    def test_cursor_declare_reads_the_selected_columns(self):
        got = {u.column for u in
               self.uses(member="CUSTCUR", stmt="DECLARE CURSOR",
                         context="SELECT")}
        self.assertEqual(got, {"CUST_ID", "BALANCE"})

    def test_fetch_pairs_columns_across_statements(self):
        got = {(u.column, u.host_var) for u in
               self.uses(member="CUSTCUR", context="FETCH")}
        self.assertEqual(got, {("CUST_ID", "CUST-ID"),
                               ("BALANCE", "BALANCE")})

    def test_order_by_is_a_column_dependency(self):
        got = self.uses(member="CUSTCUR", context="ORDER BY")
        self.assertEqual({u.column for u in got}, {"CUST_ID"})

    # ---- DCLGEN binding

    def test_dclgen_binds_columns_to_host_fields(self):
        by_col = {b.column: b for b in self.res.bindings}
        self.assertEqual(by_col["CUST_ID"].host_field, "CUST-ID")
        self.assertEqual(by_col["CUST_ID"].pic, "X(10)")
        self.assertEqual(by_col["BALANCE"].host_field, "BALANCE")
        self.assertIn("COMP-3", by_col["BALANCE"].pic)

    def test_varchar_group_takes_the_49_level_text_pic(self):
        by_col = {b.column: b for b in self.res.bindings}
        self.assertEqual(by_col["CUST_NAME"].host_field, "CUST-NAME")
        self.assertEqual(by_col["CUST_NAME"].pic, "X(40)")

    def test_host_variable_definition_resolved_to_the_dclgen(self):
        got = [u for u in self.uses(member="CUSTUPD", column="BALANCE")
               if u.host_var == "BALANCE"]
        self.assertTrue(got)
        self.assertTrue(got[0].host_def_path.upper().endswith("DCLCUST"),
                        got[0].host_def_path)
        self.assertIn("COMP-3", got[0].host_def_pic)

    # ---- utility decks: the layout is hard-coded with no SQL to find

    def test_load_deck_columns_are_writes(self):
        # Two members are called LOADCUST; the PROD.CNTL one has the list.
        got = [u for u in self.uses(member="LOADCUST", context="LOAD")
               if "PROD.CNTL" in u.path]
        self.assertEqual({u.column for u in got}, {"CUST_ID", "CUST_NAME"})
        self.assertTrue(all(u.access == "WRITE" for u in got))

    def test_instream_load_deck_in_jcl_also_found(self):
        got = {u.column for u in self.uses(member="CUSTLOAD", context="LOAD")}
        self.assertEqual(got, {"CUST_ID", "CUST_NAME"})

    def test_unload_deck_columns_are_reads(self):
        got = {u.column for u in self.uses(member="UNLDCUST", context="UNLOAD")}
        self.assertEqual(got, {"CUST_ID", "CUST_NAME", "BALANCE"})

    def test_load_without_column_list_falls_back_to_the_ddl(self):
        """TEST.CNTL(LOADCUST) has no column list - the DDL fills it in."""
        loads = [u for u in self.uses(context="LOAD")
                 if "TEST.CNTL" in u.path]
        self.assertEqual({u.column for u in loads},
                         {"CUST_ID", "CUST_NAME", "BALANCE"})
        self.assertTrue(any("no column list" in n for n in self.res.notes))

    # ---- SQL files

    def test_stored_procedure_writes_found_inside_if_blocks(self):
        got = self.uses(member="SPCUSTUP", access="WRITE")
        by_stmt = {}
        for u in got:
            by_stmt.setdefault(u.context, set()).add(u.column)
        self.assertEqual(by_stmt.get("SET"), {"BALANCE"})
        self.assertEqual(by_stmt.get("INSERT"), {"CUST_ID", "BALANCE"})

    def test_view_definition_reads_its_base_columns(self):
        got = {u.column for u in
               self.uses(member="CUSTDDL", stmt="CREATE VIEW")}
        self.assertEqual(got, {"CUST_ID", "CUST_NAME", "BALANCE"})

    def test_index_columns_are_dependencies(self):
        got = self.uses(member="CUSTDDL", context="INDEX")
        self.assertEqual({u.column for u in got}, {"CUST_ID"})

    def test_view_consumer_reported_against_the_view(self):
        got = self.uses(member="CUSTVW", column="CUST_NAME")
        self.assertTrue(got)
        self.assertEqual(got[0].table, "V_ACTIVE_CUST")

    # ---- honesty

    def test_dynamic_sql_is_a_note_not_a_guess(self):
        self.assertFalse(self.uses(member="CUSTDYN"))
        self.assertTrue(any("CUSTDYN" in n for n in self.res.notes))

    def test_layout_dependents_include_copybook_only_programs(self):
        self.assertIn("CUSTRPT", self.res.layout_dependents)

    def test_writers_only_filter(self):
        res = self.fd.get_field_dependencies("PRODDB.CUSTOMER",
                                             db_path=self.db,
                                             include_read=False)
        self.assertTrue(res.uses)
        self.assertEqual({u.access for u in res.uses}, {"WRITE"})

    # ---- a second table exercises positional INSERT and MERGE

    def test_insert_select_star_expands_via_the_ddl(self):
        res = self.fd.get_field_dependencies("PRODDB.CUST_AUDIT",
                                             db_path=self.db)
        got = {u.column for u in res.uses
               if u.member == "CUSTUPD" and u.access == "WRITE"}
        self.assertEqual(got, {"AUDIT_ID", "CUST_ID"})

    def test_merge_on_and_insert_columns(self):
        res = self.fd.get_field_dependencies("PRODDB.CUST_AUDIT",
                                             db_path=self.db)
        sp = [u for u in res.uses if u.member == "SPCUSTUP"]
        self.assertIn(("CUST_ID", "READ", "ON"),
                      {(u.column, u.access, u.context) for u in sp})
        self.assertIn(("CUST_ID", "WRITE", "INSERT"),
                      {(u.column, u.access, u.context) for u in sp})

    # ---- summary shape

    def test_summary_is_serializable_and_complete(self):
        import json
        s = self.fd.field_dependency_summary("PRODDB.CUSTOMER",
                                             db_path=self.db)
        json.dumps(s)                       # must not raise
        self.assertIn("BALANCE", s["columns"])
        bal = s["columns"]["BALANCE"]
        self.assertIn("CUSTUPD", bal["written_by"])
        self.assertIn("SPCUSTUP", bal["written_by"])
        self.assertIn("UNLDCUST", bal["read_by"])
        self.assertIn("CUSTRPT", s["layout_dependents"])

    def test_json_report(self):
        import json
        out = json.loads(self.fd.field_dependencies_json(
            "PRODDB.CUSTOMER", db_path=self.db))
        self.assertEqual(out["table"], "PRODDB.CUSTOMER")
        self.assertTrue(out["columns"])
        self.assertTrue(out["uses"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
