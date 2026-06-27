"""
Phase 4: end-to-end lifecycle.

    ExecutionPlan -> validate -> stub Sentinel -> SQLite adapter -> result

Proves the spine works against synthetic SQLite and that rejections
short-circuit at the right stage.

Run:  python tests/pipeline/test_end_to_end.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ_ROOT))

from adam.pipeline import (  # noqa: E402
    ExecutionPlan, create_synthetic_db, run_plan,
    GovernanceConfig, ScopeConfig, AdapterCostEstimate,
    POLICY_DENIED, APPROVAL_REQUIRED,
)
from adam.pipeline import runner as runner_mod  # noqa: E402

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


def test_full_flow_executes():
    conn = create_synthetic_db()
    plan = ExecutionPlan.from_dict(plan_dict())
    res = run_plan(plan, conn)
    check("pipeline ok", res.ok, f"stage={res.stage} detail={res.detail}")
    check("reached execution stage", res.stage == "execution")
    check("validation passed", res.validation.ok)
    check("sentinel allowed", res.sentinel and res.sentinel.allow)
    check("structured result returned", res.result is not None and res.result.row_count == 2,
          str(res.result.rows if res.result else None))
    check("result columns include alias",
          res.result and "avg_rate" in res.result.columns, str(res.result.columns))
    check("lineage carries plan_id",
          res.result.source_lineage.get("plan_id") == plan.plan_id)


def test_validation_short_circuits():
    conn = create_synthetic_db()
    bad = plan_dict()
    bad["body"]["projection"] = ["*"]
    res = run_plan(ExecutionPlan.from_dict(bad), conn)
    check("invalid plan stops at validation", not res.ok and res.stage == "validation",
          f"stage={res.stage}")
    check("never reached sentinel", res.sentinel is None)
    check("never reached execution", res.result is None)


def test_mutation_short_circuits_before_execution():
    conn = create_synthetic_db()
    bad = plan_dict()
    bad["intent_type"] = "mutation"
    res = run_plan(ExecutionPlan.from_dict(bad), conn)
    check("mutation stopped at validation (out of scope)",
          not res.ok and res.stage == "validation")
    check("mutation never executed", res.result is None)


def test_sentinel_denied_executes_no_sql():
    """A policy-denied plan must stop at Sentinel and never reach the
    adapter — no SQL is built or executed."""
    conn = create_synthetic_db()
    plan = ExecutionPlan.from_dict(plan_dict())

    # Spy: replace the adapter the runner would construct with one that
    # raises if it is ever instantiated.
    class ExplodingAdapter:
        def __init__(self, *a, **k):
            raise AssertionError("adapter must not be constructed on a denied plan")

    orig = runner_mod.SQLiteAdapter
    runner_mod.SQLiteAdapter = ExplodingAdapter
    try:
        res = run_plan(
            plan, conn,
            # deny by scope: attendance not allowed
            scope=ScopeConfig(allowed_entities={"schools"}, denied_entities=set(), denied_fields=set()),
        )
    finally:
        runner_mod.SQLiteAdapter = orig

    check("denied plan stops at sentinel", not res.ok and res.stage == "sentinel", f"stage={res.stage}")
    check("denied disposition is POLICY_DENIED", res.sentinel.disposition == POLICY_DENIED, str(res.sentinel))
    check("denied plan produced no result", res.result is None)
    check("denied detail explains the predicate", bool(res.detail), str(res.detail))


def test_sentinel_approval_required_executes_no_sql():
    conn = create_synthetic_db()
    plan = ExecutionPlan.from_dict(plan_dict())

    class ExplodingAdapter:
        def __init__(self, *a, **k):
            raise AssertionError("adapter must not be constructed on an approval-required plan")

    orig = runner_mod.SQLiteAdapter
    runner_mod.SQLiteAdapter = ExplodingAdapter
    try:
        res = run_plan(
            plan, conn,
            governance=GovernanceConfig(read_only=True, approval_required_for_cost_absence=True),
            cost_estimate=None,  # absent + approval-required -> APPROVAL_REQUIRED
        )
    finally:
        runner_mod.SQLiteAdapter = orig

    check("approval-required stops at sentinel", not res.ok and res.stage == "sentinel")
    check("disposition is APPROVAL_REQUIRED", res.sentinel.disposition == APPROVAL_REQUIRED, str(res.sentinel))
    check("approval-required produced no result", res.result is None)


def main():
    print("Phase 4: end-to-end lifecycle")
    print("=" * 60)
    for t in [
        test_full_flow_executes,
        test_validation_short_circuits,
        test_mutation_short_circuits_before_execution,
        test_sentinel_denied_executes_no_sql,
        test_sentinel_approval_required_executes_no_sql,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
