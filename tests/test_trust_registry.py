"""
Standalone unit test for the Truthseeker trust-registry filter.

Verifies that Director-provided inputs do not reach the verification
pipeline as claim candidates. Self-contained: imports the patched
module without running a full ADAM session.

The bug this prevents (observed in adam_20260523_184707):

  T11 Operator picked action: draft instead of action: send because
  Truthseeker had web-searched the email address and marked it
  UNSUPPORTED. The address was on the configured allowlist; the
  verifier never should have checked it.

After this patch:
  - TrustRegistry holds all Director-provided strings at session start
  - extract_claim_candidates skips candidates whose text contains any
    trusted string
  - verify_claim short-circuits to NOT_WEB_VERIFIABLE if a trusted
    string slipped past the first filter
"""
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# We need to import without triggering any of the runtime's startup
# behaviors. The patched file is designed to be imported safely --
# main() only runs under `if __name__ == '__main__'`.
import importlib.util
spec = importlib.util.spec_from_file_location(
    "adam_runtime",
    str(ROOT / "adam_agent_chat.py")
)
adam = importlib.util.module_from_spec(spec)
# We need to allow the module's top-level code to run, but it has
# I/O guards on actual session startup. Loading it is safe.
spec.loader.exec_module(adam)

# After refactor step 2, the verifier subsystem lives in adam.verifier.
# Register its runtime config so verify_claim() can read its tunables
# (search_top_n, parallel_workers, etc.). The values used here mirror
# what the production config supplies. Without this call, _rt_truthseeker
# raises KeyError on its first read.
adam._verifier_set_runtime_config({
    "search_top_n":                5,
    "max_claims_per_turn":         3,
    "parallel_workers":            5,
    "page_fetch_timeout_seconds":  6,
    "search_http_timeout_seconds": 10,
    "source_excerpt_chars":        4000,
    "skip_tier_5_sources":         True,
    "judgment_cache_enabled":      True,
})


# ============================================================
# Fixtures
# ============================================================

SEED_TEXT = """
You are advising the Amherst County School Board reviewing two
candidate responses to a timed superintendent-search written exercise.

Produce a candidate assessment.

You may email the deliverables to David Childress at
dachildress@amherst.k12.va.us if instructed.
"""

ALLOWLIST_RAW = "childrda@lcps.k12.va.us,dachildress@amherst.k12.va.us,*@louisa.k12.va.us"


class FakeContextFile:
    """Minimal stand-in for ContextFile so we don't need to construct
    real ones for the registry test."""
    def __init__(self, context_id, filename):
        self.context_id = context_id
        self.filename = filename
        self.classification = "text_document"


CONTEXT_FILES_BY_ID = {
    "CTX-20260523-001": FakeContextFile("CTX-20260523-001", "Hoden.docx"),
    "CTX-20260523-002": FakeContextFile("CTX-20260523-002", "Sheppard.docx"),
    "CTX-20260523-003": FakeContextFile("CTX-20260523-003", "prompt.docx"),
}
CONTEXT_FILES_BY_FILENAME = {cf.filename: cf for cf in CONTEXT_FILES_BY_ID.values()}


def _build_registry(allowlist=ALLOWLIST_RAW, env_extras=None):
    """Helper: build a registry with the standard test fixtures, with
    the allowlist temporarily set in env."""
    os.environ["ADAM_EMAIL_RECIPIENT_ALLOWLIST"] = allowlist
    if env_extras:
        for k, v in env_extras.items():
            os.environ[k] = v
    try:
        return adam.build_trust_registry(
            seed_text=SEED_TEXT,
            skill_args_parsed=None,
            context_files_by_id=CONTEXT_FILES_BY_ID,
            context_files_by_filename=CONTEXT_FILES_BY_FILENAME,
        )
    finally:
        del os.environ["ADAM_EMAIL_RECIPIENT_ALLOWLIST"]
        if env_extras:
            for k in env_extras:
                if k in os.environ:
                    del os.environ[k]


# ============================================================
# Tests
# ============================================================

def test_registry_populated_from_all_sources():
    reg = _build_registry(env_extras={
        "ADAM_EMAIL_FROM":     "adam@lcps.k12.va.us",
        "ADAM_SMTP_HOST":      "smtp.lcps.k12.va.us",
    })
    counts = reg.source_counts
    # Seed contributes the full text + extracted email
    assert counts["seed_tokens"] >= 2, counts
    # Allowlist contributes 3 entries (childrda, dachildress, *@louisa)
    # plus the bare domain louisa.k12.va.us from the wildcard expansion
    assert counts["allowlist_entries"] >= 3, counts
    # Three context files contribute 3 IDs + 3 filenames = 6 identifiers
    assert counts["context_identifiers"] == 6, counts
    # Two env vars
    assert counts["env_config_values"] == 2, counts
    print("PASS: registry populated from all five source types")


def test_email_address_filtered():
    reg = _build_registry()
    assert reg.contains("dachildress@amherst.k12.va.us")
    assert reg.contains("the address dachildress@amherst.k12.va.us is correct")
    # Case-insensitive
    assert reg.contains("DAChildress@Amherst.K12.VA.US")
    print("PASS: allowlisted email address is in registry, case-insensitive")


def test_ctx_id_filtered():
    reg = _build_registry()
    assert reg.contains("CTX-20260523-001")
    assert reg.contains("see [CTX-20260523-002] for details")
    print("PASS: CTX-IDs are in registry")


def test_filename_filtered():
    reg = _build_registry()
    assert reg.contains("Hoden.docx")
    assert reg.contains("Sheppard.docx")
    print("PASS: source filenames are in registry")


def test_env_config_filtered():
    reg = _build_registry(env_extras={
        "ADAM_SMTP_HOST": "smtp.lcps.k12.va.us",
    })
    assert reg.contains("smtp.lcps.k12.va.us")
    print("PASS: env config values are in registry")


def test_short_strings_excluded():
    """Strings shorter than _TRUST_REGISTRY_MIN_LENGTH must not be added,
    otherwise common tokens like 'CTX' would cause sweeping false positives."""
    # Build a registry where the only inputs are short
    reg = adam.build_trust_registry(
        seed_text="x",
        skill_args_parsed=None,
        context_files_by_id={"AB": FakeContextFile("AB", "z.docx")},
        context_files_by_filename={"z.docx": FakeContextFile("AB", "z.docx")},
    )
    # "x", "AB", and "z.docx" (6 chars exactly) are at-or-below threshold
    # Threshold is 6 chars, so "z.docx" should be in; "AB" should not
    assert not reg.contains("AB")
    assert not reg.contains("xx")
    # "z.docx" is exactly 6 chars, threshold check is < 6, so 6-char strings ARE included
    assert reg.contains("z.docx")
    print("PASS: strings shorter than minimum length are excluded from registry")


def test_extract_claim_candidates_skips_trusted_email():
    """The key bug-prevention test: a sentence containing the allowlisted
    email address should NOT produce a claim candidate when the trust
    registry is active."""
    adam.set_active_registry(_build_registry())

    # A sentence the regex would normally flag (it has "According to" + capital + sentence ending)
    text_with_trusted_email = (
        "According to the Director's instruction, the artifact must "
        "be delivered to dachildress@amherst.k12.va.us before the "
        "interview day."
    )
    candidates = adam.extract_claim_candidates(text_with_trusted_email)
    # The sentence contains the allowlisted address -> filtered
    for c in candidates:
        assert "dachildress@amherst.k12.va.us" not in c["text"].lower(), c

    # Reset for other tests
    adam.set_active_registry(None)
    print("PASS: extract_claim_candidates skips sentences containing allowlisted emails")


def test_extract_claim_candidates_keeps_real_claims():
    """Genuine factual claims must NOT be filtered. The trust registry
    only catches sentences that overlap with Director-provided strings."""
    adam.set_active_registry(_build_registry())

    # A genuine "research X" claim with no trust-registry overlap
    text_with_real_claim = (
        "Research from RAND consistently shows that 67% of districts "
        "underutilize their professional development budgets."
    )
    candidates = adam.extract_claim_candidates(text_with_real_claim)
    # At least one candidate should survive (the statistic, the RAND mention,
    # or the research-claim phrasing).
    assert len(candidates) >= 1, candidates

    # Reset
    adam.set_active_registry(None)
    print("PASS: extract_claim_candidates keeps genuine factual claims")


def test_verify_claim_short_circuits_on_trusted_text():
    """If a claim with trusted text somehow reaches verify_claim, it
    should short-circuit to NOT_WEB_VERIFIABLE without network activity."""
    adam.set_active_registry(_build_registry())

    claim = {
        "text": "the recipient at dachildress@amherst.k12.va.us has been authorized",
        "category": "other",
    }
    # Pass an empty searxng_url since we expect no network call to happen
    result = adam.verify_claim(
        models={}, providers={},
        claim=claim,
        searxng_url="http://localhost-should-not-be-called/",
    )
    assert result["status"] == "NOT_WEB_VERIFIABLE", result
    assert result["source_count"] == 0
    assert result["sources"] == []
    assert result["note"] == "claim_text_contains_director_provided_input"

    # Reset
    adam.set_active_registry(None)
    print("PASS: verify_claim short-circuits to NOT_WEB_VERIFIABLE for trusted text")


def test_registry_none_disables_filter():
    """When _TRUST_REGISTRY is None (verification disabled), filtering
    is a no-op and the extractor behaves as before."""
    adam.set_active_registry(None)

    # A sentence the regex would flag
    text = "According to RAND, 80% of students reported improved engagement."
    candidates = adam.extract_claim_candidates(text)
    assert len(candidates) >= 1, candidates
    print("PASS: trust registry disabled = no filtering, extractor works as before")


def test_format_summary_includes_not_web_verifiable():
    """The summary line should distinguish NOT_WEB_VERIFIABLE from UNSUPPORTED."""
    verifications = [
        {"status": "VERIFIED", "claim": "x", "confidence": "HIGH", "source_count": 0,
         "highest_source_tier": None, "sources": []},
        {"status": "NOT_WEB_VERIFIABLE", "claim": "y", "confidence": "N/A",
         "source_count": 0, "highest_source_tier": None, "sources": []},
        {"status": "UNSUPPORTED", "claim": "z", "confidence": "LOW", "source_count": 0,
         "highest_source_tier": None, "sources": []},
    ]
    summary = adam.format_verification_summary(verifications)
    assert "1 VERIFIED" in summary
    assert "1 NOT_WEB_VERIFIABLE" in summary
    assert "1 UNSUPPORTED" in summary
    # Order matters for readability
    assert summary.index("VERIFIED") < summary.index("NOT_WEB_VERIFIABLE") < summary.index("UNSUPPORTED")
    print("PASS: summary line includes NOT_WEB_VERIFIABLE in the right position")


def test_format_transcript_includes_guidance():
    """The transcript-injected verification block must tell downstream
    agents how to interpret NOT_WEB_VERIFIABLE."""
    verifications = [
        {"status": "NOT_WEB_VERIFIABLE", "claim": "dachildress@amherst.k12.va.us",
         "confidence": "N/A", "source_count": 0, "highest_source_tier": None, "sources": []},
    ]
    out = adam.format_verification_for_transcript(verifications)
    assert "NOT_WEB_VERIFIABLE" in out
    # Guidance text must be present
    assert "ignore the verdict" in out
    assert "authoritative" in out
    print("PASS: transcript guidance explains how to interpret NOT_WEB_VERIFIABLE")


if __name__ == "__main__":
    test_registry_populated_from_all_sources()
    test_email_address_filtered()
    test_ctx_id_filtered()
    test_filename_filtered()
    test_env_config_filtered()
    test_short_strings_excluded()
    test_extract_claim_candidates_skips_trusted_email()
    test_extract_claim_candidates_keeps_real_claims()
    test_verify_claim_short_circuits_on_trusted_text()
    test_registry_none_disables_filter()
    test_format_summary_includes_not_web_verifiable()
    test_format_transcript_includes_guidance()
    print()
    print("All trust-registry tests passed.")
