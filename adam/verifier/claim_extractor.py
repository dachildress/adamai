"""
Claim extraction from agent turn output.

Two passes drive every Truthseeker run:

1. **Document-grounded claim detection** (extract_document_grounded_claims):
   Sentences that cite [CTX-YYYYMMDD-NNN] markers or (per "filename.pdf")
   markers are matched against the loaded context files. These claims
   are document-verified, not web-verified; the verifier short-circuits
   them with status DOCUMENT_GROUNDED_NOT_WEB_VERIFIED.

2. **Regex-flagged candidate spans + LLM structured extraction**
   (extract_claim_candidates -> extract_structured_claims):
   The regex pass emits candidate spans from patterns commonly
   associated with verifiable factual claims (statistics, named
   studies, attributions). The trust-registry filter is applied here:
   candidates whose text overlaps a Director-provided string are
   dropped before they reach the LLM extractor. The LLM call then
   takes the full agent text + the regex hints and produces the
   final list of structured claims to verify.

The two passes are complementary: pass 1 catches what the document
grounds, pass 2 catches what the agent introduces from elsewhere.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from adam.verifier._config import (
    TRUTHSEEKER_MODEL_ID,
    _rt_truthseeker,
)
from adam.verifier.trust_boundary import get_active_registry
from adam.verifier.web_search import (
    EXTRACTOR_SYSTEM,
    EXTRACTOR_USER_TEMPLATE,
    _model_json_call,
)


# ============================================================
# Document-grounded markers
# ============================================================
#
# Two formats supported (per locked design):
#   - [CTX-20260522-001]      machine-friendly, audit-precise
#   - (per "filename.pdf")    human-friendly, natural in prose

DOCUMENT_GROUNDED_CTX_RE      = re.compile(r"\[CTX-\d{8}-\d{3}\]")
DOCUMENT_GROUNDED_FILENAME_RE = re.compile(r'\(per\s+"([^"]+)"\)')


def extract_document_grounded_claims(
    text:                       str,
    context_files_by_id:        Dict[str, Any],          # ContextFile, kept opaque
    context_files_by_filename:  Dict[str, Any],
    turn:                       int,
    source_agent:               str,
) -> List[Dict[str, Any]]:
    """
    Scan text for document-grounded claim markers and produce verification
    records with status DOCUMENT_GROUNDED_NOT_WEB_VERIFIED.

    Sentences containing a marker are captured verbatim as the claim_text.
    If the marker references an unknown ID/filename, the record still gets
    produced but with note='unrecognized_context_reference' so the audit log
    surfaces the discrepancy (rather than silently dropping it).
    """
    records: List[Dict[str, Any]] = []
    seen_spans: Set[Tuple[int, int]] = set()
    now = datetime.now().isoformat(timespec='seconds')

    def _enclosing_sentence(s: str, idx: int) -> Tuple[int, int, str]:
        """Return (start, end, sentence_text) of the sentence containing
        position idx. Sentence boundaries are .!? or text boundaries."""
        start = idx
        while start > 0 and s[start - 1] not in ".!?\n":
            start -= 1
        while start < len(s) and s[start].isspace():
            start += 1
        end = idx
        while end < len(s) and s[end] not in ".!?\n":
            end += 1
        if end < len(s) and s[end] in ".!?":
            end += 1
        return start, end, s[start:end].strip()

    # CTX-id markers
    for m in DOCUMENT_GROUNDED_CTX_RE.finditer(text):
        ctx_id = m.group(0).strip("[]")
        span_start, span_end, sentence = _enclosing_sentence(text, m.start())
        if (span_start, span_end) in seen_spans or not sentence:
            continue
        seen_spans.add((span_start, span_end))
        cf = context_files_by_id.get(ctx_id)
        record: Dict[str, Any] = {
            "claim":            sentence,
            "status":           "DOCUMENT_GROUNDED_NOT_WEB_VERIFIED",
            "context_id":       ctx_id,
            "marker_format":    "ctx_id",
            "source_file":      cf.filename if cf else None,
            "source_turn":      turn,
            "source_agent":     source_agent,
            "ts":               now,
        }
        if cf is None:
            record["note"] = "unrecognized_context_reference"
        records.append(record)

    # (per "filename") markers
    for m in DOCUMENT_GROUNDED_FILENAME_RE.finditer(text):
        filename = m.group(1)
        span_start, span_end, sentence = _enclosing_sentence(text, m.start())
        if (span_start, span_end) in seen_spans or not sentence:
            continue
        seen_spans.add((span_start, span_end))
        cf = context_files_by_filename.get(filename)
        record = {
            "claim":            sentence,
            "status":           "DOCUMENT_GROUNDED_NOT_WEB_VERIFIED",
            "context_id":       cf.context_id if cf else None,
            "marker_format":    "filename",
            "source_file":      filename,
            "source_turn":      turn,
            "source_agent":     source_agent,
            "ts":               now,
        }
        if cf is None:
            record["note"] = "unrecognized_context_reference"
        records.append(record)

    return records


# ============================================================
# Regex candidate patterns
# ============================================================
#
# These are best-effort enrichment. The authoritative claim extractor
# is the LLM call in extract_structured_claims; these patterns just
# bias it toward likely candidates and seed downstream verification
# even when the LLM call fails.

CLAIM_CANDIDATE_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"[^.!?\n]*\b[A-Z][A-Za-z]+(?:\s+[A-Za-z]+){0,5}\s*\((?:19|20)\d{2}\)[^.!?\n]*[.!?]"), "year-tagged citation"),
    (re.compile(r"[^.!?\n]*\b[A-Z][\w]+(?:\s+[A-Z]?[\w]+){0,4}'s?\s+(?:19|20)\d{2}\s+(?:\w+\s+){0,3}(?:study|review|report|analysis|framework|model|pilot|assessment)[^.!?\n]*[.!?]", re.I), "named-source citation"),
    (re.compile(r"[^.!?\n]*\b\d{1,3}(?:\.\d+)?%\s+(?:of\s+)?(?:teachers|students|districts|schools|users|participants|adults|children|families|administrators)[^.!?\n]*[.!?]", re.I), "specific statistic"),
    (re.compile(r"[^.!?\n]*\b(RAND|OECD|Pew|Stanford|MIT|Harvard|UNESCO|Brookings|McKinsey|Gartner|Forrester|NCES|ISTE|UNICEF|WHO|CDC)\b[^.!?\n]*[.!?]"), "named-organization claim"),
    (re.compile(r"[^.!?\n]*\b(?:studies|research|data|evidence|surveys?)\b[^.!?\n]{0,120}\b(?:show|shows|suggest|suggests|indicate|indicates|find|finds|found|reveal|reveals|reported|demonstrate|demonstrates)\b[^.!?\n]*[.!?]", re.I), "research-claim phrasing"),
    (re.compile(r"[^.!?\n]*\baccording to\s+[A-Z][^.!?\n]*[.!?]", re.I), "attributed claim"),
    (re.compile(r"[^.!?\n]*\b(FERPA|COPPA|HIPAA|IDEA|ESSA|Title I|Title IX|Section 504)\s+(?:requires?|prohibits?|mandates?|states?|specifies?|allows?)\b[^.!?\n]*[.!?]"), "legal/regulatory claim"),
    (re.compile(r"[^.!?\n]*\b(?:districts?|schools?|systems?|counties)\s+like\s+[A-Z][^.!?\n]*[.!?]", re.I), "named-district claim"),
    (re.compile(r"[^.!?\n]*\b(?:programs?|initiatives?|frameworks?|models?|curricul[au]m?s?)\s+(?:like|such as|including)\s+[A-Z][^.!?\n]*[.!?]", re.I), "named-program claim"),
    (re.compile(r"[^.!?\n]*\b[A-Z][\w]+(?:\s+[\w]+){0,3}'s\s+(?:'|\")[A-Z][^'\"\.!?\n]+(?:'|\")[^.!?\n]*[.!?]"), "named initiative citation"),
    (re.compile(r"[^.!?\n]*\b[A-Z][\w]+(?:\s+[A-Z][\w]+){1,3}\s+\([A-Z][^)]{1,30}\)[^.!?\n]*[.!?]"), "named-place attribution"),
]


def extract_claim_candidates(text: str) -> List[Dict[str, str]]:
    """
    Run the regex-pattern catalog over the agent's text and return
    candidate claims for downstream LLM-based structured extraction.

    Trust-registry filtering: if the session's TrustRegistry has been
    built and contains any string that appears in the candidate's text,
    the candidate is skipped. This prevents operational parameters like
    email addresses, allowlist entries, CTX-IDs, filenames, and env
    config values from being treated as web-verifiable factual claims.

    The trust check is intentionally conservative -- it filters only
    when a candidate's text overlaps with a Director-provided string.
    Genuine factual claims in advisory turns (research findings, named
    studies, statistics) are unaffected.
    """
    candidates = []
    seen_spans = set()
    registry = get_active_registry()
    for pattern, category in CLAIM_CANDIDATE_PATTERNS:
        for match in pattern.finditer(text):
            span = match.group(0).strip()
            key = span.lower()[:80]
            if key in seen_spans:
                continue
            seen_spans.add(key)
            # Trust-registry check
            if registry is not None and registry.contains(span):
                continue
            candidates.append({"text": span, "category_hint": category})
    return candidates


def extract_structured_claims(
    models:       Dict[str, Any],
    providers:    Dict[str, Any],
    speaker_text: str,
    candidates:   List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """
    Always calls the LLM extractor on the full agent text. Regex
    candidates are passed as hints to bias toward but are not required
    -- Haiku is the authority on what counts as a verifiable claim.

    The earlier design gated this call on candidates being non-empty,
    which made Truthseeker silently inert whenever the regex missed
    (which turned out to be most of the time on K-12 content). Now:
    regex is best-effort enrichment, LLM is the actual extractor.
    """
    if candidates:
        cand_lines = "\n".join(f"  - [{c['category_hint']}] {c['text']}" for c in candidates[:20])
        candidates_section = (
            "REGEX-FLAGGED CANDIDATES (hints only - you may discard, restate, or find others):\n"
            f"{cand_lines}\n\n"
        )
    else:
        candidates_section = (
            "(No regex pre-flags this turn. Scan the full text yourself for any claims "
            "matching the INCLUDE criteria.)\n\n"
        )

    max_claims_cap = _rt_truthseeker('max_claims_per_turn')
    user = EXTRACTOR_USER_TEMPLATE.format(
        max_claims=max_claims_cap,
        candidates_section=candidates_section,
        full_text=speaker_text,
    )
    result = _model_json_call(models, providers, TRUTHSEEKER_MODEL_ID, EXTRACTOR_SYSTEM, user, max_tokens=1500)
    if not isinstance(result, list):
        return []
    cleaned = []
    for item in result[:max_claims_cap]:
        if isinstance(item, dict) and item.get("text"):
            cleaned.append({
                "text":     str(item["text"]).strip(),
                "category": str(item.get("category", "other")),
            })
    return cleaned
