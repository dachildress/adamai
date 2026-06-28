"""
End-to-end lifecycle (Slice 1-3):

    adapter health -> validation -> adapter cost -> Sentinel -> execute

Proves the spine works against synthetic SQLite and that each stage
short-circuits at the right place — and that health/cost now come FROM the
adapter through the interface (no hand-passing).

Run:  python tests/pipeline/test_end_to_end.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ_ROOT))

from adam.pipeline import (  # noqa: E402
    ExecutionPlan, create_synthetic_db, run_plan,
    SQLiteAdapter, SYNTHETIC_SCHOOL_V1,
    GovernanceConfig, ScopeConfig, AdapterCostEstimate,
    POLICY_DENIED, APPROVAL_REQUIRED,
    OFFLINE, AUTHENTICATION_FAILED, DEGRADED, ADAPTER_UNAVAILABLE,
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


# Spy adapters: a real SQLiteAdapter that records / forbids execute() so we
# can prove a short-circuited plan never executes SQL.
class NoExecuteAdapter(SQLiteAdapter):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.executed = False

    def execute(self, plan):
        self.executed = True
        raise AssertionError("execute() must not be called on a short-circuited plan")


class NoCostNoExecuteAdapter(NoExecuteAdapter):
    def estimate_cost(self, plan):
        return None  # force "cost absent"


def plan_dict(**body_over):
    body = {
        "operation": "select",
        "entities": ["attendance", "schools"],
        "projection": ["schools.name"],
        "filters": [{"field": "schools.level", "op": "eq", "value": "elementary"}],
        "joins": [{"left": "attendance.school_id", "right": "schools.id", "type": "inner"}],
        "group_by": ["schools.name"],
        "aggregations": [{"fn": "avg", "field": "attendance.rate", "as": "avg_rate"}],
        "order_by": [{"field": "avg_rate", "direction": "asc"}],
        "limit": 100,
    }
    body.update(body_over)
    return {
        "plan_version": "1.0", "intent_type": "query",
        "connection_handle": "conn_school_ro", "source_type": "sql",
        "source_model_version": "synthetic-school-v1",
        "purpose": "elementary attendance", "estimated_row_scope": "small",
        "body": body,
    }


def simple_plan_dict():
    return plan_dict(entities=["schools"], projection=["schools.name"],
                     filters=[], joins=[], group_by=[], aggregations=[], order_by=[])


def test_full_flow_executes():
    conn = create_synthetic_db()
    plan = ExecutionPlan.from_dict(plan_dict())
    res = run_plan(plan, conn)
    check("pipeline ok", res.ok, f"stage={res.stage} detail={res.detail}")
    check("reached execution stage", res.stage == "execution")
    check("health READY recorded", res.health and res.health.is_ready)
    check("no warnings on READY", res.warnings == [])
    check("validation passed", res.validation.ok)
    check("sentinel allowed", res.sentinel and res.sentinel.allow)
    check("structured result returned", res.result is not None and res.result.row_count == 2,
          str(res.result.rows if res.result else None))
    check("lineage carries plan_id", res.result.source_lineage.get("plan_id") == plan.plan_id)


def test_validation_short_circuits():
    conn = create_synthetic_db()
    bad = plan_dict(); bad["body"]["projection"] = ["*"]
    res = run_plan(ExecutionPlan.from_dict(bad), conn)
    check("invalid plan stops at validation", not res.ok and res.stage == "validation")
    check("never reached sentinel", res.sentinel is None)
    check("never reached execution", res.result is None)


def test_mutation_short_circuits_before_execution():
    conn = create_synthetic_db()
    bad = plan_dict(); bad["intent_type"] = "mutation"
    res = run_plan(ExecutionPlan.from_dict(bad), conn)
    check("mutation stopped at validation (out of scope)",
          not res.ok and res.stage == "validation")
    check("mutation never executed", res.result is None)


def test_health_terminal_short_circuits():
    for status in (OFFLINE, AUTHENTICATION_FAILED):
        conn = create_synthetic_db()
        spy = NoExecuteAdapter(conn, SYNTHETIC_SCHOOL_V1, health_status=status, health_detail="forced")
        res = run_plan(ExecutionPlan.from_dict(plan_dict()), adapter=spy)
        check(f"{status} stops at adapter_health", not res.ok and res.stage == "adapter_health", f"stage={res.stage}")
        check(f"{status} detail is ADAPTER_UNAVAILABLE", ADAPTER_UNAVAILABLE in (res.detail or ""), str(res.detail))
        check(f"{status} never validated", res.validation is None)
        check(f"{status} never executed", spy.executed is False and res.result is None)


def test_degraded_proceeds_with_warning():
    conn = create_synthetic_db()
    adapter = SQLiteAdapter(conn, SYNTHETIC_SCHOOL_V1, health_status=DEGRADED, health_detail="slow")
    res = run_plan(ExecutionPlan.from_dict(plan_dict()), adapter=adapter)
    check("DEGRADED proceeds to execution", res.ok and res.stage == "execution", f"stage={res.stage}")
    check("DEGRADED records a warning", len(res.warnings) >= 1, str(res.warnings))


def test_sentinel_denied_executes_no_sql():
    conn = create_synthetic_db()
    spy = NoExecuteAdapter(conn, SYNTHETIC_SCHOOL_V1)
    res = run_plan(
        ExecutionPlan.from_dict(plan_dict()), adapter=spy,
        scope=ScopeConfig(allowed_entities={"schools"}, denied_entities=set(), denied_fields=set()),
    )
    check("denied plan stops at sentinel", not res.ok and res.stage == "sentinel", f"stage={res.stage}")
    check("disposition POLICY_DENIED", res.sentinel.disposition == POLICY_DENIED, str(res.sentinel))
    check("no execute on denial", spy.executed is False and res.result is None)
    check("denial detail present", bool(res.detail))


def test_cost_denied_end_to_end():
    """Cost flows adapter -> Sentinel through the runner (no hand-passed
    estimate): the complex plan estimates 'high' complexity, denied against a
    'low' governance ceiling."""
    conn = create_synthetic_db()
    spy = NoExecuteAdapter(conn, SYNTHETIC_SCHOOL_V1)
    res = run_plan(
        ExecutionPlan.from_dict(plan_dict()), adapter=spy,
        governance=GovernanceConfig(read_only=True, max_cost_complexity="low"),
    )
    check("cost-denied stops at sentinel", not res.ok and res.stage == "sentinel", f"stage={res.stage}")
    check("cost disposition POLICY_DENIED", res.sentinel.disposition == POLICY_DENIED, str(res.sentinel))
    check("cost denial mentions complexity", "complexity" in (res.detail or "").lower(), str(res.detail))
    check("no execute on cost denial", spy.executed is False)


def test_cheap_plan_passes_cost_and_executes():
    conn = create_synthetic_db()
    res = run_plan(
        ExecutionPlan.from_dict(simple_plan_dict()), conn,
        governance=GovernanceConfig(read_only=True, max_cost_complexity="high", max_estimated_rows=1000),
    )
    check("cheap plan executes", res.ok and res.stage == "execution", f"stage={res.stage} detail={res.detail}")
    check("cheap plan returns rows", res.result is not None)


def test_cost_absent_approval_required():
    conn = create_synthetic_db()
    spy = NoCostNoExecuteAdapter(conn, SYNTHETIC_SCHOOL_V1)
    res = run_plan(
        ExecutionPlan.from_dict(plan_dict()), adapter=spy,
        governance=GovernanceConfig(read_only=True, approval_required_for_cost_absence=True),
    )
    check("cost-absent -> approval stops at sentinel", not res.ok and res.stage == "sentinel")
    check("disposition APPROVAL_REQUIRED", res.sentinel.disposition == APPROVAL_REQUIRED, str(res.sentinel))
    check("no execute on approval-required", spy.executed is False and res.result is None)


def main():
    print("End-to-end lifecycle (health -> validation -> cost -> sentinel -> execute)")
    print("=" * 60)
    for t in [
        test_full_flow_executes,
        test_validation_short_circuits,
        test_mutation_short_circuits_before_execution,
        test_health_terminal_short_circuits,
        test_degraded_proceeds_with_warning,
        test_sentinel_denied_executes_no_sql,
        test_cost_denied_end_to_end,
        test_cheap_plan_passes_cost_and_executes,
        test_cost_absent_approval_required,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
