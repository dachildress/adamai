"""
Slice 7b Phase 2: MySQLIntrospector.

Tier 1 — fake information_schema (runs everywhere, no server).
Tier 2 — opt-in integration (ADAM_RUN_MYSQL_INTEGRATION=1 + ADAM_MYSQL_TEST_DSN).

Run:  python tests/pipeline/test_mysql_introspector.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ_ROOT))

from adam.pipeline import (  # noqa: E402
    MySQLIntrospector, IngestionStore, get_source_model, reset_ratified,
    ExecutionPlan, SQLITE_CAPABILITIES, validate, ValidationConfig,
    schema_fingerprint,
)

PASSED = 0
FAILED = 0
SKIPPED = 0


def check(name, cond, detail=""):
    # Hardened (test_fix): a FALSE condition now RAISES so the failure surfaces
    # loudly and located, under both pytest and the direct runner. The PASSED
    # counter is kept for the direct runner's RESULT line.
    global PASSED, FAILED
    if not cond:
        FAILED += 1
        raise AssertionError(f"{name}" + (f" -- {detail}" if detail else ""))
    PASSED += 1
    print(f"  PASS  {name}")


def skip(name, reason):
    global SKIPPED
    SKIPPED += 1
    print(f"  SKIP  {name}  -- {reason}")


# ---- Fake information_schema connection ----

# columns rows: (table, column, data_type, is_nullable, column_key, ordinal)
_COL_ROWS = [
    ("schools", "id", "int", "NO", "PRI", 1),
    ("schools", "name", "varchar", "YES", "", 2),
    ("schools", "level", "varchar", "YES", "", 3),
    ("attendance", "id", "int", "NO", "PRI", 1),
    ("attendance", "school_id", "int", "YES", "MUL", 2),
    ("attendance", "rate", "double", "YES", "", 3),
]
# fk rows: (table, column, ref_table, ref_column)
_FK_ROWS = [("attendance", "school_id", "schools", "id")]


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = []

    def execute(self, sql, params=None):
        self.conn.executed.append(sql)
        s = sql.lower()
        if "key_column_usage" in s:
            self._result = list(_FK_ROWS)
        elif "information_schema.columns" in s:
            self._result = list(_COL_ROWS)
        else:
            self._result = []

    def fetchall(self):
        return self._result


class FakeConn:
    def __init__(self):
        self.executed = []

    def cursor(self):
        return FakeCursor(self)


def introspector():
    return MySQLIntrospector(connection=FakeConn())


# ============================ Tier 1 ============================

def test_builds_rich_schema():
    schema = introspector()("powerschool")
    names = schema.field_names()
    check("entities discovered", set(names.keys()) == {"schools", "attendance"}, str(names.keys()))
    check("schools fields", names["schools"] == ("id", "name", "level"))
    schools = next(e for e in schema.entities if e.name == "schools")
    id_f = next(f for f in schools.fields if f.name == "id")
    check("source_type captured", id_f.source_type == "int")
    check("primary_key captured (column_key PRI)", id_f.primary_key is True)
    name_f = next(f for f in schools.fields if f.name == "name")
    check("nullable captured (is_nullable YES)", name_f.nullable is True)
    check("non-null captured (is_nullable NO)", id_f.nullable is False)
    check("source_name carried", schema.source_name == "powerschool")


def test_foreign_keys_captured():
    schema = introspector()("powerschool")
    check("one FK relationship", len(schema.relationships) == 1)
    r = schema.relationships[0]
    check("FK from attendance.school_id -> schools.id",
          r.from_entity == "attendance" and r.from_field == "school_id"
          and r.to_entity == "schools" and r.to_field == "id"
          and r.relationship_type == "foreign_key")


def test_down_projection_back_compat():
    schema = introspector()("x")
    names = schema.field_names()
    check("down-projects to name-only view",
          names["attendance"] == ("id", "school_id", "rate"))


def test_fingerprint_change_detection_on_type():
    schema = introspector()("x")
    fp1 = schema_fingerprint(schema)
    # Mutate one column's type via a second fake with a different data_type.
    global _COL_ROWS
    saved = list(_COL_ROWS)
    _COL_ROWS = [("schools", "id", "bigint", "NO", "PRI", 1)] + saved[1:]
    try:
        fp2 = schema_fingerprint(introspector()("x"))
    finally:
        _COL_ROWS = saved
    check("changing only a column type changes the fingerprint", fp1 != fp2)


def test_read_only_only_information_schema():
    conn = FakeConn()
    MySQLIntrospector(connection=conn)("x")
    check("issued exactly 2 statements", len(conn.executed) == 2, str(conn.executed))
    for sql in conn.executed:
        s = sql.strip().lower()
        check(f"read-only SELECT on information_schema ({s[:24]}...)",
              s.startswith("select") and "information_schema" in s)
        check("no DDL/DML", not any(k in s for k in ("insert", "update", "delete", "drop", "alter", "create")))


def test_lifecycle_with_real_introspector_shape():
    # The introspected schema (from the fake) drives the FULL Slice-6 lifecycle.
    reset_ratified()
    path = Path(tempfile.mkdtemp()) / "ingestion.json"
    st = IngestionStore(path)
    cand = st.submit("powerschool", introspect_fn=MySQLIntrospector(connection=FakeConn()))
    check("candidate carries rich schema_detail", cand.schema_detail is not None)
    rec = st.approve(cand.candidate_id)
    plan = ExecutionPlan.from_dict({
        "plan_version": "1.0", "intent_type": "query", "connection_handle": "c",
        "source_type": "sql", "source_model_version": rec.version, "purpose": "t",
        "estimated_row_scope": "small",
        "body": {"operation": "select", "entities": ["schools"],
                 "projection": ["schools.name"], "limit": 10},
    })
    out = validate(plan, SQLITE_CAPABILITIES, ValidationConfig())
    check("ratified introspected model grounds a plan", out.ok, f"{out.category}: {out.detail}")
    reset_ratified()


# ============================ Tier 2 (opt-in) ============================

def test_integration_optin():
    if os.environ.get("ADAM_RUN_MYSQL_INTEGRATION") != "1" or not os.environ.get("ADAM_MYSQL_TEST_DSN"):
        reason = "set ADAM_RUN_MYSQL_INTEGRATION=1 and ADAM_MYSQL_TEST_DSN to run"
        if "pytest" in sys.modules:           # real SKIP under pytest
            import pytest
            pytest.skip(reason)
        skip("MySQL introspection integration", reason)   # direct runner: print + return
        return
    from urllib.parse import urlparse
    from adam.pipeline import make_pymysql_connect_fn
    u = urlparse(os.environ["ADAM_MYSQL_TEST_DSN"])
    connect_fn = make_pymysql_connect_fn(
        host=u.hostname, port=u.port or 3306, user=u.username,
        password=u.password or "", database=(u.path or "/").lstrip("/"),
    )
    schema = MySQLIntrospector(connect_fn=connect_fn)("integration")
    check("real introspection returned entities", len(schema.entities) >= 1, str(schema.field_names().keys()))
    reset_ratified()
    st = IngestionStore(Path(tempfile.mkdtemp()) / "ing.json")
    rec = st.approve(st.submit("integration", introspect_fn=MySQLIntrospector(connect_fn=connect_fn)).candidate_id)
    check("integration: ratified model usable", get_source_model(rec.version) is not None)
    reset_ratified()


def main():
    print("Slice 7b Phase 2: MySQLIntrospector (Tier 1 fakes + Tier 2 opt-in)")
    print("=" * 60)
    for t in [
        test_builds_rich_schema,
        test_foreign_keys_captured,
        test_down_projection_back_compat,
        test_fingerprint_change_detection_on_type,
        test_read_only_only_information_schema,
        test_lifecycle_with_real_introspector_shape,
        test_integration_optin,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
