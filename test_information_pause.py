"""
Tests for Slice 4b mid-loop information pause.

Run: python test_information_pause.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adam.core.information_pause import (
    INFORMATION_PAUSE_END_REASON,
    evaluate_information_pause,
)
from adam.core.empty_termination import evaluate_refusal_termination

PASSED = 0
FAILED = 0

NOT_READY = (
    "Not ready for decision: we cannot proceed without the district privacy policy. "
    "Awaiting that document before recommending parent notification language."
)
DECISION = (
    "Decision Point: Operator will produce a parent notification in docx. "
    "Confidence: High"
)
DEFERRAL = (
    "We cannot proceed without the signed data-sharing agreement. "
    "Please provide it before we recommend external distribution."
)
REFUSAL = (
    "Decision Point: We cannot execute the requested shell command. "
    "Operator will produce a security incident record document."
)


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}" + (f"  -- {detail}" if detail else ""))


def test_not_ready_pauses():
    reason = evaluate_information_pause(NOT_READY, "Synthesizer")
    check("not ready for decision pauses", reason is not None)
    check("reason mentions privacy", "privacy" in (reason or "").lower())


def test_decision_point_does_not_pause():
    check("decision point does not pause",
          evaluate_information_pause(DECISION, "Synthesizer") is None)


def test_deferral_pauses():
    check("cannot proceed without pauses",
          evaluate_information_pause(DEFERRAL, "Synthesizer") is not None)


def test_non_synthesizer_ignored():
    check("logician ignored",
          evaluate_information_pause(NOT_READY, "Logician") is None)


def test_not_confused_with_refusal():
    check("refusal termination not triggered for not-ready",
          evaluate_refusal_termination(NOT_READY) is None)


def test_refusal_still_terminates():
    check("substitute refusal still terminates",
          evaluate_refusal_termination(REFUSAL) is not None)


def test_end_reason_constant():
    check("end reason stable",
          INFORMATION_PAUSE_END_REASON == "awaiting_information")


def main():
    print("test_information_pause.py")
    print("=" * 60)
    test_end_reason_constant()
    test_not_ready_pauses()
    test_decision_point_does_not_pause()
    test_deferral_pauses()
    test_non_synthesizer_ignored()
    test_not_confused_with_refusal()
    test_refusal_still_terminates()
    print("=" * 60)
    print(f"Results: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
