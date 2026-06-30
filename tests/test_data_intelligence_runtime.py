"""
Data Intelligence skill: runtime allowed_callers enforcement (real SkillRuntime).

Lives at tests/ root (not tests/pipeline/) because importing the skill runtime
pulls in adam.core.exceptions; the pipeline suite asserts a model-free import
graph and runs in isolation, so this end-to-end runtime check belongs here.

Run:  python tests/test_data_intelligence_runtime.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ_ROOT))
sys.path.insert(0, str(PROJ_ROOT / "skills"))

PASSED = 0
FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if not cond:
        FAILED += 1
        print(f"  FAIL  {name}" + (f"  -- {detail}" if detail else ""))
    else:
        PASSED += 1
        print(f"  PASS  {name}")


BLOCK = {
    "enabled": True,
    "allowed_sources": ["adam-test-mysql-v1"],
    "denied_fields": ["students.first_name"],
    "budgets": {"max_data_queries_per_session": 5, "max_data_queries_per_agent": 3,
                "max_rows_returned": 50},
}

CALL = ('```skill_call\n{"skill_calls":[{"skill":"data_intelligence","action":"query",'
        '"args":{"source":"adam-test-mysql-v1","objective":"x"}}]}\n```')


class FakeRunQuery:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def __call__(self, *, source, objective, caller, scope):
        self.calls.append((source, caller))
        return self.outcome


OK_OUTCOME = {"result": {
    "objective": "x", "status": "ok", "observations": [{"label": "rows_returned", "value": 1}],
    "inferences": [], "recommendations": [], "assumptions": [], "limitations": [],
    "confidence": "low", "confidence_rationale": "x",
    "source_lineage": {"source_model_version": "adam-test-mysql-v1", "entities": ["schools"]},
}}


def test_allowed_callers_enforced():
    from adam.skills_runtime.manifest import discover_skills
    from adam.skills_runtime.runtime import SkillRuntime
    from adam.skills_runtime._config import set_runtime_config
    from data_intelligence import store

    set_runtime_config({"max_content_size_bytes": 100_000})
    cat = discover_skills({"skill_dir": str(PROJ_ROOT / "skills")})
    m = cat.get("data_intelligence")
    check("skill is executable", m is not None and m.category == "executable")
    check("allowed_callers are Seeker + Truthseeker", sorted(m.allowed_callers) == ["Seeker", "Truthseeker"])

    # discover_skills re-imports the package fresh; patch the FRESHLY loaded module.
    fresh = sys.modules["data_intelligence.handler"]
    saved = fresh.run_query
    fake = FakeRunQuery(OK_OUTCOME)
    fresh.run_query = fake
    try:
        with tempfile.TemporaryDirectory() as raw:
            sd = Path(raw)
            store.write_session_data_scope(sd, BLOCK)
            (sd / "artifacts").mkdir(parents=True, exist_ok=True)
            rt = SkillRuntime(catalog=cat, skills_log_path=sd / "skills.jsonl",
                              session_id="s", artifacts_root=sd / "artifacts")

            res, _ = rt.process_agent_output(agent="Logician", turn=1, agent_output=CALL)
            check("Logician rejected by runtime",
                  len(res) == 1 and res[0]["status"] == "failed"
                  and res[0]["error_class"] == "disallowed_caller", str(res))
            check("rejected call never reached the handler", len(fake.calls) == 0)

            res2, _ = rt.process_agent_output(agent="Seeker", turn=2, agent_output=CALL)
            check("Seeker accepted by runtime",
                  len(res2) == 1 and res2[0]["status"] == "success", str(res2))
            check("Seeker call reached the handler", len(fake.calls) == 1)
            check("runtime result carries data_result_id",
                  res2[0].get("data_result_id", "").startswith("dr_"))

            res3, _ = rt.process_agent_output(agent="Truthseeker", turn=3, agent_output=CALL)
            check("Truthseeker accepted by runtime",
                  len(res3) == 1 and res3[0]["status"] == "success", str(res3))
    finally:
        fresh.run_query = saved


def main():
    print("Data Intelligence runtime: allowed_callers enforcement")
    print("=" * 60)
    test_allowed_callers_enforced()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
