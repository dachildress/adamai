"""
Probe suite for adam/core/information_pause.py (handoff fix1 Part 2).

Run: python tests/test_information_pause.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adam.core.information_pause import (
    INFORMATION_PAUSE_END_REASON,
    evaluate_information_pause,
)
from adam.core.empty_termination import evaluate_refusal_termination

PASSED = 0
FAILED = 0

MUST_PAUSE = [
    ("COPPA privacy policy", (
        "Not ready for decision: we cannot proceed without the district privacy policy. "
        "Awaiting that document before recommending parent notification language."
    )),
    ("cannot proceed without agreement", (
        "We cannot proceed without the signed data-sharing agreement. "
        "Please provide it before we recommend external distribution."
    )),
    ("missing director input", (
        "Not ready for decision: missing the board's approved budget figures."
    )),
]

MUST_NOT_PAUSE = [
    ("decision point converged", (
        "Decision Point: Operator will produce a parent notification in docx. "
        "Confidence: High"
    )),
    ("ordinary information mention", (
        "The Logician noted that additional information about reading scores "
        "would strengthen the recommendation, but we can proceed with estimates."
    )),
    ("non-synthesizer", "Not ready for decision: we need the privacy policy.", "Logician"),
]


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}" + (f"  -- {detail}" if detail else ""))


def main() -> int:
    print("tests/test_information_pause.py")
    print("=" * 60)

    check("end reason constant",
          INFORMATION_PAUSE_END_REASON == "awaiting_information")

    print("\nMUST pause:")
    for item in MUST_PAUSE:
        if len(item) == 3:
            name, text, agent = item
        else:
            name, text = item
            agent = "Synthesizer"
        check(name, evaluate_information_pause(text, agent) is not None)

    print("\nMUST NOT pause:")
    for item in MUST_NOT_PAUSE:
        if len(item) == 3:
            name, text, agent = item
        else:
            name, text = item
            agent = "Synthesizer"
        check(name, evaluate_information_pause(text, agent) is None)

    print("\ncross-gate: deferral is pause not refusal:")
    deferral = MUST_PAUSE[0][1]
    check("deferral not refusal termination",
          evaluate_refusal_termination(deferral) is None)

    print("=" * 60)
    print(f"Results: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
