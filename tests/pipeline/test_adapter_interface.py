"""
Slice 3 Phase 1: Adapter interface + health + cost (adapter side).

Run:  python tests/pipeline/test_adapter_interface.py
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ_ROOT))

from adam.pipeline import (  # noqa: E402
    Adapter, AdapterHealth, AdapterCapabilities, AdapterCostEstimate,
    SQLiteAdapter, SYNTHETIC_SCHOOL_V1, create_synthetic_db,
    READY, DEGRADED, REINDEXING, OFFLINE, AUTHENTICATION_FAILED,
    ExecutionPlan,
)
from adam.pipeline import sentinel as sentinel_mod  # noqa: E402

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


def adapter(**kw):
    return SQLiteAdapter(create_synthetic_db(), SYNTHETIC_SCHOOL_V1, **kw)


def plan(**body_over):
    body = {"operation": "select", "entities": ["schools"],
            "projection": ["schools.name"], "limit": 100}
    body.update(body_over)
    return ExecutionPlan.from_dict({
        "plan_version": "1.0", "intent_type": "query",
        "connection_handle": "conn_school_ro", "source_type": "sql",
        "source_model_version": "synthetic-school-v1",
        "purpose": "t", "estimated_row_scope": "small", "body": body,
    })


def complex_plan():
    return plan(
        entities=["attendance", "schools"], projection=["schools.name"],
        joins=[{"left": "attendance.school_id", "right": "schools.id", "type": "inner"}],
        group_by=["schools.name"],
        aggregations=[{"fn": "avg", "field": "attendance.rate", "as": "avg_rate"}],
        order_by=[{"field": "avg_rate", "direction": "asc"}],
    )


def test_conformance():
    a = adapter()
    check("SQLiteAdapter is an Adapter (isinstance)", isinstance(a, Adapter))
    check("implements capabilities()", callable(getattr(a, "capabilities", None)))
    check("implements health()", callable(getattr(a, "health", None)))
    check("implements estimate_cost()", callable(getattr(a, "estimate_cost", None)))
    check("implements execute()", callable(getattr(a, "execute", None)))
    check("capabilities() returns AdapterCapabilities",
          isinstance(a.capabilities(), AdapterCapabilities))


def test_health_ready():
    a = adapter()
    h = a.health()
    check("health() READY for live in-memory DB", h.status == READY and h.is_ready)
    check("READY may proceed", h.may_proceed is True)
    check("health carries checked_at", bool(h.checked_at))


def test_health_forced_states():
    for status, terminal in [(OFFLINE, True), (AUTHENTICATION_FAILED, True),
                             (DEGRADED, False), (REINDEXING, False)]:
        h = adapter(health_status=status, health_detail="forced").health()
        check(f"forced {status} reported", h.status == status)
        check(f"{status} terminal={terminal}", h.is_terminal == terminal)
        check(f"{status} may_proceed={not terminal}", h.may_proceed == (not terminal))
    # transient classification
    check("DEGRADED is transient", adapter(health_status=DEGRADED).health().is_transient)
    check("REINDEXING is transient", adapter(health_status=REINDEXING).health().is_transient)


def test_estimate_cost():
    a = adapter()
    est = a.estimate_cost(complex_plan())
    check("estimate_cost returns AdapterCostEstimate", isinstance(est, AdapterCostEstimate))
    check("estimate uses Sentinel's cost type (one type)",
          type(est) is sentinel_mod.AdapterCostEstimate)
    check("complex plan -> high complexity (join+agg+group)", est.complexity == "high", str(est))
    simple = a.estimate_cost(plan())
    check("simple select -> low complexity", simple.complexity == "low", str(simple))
    # rows capped by limit
    capped = a.estimate_cost(plan(limit=1))
    check("rows capped by limit", capped.rows == 1, str(capped))


def test_interface_is_source_agnostic():
    abstract = set(Adapter.__abstractmethods__)
    check("abstract methods are exactly the 4 contract methods",
          abstract == {"capabilities", "health", "estimate_cost", "execute"}, str(abstract))
    check("translate is NOT in the interface", "translate" not in abstract)
    banned = ("sql", "cursor", "statement", "translate")
    for name in abstract:
        sig = str(inspect.signature(getattr(Adapter, name)))
        text = (name + sig).lower()
        check(f"signature of {name}() mentions no SQL/cursor/statement/translate",
              not any(b in text for b in banned), text)


def main():
    print("Slice 3 Phase 1: Adapter interface + health + cost")
    print("=" * 60)
    for t in [
        test_conformance,
        test_health_ready,
        test_health_forced_states,
        test_estimate_cost,
        test_interface_is_source_agnostic,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
