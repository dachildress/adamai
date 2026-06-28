"""
Slice 5 Phase 2: analyze_objective end-to-end — governed, attributed answer
on synthetic data. Runtime owns observations; model interprets them; honest
on denial/empty/failure. All FAKE seams; no real call.

Run:  python tests/pipeline/test_skill_result_e2e.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ_ROOT))

from adam.pipeline import (  # noqa: E402
    SYNTHETIC_SCHOOL_V1, create_synthetic_db, analyze_objective, SkillResult,
    GovernanceConfig, ScopeConfig,
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


# Aggregation body: avg attendance rate per elementary school.
AGG_BODY = json.dumps({
    "operation": "select", "entities": ["attendance", "schools"],
    "projection": ["schools.name"],
    "filters": [{"field": "schools.level", "op": "eq", "value": "elementary"}],
    "joins": [{"left": "attendance.school_id", "right": "schools.id", "type": "inner"}],
    "group_by": ["schools.name"],
    "aggregations": [{"fn": "avg", "field": "attendance.rate", "as": "avg_rate"}],
    "order_by": [{"field": "avg_rate", "direction": "asc"}],
    "limit": 100,
})

GOOD_INTERP = json.dumps({
    "inferences": ["Maple's attendance exceeds Oak's this term."],
    "recommendations": ["Review Oak's attendance interventions."],
    "assumptions": ["The single term is representative."],
    "limitations": ["No causal claims from one term."],
    "confidence": "medium", "confidence_rationale": "Two schools, one metric.",
})


class Fake:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, system_prompt, user_text):
        self.calls.append((system_prompt, user_text))
        return self.response


def analyze(plan_resp, interp_resp, **kw):
    conn = create_synthetic_db()
    planning = Fake(plan_resp)
    interp = Fake(interp_resp)
    kw.setdefault("connection_handle", "conn_school_ro")
    sr = analyze_objective("Which elementary schools have the lowest attendance?",
                           conn, SYNTHETIC_SCHOOL_V1, planning, interp, **kw)
    return sr, planning, interp


def test_attributed_answer():
    sr, planning, interp = analyze(AGG_BODY, GOOD_INTERP)
    check("status ok", sr.status == "ok", f"{sr.status}: {sr.limitations}")
    check("is a SkillResult", isinstance(sr, SkillResult))
    # Observations are runtime dicts; inferences/recommendations are model strings.
    labels = {o["label"] for o in sr.observations}
    check("runtime observations present", "rows_returned" in labels and "max:avg_rate" in labels, str(labels))
    check("observation value matches data (max avg_rate = 0.925)",
          any(o["label"] == "max:avg_rate" and abs(o["value"] - 0.925) < 1e-9 for o in sr.observations))
    check("inferences are model strings (separate field)", sr.inferences == ["Maple's attendance exceeds Oak's this term."])
    check("recommendations separate field", sr.recommendations == ["Review Oak's attendance interventions."])
    check("assumptions carried", sr.assumptions == ["The single term is representative."])
    check("confidence is model self-report", sr.confidence == "medium")
    check("confidence_rationale carried", "Two schools" in (sr.confidence_rationale or ""))
    check("limitations include runtime synthetic note + model note",
          any("Synthetic data" in l for l in sr.limitations) and any("causal" in l for l in sr.limitations))
    check("source_lineage traceable (plan_id + version)",
          sr.source_lineage.get("plan_id") and sr.source_lineage.get("source_model_version") == "synthetic-school-v1")
    # fact/judgment line: no model inference text leaked into observations.
    check("inference text not in observations",
          not any("Maple's attendance exceeds" in str(o.get("value")) for o in sr.observations))
    check("interpretation model was called once", len(interp.calls) == 1)


def test_model_cannot_add_observations():
    sneaky = json.dumps({
        "observations": ["FAKE: attendance is perfect everywhere"],
        "facts": ["FAKE fact"],
        "inferences": ["legit inference"],
        "confidence": "low", "confidence_rationale": "r",
    })
    sr, _, _ = analyze(AGG_BODY, sneaky)
    check("status ok", sr.status == "ok")
    check("observations are runtime-only (model's ignored)",
          all("FAKE" not in str(o.get("value")) and "FAKE" not in o.get("label", "") for o in sr.observations))
    check("model inference still present", sr.inferences == ["legit inference"])


def test_interpretation_never_receives_rows():
    sr, _, interp = analyze(AGG_BODY, GOOD_INTERP)
    _system, payload = interp.calls[0]
    parsed = json.loads(payload)
    check("interp payload has observations/metadata/lineage",
          {"observations", "data_analyzed", "source_lineage"} <= set(parsed.keys()))
    check("interp payload has NO rows key", "rows" not in parsed)


def test_policy_denied_honest():
    interp = Fake(GOOD_INTERP)
    conn = create_synthetic_db()
    planning = Fake(json.dumps({"operation": "select", "entities": ["students"],
                                "projection": ["students.name"], "limit": 10}))
    sr = analyze_objective("list students", conn, SYNTHETIC_SCHOOL_V1, planning, interp,
                           connection_handle="conn_school_ro",
                           scope=ScopeConfig(allowed_entities={"schools"},
                                             denied_entities=set(), denied_fields=set()))
    check("status policy_denied", sr.status == "policy_denied", sr.status)
    check("no fabricated observations", sr.observations == [])
    check("no fabricated inferences", sr.inferences == [])
    check("limitations explain non-execution", any("not executed" in l for l in sr.limitations))
    check("interpretation model NOT called on denial", len(interp.calls) == 0)


def test_validation_error_honest():
    sr, _, interp = analyze(json.dumps({"operation": "select", "entities": ["schools"],
                                        "projection": ["*"], "limit": 10}), GOOD_INTERP)
    check("status validation_error", sr.status == "validation_error", sr.status)
    check("no fabricated answer", sr.observations == [] and sr.inferences == [])
    check("interp not called", len(interp.calls) == 0)


def test_plan_parse_error_honest():
    sr, _, interp = analyze("I cannot help with that.", GOOD_INTERP)
    check("status plan_parse_error", sr.status == "plan_parse_error", sr.status)
    check("no fabricated observations/inferences", sr.observations == [] and sr.inferences == [])
    check("interp not called", len(interp.calls) == 0)


def test_empty_result_honest():
    empty_body = json.dumps({"operation": "select", "entities": ["schools"],
                             "projection": ["schools.name"],
                             "filters": [{"field": "schools.level", "op": "eq", "value": "college"}],
                             "limit": 10})
    sr, _, interp = analyze(empty_body, GOOD_INTERP)
    check("status empty", sr.status == "empty", sr.status)
    check("rows_returned 0 observation", any(o["label"] == "rows_returned" and o["value"] == 0 for o in sr.observations))
    check("no 'top_by' observation on empty",
          not any(o["label"].startswith("top_by:") for o in sr.observations))
    check("limitations note empty set", any("no rows" in l.lower() for l in sr.limitations))
    check("interpretation NOT called on empty", len(interp.calls) == 0)


def test_interpretation_error_honest():
    sr, _, _ = analyze(AGG_BODY, "this is not json")
    check("status interpretation_error", sr.status == "interpretation_error", sr.status)
    check("observations still present (runtime owns them)", len(sr.observations) > 0)
    check("no fabricated inferences", sr.inferences == [])
    check("limitations note parse failure", any("interpretation" in l.lower() for l in sr.limitations))


def test_two_distinct_seams_no_real_call():
    sr, planning, interp = analyze(AGG_BODY, GOOD_INTERP)
    check("planning seam called", len(planning.calls) == 1)
    check("interpretation seam called", len(interp.calls) == 1)
    check("seams are distinct objects", planning is not interp)
    check("no adam.core imported", not any(m.startswith("adam.core") for m in sys.modules))


def test_core_stays_model_free():
    import importlib
    importlib.import_module("adam.pipeline")
    leaked = [m for m in sys.modules if m.startswith("adam.core")]
    check("import adam.pipeline pulls in no adam.core", not leaked, str(leaked))


def main():
    print("Slice 5 Phase 2: analyze_objective end-to-end")
    print("=" * 60)
    for t in [
        test_attributed_answer,
        test_model_cannot_add_observations,
        test_interpretation_never_receives_rows,
        test_policy_denied_honest,
        test_validation_error_honest,
        test_plan_parse_error_honest,
        test_empty_result_honest,
        test_interpretation_error_honest,
        test_two_distinct_seams_no_real_call,
        test_core_stays_model_free,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
