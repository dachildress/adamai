"""
Slice 5 Phase 1: SkillResult + observations + interpretation (unit).

Runtime owns observations (deterministic, model-free); the model only
interprets them and never sees raw rows. All with FAKE seams; no real call.

Run:  python tests/pipeline/test_skill_result.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ_ROOT))

from adam.pipeline import (  # noqa: E402
    ExecutionPlan, derive_observations, build_interpretation_system_prompt,
)
from adam.pipeline.skill import _interpret  # noqa: E402
from adam.pipeline.sqlite_adapter import QueryResult  # noqa: E402

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


def a_plan(entities=("schools",)):
    return ExecutionPlan.from_dict({
        "plan_version": "1.0", "intent_type": "query",
        "connection_handle": "conn_school_ro", "source_type": "sql",
        "source_model_version": "synthetic-school-v1",
        "purpose": "t", "estimated_row_scope": "small",
        "body": {"operation": "select", "entities": list(entities),
                 "projection": ["schools.name"], "limit": 100},
    })


def qr(columns, rows, lineage=None):
    return QueryResult(columns=columns, rows=rows, row_count=len(rows),
                       sql="<sql>", params=[], source_lineage=lineage or {})


# ---- derive_observations: deterministic, runtime-owned ----

def test_observations_match_data():
    result = qr(["name", "avg_rate"],
               [("Oak Elementary", 0.80), ("Maple Elementary", 0.925)])
    obs = derive_observations(result, a_plan(("attendance", "schools")))
    by = {o.label: o for o in obs}
    check("rows_returned counts rows", by["rows_returned"].value == 2)
    check("max:avg_rate equals data max", by["max:avg_rate"].value == 0.925, str(by.get("max:avg_rate")))
    check("min:avg_rate equals data min", by["min:avg_rate"].value == 0.80)
    check("mean:avg_rate computed", abs(by["mean:avg_rate"].value - 0.8625) < 1e-6)
    check("top_by:avg_rate is the right record",
          by["top_by:avg_rate"].value == "Maple Elementary", str(by.get("top_by:avg_rate")))
    check("entities_queried carried", by["entities_queried"].value == ["attendance", "schools"])


def test_observations_deterministic():
    result = qr(["name", "avg_rate"], [("A", 1.0), ("B", 2.0)])
    o1 = [o.to_dict() for o in derive_observations(result, a_plan())]
    o2 = [o.to_dict() for o in derive_observations(result, a_plan())]
    check("observations are deterministic", o1 == o2)


def test_observations_empty_no_fabrication():
    result = qr(["name", "avg_rate"], [])
    obs = derive_observations(result, a_plan())
    labels = {o.label for o in obs}
    check("empty -> rows_returned 0", any(o.label == "rows_returned" and o.value == 0 for o in obs))
    check("empty -> NO max/top observation",
          not any(l.startswith("max:") or l.startswith("top_by:") for l in labels), str(labels))


# ---- interpretation: model sees observations, NOT rows ----

class FakeInterp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, system_prompt, user_payload):
        self.calls.append((system_prompt, user_payload))
        return self.response


GOOD_INTERP = json.dumps({
    "inferences": ["Maple outperforms Oak on attendance."],
    "recommendations": ["Investigate Oak's attendance supports."],
    "assumptions": ["The term is representative."],
    "limitations": ["Single term only."],
    "confidence": "medium",
    "confidence_rationale": "Two schools, one metric.",
})


def test_interpret_parses_and_labels():
    observations = [{"label": "max:avg_rate", "value": 0.925, "detail": None}]
    fake = FakeInterp(GOOD_INTERP)
    interp, err = _interpret("obj", {"row_count": 2}, observations, {"plan_id": "p"}, fake)
    check("no interpretation error", err is None, str(err))
    check("inferences parsed", interp["inferences"] == ["Maple outperforms Oak on attendance."])
    check("recommendations parsed", len(interp["recommendations"]) == 1)
    check("confidence carried", interp["confidence"] == "medium")
    check("rationale carried", "Two schools" in (interp["confidence_rationale"] or ""))


def test_interpret_never_receives_rows():
    observations = [{"label": "rows_returned", "value": 2, "detail": None}]
    fake = FakeInterp(GOOD_INTERP)
    _interpret("obj", {"row_count": 2, "columns": ["name", "avg_rate"]},
               observations, {"plan_id": "p"}, fake)
    _system, payload = fake.calls[0]
    parsed = json.loads(payload)
    check("payload has observations", "observations" in parsed)
    check("payload has data_analyzed + lineage", "data_analyzed" in parsed and "source_lineage" in parsed)
    check("payload has NO rows key", "rows" not in parsed, str(list(parsed.keys())))


def test_interpret_ignores_model_observations():
    # Model tries to assert its own observations/facts -> ignored.
    sneaky = json.dumps({
        "observations": ["FAKE: everything is fine"],
        "facts": ["FAKE fact"],
        "inferences": ["real inference"],
        "confidence": "low", "confidence_rationale": "r",
    })
    interp, err = _interpret("o", {}, [], {}, FakeInterp(sneaky))
    check("no error", err is None)
    check("model-asserted observations ignored (not in interp)",
          "observations" not in interp and "facts" not in interp)
    check("real inference still parsed", interp["inferences"] == ["real inference"])


def test_interpret_malformed_typed_failure():
    for bad in ["not json at all", "{bad", json.dumps({"a": 1}) + json.dumps({"b": 2})]:
        interp, err = _interpret("o", {}, [], {}, FakeInterp(bad))
        check(f"malformed -> typed failure ({bad[:12]!r})", interp is None and bool(err))


def test_interpretation_prompt_forbids_rows_and_facts():
    sp = build_interpretation_system_prompt()
    check("prompt says model does not see raw rows", "raw rows" in sp.lower() or "do not see" in sp.lower())
    check("prompt forbids observations/facts field", "observations" in sp and "facts" in sp)
    check("prompt requires JSON-only", "ONLY a single JSON object" in sp)


def main():
    print("Slice 5 Phase 1: SkillResult + observations + interpretation")
    print("=" * 60)
    for t in [
        test_observations_match_data,
        test_observations_deterministic,
        test_observations_empty_no_fabrication,
        test_interpret_parses_and_labels,
        test_interpret_never_receives_rows,
        test_interpret_ignores_model_observations,
        test_interpret_malformed_typed_failure,
        test_interpretation_prompt_forbids_rows_and_facts,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
