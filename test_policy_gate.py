"""
Tests for the Slice 3 policy gate (loop.evaluate_policy_gate).

The gate runs at the wrap-up chokepoint, after the Synthesizer's final
pass and before Operator executes. It inspects the ratified synthesis
against the session's policy bounds and returns None (permit) or a
human-readable reason (block -> session ends 'policy_blocked', Operator
never runs, no artifact).

Design: COARSE and conservative. Catches genuine send/external-action
intent by the system; does NOT fire on incidental mentions. Permissive
bounds (the default 'standard' ruleset) never block -- behavior-neutral
for the default profile. Absent bounds => gate OFF.

Run: python3 test_policy_gate.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adam.core.loop import evaluate_policy_gate as g

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


PERMISSIVE = {"email_send_allowed": True, "external_actions_allowed": True}
EDU = {"email_send_allowed": False, "external_actions_allowed": False}
STRICT = {"email_send_allowed": False, "external_actions_allowed": False}


def test_gate_off_when_no_bounds():
    check("None bounds -> permitted (gate off)",
          g("Operator sends an email to the board.", None) is None)
    check("empty bounds -> permitted (gate off)",
          g("Operator sends an email to the board.", {}) is None)


def test_permissive_never_blocks():
    check("permissive permits email send",
          g("Operator sends an email to x@y.com with the plan.", PERMISSIVE) is None)
    check("permissive permits external action",
          g("Operator submits the report to the NRC portal.", PERMISSIVE) is None)
    check("permissive permits document",
          g("Operator produces a board presentation document.", PERMISSIVE) is None)


def test_education_blocks_email_send():
    fram = ("Decision Point: Operator sends an email to childrda@lcps.k12.va.us "
            "with a plain-language summary of the action plan. Confidence: High")
    r = g(fram, EDU)
    check("blocks the Framatome 'Operator sends email' synthesis", r is not None)
    check("block reason names email", r and "email" in r.lower())
    check("blocks 'send an email to'",
          g("We will send an email to the parents.", EDU) is not None)
    check("blocks 'email the plan to'",
          g("Operator will email the plan to the director.", EDU) is not None)


def test_education_permits_ordinary_work():
    check("permits a document",
          g("Operator produces a Word document summarizing the K-5 plan. "
            "Confidence: High", EDU) is None)
    check("permits a slide deck",
          g("Synthesized recommendation: create a board presentation deck.", EDU) is None)


def test_no_false_positive_on_incidental_email():
    check("incidental 'received a phishing email' not blocked",
          g("Staff received a phishing email last week; Operator produces an "
            "incident document.", EDU) is None)
    check("incidental 'the email was suspicious' not blocked",
          g("The deliberation analyzed why the email was suspicious and Operator "
            "wrote a report.", EDU) is None)


def test_strict_blocks_external_action():
    check("strict blocks NRC submission",
          g("Operator will submit the deviation report to the NRC portal.", STRICT)
          is not None)


def main():
    print("Slice 3 policy gate tests")
    print("=" * 56)
    for t in [test_gate_off_when_no_bounds,
              test_permissive_never_blocks,
              test_education_blocks_email_send,
              test_education_permits_ordinary_work,
              test_no_false_positive_on_incidental_email,
              test_strict_blocks_external_action]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 56)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
