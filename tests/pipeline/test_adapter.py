"""
Phase 3 tests: Sentinel stub + SQLite adapter (translation, execution,
parameterization / SQL-injection safety).

Run:  python tests/pipeline/test_adapter.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ_ROOT))

from adam.pipeline import (  # noqa: E402
    ExecutionPlan, SQLITE_CAPABILITIES, SQLiteAdapter, SYNTHETIC_SCHOOL_V1,
    create_synthetic_db, sentinel_check, validate, ValidationConfig,
)

PASSED = 0
FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}" + (f"  -- {detail}" if detail else ""))


def adapter():
    conn = create_synthetic_db()
    return SQLiteAdapter(conn, SYNTHETIC_SCHOOL_V1, SQLITE_CAPABILITIES)


def simple_plan(**body_over):
    body = {
        "operation": "select",
        "entities": ["schools"],
        "projection": ["schools.name", "schools.level"],
        "limit": 100,
    }
    body.update(body_over)
    return ExecutionPlan.from_dict({
        "plan_version": "1.0", "intent_type": "query",
        "connection_handle": "conn_school_ro", "source_type": "sql",
        "source_model_version": "synthetic-school-v1",
        "purpose": "test", "estimated_row_scope": "small", "body": body,
    })


def test_sentinel_allows_query():
    d = sentinel_check(simple_plan())
    check("sentinel allows valid query plan", d.allow is True)


def test_simple_select_executes():
    a = adapter()
    plan = simple_plan(
        projection=["schools.name", "schools.level"],
        filters=[{"field": "schools.level", "op": "eq", "value": "elementary"}],
        order_by=[{"field": "schools.name", "direction": "asc"}],
    )
    res = a.execute(plan)
    check("returns structured columns", res.columns == ["name", "level"], str(res.columns))
    check("filters elementary only (2 rows)", res.row_count == 2, str(res.rows))
    check("values bound as params (not inlined)", res.params == ["elementary"], str(res.params))
    check("source_lineage carries plan_id + version",
          res.source_lineage.get("plan_id") == plan.plan_id and
          res.source_lineage.get("source_model_version") == "synthetic-school-v1")


def test_join_group_aggregate_order():
    a = adapter()
    # avg attendance rate per elementary school, lowest first.
    plan = simple_plan(
        entities=["attendance", "schools"],
        projection=["schools.name"],
        filters=[{"field": "schools.level", "op": "eq", "value": "elementary"}],
        joins=[{"left": "attendance.school_id", "right": "schools.id", "type": "inner"}],
        group_by=["schools.name"],
        aggregations=[{"fn": "avg", "field": "attendance.rate", "as": "avg_rate"}],
        order_by=[{"field": "avg_rate", "direction": "asc"}],
    )
    # sanity: it validates and sentinel allows
    out = validate(plan, SQLITE_CAPABILITIES, ValidationConfig())
    check("join/group/agg plan validates", out.ok, f"{out.category}: {out.detail}")
    res = a.execute(plan)
    check("agg result has alias column", "avg_rate" in res.columns, str(res.columns))
    # Maple avg (0.95+0.90)/2=0.925, Oak 0.80 -> Oak first (asc).
    check("two elementary schools grouped", res.row_count == 2, str(res.rows))
    first_school = res.rows[0][res.columns.index("name")]
    check("ordered by avg_rate ascending (Oak first)", first_school == "Oak Elementary", str(res.rows))


def test_sql_injection_value_is_parameterized():
    a = adapter()
    evil = "'; DROP TABLE students;--"
    plan = simple_plan(
        entities=["students"],
        projection=["students.name"],
        filters=[{"field": "students.name", "op": "eq", "value": evil}],
    )
    res = a.execute(plan)
    # The injection value is bound, not executed: no rows match, table intact.
    check("injection value bound as param", res.params == [evil], str(res.params))
    check("injection value did not match any row", res.row_count == 0, str(res.rows))
    check("'?' placeholder used in SQL (value not concatenated)",
          "?" in res.sql and evil not in res.sql, res.sql)
    # students table still exists and still has its rows.
    cur = a.connection.cursor()
    cur.execute("SELECT COUNT(*) FROM students")
    count = cur.fetchone()[0]
    check("students table not dropped (4 rows intact)", count == 4, f"count={count}")


def test_identifiers_from_model_not_plan():
    # Even if a plan smuggled a weird entity, resolve() would reject it; but
    # more importantly the SQL only ever contains model-derived names. Spot
    # check that the emitted SQL references quoted model identifiers.
    a = adapter()
    plan = simple_plan(projection=["schools.name"], entities=["schools"])
    sql, _ = a.translate(plan)
    check('SQL uses quoted model identifiers', '"schools"."name"' in sql, sql)
    check("SQL has no raw-statement passthrough", "DROP" not in sql.upper(), sql)


def main():
    print("Phase 3: sentinel stub + SQLite adapter")
    print("=" * 60)
    for t in [
        test_sentinel_allows_query,
        test_simple_select_executes,
        test_join_group_aggregate_order,
        test_sql_injection_value_is_parameterized,
        test_identifiers_from_model_not_plan,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
