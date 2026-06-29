"""
Phase 2 tests: validation rules + credential-like detection.

Run:  python tests/pipeline/test_validation.py
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ_ROOT))

from adam.pipeline import (  # noqa: E402
    AdapterCapabilities, ExecutionPlan, SQLITE_CAPABILITIES,
    ValidationConfig, validate,
    VALIDATION_ERROR, SOURCE_MODEL_ERROR, CAPABILITY_ERROR,
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


def base_plan_dict():
    return {
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


def v(d, capabilities=SQLITE_CAPABILITIES, config=ValidationConfig()):
    return validate(ExecutionPlan.from_dict(d), capabilities, config)


def test_valid_passes():
    out = v(base_plan_dict())
    check("valid query passes validation", out.ok, f"{out.category}: {out.detail}")
    check("valid outcome has no category", out.category is None)


def test_missing_envelope():
    d = base_plan_dict(); del d["source_type"]
    out = v(d)
    check("missing envelope field rejected", not out.ok and out.category == VALIDATION_ERROR,
          f"{out.category}: {out.detail}")


def test_unknown_intent():
    d = base_plan_dict(); d["intent_type"] = "frobnicate"
    out = v(d)
    check("unknown intent_type rejected", out.category == VALIDATION_ERROR, str(out))


def test_mutation_out_of_scope():
    d = base_plan_dict(); d["intent_type"] = "mutation"
    out = v(d)
    check("mutation rejected as out of scope",
          out.category == VALIDATION_ERROR and "out of scope" in (out.detail or ""), str(out))


def test_raw_statement_out_of_scope():
    d = base_plan_dict(); d["intent_type"] = "raw_statement"
    out = v(d)
    check("raw_statement rejected as out of scope",
          out.category == VALIDATION_ERROR and "out of scope" in (out.detail or ""), str(out))


def test_bad_operation():
    d = base_plan_dict(); d["body"]["operation"] = "delete"
    out = v(d)
    check("operation other than select rejected", out.category == VALIDATION_ERROR, str(out))


def test_projection_star():
    d = base_plan_dict(); d["body"]["projection"] = ["*"]
    out = v(d)
    check("projection ['*'] rejected", out.category == VALIDATION_ERROR, str(out))


def test_projection_empty():
    d = base_plan_dict(); d["body"]["projection"] = []
    out = v(d)
    check("empty projection rejected", out.category == VALIDATION_ERROR, str(out))


def test_missing_limit():
    d = base_plan_dict(); del d["body"]["limit"]
    out = v(d)
    check("missing limit rejected", out.category == VALIDATION_ERROR, str(out))


def test_limit_over_max():
    d = base_plan_dict(); d["body"]["limit"] = 5000
    out = v(d, config=ValidationConfig(max_limit=1000))
    check("limit over max rejected", out.category == VALIDATION_ERROR, str(out))


def test_unknown_source_model():
    d = base_plan_dict(); d["source_model_version"] = "does-not-exist-v9"
    out = v(d)
    check("unknown source model rejected", out.category == SOURCE_MODEL_ERROR, str(out))


def test_unresolved_entity():
    d = base_plan_dict()
    d["body"]["entities"] = ["teachers"]
    d["body"]["projection"] = ["teachers.name"]
    d["body"]["filters"] = []; d["body"]["joins"] = []
    d["body"]["group_by"] = []; d["body"]["aggregations"] = []; d["body"]["order_by"] = []
    out = v(d)
    check("unresolved entity rejected", out.category == SOURCE_MODEL_ERROR, str(out))


def test_unresolved_field():
    d = base_plan_dict()
    # students.gpa is not in the source model.
    d["body"]["entities"] = ["students"]
    d["body"]["projection"] = ["students.name"]
    d["body"]["filters"] = [{"field": "students.gpa", "op": "eq", "value": 4.0}]
    d["body"]["joins"] = []; d["body"]["group_by"] = []
    d["body"]["aggregations"] = []; d["body"]["order_by"] = []
    out = v(d)
    check("unresolved field rejected", out.category == SOURCE_MODEL_ERROR, str(out))


def test_join_capability():
    d = base_plan_dict()
    caps = AdapterCapabilities(supports_join=False, supports_grouping=True,
                               supports_aggregation=True, supports_ordering=True)
    out = v(d, capabilities=caps)
    check("join rejected when supports_join=false", out.category == CAPABILITY_ERROR, str(out))


def test_credential_detection():
    # A credential-like value injected into a filter value.
    creds = [
        ("password kv", "Server=db;Password=hunter2;"),
        ("uri creds", "postgres://user:secretpw@host:5432/db"),
        ("bearer", "Bearer abc.def.ghi"),
        ("authorization", "Authorization: Basic Zm9v"),
        ("private key", "-----BEGIN RSA PRIVATE KEY-----"),
        ("api_key", "api_key=sk-livexyz"),
        ("access_token", "access_token=ya29.abc"),
    ]
    for label, val in creds:
        d = base_plan_dict()
        d["body"]["filters"] = [{"field": "schools.level", "op": "eq", "value": val}]
        d["body"]["joins"] = []; d["body"]["group_by"] = []
        d["body"]["aggregations"] = []; d["body"]["order_by"] = []
        d["body"]["projection"] = ["schools.name"]; d["body"]["entities"] = ["schools"]
        out = v(d)
        check(f"credential rejected: {label}",
              out.category == VALIDATION_ERROR and "credential" in (out.detail or ""), str(out))


def test_connection_handle_not_falsely_rejected():
    # A handle that resembles a name must NOT trip credential detection,
    # and a handle is opaque so even underscores/colons are fine.
    for handle in ("conn_powerschool_ro", "conn_finance_ro", "handle:powerschool:ro"):
        d = base_plan_dict(); d["connection_handle"] = handle
        out = v(d)
        check(f"connection_handle not falsely rejected: {handle}", out.ok, str(out))


def test_aggregation_fn_case_insensitive():
    # The model may emit standard-SQL uppercase agg names; these must validate
    # and be normalized to lowercase in the canonical plan. The allowed set is
    # unchanged — a genuinely unknown fn still fails.
    for cased in ("COUNT", "Count", "count", "AVG", "Sum"):
        d = base_plan_dict()
        d["body"]["aggregations"] = [{"fn": cased, "field": "attendance.rate", "as": "avg_rate"}]
        plan = ExecutionPlan.from_dict(d)
        check(f"agg fn {cased!r} normalized to lowercase in canonical plan",
              plan.body.aggregations[0].fn == cased.lower(), plan.body.aggregations[0].fn)
        out = validate(plan, SQLITE_CAPABILITIES, ValidationConfig())
        check(f"agg fn {cased!r} validates", out.ok, f"{out.category}: {out.detail}")


def test_unknown_aggregation_fn_still_rejected():
    d = base_plan_dict()
    d["body"]["aggregations"] = [{"fn": "median", "field": "attendance.rate", "as": "avg_rate"}]
    out = v(d)
    check("genuinely unknown agg fn still rejected",
          not out.ok and out.category == VALIDATION_ERROR, str(out))
    # case variant of an unknown fn is also rejected (set not widened)
    d["body"]["aggregations"] = [{"fn": "MEDIAN", "field": "attendance.rate", "as": "avg_rate"}]
    out2 = v(d)
    check("unknown agg fn rejected regardless of case",
          not out2.ok and out2.category == VALIDATION_ERROR, str(out2))


def main():
    print("Phase 2: validation + credential detection")
    print("=" * 60)
    for t in [
        test_valid_passes,
        test_missing_envelope,
        test_unknown_intent,
        test_mutation_out_of_scope,
        test_raw_statement_out_of_scope,
        test_bad_operation,
        test_projection_star,
        test_projection_empty,
        test_missing_limit,
        test_limit_over_max,
        test_unknown_source_model,
        test_unresolved_entity,
        test_unresolved_field,
        test_join_capability,
        test_credential_detection,
        test_connection_handle_not_falsely_rejected,
        test_aggregation_fn_case_insensitive,
        test_unknown_aggregation_fn_still_rejected,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
