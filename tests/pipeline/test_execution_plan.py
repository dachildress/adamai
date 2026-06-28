"""
Phase 1 tests: ExecutionPlan immutability + deterministic plan_id.

Run:  python tests/pipeline/test_execution_plan.py
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parents[2]   # /opt/adam
sys.path.insert(0, str(PROJ_ROOT))

from adam.pipeline import (  # noqa: E402
    ExecutionPlan, ExecutionRequest, QueryBody, compute_plan_id,
)

PASSED = 0
FAILED = 0


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


# A representative valid query plan dict (interface §10.1 shape).
def plan_dict(**overrides):
    d = {
        "plan_version": "1.0",
        "intent_type": "query",
        "connection_handle": "conn_school_ro",
        "source_type": "sql",
        "source_model_version": "synthetic-school-v1",
        "purpose": "Elementary attendance by school",
        "estimated_row_scope": "small",
        "body": {
            "operation": "select",
            "entities": ["attendance", "schools"],
            "projection": ["schools.name", "attendance.rate"],
            "filters": [{"field": "schools.level", "op": "eq", "value": "elementary"}],
            "joins": [{"left": "attendance.school_id", "right": "schools.id", "type": "inner"}],
            "group_by": ["schools.name"],
            "aggregations": [{"fn": "avg", "field": "attendance.rate", "as": "avg_rate"}],
            "order_by": [{"field": "avg_rate", "direction": "asc"}],
            "limit": 100,
        },
    }
    d.update(overrides)
    return d


def test_immutability():
    plan = ExecutionPlan.from_dict(plan_dict())

    try:
        plan.intent_type = "mutation"
        check("ExecutionPlan is frozen", False, "assignment succeeded")
    except dataclasses.FrozenInstanceError:
        check("ExecutionPlan is frozen", True)

    try:
        plan.body.limit = 5
        check("QueryBody is frozen", False, "assignment succeeded")
    except dataclasses.FrozenInstanceError:
        check("QueryBody is frozen", True)

    check("collections stored as tuples (entities)", isinstance(plan.body.entities, tuple))
    check("collections stored as tuples (filters)", isinstance(plan.body.filters, tuple))


def test_plan_id_identical():
    a = ExecutionPlan.from_dict(plan_dict())
    b = ExecutionPlan.from_dict(plan_dict())
    check("identical plans -> identical plan_id", a.plan_id == b.plan_id, f"{a.plan_id} vs {b.plan_id}")
    check("plan_id is sha256 hex (64 chars)", len(a.plan_id) == 64)
    check("compute_plan_id matches property", compute_plan_id(a) == a.plan_id)


def test_plan_id_key_order_independent():
    # Same logical plan, envelope keys supplied in a different order.
    d = plan_dict()
    reordered = {k: d[k] for k in reversed(list(d.keys()))}
    a = ExecutionPlan.from_dict(d)
    b = ExecutionPlan.from_dict(reordered)
    check("JSON key ordering does not affect plan_id", a.plan_id == b.plan_id)


def test_plan_id_changes_with_logical_change():
    a = ExecutionPlan.from_dict(plan_dict())
    b = ExecutionPlan.from_dict(plan_dict(purpose="A different objective"))
    check("changed envelope -> different plan_id", a.plan_id != b.plan_id)

    body_changed = plan_dict()
    body_changed["body"]["limit"] = 50
    c = ExecutionPlan.from_dict(body_changed)
    check("changed body (limit) -> different plan_id", a.plan_id != c.plan_id)

    filt_changed = plan_dict()
    filt_changed["body"]["filters"] = [
        {"field": "schools.level", "op": "eq", "value": "middle"}]
    e = ExecutionPlan.from_dict(filt_changed)
    check("changed filter value -> different plan_id", a.plan_id != e.plan_id)


def test_plan_id_runtime_context_invariant():
    plan = ExecutionPlan.from_dict(plan_dict())
    base_id = plan.plan_id

    req_a = ExecutionRequest(execution_id="x1", plan=plan, runtime_context={})
    req_b = ExecutionRequest(
        execution_id="x2", plan=plan,
        runtime_context={"retry_count": 3, "adapter_health": "READY",
                         "estimated_cost": {"rows": 999}},
    )
    check("runtime_context does not change plan_id",
          req_a.plan.plan_id == req_b.plan.plan_id == base_id)

    # Mutating runtime_context after construction must not affect plan_id.
    req_b.runtime_context["retry_count"] = 9
    req_b.runtime_context["execution_state"] = "running"
    check("mutating runtime_context does not change plan_id",
          req_b.plan.plan_id == base_id)

    # ExecutionRequest itself is frozen (can't swap the plan out).
    try:
        req_a.plan = ExecutionPlan.from_dict(plan_dict(purpose="swap"))
        check("ExecutionRequest is frozen", False, "assignment succeeded")
    except dataclasses.FrozenInstanceError:
        check("ExecutionRequest is frozen", True)


def main():
    print("Phase 1: ExecutionPlan + plan_id")
    print("=" * 60)
    for t in [
        test_immutability,
        test_plan_id_identical,
        test_plan_id_key_order_independent,
        test_plan_id_changes_with_logical_change,
        test_plan_id_runtime_context_invariant,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
