"""
Slice 7: MySQLAdapter.

Tier 1 — fake-backed unit tests (run everywhere, no real MySQL).
Tier 2 — opt-in integration, gated on ADAM_RUN_MYSQL_INTEGRATION=1 +
         ADAM_MYSQL_TEST_DSN=...; SKIPS cleanly otherwise.

Run:  python tests/pipeline/test_mysql_adapter.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ_ROOT))

from adam.pipeline import (  # noqa: E402
    SYNTHETIC_SCHOOL_V1, ExecutionPlan, run_plan,
    Adapter, MySQLAdapter, MYSQL_CAPABILITIES, AdapterCostEstimate,
    TranslatedQuery, IdentifierResolutionError,
    AUTHENTICATION_FAILED, OFFLINE, ADAPTER_UNAVAILABLE,
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


# ---------------------------------------------------------------------------
# Fake MySQL-shaped connection / cursor (Tier 1)
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._result = []

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, list(params or [])))
        if sql.strip().upper().startswith("SELECT COUNT(*)"):
            self._result = [(self.conn.count,)]
            self.description = [("COUNT(*)", None, None, None, None, None, None)]
        else:
            self._result = list(self.conn.rows)
            self.description = self.conn.description

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None


class FakeConn:
    def __init__(self, rows=None, description=None, count=0, ping_error=None):
        self.rows = rows or []
        self.description = description or []
        self.count = count
        self.ping_error = ping_error
        self.executed = []
        self.ping_calls = 0

    def cursor(self):
        return FakeCursor(self)

    def ping(self, *a, **k):
        self.ping_calls += 1
        if self.ping_error:
            raise self.ping_error


def plan(**body_over):
    body = {
        "operation": "select", "entities": ["attendance", "schools"],
        "projection": ["schools.name"],
        "filters": [{"field": "schools.level", "op": "eq", "value": "elementary"}],
        "joins": [{"left": "attendance.school_id", "right": "schools.id", "type": "inner"}],
        "group_by": ["schools.name"],
        "aggregations": [{"fn": "avg", "field": "attendance.rate", "as": "avg_rate"}],
        "order_by": [{"field": "avg_rate", "direction": "asc"}],
        "limit": 100,
    }
    body.update(body_over)
    return ExecutionPlan.from_dict({
        "plan_version": "1.0", "intent_type": "query",
        "connection_handle": "conn_mysql_ro", "source_type": "sql",
        "source_model_version": "synthetic-school-v1",
        "purpose": "t", "estimated_row_scope": "small", "body": body,
    })


def adapter(conn=None, **kw):
    return MySQLAdapter(SYNTHETIC_SCHOOL_V1, connection=conn, **kw)


# ============================ Tier 1 ============================

def test_translate_dialect_and_safety():
    tq = adapter().translate(plan())
    check("translate returns TranslatedQuery", isinstance(tq, TranslatedQuery))
    check("uses MySQL %s placeholders (not ?)", "%s" in tq.sql and "?" not in tq.sql, tq.sql)
    check("uses backtick-quoted identifiers", "`schools`.`name`" in tq.sql, tq.sql)
    check("aggregation alias backtick-quoted", "`avg_rate`" in tq.sql, tq.sql)
    check("filter value is bound (in params)", tq.params == ["elementary"], str(tq.params))
    check("filter value NOT in SQL string", "elementary" not in tq.sql, tq.sql)


def test_identifier_not_in_allowlist():
    try:
        adapter().translate(plan(projection=["schools.bogus"], entities=["schools"],
                                 filters=[], joins=[], group_by=[], aggregations=[], order_by=[]))
        check("unresolved identifier raises", False, "no error")
    except IdentifierResolutionError as e:
        check("unresolved identifier -> IDENTIFIER_RESOLUTION_ERROR",
              e.category == "IDENTIFIER_RESOLUTION_ERROR")


def test_injection_probe_is_bound():
    evil = "'; DROP TABLE students;--"
    tq = adapter().translate(plan(
        entities=["students"], projection=["students.name"],
        filters=[{"field": "students.name", "op": "eq", "value": evil}],
        joins=[], group_by=[], aggregations=[], order_by=[]))
    check("injection value bound as param", tq.params == [evil], str(tq.params))
    check("injection value not concatenated into SQL", evil not in tq.sql, tq.sql)
    check("placeholder used", "%s" in tq.sql)


def test_health_offline_and_auth():
    a_off = adapter(connect_fn=lambda: (_ for _ in ()).throw(Exception("can't reach")))
    check("unreachable -> OFFLINE", a_off.health().status == OFFLINE)
    a_auth = adapter(connect_fn=lambda: (_ for _ in ()).throw(Exception(1045, "Access denied")))
    check("bad creds -> AUTHENTICATION_FAILED", a_auth.health().status == AUTHENTICATION_FAILED)


def test_runner_short_circuits_terminal_health():
    a = adapter(connect_fn=lambda: (_ for _ in ()).throw(Exception("down")))
    res = run_plan(plan(), adapter=a, source_model=SYNTHETIC_SCHOOL_V1)
    check("terminal health stops at adapter_health", not res.ok and res.stage == "adapter_health")
    check("ADAPTER_UNAVAILABLE detail", ADAPTER_UNAVAILABLE in (res.detail or ""))
    check("no result produced", res.result is None)


def test_health_ttl_caches():
    conn = FakeConn()
    a = adapter(conn, health_ttl_seconds=60.0)
    a.health(); a.health()
    check("health TTL caches (ping called once)", conn.ping_calls == 1, f"pings={conn.ping_calls}")


def test_execute_returns_query_result():
    conn = FakeConn(rows=[("Oak Elementary", 0.80), ("Maple Elementary", 0.925)],
                    description=[("name", None, None, None, None, None, None),
                                 ("avg_rate", None, None, None, None, None, None)])
    qr = adapter(conn).execute(plan())
    check("columns from cursor.description", qr.columns == ["name", "avg_rate"], str(qr.columns))
    check("rows returned", qr.row_count == 2)
    check("sql + params carried", "%s" in qr.sql and qr.params == ["elementary"])
    check("lineage notes mysql adapter", qr.source_lineage.get("adapter") == "mysql")
    check("lineage has version + plan_id",
          qr.source_lineage.get("source_model_version") == "synthetic-school-v1"
          and qr.source_lineage.get("plan_id"))


def test_capabilities_and_cost():
    conn = FakeConn(count=42)
    a = adapter(conn)
    check("capabilities is MySQL set", a.capabilities() == MYSQL_CAPABILITIES)
    est = a.estimate_cost(plan())
    check("estimate reuses AdapterCostEstimate", isinstance(est, AdapterCostEstimate))
    check("complex plan -> high complexity", est.complexity == "high", str(est))
    check("rows from COUNT(*) capped by limit", est.rows == 42)


def test_is_adapter_no_base_class():
    a = adapter()
    check("MySQLAdapter is an Adapter", isinstance(a, Adapter))
    check("MySQLAdapter's ONLY base is Adapter (no BaseSQLAdapter)",
          MySQLAdapter.__bases__ == (Adapter,), str(MySQLAdapter.__bases__))


def test_runner_end_to_end_with_fake_conn():
    conn = FakeConn(rows=[("Oak Elementary", 0.80)],
                    description=[("name", None, None, None, None, None, None),
                                 ("avg_rate", None, None, None, None, None, None)],
                    count=4)
    res = run_plan(plan(), adapter=adapter(conn), source_model=SYNTHETIC_SCHOOL_V1)
    check("governed plan executes via MySQLAdapter", res.ok and res.stage == "execution",
          f"stage={res.stage} detail={res.detail}")
    check("result came through", res.result and res.result.row_count == 1)


def test_dialect_stays_private():
    # The SQL-generation dialect markers — `%s` placeholders and the
    # backtick-quoting f-string fragment ( `{ ) — must appear ONLY in the
    # MySQL adapter, never in the contract, runner, Sentinel, or skill.
    # (Plain prose backticks in docstrings are fine; we match the code
    # constructs, not every backtick character.)
    import inspect
    from adam.pipeline import adapter as adapter_mod, runner as runner_mod
    from adam.pipeline import sentinel as sentinel_mod, skill as skill_mod, mysql_adapter as mysql_mod
    for mod in (adapter_mod, runner_mod, sentinel_mod, skill_mod):
        src = inspect.getsource(mod)
        name = mod.__name__.split(".")[-1]
        check(f"no %s placeholder leak in {name}", "%s" not in src)
        check(f"no backtick-quoting leak in {name}", "`{" not in src)
    msrc = inspect.getsource(mysql_mod)
    check("MySQL adapter DOES own the dialect", "%s" in msrc and "`{" in msrc)


# ============================ Tier 2 (opt-in) ============================

def test_integration_optin():
    if os.environ.get("ADAM_RUN_MYSQL_INTEGRATION") != "1" or not os.environ.get("ADAM_MYSQL_TEST_DSN"):
        reason = "set ADAM_RUN_MYSQL_INTEGRATION=1 and ADAM_MYSQL_TEST_DSN to run"
        if "pytest" in sys.modules:           # real SKIP under pytest
            import pytest
            pytest.skip(reason)
        skip("MySQL integration suite", reason)   # direct runner: print + return
        return
    # When env present: real driver against a real MySQL with the pre-ratified
    # fixture schema. (Connection parsing + fixture setup live here.)
    from urllib.parse import urlparse
    from adam.pipeline import make_pymysql_connect_fn, create_synthetic_db, SQLiteAdapter
    dsn = os.environ["ADAM_MYSQL_TEST_DSN"]
    u = urlparse(dsn)
    connect_fn = make_pymysql_connect_fn(
        host=u.hostname, port=u.port or 3306, user=u.username,
        password=u.password or "", database=(u.path or "/").lstrip("/"),
    )
    a = MySQLAdapter(SYNTHETIC_SCHOOL_V1, connect_fn=connect_fn)
    check("real MySQL health READY", a.health().status == "READY", str(a.health()))
    res = run_plan(plan(), adapter=a, source_model=SYNTHETIC_SCHOOL_V1)
    check("real MySQL governed plan executes", res.ok, f"{res.stage}: {res.detail}")
    # Equivalence vs SQLite (structure): same plan, equivalent QueryResult shape.
    sqlite_res = run_plan(plan(), adapter=SQLiteAdapter(create_synthetic_db(), SYNTHETIC_SCHOOL_V1),
                          source_model=SYNTHETIC_SCHOOL_V1)
    check("SQLite==MySQL column structure",
          res.result and sqlite_res.result and res.result.columns == sqlite_res.result.columns)


def main():
    print("Slice 7: MySQLAdapter (Tier 1 fakes + Tier 2 opt-in)")
    print("=" * 60)
    for t in [
        test_translate_dialect_and_safety,
        test_identifier_not_in_allowlist,
        test_injection_probe_is_bound,
        test_health_offline_and_auth,
        test_runner_short_circuits_terminal_health,
        test_health_ttl_caches,
        test_execute_returns_query_result,
        test_capabilities_and_cost,
        test_is_adapter_no_base_class,
        test_runner_end_to_end_with_fake_conn,
        test_dialect_stays_private,
        test_integration_optin,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
