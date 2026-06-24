"""
Verification policy and orchestrator for Truthseeker.

Four responsibilities:

1. **Policy resolution** (apply_verification_policy): given a list of
   source records (each with supports_claim, tier_score,
   extraction_partial flag), apply the deterministic policy rules and
   return (status, confidence). The status is one of:
     VERIFIED | PARTIALLY_VERIFIED | UNSUPPORTED | CONTRADICTED |
     NEEDS_HUMAN_REVIEW
   (NOT_WEB_VERIFIABLE is set elsewhere, before policy runs, by the
   trust-registry short-circuit.)

2. **Judgment cache** (_judgment_cache, _judgment_cache_key): bounded
   per-session cache of (claim_hash, url) -> source record. Skips the
   judge_source LLM call when the same (claim, url) pair has already
   been judged earlier in the session.

3. **Source processing** (_process_one_source): fetch + tier-classify +
   judge a single source. Heuristic tier classification first (fast,
   no LLM); abandon Tier-5 sources if runtime config requests it;
   judgment cache lookup; trafilatura fetch with snippet fallback;
   LLM source judgment.

4. **verify_claim orchestrator**: trust-registry short-circuit ->
   SearXNG query -> parallel source processing with early termination ->
   policy resolution -> structured verification record.

Plus formatters for summary/transcript output.
"""
from __future__ import annotations

import hashlib
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from adam.verifier._config import _rt_truthseeker
from adam.verifier.trust_boundary import get_active_registry
from adam.verifier.web_search import (
    _normalize_domain,
    classify_domain,
    classify_domain_heuristic,
    judge_source,
    searxng_search,
    trafilatura_extract,
)


# ============================================================
# Deterministic policy rules
# ============================================================

def apply_verification_policy(
    sources_with_judgments: List[Dict[str, Any]]
) -> Tuple[str, str]:
    """
    Hard policy rules. See spec for full reasoning. Returns
    (status, confidence).
    """
    if not sources_with_judgments:
        return ("UNSUPPORTED", "LOW")

    full_supporters    = [s for s in sources_with_judgments if s["supports_claim"] == "full"]
    partial_supporters = [s for s in sources_with_judgments if s["supports_claim"] == "partial"]
    contradictors      = [s for s in sources_with_judgments if s["supports_claim"] == "contradicts"]

    tier12_supporters    = [s for s in full_supporters + partial_supporters if s["tier_score"] >= 4]
    tier12_full_support  = [s for s in full_supporters    if s["tier_score"] >= 4]
    tier12_contradictors = [s for s in contradictors      if s["tier_score"] >= 4]
    has_extracted_support = any(not s.get("extraction_partial", False)
                                for s in full_supporters + partial_supporters)

    if tier12_contradictors and not tier12_full_support:
        return ("CONTRADICTED", "HIGH")
    if tier12_full_support and tier12_contradictors:
        return ("NEEDS_HUMAN_REVIEW", "MEDIUM")
    if (
        len(full_supporters) >= 2
        and tier12_full_support
        and not tier12_contradictors
        and has_extracted_support
    ):
        confidence = "HIGH" if len(tier12_full_support) >= 2 else "MEDIUM"
        return ("VERIFIED", confidence)
    if tier12_supporters:
        return ("PARTIALLY_VERIFIED", "MEDIUM")
    if full_supporters or partial_supporters:
        return ("UNSUPPORTED", "LOW")
    return ("UNSUPPORTED", "LOW")


# ============================================================
# Judgment cache
# ============================================================
#
# Bounded per-session cache of (claim_hash, url) -> source record.
# Avoids re-judging the same source for the same claim across turns.
# Module-level today (matches the pre-refactor pattern); step 7 will
# move this to SessionContext for multi-instance safety.

_judgment_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
_JUDGMENT_CACHE_MAX_ENTRIES = 5000


def _judgment_cache_key(claim_query: str, url: str) -> Tuple[str, str]:
    """Stable cache key for a (claim, url) pair."""
    return (hashlib.sha256(claim_query.encode("utf-8")).hexdigest()[:32], url)


# ============================================================
# Source processing
# ============================================================

def _process_one_source(
    models:        Dict[str, Any],
    providers:     Dict[str, Any],
    claim_query:   str,
    search_result: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    """
    Fetch + judge + tier-classify a single source. Runs entirely in one
    thread so it can be parallelized across sources. Returns a fully-built
    source record or None if the source was unusable.

    Optimizations:
    - Tier classification happens FIRST (heuristic-only, fast). If the
      domain is Tier-5 (social media, forums) and runtime config sets
      skip_tier_5_sources=true, we don't fetch the page at all.
    - Judgment cache: if we've already judged this (claim, URL) pair
      earlier in the run, reuse the result instead of paying for the
      Haiku call again.
    """
    url    = search_result["url"]
    title  = search_result["title"]
    domain = _normalize_domain(url)

    # (a) heuristic tier classification first
    heuristic = classify_domain_heuristic(url)
    if (
        heuristic is not None
        and heuristic[0] == "TIER_5"
        and _rt_truthseeker("skip_tier_5_sources")
    ):
        return None

    # (c) judgment cache lookup
    cache_enabled = _rt_truthseeker("judgment_cache_enabled")
    cache_key = _judgment_cache_key(claim_query, url) if cache_enabled else None
    if cache_enabled and cache_key in _judgment_cache:
        return dict(_judgment_cache[cache_key])  # defensive copy

    text, extraction_partial = trafilatura_extract(url)
    usable_text = text if text else search_result["snippet"]

    if not usable_text or len(usable_text.strip()) < 50:
        return None

    judgment = judge_source(models, providers, claim_query, url, title, domain, usable_text)
    if not judgment:
        return None

    if heuristic is not None:
        tier_label, tier_score, source_type = heuristic
    else:
        tier_label, tier_score, source_type = classify_domain(models, providers, url, title)

    record = {
        "url":                url,
        "title":              title,
        "domain":             domain,
        "tier":               tier_label,
        "tier_score":         tier_score,
        "source_type":        source_type,
        "supports_claim":     judgment["supports_claim"],
        "notes":              judgment["notes"],
        "extraction_partial": extraction_partial,
    }

    if cache_enabled and cache_key is not None and len(_judgment_cache) < _JUDGMENT_CACHE_MAX_ENTRIES:
        _judgment_cache[cache_key] = dict(record)

    return record


def _is_outcome_locked(sources: List[Dict[str, Any]]) -> bool:
    """
    Early-termination check: returns True if additional sources cannot
    change the verification status.

    Locked when:
      - 2+ Tier-1/2 full supporters with non-snippet evidence and no
        Tier-1/2 contradiction -> VERIFIED is locked (more sources can
        only confirm)
      - Any Tier-1/2 contradiction present -> CONTRADICTED or
        NEEDS_HUMAN_REVIEW is locked depending on opposing support
    """
    tier12_full_real = [
        s for s in sources
        if s["supports_claim"] == "full"
        and s["tier_score"] >= 4
        and not s.get("extraction_partial", False)
    ]
    tier12_contradictors = [
        s for s in sources
        if s["supports_claim"] == "contradicts" and s["tier_score"] >= 4
    ]
    if len(tier12_full_real) >= 2 and not tier12_contradictors:
        return True
    if tier12_contradictors:
        return True
    return False


# ============================================================
# verify_claim
# ============================================================

def verify_claim(
    models:      Dict[str, Any],
    providers:   Dict[str, Any],
    claim:       Dict[str, str],
    searxng_url: str,
) -> Dict[str, Any]:
    """
    Verify one claim with parallel source processing and early termination.

    Pipeline per claim:
      0. Trust-registry check -- if the claim text contains a Director-
         provided string, short-circuit to NOT_WEB_VERIFIABLE without
         any network activity. Second-line defense beyond
         extract_claim_candidates; catches claims that were synthesized
         by extract_structured_claims rather than pulled directly from
         regex hits.
      1. SearXNG search returns up to runtime.json's search_top_n URLs
      2. Sources are processed in parallel (fetch + judge + tier-classify)
         using up to runtime.json's parallel_workers threads
      3. As completed sources arrive, we check whether the policy
         outcome is locked. If so, we stop waiting on the remaining
         work and apply the policy to what we have.
    """
    query = claim["text"]

    # Trust-registry short-circuit (uses the get_active_registry accessor
    # introduced during the verifier extraction; old code directly read
    # the _TRUST_REGISTRY module global).
    registry = get_active_registry()
    if registry is not None and registry.contains(query):
        return {
            "claim":                claim["text"],
            "category":             claim.get("category", "other"),
            "status":               "NOT_WEB_VERIFIABLE",
            "confidence":           "N/A",
            "source_count":         0,
            "highest_source_tier":  None,
            "highest_source_score": 0,
            "sources":              [],
            "verified_at":          datetime.now().isoformat(timespec="seconds"),
            "policy_version":       "1.0",
            "note":                 "claim_text_contains_director_provided_input",
        }

    results = searxng_search(query, searxng_url, _rt_truthseeker('search_top_n'))

    sources: List[Dict[str, Any]] = []

    if results:
        with ThreadPoolExecutor(max_workers=_rt_truthseeker('parallel_workers')) as pool:
            futures = {
                pool.submit(_process_one_source, models, providers, query, r): r
                for r in results
            }
            for fut in as_completed(futures):
                try:
                    source_record = fut.result()
                except Exception as e:
                    sys.stderr.write(f"[TRUTHSEEKER] source-processing error: {type(e).__name__}: {e}\n")
                    continue
                if source_record is not None:
                    sources.append(source_record)
                if _is_outcome_locked(sources):
                    for pending in futures:
                        if not pending.done():
                            pending.cancel()
                    break

    status, confidence = apply_verification_policy(sources)

    source_count       = len(sources)
    highest_tier_score = max((s["tier_score"] for s in sources), default=0)
    highest_tier_label = next(
        (s["tier"] for s in sorted(sources, key=lambda x: -x["tier_score"])),
        None
    )

    return {
        "claim":                claim["text"],
        "category":             claim.get("category", "other"),
        "status":               status,
        "confidence":           confidence,
        "source_count":         source_count,
        "highest_source_tier":  highest_tier_label,
        "highest_source_score": highest_tier_score,
        "sources":              sources,
        "verified_at":          datetime.now().isoformat(timespec="seconds"),
        "policy_version":       "1.0",
    }


# ============================================================
# Formatters
# ============================================================

def format_verification_summary(verifications: List[Dict[str, Any]]) -> str:
    counts: Dict[str, int] = {}
    for v in verifications:
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    parts = []
    for status in ("VERIFIED", "PARTIALLY_VERIFIED", "NOT_WEB_VERIFIABLE",
                   "UNSUPPORTED", "CONTRADICTED", "NEEDS_HUMAN_REVIEW"):
        if counts.get(status):
            parts.append(f"{counts[status]} {status}")
    return f"{len(verifications)} claims checked - " + ", ".join(parts) if parts else f"{len(verifications)} claims checked"


def format_verification_for_transcript(verifications: List[Dict[str, Any]]) -> str:
    if not verifications:
        return ""
    lines = ["Truthseeker verification of claims in the previous turn:"]
    for v in verifications:
        claim_short = v["claim"] if len(v["claim"]) <= 160 else v["claim"][:157] + "..."
        line = f"  - [{v['status']}] (conf {v['confidence']}, {v['source_count']} src, top tier {v.get('highest_source_tier') or 'none'}): {claim_short}"
        lines.append(line)
        sources = sorted(v["sources"], key=lambda s: -s["tier_score"])[:2]
        for s in sources:
            if s["supports_claim"] in ("full", "partial", "contradicts"):
                lines.append(f"      [{s['tier']} {s['supports_claim']}] {s['url']}")
    lines.append(
        "Per ADAM policy: do NOT rely on UNSUPPORTED or CONTRADICTED claims "
        "in subsequent reasoning. Treat PARTIALLY_VERIFIED claims as tentative. "
        "NOT_WEB_VERIFIABLE means the verifier shouldn't have checked this "
        "kind of claim (it's an identifier, address, path, or configuration "
        "value, not a factual assertion about the world) -- ignore the verdict "
        "and treat the underlying item as authoritative per its source "
        "(seed, allowlist, skill_arg, or context file)."
    )
    return "\n".join(lines)
