"""
Profile data-scope → ScopeConfig bridge tests (agent Data Intelligence).

Run:  python tests/pipeline/test_data_scope.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ_ROOT))

import json  # noqa: E402
import tempfile  # noqa: E402

from adam.pipeline import DataScope, SourceModel  # noqa: E402
from adam.pipeline.data_scope import (  # noqa: E402
    write_session_scope, load_session_scope, session_scope_path,
)
from adam.pipeline.sentinel import (  # noqa: E402
    GovernanceConfig, ScopeConfig, evaluate, ALLOWED, POLICY_DENIED,
)
from adam.pipeline import ExecutionPlan  # noqa: E402

PASSED = 0
FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if not cond:
        FAILED += 1
        raise AssertionError(f"{name}" + (f" -- {detail}" if detail else ""))
    PASSED += 1
    print(f"  PASS  {name}")


MODEL = SourceModel(version="adam-test-mysql-v1", entities={
    "students": ("id", "name", "school_id", "first_name", "last_name", "dob"),
    "schools": ("id", "name", "level"),
    "guardians": ("id", "student_id", "name", "email"),
})

SAMPLE_BLOCK = {
    "enabled": True,
    "allowed_sources": ["adam-test-mysql-v1"],
    "default_detail_level": "aggregate",
    "student_level_allowed": False,
    "denied_fields": [
        "students.first_name", "students.last_name", "students.dob", "guardians.*",
    ],
    "budgets": {
        "max_data_queries_per_session": 7,
        "max_data_queries_per_agent": 2,
        "max_rows_returned": 50,
    },
}


def plan(**body_over):
    body = {
        "operation": "select", "entities": ["schools"],
        "projection": ["schools.name"], "filters": [], "joins": [],
        "group_by": ["schools.name"],
        "aggregations": [{"fn": "count", "field": "schools.id", "as": "n"}],
        "order_by": [], "limit": 10,
    }
    body.update(body_over)
    return ExecutionPlan.from_dict({
        "plan_version": "1.0", "intent_type": "query",
        "connection_handle": "conn_ro", "source_type": "sql",
        "source_model_version": "adam-test-mysql-v1",
        "purpose": "test", "estimated_row_scope": "small", "body": body,
    })


def test_disabled_when_absent_or_off():
    for block in (None, {}, {"enabled": False}, {"allowed_sources": ["x"]}, "nope"):
        ds = DataScope.from_block(block)
        check(f"absent/off block -> disabled ({block!r})", ds.enabled is False)
        check("disabled grants no source", ds.permits_source("adam-test-mysql-v1") is False)


def test_parses_block_fields():
    ds = DataScope.from_block(SAMPLE_BLOCK)
    check("enabled", ds.enabled is True)
    check("allowed_sources parsed", ds.allowed_sources == {"adam-test-mysql-v1"})
    check("permits listed source", ds.permits_source("adam-test-mysql-v1") is True)
    check("denies unlisted source", ds.permits_source("other-v1") is False)
    check("denied_fields parsed", "students.first_name" in ds.denied_fields and "guardians.*" in ds.denied_fields)
    check("budgets parsed", ds.max_queries_per_session == 7 and ds.max_queries_per_agent == 2 and ds.max_rows_returned == 50)
    check("aggregate_only since student_level not allowed", ds.aggregate_only is True)


def test_student_level_allowed_flips_aggregate_only():
    ds = DataScope.from_block({**SAMPLE_BLOCK, "student_level_allowed": True})
    check("student_level_allowed -> not aggregate_only", ds.aggregate_only is False)


def test_budget_defaults_when_missing():
    ds = DataScope.from_block({"enabled": True, "allowed_sources": ["x"]})
    check("default session budget", ds.max_queries_per_session == 5)
    check("default agent budget", ds.max_queries_per_agent == 3)
    check("default rows", ds.max_rows_returned == 100)


def test_build_scope_config_denylist_and_wildcard():
    ds = DataScope.from_block(SAMPLE_BLOCK)
    scope = ds.build_scope_config(MODEL)
    check("allowed_entities from model", scope.allowed_entities == {"students", "schools", "guardians"})
    check("explicit denied field carried", "students.first_name" in scope.denied_fields)
    check("wildcard expands to denied_entity", "guardians" in scope.denied_entities)
    check("wildcard not left as a literal field", "guardians.*" not in scope.denied_fields)
    check("aggregate_only propagated", scope.aggregate_only is True)
    check("identifying fields are qualified to student entities",
          "students.dob" in scope.identifying_fields and "students.name" in scope.identifying_fields)
    check("benign school dimension not flagged identifying", "schools.name" not in scope.identifying_fields)
    check("student_entities default", "students" in scope.student_entities)


def test_derived_scope_enforced_by_sentinel():
    # End-to-end through the real Sentinel: the derived scope blocks a denied
    # field and student-level detail, and allows a clean aggregate plan.
    gov = GovernanceConfig(read_only=True)
    scope = DataScope.from_block(SAMPLE_BLOCK).build_scope_config(MODEL)

    # denied field projected -> POLICY_DENIED
    out = evaluate(plan(entities=["students"], projection=["students.first_name"],
                        group_by=[], aggregations=[]), gov, scope)
    check("denied field blocked via derived scope", out.disposition == POLICY_DENIED, str(out))

    # whole guardians entity blocked (wildcard)
    out = evaluate(plan(entities=["guardians"], projection=["guardians.name"],
                        group_by=[], aggregations=[]), gov, scope)
    check("wildcard-denied entity blocked", out.disposition == POLICY_DENIED, str(out))

    # unaggregated student rows -> blocked (aggregate-only)
    out = evaluate(plan(entities=["students"], projection=["students.id"],
                        group_by=[], aggregations=[]), gov, scope)
    check("unaggregated student rows blocked", out.disposition == POLICY_DENIED, str(out))

    # clean aggregate-by-school plan -> ALLOWED
    out = evaluate(plan(), gov, scope)
    check("clean aggregate plan allowed", out.disposition == ALLOWED, str(out))


def test_session_scope_file_roundtrip():
    # The GUI spawn writes the profile block here; the skill handler reads it.
    with tempfile.TemporaryDirectory() as raw:
        sd = Path(raw)
        # Absent file -> disabled (fail-closed).
        check("missing session scope -> disabled", load_session_scope(sd).enabled is False)
        # Written block -> parsed back faithfully.
        p = write_session_scope(sd, SAMPLE_BLOCK)
        check("scope file lives at the canonical path", p == session_scope_path(sd) and p.exists())
        ds = load_session_scope(sd)
        check("written block reloads enabled", ds.enabled is True)
        check("reload preserves sources", ds.permits_source("adam-test-mysql-v1"))
        check("reload preserves denied fields", "students.first_name" in ds.denied_fields)
        check("reload preserves budgets", ds.max_rows_returned == 50)
        # Empty/None block -> {} on disk -> disabled.
        write_session_scope(sd, None)
        check("empty block -> disabled", load_session_scope(sd).enabled is False)
        check("empty block wrote {}", json.loads(session_scope_path(sd).read_text()) == {})


def main():
    print("Agent Data Intelligence: profile data-scope bridge")
    print("=" * 60)
    for t in [
        test_disabled_when_absent_or_off,
        test_parses_block_fields,
        test_student_level_allowed_flips_aggregate_only,
        test_budget_defaults_when_missing,
        test_build_scope_config_denylist_and_wildcard,
        test_derived_scope_enforced_by_sentinel,
        test_session_scope_file_roundtrip,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
