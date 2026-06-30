"""
Slice 4 Phase 2: skill end-to-end through the EXISTING governed pipeline.

Proves the architecture's payoff: the model can be wrong (hallucinated
entity, out-of-scope, omitted limit) and governance still catches it — the
skill doesn't have bespoke legality logic. All with a fake model.

Run:  python tests/pipeline/test_skill_e2e.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ_ROOT))

from adam.pipeline import (  # noqa: E402
    SYNTHETIC_SCHOOL_V1, create_synthetic_db, run_objective, analyze_objective,
    GovernanceConfig, ScopeConfig,
    SOURCE_MODEL_ERROR, VALIDATION_ERROR, POLICY_DENIED,
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


def fake(response):
    def fn(system_prompt, objective):
        return response
    return fn


def run(response, **kw):
    conn = create_synthetic_db()
    kw.setdefault("connection_handle", "conn_school_ro")
    return run_objective("an objective", conn, SYNTHETIC_SCHOOL_V1, fake(response), **kw)


def test_well_formed_executes():
    body = ('{"operation":"select","entities":["schools"],'
            '"projection":["schools.name","schools.level"],'
            '"filters":[{"field":"schools.level","op":"eq","value":"elementary"}],'
            '"limit":50}')
    res = run(body)
    check("skill ok", res.ok, f"parse_error={res.parse_error} pipe={res.pipeline}")
    check("plan constructed", res.plan is not None)
    check("pipeline executed", res.pipeline and res.pipeline.stage == "execution")
    check("returns rows (2 elementary schools)",
          res.pipeline.result and res.pipeline.result.row_count == 2,
          str(res.pipeline.result.rows if res.pipeline.result else None))


def test_hallucinated_entity_caught_by_validation():
    body = ('{"operation":"select","entities":["teachers"],'
            '"projection":["teachers.name"],"limit":10}')
    res = run(body)
    check("not ok", not res.ok)
    check("plan WAS constructed (skill doesn't pre-judge legality)", res.plan is not None)
    check("caught at validation as SOURCE_MODEL_ERROR",
          res.pipeline and res.pipeline.stage == "validation"
          and res.pipeline.validation.category == SOURCE_MODEL_ERROR,
          str(res.pipeline and res.pipeline.validation))


def test_missing_limit_caught_by_validation():
    # Parseable but incomplete (no limit) — governance catches OMISSION.
    body = ('{"operation":"select","entities":["attendance"],'
            '"projection":["attendance.rate"]}')
    res = run(body)
    check("incomplete body constructed a plan", res.plan is not None)
    check("missing limit -> VALIDATION_ERROR",
          res.pipeline and res.pipeline.validation.category == VALIDATION_ERROR,
          str(res.pipeline and res.pipeline.validation))


def test_select_star_caught_by_validation():
    body = ('{"operation":"select","entities":["schools"],'
            '"projection":["*"],"limit":10}')
    res = run(body)
    check("projection ['*'] -> VALIDATION_ERROR",
          res.pipeline and res.pipeline.validation.category == VALIDATION_ERROR,
          str(res.pipeline and res.pipeline.validation))


def test_out_of_scope_caught_by_sentinel():
    # Valid plan, but entity outside the Sentinel allowlist -> POLICY_DENIED.
    body = ('{"operation":"select","entities":["students"],'
            '"projection":["students.name"],"limit":10}')
    res = run(body, scope=ScopeConfig(allowed_entities={"schools"},
                                      denied_entities=set(), denied_fields=set()))
    check("plan constructed", res.plan is not None)
    check("out-of-scope -> POLICY_DENIED at sentinel",
          res.pipeline and res.pipeline.stage == "sentinel"
          and res.pipeline.sentinel.disposition == POLICY_DENIED,
          str(res.pipeline and res.pipeline.sentinel))


def test_parse_error_never_reaches_pipeline():
    res = run("I can't do that, but here's some prose.")
    check("parse error reported", not res.ok and res.parse_error)
    check("no plan constructed", res.plan is None)
    check("pipeline never ran", res.pipeline is None)


def test_max_rows_clamps_effective_limit():
    # agent Data Intelligence: max_rows CLAMPS the plan's limit down (min), never
    # raising it. 3 schools exist; a plan with limit=50 + max_rows=1 returns 1.
    conn = create_synthetic_db()
    body = ('{"operation":"select","entities":["schools"],'
            '"projection":["schools.name"],"limit":50}')
    interp = ('{"inferences":[],"recommendations":[],"assumptions":[],'
              '"limitations":[],"confidence":"low","confidence_rationale":"x"}')
    res = analyze_objective(
        "list schools", connection=conn, source_model=SYNTHETIC_SCHOOL_V1,
        planning_model_fn=fake(body), interpretation_model_fn=fake(interp),
        connection_handle="conn_school_ro", max_rows=1,
    )
    check("clamped query executed ok", res.status in ("ok", "empty"), str(res))
    check("effective limit clamped to 1 row (not 3)", res.data_analyzed.get("row_count") == 1,
          str(res.data_analyzed))
    # Without the clamp, the same plan returns all 3.
    res2 = analyze_objective(
        "list schools", connection=create_synthetic_db(), source_model=SYNTHETIC_SCHOOL_V1,
        planning_model_fn=fake(body), interpretation_model_fn=fake(interp),
        connection_handle="conn_school_ro",
    )
    check("unclamped returns all 3 rows", res2.data_analyzed.get("row_count") == 3, str(res2.data_analyzed))


ROSTER_BODY = ('{"operation":"select","entities":["students"],'
               '"projection":["students.id","students.name","students.grade_level"],"limit":10}')
INTERP_JSON = ('{"inferences":[],"recommendations":[],"assumptions":[],'
               '"limitations":[],"confidence":"low","confidence_rationale":"x"}')


def _student_scope(**over):
    from adam.pipeline import DataScope
    block = {"enabled": True, "allowed_sources": ["x"], "student_level_allowed": True,
             "allowed_student_fields": ["students.name"],
             "denied_fields": ["students.grade_level"], "budgets": {"max_rows_returned": 2}}
    block.update(over)
    return DataScope.from_block(block)


def _analyze(objective, data_scope, interp_fn=None):
    return analyze_objective(
        objective, connection=create_synthetic_db(), source_model=SYNTHETIC_SCHOOL_V1,
        planning_model_fn=fake(ROSTER_BODY),
        interpretation_model_fn=interp_fn or fake(INTERP_JSON),
        connection_handle="c", data_scope=data_scope)


def _rows(res):
    r = [o for o in res.observations if o["label"] == "rows"]
    return r[0]["value"] if r else None


def test_governed_rows_for_roster_under_student_level():
    res = _analyze("list the students", _student_scope())
    check("status ok", res.status == "ok", str(res.status))
    v = _rows(res)
    check("rows observation emitted", v is not None)
    check("permitted identifier (name) kept", "name" in v["columns"], str(v["columns"]))
    check("non-identifying id kept", "id" in v["columns"], str(v["columns"]))
    check("denied grade_level stripped", "grade_level" not in v["columns"], str(v["columns"]))
    check("capped at max_rows_returned (2)", len(v["rows"]) == 2, str(len(v["rows"])))
    # aggregate observations still present (additive).
    check("aggregate observations retained", any(o["label"] == "rows_returned" for o in res.observations))


def test_no_rows_when_student_level_denied():
    res = _analyze("list the students", _student_scope(student_level_allowed=False))
    check("no rows when student_level not allowed", _rows(res) is None)


def test_no_rows_for_aggregate_objective():
    res = _analyze("how many students are enrolled", _student_scope())
    check("aggregate-phrased objective emits no rows", _rows(res) is None)


def test_no_rows_without_data_scope():
    # The web path passes no DataScope -> never emits rows.
    res = _analyze("list the students", None)
    check("no rows without a DataScope (web path)", _rows(res) is None)


def test_identifier_stripped_without_explicit_permission():
    res = _analyze("list the students", _student_scope(allowed_student_fields=[]))
    v = _rows(res)
    check("rows still emitted", v is not None)
    check("identifier name stripped without allowance", "name" not in v["columns"], str(v["columns"]))
    check("non-identifying id still kept", "id" in v["columns"], str(v["columns"]))


def test_rows_never_enter_interpretation_prompt():
    captured = {}
    def recording_interp(system_prompt, observations_json):
        captured["payload"] = observations_json
        return INTERP_JSON
    res = _analyze("list the students", _student_scope(), interp_fn=recording_interp)
    check("rows still in the result", _rows(res) is not None)
    payload = captured.get("payload", "")
    check("interpretation payload carries observations", "rows_returned" in payload)
    check("interpretation payload does NOT contain the rows observation",
          '"rows"' not in payload and "Ann" not in payload, payload[:200])


def test_core_stays_model_free():
    # Importing the pipeline (incl. skill) must not pull in adam.core; the
    # governed core is deterministic and model-free.
    import importlib
    # Force a fresh import graph check.
    mods_before = {m for m in sys.modules if m.startswith("adam.core")}
    importlib.import_module("adam.pipeline")
    mods_after = {m for m in sys.modules if m.startswith("adam.core")}
    check("import adam.pipeline pulls in no adam.core",
          not mods_after, f"leaked: {sorted(mods_after - mods_before)}")
    # And the production wrapper exists but is lazy (defined without importing core).
    from adam.pipeline import make_call_model_fn
    check("make_call_model_fn is available (lazy seam)", callable(make_call_model_fn))


def main():
    print("Slice 4 Phase 2: skill end-to-end (governed by existing pipeline)")
    print("=" * 60)
    for t in [
        test_well_formed_executes,
        test_hallucinated_entity_caught_by_validation,
        test_missing_limit_caught_by_validation,
        test_select_star_caught_by_validation,
        test_out_of_scope_caught_by_sentinel,
        test_parse_error_never_reaches_pipeline,
        test_max_rows_clamps_effective_limit,
        test_governed_rows_for_roster_under_student_level,
        test_no_rows_when_student_level_denied,
        test_no_rows_for_aggregate_objective,
        test_no_rows_without_data_scope,
        test_identifier_stripped_without_explicit_permission,
        test_rows_never_enter_interpretation_prompt,
        test_core_stays_model_free,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
