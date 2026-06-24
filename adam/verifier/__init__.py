"""
Truthseeker verification engine.

Module map:
  - claim_extractor.py  regex patterns, LLM structured-claim extraction,
                        document-grounded claim detection
  - trust_boundary.py   TrustRegistry, Director-provided input filtering
  - web_search.py       SearXNG queries, trafilatura page fetch, source
                        tier classification, _model_json_call
  - policy_rules.py     verify_claim, verification policy resolution,
                        formatters for transcript/audit
  - _config.py          (private) verifier-owned constants and runtime
                        config accessor. Submodules import from here
                        directly to avoid circular imports through the
                        package __init__.

Public API:

    from adam.verifier import (
        TrustRegistry, build_trust_registry,
        set_active_registry, get_active_registry,
        extract_claim_candidates, extract_structured_claims,
        extract_document_grounded_claims,
        verify_claim,
        format_verification_summary,
        format_verification_for_transcript,
        set_runtime_config,
        TRUTHSEEKER_MODEL_ID, TRUTHSEEKER_TEMPERATURE,
    )
"""
from __future__ import annotations

# Constants and runtime-config layer (re-exported from _config)
from adam.verifier._config import (
    TRUTHSEEKER_MODEL_ID,
    TRUTHSEEKER_TEMPERATURE,
    set_runtime_config,
)

# Trust boundary
from adam.verifier.trust_boundary import (
    TrustRegistry,
    build_trust_registry,
    set_active_registry,
    get_active_registry,
)

# Claim extraction
from adam.verifier.claim_extractor import (
    CLAIM_CANDIDATE_PATTERNS,
    extract_claim_candidates,
    extract_document_grounded_claims,
    extract_structured_claims,
)

# Web search & source judgment
from adam.verifier.web_search import (
    searxng_search,
    trafilatura_extract,
    classify_source_tier,
)

# Verification policy
from adam.verifier.policy_rules import (
    verify_claim,
    apply_verification_policy,
    format_verification_summary,
    format_verification_for_transcript,
)

__all__ = [
    "TRUTHSEEKER_MODEL_ID",
    "TRUTHSEEKER_TEMPERATURE",
    "set_runtime_config",
    "TrustRegistry",
    "build_trust_registry",
    "set_active_registry",
    "get_active_registry",
    "CLAIM_CANDIDATE_PATTERNS",
    "extract_claim_candidates",
    "extract_document_grounded_claims",
    "extract_structured_claims",
    "searxng_search",
    "trafilatura_extract",
    "classify_source_tier",
    "verify_claim",
    "apply_verification_policy",
    "format_verification_summary",
    "format_verification_for_transcript",
]
