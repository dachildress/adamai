"""
Slice 4 Phase 1: Data Intelligence skill core — prompt, parsing, ownership
boundary, injectable seam. All with a FAKE model; no real call.

Run:  python tests/pipeline/test_skill.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ_ROOT))

from adam.pipeline import (  # noqa: E402
    SYNTHETIC_SCHOOL_V1, ExecutionPlan, QueryBody,
    build_system_prompt, parse_body, propose_plan,
    PlanParseError, PLAN_PARSE_ERROR,
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


WELL_FORMED_BODY = (
    '{"operation":"select","entities":["schools"],'
    '"projection":["schools.name","schools.level"],'
    '"filters":[{"field":"schools.level","op":"eq","value":"elementary"}],'
    '"limit":50}'
)


class FakeModel:
    """Injectable PlanModelFn that records calls and returns a canned string."""
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, system_prompt, objective):
        self.calls.append((system_prompt, objective))
        return self.response


def propose(response, **kw):
    fake = FakeModel(response)
    kw.setdefault("connection_handle", "conn_school_ro")
    plan = propose_plan("an objective", SYNTHETIC_SCHOOL_V1, fake, **kw)
    return plan, fake


def expect_parse_error(response, label):
    try:
        propose(response)
        check(f"{label} -> PLAN_PARSE_ERROR", False, "no error raised")
    except PlanParseError as e:
        check(f"{label} -> PLAN_PARSE_ERROR",
              e.category == PLAN_PARSE_ERROR and bool(e.detail))


def test_well_formed_body():
    plan, fake = propose(WELL_FORMED_BODY)
    check("model was called (seam injected)", len(fake.calls) == 1)
    check("returns an ExecutionPlan", isinstance(plan, ExecutionPlan))
    check("body parsed into QueryBody", isinstance(plan.body, QueryBody))
    check("body operation/select", plan.body.operation == "select")
    check("body entities from model", plan.body.entities == ("schools",))
    # Envelope is skill-owned.
    check("envelope intent_type=query (skill-owned)", plan.intent_type == "query")
    check("envelope connection_handle skill-owned", plan.connection_handle == "conn_school_ro")
    check("envelope source_model_version skill-owned",
          plan.source_model_version == SYNTHETIC_SCHOOL_V1.version)


def test_fenced_and_prose():
    fenced = "Here is the plan:\n```json\n" + WELL_FORMED_BODY + "\n```\nHope that helps!"
    plan, _ = propose(fenced)
    check("body wrapped in fences/prose still parses", plan.body.operation == "select")


def test_whole_string_json_fence_extracts_one_object():
    # fix_tokens Part 3: a whole-string ```json fence wrapping a complete object
    # is stripped explicitly so the object is never missed.
    from adam.pipeline.skill import _extract_json_objects, _strip_code_fences
    fenced = "```json\n" + WELL_FORMED_BODY + "\n```"
    check("fenced complete object -> exactly one extracted", len(_extract_json_objects(fenced)) == 1)
    check("strip returns inner JSON", _strip_code_fences(fenced).strip().startswith("{"))
    check("plain (no-lang) fence also yields one object",
          len(_extract_json_objects("```\n" + WELL_FORMED_BODY + "\n```")) == 1)
    check("unfenced still yields one object", len(_extract_json_objects(WELL_FORMED_BODY)) == 1)


def test_non_json():
    expect_parse_error("I cannot help with that.", "non-JSON text")
    expect_parse_error("", "empty output")


def test_multiple_objects():
    two = WELL_FORMED_BODY + "\n" + WELL_FORMED_BODY
    expect_parse_error(two, "multiple JSON objects")


def test_sql_text():
    expect_parse_error("SELECT name, level FROM schools WHERE level = 'elementary' LIMIT 50;",
                       "SQL text instead of body")


def test_envelope_bearing_rejected_and_cannot_steer_connection():
    # Model tries to supply a full envelope incl. a DIFFERENT connection_handle.
    full = (
        '{"plan_version":"1.0","intent_type":"query",'
        '"connection_handle":"conn_ATTACKER","source_type":"sql",'
        '"source_model_version":"synthetic-school-v1","purpose":"x",'
        '"estimated_row_scope":"small",'
        '"body":{"operation":"select","entities":["schools"],'
        '"projection":["schools.name"],"limit":10}}'
    )
    fake = FakeModel(full)
    raised = False
    try:
        propose_plan("obj", SYNTHETIC_SCHOOL_V1, fake, connection_handle="conn_school_ro")
    except PlanParseError as e:
        raised = True
        check("envelope-bearing output -> PLAN_PARSE_ERROR", e.category == PLAN_PARSE_ERROR)
        check("error names the envelope fields", "connection_handle" in (e.detail or ""))
    check("no plan was constructed (model can't steer connection)", raised)


def test_single_envelope_field_rejected():
    # Even one stray envelope field (just connection_handle) must be rejected.
    sneaky = (
        '{"connection_handle":"conn_ATTACKER","operation":"select",'
        '"entities":["schools"],"projection":["schools.name"],"limit":10}'
    )
    expect_parse_error(sneaky, "single stray connection_handle")


def test_not_a_body_object():
    expect_parse_error('{"foo":"bar","baz":1}', "JSON object that is not a body")


def test_prompt_builder_content():
    sp = build_system_prompt(SYNTHETIC_SCHOOL_V1)
    check("prompt lists entities", "students" in sp and "attendance" in sp and "schools" in sp)
    check("prompt lists fields", "attendance: id, student_id" in sp or "rate" in sp)
    check("prompt instructs body-only JSON", "ONLY a single JSON object" in sp or "ONLY the JSON body" in sp)
    check("prompt forbids SQL", "no SQL" in sp.lower() or "not sql" in sp.lower())
    check("prompt forbids envelope fields",
          "connection_handle" in sp and "do not emit" in sp.lower())
    # fix_join: field references must be qualified entity.field.
    check("prompt requires qualified entity.field references", "entity.field" in sp)
    check("prompt names every field-ref location",
          all(loc in sp for loc in ("projection", "filters[].field", "joins[].left",
                                    "joins[].right", "group_by", "aggregations[].field",
                                    "order_by[].field")))
    check("prompt forbids bare-entity references", "bare entity" in sp.lower())
    check("prompt includes a worked join example",
          '"students.school_id"' in sp and '"schools.id"' in sp)
    # fix_sql: the COMPLETE contract — closed enum sets, alias exception, limit max.
    check("prompt lists filter ops verbatim",
          all(op in sp for op in ("eq", "ne", "lt", "lte", "gt", "gte", "in",
                                  "not_in", "between", "like", "is_null", "not_null")))
    check("prompt lists join types verbatim", "inner, left, right, full" in sp)
    check("prompt lists aggregation fns verbatim", "count, sum, avg, min, max" in sp)
    check("prompt lists order directions verbatim", "asc, desc" in sp)
    check("prompt states the limit <= 1000 max", "<= 1000" in sp)
    check("prompt explains the aggregation-alias exception",
          "alias" in sp.lower() and "student_count" in sp)
    check("prompt requires non-empty projection / never ['*']",
          "['*']" in sp or "never project" in sp.lower() or "NEVER be ['*']" in sp)
    # fix_having: HAVING (aggregate-threshold filtering) is taught.
    check("prompt documents the having schema", '"having": [{"field","op","value"}]' in sp)
    check("prompt lists having ops", "having[].op: eq, ne, lt, lte, gt, gte" in sp)
    check("prompt explains having vs filters", "use 'having'" in sp and "AFTER grouping" in sp)
    check("prompt has the aggregate-threshold worked example",
          '"having":[{"field":"total_absences","op":"gt","value":5}]' in sp)


def test_no_real_model_call():
    # The seam is fully injected; constructing/using the skill must not import
    # or call adam.core. (Belt-and-suspenders alongside the isolation test.)
    import sys as _sys
    fake = FakeModel(WELL_FORMED_BODY)
    propose_plan("obj", SYNTHETIC_SCHOOL_V1, fake, connection_handle="conn_school_ro")
    check("fake model recorded the call", len(fake.calls) == 1)
    check("no adam.core imported by skill use",
          not any(m.startswith("adam.core") for m in _sys.modules))


def main():
    print("Slice 4 Phase 1: Data Intelligence skill core")
    print("=" * 60)
    for t in [
        test_well_formed_body,
        test_fenced_and_prose,
        test_whole_string_json_fence_extracts_one_object,
        test_non_json,
        test_multiple_objects,
        test_sql_text,
        test_envelope_bearing_rejected_and_cannot_steer_connection,
        test_single_envelope_field_rejected,
        test_not_a_body_object,
        test_prompt_builder_content,
        test_no_real_model_call,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
