"""
Web search and source-judgment subsystem for Truthseeker.

Three responsibilities:

1. **SearXNG queries** (searxng_search): hit the configured SearXNG
   instance for the top-N results matching a claim query. Returns
   list of {url, title, snippet} dicts. Network failure or empty
   results return []. Never raises.

2. **Page fetch and extraction** (trafilatura_extract): fetch the URL
   with a hard per-call timeout (trafilatura's fetch_url has no native
   timeout), run extraction with precision-favoring settings, return
   (text, extraction_partial_flag). The partial flag tells the policy
   layer the source can't reach VERIFIED on its own.

3. **Tier classification** (classify_domain_heuristic, classify_domain):
   first try a heuristic against curated tier lists (.gov -> TIER_1,
   .edu -> TIER_2, then explicit per-tier domain sets); fall back to
   LLM classification for unknown domains. Results are cached per-domain
   for the session.

4. **Source judgment** (judge_source): call the LLM to decide whether
   the fetched source text supports, partially supports, fails to
   address, or contradicts the claim. Returns {supports_claim, notes}.

Plus _model_json_call: shared LLM-with-JSON-output helper used by
judge_source, classify_domain, and (from claim_extractor) the structured
claim extractor. Tolerates conversational preambles, code fences, and
decoy brackets.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from adam.verifier._config import (
    TRUTHSEEKER_MODEL_ID,
    TRUTHSEEKER_TEMPERATURE,
    _rt_truthseeker,
)
from adam.core.client_dispatch import call_model


# ============================================================
# Source tier domain catalogs
# ============================================================

TIER_1_DOMAINS = {
    "ed.gov", "whitehouse.gov", "congress.gov", "supremecourt.gov",
    "uscourts.gov", "fcc.gov", "ftc.gov", "hhs.gov", "cdc.gov", "nih.gov",
    "ies.ed.gov", "nces.ed.gov", "studentprivacy.ed.gov",
    "europa.eu", "gov.uk", "canada.ca",
}

# State, local, and territorial government domains follow the well-known
# `<entity>.<state>.us` and `<entity>.k12.<state>.us` patterns documented
# in NIST SP 800-63 and US-CERT. These are matched via the structural
# check in classify_domain_heuristic (below), not as a literal set, since
# enumerating every district and agency would be both incomplete and
# brittle. They are Tier 2 rather than Tier 1 because they are
# state/local government rather than federal, but they remain official
# institutional sources.

TIER_2_DOMAINS = {
    "rand.org", "brookings.edu", "oecd.org", "unesco.org",
    "worldbank.org", "wipo.int", "iste.org",
    "edweek.org", "ascd.org",
    "nationalacademies.org", "nap.edu",
    "stanford.edu", "harvard.edu", "mit.edu",
}

TIER_3_DOMAINS = {
    "pewresearch.org",
    "nytimes.com", "washingtonpost.com", "wsj.com",
    "npr.org", "bbc.com", "bbc.co.uk", "reuters.com", "apnews.com",
    "theatlantic.com", "economist.com", "newyorker.com",
    "the74million.org", "chalkbeat.org",
}

TIER_4_DOMAINS = {
    "edutopia.org",
    "medium.com", "substack.com",
    "techcrunch.com", "wired.com",
    # Trade press covering education and government technology. These
    # are real reporting with named bylines, not blogs, but they sit
    # below Tier 3 because they are vertical/trade rather than
    # general-audience journalism with the editorial scale of NYT/WSJ/
    # Reuters.
    "govtech.com", "edsurge.com", "k12dive.com", "edscoop.com",
    # Regional Virginia newspapers and local-news outlets. Curated
    # because hostnames are unpredictable; expand here as new papers
    # appear in actual session traffic. Unmatched local news falls
    # through to None and is left for the convergence layer to weigh.
    "centralvirginian.com", "richmond.com", "wtvr.com", "wric.com",
    "wsls.com", "wdbj7.com", "newsadvance.com", "roanoke.com",
    "dailyprogress.com", "vpm.org",
}

TIER_5_DOMAINS = {
    "twitter.com", "x.com", "reddit.com", "facebook.com",
    "linkedin.com", "instagram.com", "tiktok.com",
    "quora.com", "youtube.com",
}


def _normalize_domain(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def _domain_matches_set(domain: str, tier_set: Set[str]) -> bool:
    """
    FIX #2: a domain matches a tier set if it IS in the set OR is a
    true subdomain of any entry. Previously the suffix check required
    a leading dot, which would miss the exact-match case if the `in`
    test fell through for any reason (whitespace, case, etc.).
    """
    if domain in tier_set:
        return True
    for d in tier_set:
        if domain == d or domain.endswith("." + d):
            return True
    return False


# US state two-letter codes used by the geographic .us subdomain policy.
# Source: USPS state abbreviations, applied as the .us delegated state
# codes. Frozen as a constant because the set is stable and the lookup
# is hot (called on every classified URL).
_US_STATE_CODES: frozenset[str] = frozenset({
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga",
    "hi", "id", "il", "in", "ia", "ks", "ky", "la", "me", "md",
    "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj",
    "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc",
    "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy",
    # Territories and DC, which also use .us geographic subdomains.
    "dc", "pr", "vi", "gu", "as", "mp",
})


def _is_us_state_local_domain(domain: str, *, require_k12: bool) -> bool:
    """
    Recognize US state/local government domains under the .us
    geographic policy.

    - require_k12=True matches `<anything>.k12.<state>.us`
      (state-administered K-12 districts).
    - require_k12=False matches the broader `<anything>.<state>.us`
      form (state agencies, counties, municipalities, etc.) AND
      excludes the k12 case so callers can test the more specific
      form first without double-matching.

    Inputs are expected to be already lowercased and `www.` stripped
    (see _normalize_domain). Returns False on anything that does not
    end in .us, has too few labels, or fails the state-code check.
    """
    if not domain.endswith(".us"):
        return False
    parts = domain.split(".")
    # Need at least three labels to have <sub>.<state>.us.
    if len(parts) < 3:
        return False
    # parts[-1] == "us"
    state = parts[-2]
    if state not in _US_STATE_CODES:
        return False
    if require_k12:
        # parts[-3] must be exactly "k12" and there must be at least
        # one more label in front of it (the district name).
        return len(parts) >= 4 and parts[-3] == "k12"
    # Non-k12 path: match the broad case but exclude the k12 form
    # so the two callers in classify_domain_heuristic stay distinct.
    return not (len(parts) >= 4 and parts[-3] == "k12")


def classify_domain_heuristic(url: str) -> Optional[Tuple[str, int, str]]:
    """Return (tier_label, tier_score, source_type) or None if unknown."""
    domain = _normalize_domain(url)
    if not domain:
        return None

    if domain.endswith(".gov") or ".gov." in domain or domain.endswith(".mil"):
        return ("TIER_1", 5, "government")
    if domain.endswith(".edu"):
        return ("TIER_2", 4, "university")

    # State and local US government domains.
    # Per the geographic Names Authorities and the historical .us
    # delegation policy, hostnames matching `<sub>.k12.<state>.us`
    # are state-administered K-12 districts, and the broader
    # `<sub>.<state>.us` pattern covers state and municipal sites.
    # We match the two-letter US state code (lowercased) between
    # the entity and the trailing .us label. The k12 path is matched
    # first because it is the more specific form.
    if _is_us_state_local_domain(domain, require_k12=True):
        return ("TIER_2", 4, "k12-district")
    if _is_us_state_local_domain(domain, require_k12=False):
        return ("TIER_2", 4, "state-local-government")

    for tier_set, tier_label, tier_score, src_type in [
        (TIER_1_DOMAINS, "TIER_1", 5, "primary-source"),
        (TIER_2_DOMAINS, "TIER_2", 4, "research-org"),
        (TIER_3_DOMAINS, "TIER_3", 3, "established-journalism"),
        (TIER_4_DOMAINS, "TIER_4", 2, "trade-or-blog"),
        (TIER_5_DOMAINS, "TIER_5", 1, "social-media-or-forum"),
    ]:
        if _domain_matches_set(domain, tier_set):
            return (tier_label, tier_score, src_type)
    return None


# ============================================================
# SearXNG + trafilatura
# ============================================================

def searxng_search(query: str, base_url: str, top_n: int) -> List[Dict[str, str]]:
    import requests
    try:
        resp = requests.get(
            f"{base_url.rstrip('/')}/search",
            params={"q": query, "format": "json", "safesearch": 1},
            timeout=_rt_truthseeker('search_http_timeout_seconds'),
            headers={"User-Agent": "ADAM-Truthseeker/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for r in data.get("results", [])[:top_n]:
            url = r.get("url", "")
            if not url:
                continue
            results.append({
                "url":     url,
                "title":   r.get("title", "")[:300],
                "snippet": r.get("content", "")[:1000],
            })
        return results
    except Exception as e:
        sys.stderr.write(f"[TRUTHSEEKER] SearXNG search failed for {query!r}: {type(e).__name__}: {e}\n")
        return []


def trafilatura_extract(url: str) -> Tuple[Optional[str], bool]:
    """
    Fetch and extract main text from a page using trafilatura, with a HARD
    per-call timeout. trafilatura.fetch_url() has no native timeout parameter
    and can hang on slow pages, JS-heavy sites, or bot-challenge endpoints.
    We wrap it in a thread and abandon any fetch that exceeds the deadline.

    Returns (text_or_None, extraction_partial_flag). extraction_partial=True
    means the source can NOT support a VERIFIED status per policy rules; the
    caller falls back to the SearXNG snippet for judgment.
    """
    import trafilatura  # safe -- startup validator confirms it's installed

    result_holder: Dict[str, Any] = {"downloaded": None, "error": None}

    def _fetch():
        try:
            result_holder["downloaded"] = trafilatura.fetch_url(url, no_ssl=True)
        except Exception as e:
            result_holder["error"] = e

    fetch_thread = threading.Thread(target=_fetch, daemon=True)
    fetch_thread.start()
    page_timeout = _rt_truthseeker('page_fetch_timeout_seconds')
    fetch_thread.join(timeout=page_timeout)

    if fetch_thread.is_alive():
        sys.stderr.write(f"[TRUTHSEEKER] fetch timeout ({page_timeout}s) on {url}\n")
        return None, True

    if result_holder["error"] is not None:
        e = result_holder["error"]
        sys.stderr.write(f"[TRUTHSEEKER] fetch failed on {url}: {type(e).__name__}: {e}\n")
        return None, True

    downloaded = result_holder["downloaded"]
    if not downloaded:
        return None, True

    try:
        text = trafilatura.extract(
            downloaded,
            favor_precision=True,
            include_comments=False,
            include_tables=False,
            include_links=False,
            no_fallback=False,
        )
    except Exception as e:
        sys.stderr.write(f"[TRUTHSEEKER] extract failed on {url}: {type(e).__name__}: {e}\n")
        return None, True

    if not text or len(text.strip()) < 200:
        return None, True
    return text.strip(), False


# ============================================================
# Truthseeker prompts
# ============================================================

EXTRACTOR_SYSTEM = (
    "You are ADAM's claim extractor. You do not debate or interpret. You only identify "
    "specific factual claims that could potentially be verified against external sources."
)

EXTRACTOR_USER_TEMPLATE = """\
Below is text generated by an ADAM agent during a deliberation about K-12 education. \
Your job is to identify every specific factual claim in the text that could be checked \
against external sources.

Return a JSON array. Each element has:
- "text":     a verbatim or near-verbatim quote of the claim
- "category": one of "named_district", "named_program", "named_organization",
              "named_study", "statistic", "legal_claim", "historical_claim",
              "policy_assertion", "citation", "other"

INCLUDE any claim that references:
- Specific named districts, schools, or school systems (e.g., "Mooresville, NC",
  "Vista Unified (California)", "Gwinnett County (Georgia)")
- Specific named programs, curricula, or initiatives (e.g., "Teach Less, Learn More",
  "Student Technology Leadership Program (STLP)", "5C framework")
- Specific named organizations and what they did or found (e.g., "CoSN found...",
  "CPRE research consistently shows...", "Future of Privacy Forum recommends...")
- Specific statistics (numbers, percentages, counts with named populations or contexts)
- Specific dated events, studies, research findings, or rollouts
- Specific legal or regulatory assertions (e.g., "FERPA requires...")
- Country-level claims about education systems (e.g., "Finland embedded X across grade levels",
  "Singapore's initiative reduced Y")
- Any other concrete fact a reasonable school board member would want to see sourced

EXCLUDE:
- Generic statements ("teachers need training")
- Recommendations, proposals, or aspirations ("we should...", "the plan must...")
- Opinions or interpretations
- Hypothetical scenarios
- Internal references to the conversation itself

Maximum {max_claims} claims. If fewer real claims exist, return fewer. If genuinely none, return [].

{candidates_section}\
FULL AGENT TEXT:
\"\"\"
{full_text}
\"\"\"

Return ONLY the JSON array. No preamble, no markdown.\
"""

JUDGE_SYSTEM = (
    "You are ADAM's source-judgment service. You decide whether a given source text "
    "supports, partially supports, fails to address, or contradicts a specific claim. "
    "You never infer beyond what the source text actually says."
)

JUDGE_USER_TEMPLATE = """\
CLAIM:
{claim_text}

SOURCE URL:    {url}
SOURCE TITLE:  {title}
SOURCE DOMAIN: {domain}

SOURCE TEXT (excerpt, may be truncated):
\"\"\"
{source_excerpt}
\"\"\"

Decide whether this source supports the claim. Be strict:
- "full":        the source directly and clearly supports the specific claim
- "partial":     the source supports a weaker/broader version of the claim
- "none":        the source does not address the claim
- "contradicts": the source actually contradicts the claim

Return JSON:
{{"supports_claim": "full|partial|none|contradicts", "notes": "one sentence explaining the judgment"}}

Return ONLY the JSON.\
"""

DOMAIN_CLASSIFIER_SYSTEM = (
    "You classify URLs into ADAM's source tier system. Output JSON only."
)

DOMAIN_CLASSIFIER_USER_TEMPLATE = """\
TIER_1: government (.gov, courts, laws, regulations, official agency docs, official primary docs)
TIER_2: peer-reviewed research, universities (.edu), recognized research institutions, standards bodies
TIER_3: established nonprofits, professional associations, reputable think tanks, major journalism with named sources
TIER_4: trade publications, vendor blogs, local news, practitioner blogs with clear authorship
TIER_5: personal blogs, social media, forums, unsourced summaries, AI-generated content

URL:    {url}
DOMAIN: {domain}
TITLE:  {title}

Classify into exactly one tier. Return JSON:
{{"tier": "TIER_N", "tier_score": <1-5>, "source_type": "short label", "reasoning": "one sentence"}}

Return ONLY the JSON.\
"""


# ============================================================
# JSON-tolerant LLM caller
# ============================================================

def _extract_first_json_value(raw: str) -> Optional[str]:
    """
    Bulletproof JSON extraction. Walks the string finding balanced JSON
    values (object or array) and returns the first one that actually
    parses as valid JSON. Tolerates conversational preambles like
    "Sure, here is the JSON:", trailing prose, and decoy brackets in
    prose (e.g. "I tested [several options]" before the real JSON).

    Returns the JSON substring, or None if no balanced parseable value
    found.
    """
    s = raw.strip()
    fence_match = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", s, re.DOTALL)
    if fence_match:
        s = fence_match.group(1).strip()

    n = len(s)
    start = 0
    while start < n:
        ch = s[start]
        if ch not in ("{", "["):
            start += 1
            continue

        open_ch  = ch
        close_ch = "}" if ch == "{" else "]"
        depth = 0
        in_string = False
        escape = False
        end_idx = -1
        for i in range(start, n):
            c = s[i]
            if in_string:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_string = False
                continue
            if c == '"':
                in_string = True
                continue
            if c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    end_idx = i
                    break

        if end_idx >= 0:
            candidate = s[start:end_idx + 1]
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                start = end_idx + 1
                continue
        start += 1

    return None


def _model_json_call(
    models:        Dict[str, Any],
    providers:     Dict[str, Any],
    model_id:      str,
    system:        str,
    user:          str,
    max_tokens:    int = 1500,
) -> Optional[Any]:
    """
    Call a model and return parsed JSON. Tolerant of preambles & fences.
    """
    try:
        raw = call_model(
            model_id=model_id,
            system_prompt=system,
            messages=[{"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=TRUTHSEEKER_TEMPERATURE,
            models=models,
            providers=providers,
        )
    except Exception as e:
        sys.stderr.write(f"[TRUTHSEEKER] model call failed: {type(e).__name__}: {e}\n")
        return None

    json_str = _extract_first_json_value(raw)
    if json_str is None:
        sys.stderr.write(f"[TRUTHSEEKER] no balanced JSON found in response:\n--- raw ---\n{raw}\n-----------\n")
        return None
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"[TRUTHSEEKER] JSON parse failed: {e}\n--- extracted ---\n{json_str}\n-----------------\n")
        return None


# ============================================================
# Source judgment + domain classification (LLM)
# ============================================================

def judge_source(
    models:        Dict[str, Any],
    providers:     Dict[str, Any],
    claim_text:    str,
    url:           str,
    title:         str,
    domain:        str,
    source_text:   str,
) -> Optional[Dict[str, str]]:
    excerpt = source_text[:_rt_truthseeker('source_excerpt_chars')]
    user = JUDGE_USER_TEMPLATE.format(
        claim_text=claim_text, url=url, title=title, domain=domain, source_excerpt=excerpt,
    )
    result = _model_json_call(models, providers, TRUTHSEEKER_MODEL_ID, JUDGE_SYSTEM, user, max_tokens=400)
    if not isinstance(result, dict):
        return None
    supports = result.get("supports_claim", "none")
    if supports not in ("full", "partial", "none", "contradicts"):
        supports = "none"
    return {
        "supports_claim": supports,
        "notes":          str(result.get("notes", ""))[:500],
    }


# Per-session domain tier cache. Module-global, like the original;
# step 7 will move this to SessionContext for multi-instance safety.
_domain_tier_cache: Dict[str, Tuple[str, int, str]] = {}


def classify_domain(
    models:    Dict[str, Any],
    providers: Dict[str, Any],
    url:       str,
    title:     str,
) -> Tuple[str, int, str]:
    heuristic = classify_domain_heuristic(url)
    if heuristic:
        return heuristic

    domain = _normalize_domain(url)
    if domain in _domain_tier_cache:
        return _domain_tier_cache[domain]

    user = DOMAIN_CLASSIFIER_USER_TEMPLATE.format(url=url, domain=domain, title=title)
    result = _model_json_call(models, providers, TRUTHSEEKER_MODEL_ID,
                              DOMAIN_CLASSIFIER_SYSTEM, user, max_tokens=200)

    if isinstance(result, dict):
        tier = str(result.get("tier", "TIER_5"))
        try:
            score = int(result.get("tier_score", 1))
        except (ValueError, TypeError):
            score = 1
        score = max(1, min(5, score))
        src_type = str(result.get("source_type", "unknown"))[:50]
        tier_tuple = (tier, score, src_type)
    else:
        tier_tuple = ("TIER_5", 1, "unclassified")

    _domain_tier_cache[domain] = tier_tuple
    return tier_tuple


# Public alias for callers that want a uniform "classify any source" entry
# point. classify_domain is what the original code names it; classify_source_tier
# is the name used in step 1's events schema and is also what makes more
# sense to external consumers.
classify_source_tier = classify_domain
