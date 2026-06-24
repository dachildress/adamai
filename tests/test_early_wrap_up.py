"""
Tests for early wrap-up convergence detection (detect_synth_convergence).

Run: python tests/test_early_wrap_up.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adam.core.loop import detect_synth_convergence as converge

PASSED = 0
FAILED = 0


def check(name: str, cond: bool) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}")


def main() -> int:
    print("tests/test_early_wrap_up.py")
    print("=" * 60)

    high_conf = (
        "Decision Point: Operator will produce the literacy plan in docx. "
        "Confidence: High"
    )
    check("high-confidence decision point converges", converge(high_conf))

    synth_rec = (
        "Synthesized recommendation: adopt the phased rollout plan. "
        "Confidence: High"
    )
    check("synthesized recommendation high converges", converge(synth_rec))

    not_ready = (
        "Not ready for decision: need the privacy policy first. "
        "Confidence: High"
    )
    check("not ready vetoes despite high confidence", not converge(not_ready))

    medium = (
        "Decision Point: proceed with the workshop plan. Confidence: Medium"
    )
    check("medium confidence does not converge", not converge(medium))

    no_conf = "Decision Point: proceed with the workshop plan."
    check("missing confidence does not converge", not converge(no_conf))

    advisory = "The Logician raised concerns about staffing capacity."
    check("non-decisive text does not converge", not converge(advisory))

    print("=" * 60)
    print(f"Results: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
