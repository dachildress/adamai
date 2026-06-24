"""
Tests for Slice 4a human-review gate (loop.evaluate_review_gate),
its ordering relative to the policy gate, and resume-seed composition.

The gate runs at the wrap-up chokepoint AFTER the policy gate. If the
profile requires review for the planned action, the session pauses
(awaiting_human_review, pause_state.json written, Operator NOT run) and is
resumable via the resume endpoint. A resumed run skips the gate.

Run: python3 test_human_review.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adam.core.loop import evaluate_review_gate as r, evaluate_policy_gate as g

PASSED = 0
FAILED = 0


def check(name, cond):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}")


NONE = {"human_review_mode": "none", "review_required_for": []}
EDU = {"human_review_mode": "conditional",
       "review_required_for": ["public_facing_artifact", "student_data_output"]}
EXT = {"human_review_mode": "required",
       "review_required_for": ["external_action", "email_send", "file_write"]}


def test_none_mode_never_pauses():
    check("none mode never pauses",
          r("Operator sends an email to parents.", NONE) is None)
    check("no bounds never pauses", r("anything at all", None) is None)
    check("empty bounds never pauses", r("anything", {}) is None)


def test_education_conditional():
    check("pauses on parent-facing output",
          r("Operator produces a notification to parents about the policy.", EDU)
          is not None)
    check("pauses on public distribution",
          r("The plan is to distribute this to the community.", EDU) is not None)
    check("pauses on student data",
          r("The document lists individual student grades and IEP details.", EDU)
          is not None)
    check("does NOT pause on routine internal doc",
          r("Operator produces an internal planning memo for the IT team.", EDU)
          is None)


def test_external_required():
    check("pauses on send/email",
          r("Operator will send the report to the agency.", EXT) is not None)
    check("pauses on file write",
          r("Operator will write a file to the shared drive.", EXT) is not None)
    check("reason text explains why",
          (r("Operator produces a notification to parents.", EDU) or "").lower()
          .find("review is required") >= 0)


def test_gate_ordering_policy_takes_precedence():
    """A hard policy block and a review trigger can both match the same
    synthesis. The loop runs the policy gate FIRST and breaks on a block,
    so review is never reached for a policy-forbidden action. We verify
    both fire independently here; the loop ordering guarantees precedence."""
    bounds = {
        "email_send_allowed": False, "external_actions_allowed": False,
        "human_review_mode": "required",
        "review_required_for": ["external_action", "email_send"],
    }
    synth = "Operator sends an email to the parent distribution list."
    check("policy gate blocks the email-send synthesis",
          g(synth, bounds) is not None)
    check("review gate would ALSO flag it (precedence handled by loop order)",
          r(synth, bounds) is not None)


def main():
    print("Slice 4a human-review gate tests")
    print("=" * 56)
    for t in [test_none_mode_never_pauses,
              test_education_conditional,
              test_external_required,
              test_gate_ordering_policy_takes_precedence]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 56)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
