"""
Slice 2 Phase 1: Sentinel predicate tests (isolated, no adapter/DB).

Run:  python tests/pipeline/test_sentinel.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ_ROOT))

from adam.pipeline import ExecutionPlan, validate, ValidationConfig  # noqa: E402
from adam.pipeline.sentinel import (  # noqa: E402
    AdapterCostEstimate, GovernanceConfig, ScopeConfig, SentinelOutcome,
    evaluate, is_write, ALLOWED, POLICY_DENIED, APPROVAL_REQUIRED,
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


ALL_ENTITIES = {"students", "attendance", "schools"}


def gov(**over):
    base = dict(read_only=True, approval_required_for_cost_absence=False,
                max_estimated_rows=None, max_cost_complexity=None)
    base.update(over)
    return GovernanceConfig(**base)


def scope(**over):
    base = dict(allowed_entities=set(ALL_ENTITIES), denied_entities=set(), denied_fields=set())
    base.update(over)
    return ScopeConfig(**base)


def plan(**body_over):
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
    return ExecutionPlan.from_dict({
        "plan_version": "1.0", "intent_type": "query",
        "connection_handle": "conn_school_ro", "source_type": "sql",
        "source_model_version": "synthetic-school-v1",
        "purpose": "test", "estimated_row_scope": "small", "body": body,
    })


def mutation_like_plan():
    # A fabricated plan that reaches Sentinel directly (validation would
    # reject it; here we prove the read-only predicate denies it).
    return ExecutionPlan.from_dict({
        "plan_version": "1.0", "intent_type": "mutation",
        "connection_handle": "conn_school_ro", "source_type": "sql",
        "source_model_version": "synthetic-school-v1",
        "purpose": "test", "estimated_row_scope": "small",
        "body": {"operation": "update", "entities": ["students"],
                 "projection": ["students.name"], "limit": 1},
    })


def test_is_write_helper():
    check("select is not a write", is_write(plan()) is False)
    check("mutation intent is a write", is_write(mutation_like_plan()) is True)
    check("insert operation is a write",
          is_write(plan(operation="insert")) is True)
    raw = ExecutionPlan.from_dict({
        "plan_version": "1.0", "intent_type": "raw_statement",
        "connection_handle": "c", "source_type": "sql",
        "source_model_version": "synthetic-school-v1", "purpose": "p",
        "estimated_row_scope": "small", "body": None})
    check("raw_statement is treated as write", is_write(raw) is True)


def test_valid_allowed():
    out = evaluate(plan(), gov(), scope())
    check("valid plan allowed", out.disposition == ALLOWED and out.ok, str(out))
    check("read-only query/select allowed", out.allow is True)


def test_read_only_denies_write():
    out = evaluate(mutation_like_plan(), gov(read_only=True), scope())
    check("write denied under read_only", out.disposition == POLICY_DENIED, str(out))
    out2 = evaluate(plan(operation="insert"), gov(read_only=True), scope())
    check("non-read operation denied (POLICY_DENIED)", out2.disposition == POLICY_DENIED, str(out2))


def test_entity_scope():
    out = evaluate(plan(entities=["attendance", "schools"]),
                   gov(), scope(allowed_entities={"schools"}))
    check("entity outside allowlist denied", out.disposition == POLICY_DENIED, str(out))
    out2 = evaluate(plan(), gov(), scope(denied_entities={"attendance"}))
    check("entity in denylist denied", out2.disposition == POLICY_DENIED, str(out2))


def test_field_denylist():
    cases = [
        ("projection", dict(projection=["students.ssn"], entities=["students"],
                            filters=[], joins=[], group_by=[], aggregations=[], order_by=[])),
        ("filter", dict(filters=[{"field": "students.ssn", "op": "eq", "value": 1}],
                        entities=["students"], projection=["students.name"],
                        joins=[], group_by=[], aggregations=[], order_by=[])),
        ("join", dict(joins=[{"left": "students.ssn", "right": "schools.id", "type": "inner"}],
                      entities=["students", "schools"], projection=["schools.name"],
                      filters=[], group_by=[], aggregations=[], order_by=[])),
        ("aggregation", dict(aggregations=[{"fn": "count", "field": "students.ssn", "as": "n"}],
                             entities=["students"], projection=["students.name"],
                             filters=[], joins=[], group_by=[], order_by=[])),
        ("order_by", dict(order_by=[{"field": "students.ssn", "direction": "asc"}],
                          entities=["students"], projection=["students.name"],
                          filters=[], joins=[], group_by=[], aggregations=[])),
    ]
    for label, over in cases:
        out = evaluate(plan(**over), gov(), scope(denied_fields={"students.ssn"}))
        check(f"denied {label} field -> POLICY_DENIED",
              out.disposition == POLICY_DENIED, str(out))


def test_cost_predicate():
    # rows over threshold
    out = evaluate(plan(), gov(max_estimated_rows=100_000),
                   scope(), AdapterCostEstimate(rows=500_000))
    check("cost rows over threshold denied", out.disposition == POLICY_DENIED, str(out))
    # complexity over threshold (numeric ordering: high > medium)
    out = evaluate(plan(), gov(max_cost_complexity="medium"),
                   scope(), AdapterCostEstimate(complexity="high"))
    check("cost complexity over threshold denied", out.disposition == POLICY_DENIED, str(out))
    # complexity within threshold allowed
    out = evaluate(plan(), gov(max_cost_complexity="high"),
                   scope(), AdapterCostEstimate(complexity="medium"))
    check("cost complexity within threshold allowed", out.disposition == ALLOWED, str(out))
    # absent + approval required
    out = evaluate(plan(), gov(approval_required_for_cost_absence=True), scope(), None)
    check("cost absent -> APPROVAL_REQUIRED when configured",
          out.disposition == APPROVAL_REQUIRED, str(out))
    # absent + allowed
    out = evaluate(plan(), gov(approval_required_for_cost_absence=False), scope(), None)
    check("cost absent allowed when configured", out.disposition == ALLOWED, str(out))


def test_detail_strings():
    out = evaluate(plan(), gov(), scope(denied_entities={"attendance"}))
    check("denied outcome has a detail string", bool(out.detail), str(out))
    out2 = evaluate(plan(), gov(approval_required_for_cost_absence=True), scope(), None)
    check("approval outcome has a detail string", bool(out2.detail), str(out2))


def test_validation_separate_from_sentinel():
    # A malformed plan is a ValidationOutcome (VALIDATION_ERROR), never a
    # SentinelOutcome. The two types/stages stay distinct.
    from adam.pipeline import SQLITE_CAPABILITIES, VALIDATION_ERROR
    bad = plan(projection=["*"])
    vout = validate(bad, SQLITE_CAPABILITIES, ValidationConfig())
    check("malformed plan is a validation error", vout.category == VALIDATION_ERROR)
    check("validation outcome is not a SentinelOutcome",
          not isinstance(vout, SentinelOutcome))
    check("validation outcome has no disposition attr", not hasattr(vout, "disposition"))


def test_sentinel_ignores_sql_text():
    # Sentinel is structural: a scary SQL string in a FILTER VALUE is not
    # its concern (the adapter parameterizes it). With in-scope fields the
    # plan is ALLOWED — proving Sentinel does not parse/inspect SQL text.
    p = plan(
        entities=["students"], projection=["students.name"],
        filters=[{"field": "students.name", "op": "eq",
                  "value": "'; DROP TABLE students;--"}],
        joins=[], group_by=[], aggregations=[], order_by=[],
    )
    out = evaluate(p, gov(), scope())
    check("sentinel allows despite SQL-ish filter value (no SQL parsing)",
          out.disposition == ALLOWED, str(out))


def test_detail_level_aggregate_only():
    # Default (aggregate_only=False): the web path is unchanged — a student-row
    # plan with no aggregation is still ALLOWED by this gate.
    student_rows = dict(entities=["students"], projection=["students.name"],
                        filters=[], joins=[], group_by=[], aggregations=[], order_by=[])
    out = evaluate(plan(**student_rows), gov(), scope())
    check("default scope leaves student-row plan allowed", out.disposition == ALLOWED, str(out))

    agg_scope = dict(aggregate_only=True, student_entities={"students"},
                     identifying_fields={"students.name"})

    # Unaggregated rows from a student entity -> denied under aggregate_only.
    out = evaluate(plan(**student_rows), gov(), scope(**agg_scope))
    check("aggregate-only denies unaggregated student rows",
          out.disposition == POLICY_DENIED, str(out))

    # Projecting an identifying field (even with an aggregation) -> denied.
    ident = dict(entities=["students"], projection=["students.name"],
                 filters=[], joins=[], group_by=["students.name"],
                 aggregations=[{"fn": "count", "field": "students.id", "as": "n"}], order_by=[])
    out = evaluate(plan(**ident), gov(), scope(**agg_scope))
    check("aggregate-only denies identifying field in projection/group_by",
          out.disposition == POLICY_DENIED, str(out))

    # denied_fields are also treated as identifying under aggregate_only.
    deny = dict(entities=["students", "schools"], projection=["schools.name", "students.ssn"],
                filters=[], joins=[{"left": "students.school_id", "right": "schools.id", "type": "inner"}],
                group_by=["schools.name"],
                aggregations=[{"fn": "count", "field": "students.id", "as": "n"}], order_by=[])
    out = evaluate(plan(**deny), gov(), scope(denied_fields={"students.ssn"}, **agg_scope))
    check("denied field still blocked under aggregate-only",
          out.disposition == POLICY_DENIED, str(out))

    # A clean aggregate plan (count by school, no identifying fields) -> ALLOWED.
    good = dict(entities=["students", "schools"], projection=["schools.name", "n"],
                filters=[], joins=[{"left": "students.school_id", "right": "schools.id", "type": "inner"}],
                group_by=["schools.name"],
                aggregations=[{"fn": "count", "field": "students.id", "as": "n"}],
                order_by=[{"field": "n", "direction": "desc"}])
    out = evaluate(plan(**good), gov(), scope(**agg_scope))
    check("aggregate-only allows a clean aggregate-by-school plan",
          out.disposition == ALLOWED, str(out))


def main():
    print("Slice 2 Phase 1: Sentinel predicates")
    print("=" * 60)
    for t in [
        test_is_write_helper,
        test_valid_allowed,
        test_read_only_denies_write,
        test_entity_scope,
        test_field_denylist,
        test_cost_predicate,
        test_detail_level_aggregate_only,
        test_detail_strings,
        test_validation_separate_from_sentinel,
        test_sentinel_ignores_sql_text,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
