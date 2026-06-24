"""
Tests for verifier claims drill-down and admin override (handoff §4.1).

Run: python tests/test_verification_drilldown.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gui" / "backend"))

import verification

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


def _write_verification(session_dir: Path, records):
    path = session_dir / "verification.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_claim_id_stable():
    rec = {
        "claim": "Students read 20 minutes daily",
        "source_turn": 2,
        "source_agent": "Logician",
        "status": "UNSUPPORTED",
    }
    a = verification.claim_id_for_record(rec)
    b = verification.claim_id_for_record(rec)
    check("claim_id stable", a == b and len(a) == 16)


def test_load_claims_with_sources():
    with tempfile.TemporaryDirectory() as tmp:
        sdir = Path(tmp)
        _write_verification(sdir, [{
            "claim": "District X has 1200 students",
            "category": "statistic",
            "status": "VERIFIED",
            "confidence": "HIGH",
            "source_count": 1,
            "highest_source_tier": "TIER_2",
            "highest_source_score": 80,
            "sources": [{
                "url": "https://example.edu/stats",
                "title": "Enrollment",
                "tier": "TIER_2",
                "tier_score": 80,
                "supports_claim": "full",
                "notes": "Exact match",
            }],
            "source_turn": 3,
            "source_agent": "Seeker",
        }])
        claims = verification.load_claims(sdir)
        check("loads one claim", len(claims) == 1)
        c = claims[0]
        check("claim text preserved", "1200 students" in c["claim"])
        check("sources attached", len(c["sources"]) == 1)
        check("effective equals original", c["effective_status"] == "VERIFIED")


def test_override_changes_effective_status():
    with tempfile.TemporaryDirectory() as tmp:
        sdir = Path(tmp)
        feedback_dir = Path(tmp) / "data"
        rec = {
            "claim": "Reading scores rose 15%",
            "status": "UNSUPPORTED",
            "source_turn": 4,
            "source_agent": "Visionary",
        }
        _write_verification(sdir, [rec])
        cid = verification.claim_id_for_record(rec)
        verification.save_override(
            session_dir=sdir,
            session_id="sess-test",
            feedback_dir=feedback_dir,
            claim_id=cid,
            admin_username="admin1",
            status="VERIFIED",
            reason="Director confirmed with district report",
            feedback="Should weight .edu sources higher",
        )
        claims = verification.load_claims(sdir)
        check("override applied", claims[0]["effective_status"] == "VERIFIED")
        check("original preserved", claims[0]["original_status"] == "UNSUPPORTED")
        check("override metadata", claims[0]["override"]["by"] == "admin1")
        fb = list((feedback_dir / "truthseeker_feedback.jsonl").read_text().splitlines())
        check("feedback logged globally", len(fb) == 1)


def test_invalid_override_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        sdir = Path(tmp)
        rec = {"claim": "x", "status": "VERIFIED", "source_turn": 1, "source_agent": "A"}
        _write_verification(sdir, [rec])
        cid = verification.claim_id_for_record(rec)
        try:
            verification.save_override(
                session_dir=sdir,
                session_id="s",
                feedback_dir=sdir,
                claim_id=cid,
                admin_username="admin",
                status="BOGUS",
                reason="test",
            )
            check("invalid status rejected", False)
        except ValueError:
            check("invalid status rejected", True)


def test_summarize():
    claims = [
        {"effective_status": "VERIFIED"},
        {"effective_status": "UNSUPPORTED", "override": {"status": "VERIFIED"}},
    ]
    s = verification.summarize_claims(claims)
    check("summary total", s["total"] == 2)
    check("summary overridden count", s["overridden"] == 1)


def main():
    print("test_verification_drilldown.py")
    print("=" * 60)
    test_claim_id_stable()
    test_load_claims_with_sources()
    test_override_changes_effective_status()
    test_invalid_override_rejected()
    test_summarize()
    print("=" * 60)
    print(f"Results: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
