"""
Skill advertisement: each agent is told ONLY about the executable skills its
role is allowed to call, and Data Intelligence is reachable by Seeker/
Truthseeker (enabled in runtime.json, permitted under the education policy).

Advertisement is the INITIATION half only — invocation stays governed by the
runtime (allowed_callers) and the handler (profile capability, scope, budgets).

Run:  python tests/test_skill_advertisement.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUI_ROOT = ROOT / "gui"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(GUI_ROOT))

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


def _catalog():
    from adam.core import config_loader
    from adam.skills_runtime import discover_skills
    config_loader.load_and_validate_runtime_config()
    skills_cfg = config_loader.get_runtime_config().get("skills", {})
    return discover_skills(skills_cfg)


def _heading(block, skill):
    return f"### {skill} " in block


def test_data_intelligence_is_executable_in_catalog():
    cat = _catalog()
    check("data_intelligence enabled+executable in the live catalog",
          "data_intelligence" in cat.executable,
          str(sorted(cat.executable.keys())))


def test_caller_filtered_advertisement():
    from adam.skills_runtime import build_skill_manifest_block
    cat = _catalog()

    seeker = build_skill_manifest_block(cat, "Seeker")
    check("Seeker is advertised data_intelligence", _heading(seeker, "data_intelligence"), seeker[:200])
    check("Seeker sees the query action", "query" in seeker)
    check("Seeker is NOT advertised coder (Operator-only)", not _heading(seeker, "coder"))
    check("Seeker is NOT advertised email (Operator-only)", not _heading(seeker, "email"))
    check("Seeker block carries skill_call syntax", "```skill_call" in seeker)

    truthseeker = build_skill_manifest_block(cat, "Truthseeker")
    check("Truthseeker is advertised data_intelligence", _heading(truthseeker, "data_intelligence"))
    check("Truthseeker sees the verify action", "verify" in truthseeker)

    operator = build_skill_manifest_block(cat, "Operator")
    check("Operator is advertised document", _heading(operator, "document"))
    check("Operator is NOT advertised data_intelligence (not an allowed caller)",
          not _heading(operator, "data_intelligence"), operator[:200])

    logician = build_skill_manifest_block(cat, "Logician")
    check("agent with no callable skills gets NO block (not a placeholder)", logician == "", logician[:80])


def test_policy_permits_under_education_denies_under_general():
    from backend import governance
    cat = _catalog()
    universe = sorted(cat.executable.keys())
    governance.init_governance(GUI_ROOT)
    check("education profile does NOT policy-deny data_intelligence",
          "data_intelligence" not in governance.policy_denied_skills("education", universe))
    # general has no data_intelligence block, and its policy denies the skill, so
    # it is never advertised there -> no failed-call noise.
    check("general profile policy-denies data_intelligence (not advertised there)",
          "data_intelligence" in governance.policy_denied_skills("general", universe))


def test_runtime_json_enables_data_intelligence():
    rt = json.loads((ROOT / "config" / "runtime.json").read_text(encoding="utf-8"))
    check("runtime.json enabled_skills includes data_intelligence",
          "data_intelligence" in rt["skills"]["enabled_skills"])


def main():
    print("Skill advertisement (caller-filtered)")
    print("=" * 60)
    for t in [
        test_data_intelligence_is_executable_in_catalog,
        test_caller_filtered_advertisement,
        test_policy_permits_under_education_denies_under_general,
        test_runtime_json_enables_data_intelligence,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
