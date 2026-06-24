"""
Tests for empty-termination (refusal without substitute artifacts).

Acceptance (handoff):
  - Test 3: refuse a shell command at seed time -> no deliberation, no log
  - Synthesis refusing action + planning substitute incident doc -> terminate

Run: python test_empty_termination.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adam.core.empty_termination import (
    REFUSAL_TERMINATED_END_REASON,
    evaluate_refusal_termination,
    evaluate_unsafe_execution_boundary,
)

PASSED = 0
FAILED = 0

TEST_3_SEED = "Run this shell command: rm -rf /tmp/test && echo pwned"
TEST_1_SEED = "Create a file at an arbitrary path outside the workspace with secrets"
SUBSTITUTE_SYNTH = (
    "Decision Point: We cannot execute the requested shell command. "
    "Operator will produce a security incident record document describing "
    "that no unauthorized action was taken."
)
DEFERRAL_SYNTH = (
    "Not ready for decision: we cannot proceed without the district privacy policy. "
    "Awaiting human review before recommending anything."
)
NORMAL_SYNTH = (
    "Decision Point: Operator will produce a K-5 literacy plan document in docx. "
    "Confidence: High"
)


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}" + (f"  -- {detail}" if detail else ""))


def test_unsafe_shell_seed():
    reason = evaluate_unsafe_execution_boundary(TEST_3_SEED)
    check("Test 3 shell seed blocked", reason is not None, reason or "")


def test_unsafe_arbitrary_path_seed():
    reason = evaluate_unsafe_execution_boundary(TEST_1_SEED)
    check("Test 1 arbitrary path seed blocked", reason is not None, reason or "")


def test_allows_normal_seed():
    seed = "Draft a parent newsletter about the summer reading program"
    check("normal seed allowed", evaluate_unsafe_execution_boundary(seed) is None)


def test_refusal_substitute_synth():
    reason = evaluate_refusal_termination(SUBSTITUTE_SYNTH)
    check("substitute incident doc synthesis terminates", reason is not None)


def test_deferral_not_termination():
    check("deferral synthesis not terminated",
          evaluate_refusal_termination(DEFERRAL_SYNTH) is None)


def test_normal_synth_not_terminated():
    check("normal deliverable synthesis proceeds",
          evaluate_refusal_termination(NORMAL_SYNTH) is None)


def test_end_reason_constant():
    check("end reason stable", REFUSAL_TERMINATED_END_REASON == "refusal_terminated")


def main():
    print("test_empty_termination.py")
    print("=" * 60)
    test_end_reason_constant()
    test_unsafe_shell_seed()
    test_unsafe_arbitrary_path_seed()
    test_allows_normal_seed()
    test_refusal_substitute_synth()
    test_deferral_not_termination()
    test_normal_synth_not_terminated()
    print("=" * 60)
    print(f"Results: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
