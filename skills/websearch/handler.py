"""
ADAM websearch skill.

Read-only SearXNG search bridge for candidate public sources.
This skill does not verify claims and does not fetch page bodies.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

MIN_TOP_N = 1
MAX_TOP_N = 10
DEFAULT_TOP_N = 5
DEFAULT_SAFE_SEARCH = 1
REQUEST_TIMEOUT_SECONDS = 10.0
USER_AGENT = "ADAM-WebsearchSkill/1.1"


def _fail(action: str, error_class: str, error_message: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "status": "failed",
        "skill": "websearch",
        "action": action,
        "error_class": error_class,
        "error_message": error_message,
    }


def _coerce_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))


def _clean_optional_string(value: Any, max_len: int = 64) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    # Keep optional filters simple and header-safe.
    value = re.sub(r"[^A-Za-z0-9_\-.,/ ]", "", value)[:max_len].strip()
    return value or None


def _get_searxng_url() -> str:
    """
    Resolve SearXNG endpoint.

    ADAM Truthseeker should normally configure SEARXNG_URL in .env.
    We also accept SEARXNG_BASE_URL as a harmless alias for deployments
    that use that naming convention. We intentionally do not use a public
    fallback because this skill should use the operator-controlled SearXNG.
    """
    return (
        os.environ.get("SEARXNG_URL", "").strip()
        or os.environ.get("SEARXNG_BASE_URL", "").strip()
    )


def _safe_endpoint_host(url: str) -> str:
    try:
        parsed = urlparse(url)
        return parsed.netloc or parsed.path
    except Exception:
        return "unavailable"


def _is_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def _domain_from_url(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _classify_source(url: str) -> Dict[str, str]:
    """
    Source-tier classification.

    Delegates to adam.verifier.web_search.classify_domain_heuristic so the
    skill stays consistent with Truthseeker's tier policy and benefits
    from the curated tier sets (state/local government, ed-tech trade
    press, regional newspapers, etc.). Only the heuristic path is used
    here; the LLM-backed fallback in classify_domain requires models/
    providers and is intentionally out of scope for a synchronous skill
    invocation.

    For URLs the heuristic doesn't recognize, return TIER_5/unclassified.
    Operator should weigh those alongside any convergent_findings the
    skill emits, rather than treating an unrecognized domain as low
    quality.
    """
    try:
        from adam.verifier.web_search import classify_domain_heuristic
    except Exception:
        # Verifier subsystem not importable. Preserve the original
        # standalone behavior so the skill still works in isolation
        # (e.g. unit tests, debug shells).
        return _classify_source_local_fallback(url)

    result = classify_domain_heuristic(url)
    if result is None:
        return {"source_tier": "TIER_5", "source_type": "unclassified"}
    tier_label, _score, src_type = result
    return {"source_tier": tier_label, "source_type": src_type}


def _classify_source_local_fallback(url: str) -> Dict[str, str]:
    """
    Standalone fallback when the verifier subsystem is not importable.

    Mirrors the previous skill-local heuristic so the skill remains a
    self-contained read-only search bridge. Kept deliberately narrow:
    if the curated tier sets matter, the verifier path will be present.
    """
    domain = _domain_from_url(url)
    host = domain.split(":", 1)[0]

    if host.endswith(".gov") or ".gov." in host:
        return {"source_tier": "TIER_1", "source_type": "government"}
    if host.endswith(".edu") or ".edu." in host:
        return {"source_tier": "TIER_2", "source_type": "education"}
    if host.endswith(".mil") or ".mil." in host:
        return {"source_tier": "TIER_1", "source_type": "military"}
    if host.endswith(".org") or ".org." in host:
        return {"source_tier": "TIER_3", "source_type": "organization"}

    return {"source_tier": "TIER_5", "source_type": "unclassified"}


def _search_with_stable_helper(
    query: str,
    top_n: int,
    safe_search: int,
    language: Optional[str],
    category: Optional[str],
) -> Optional[List[Dict[str, Any]]]:
    """
    Try to use a future stable ADAM helper if present.

    This intentionally avoids calling an uncertain Truthseeker helper signature.
    It only calls adam.verifier.web_search.public_search if that exact stable
    function exists. Otherwise, caller falls back to local SearXNG HTTP.
    """
    try:
        from adam.verifier import web_search as ws  # type: ignore
    except Exception:
        return None

    public_search = getattr(ws, "public_search", None)
    if not callable(public_search):
        return None

    try:
        return public_search(
            query=query,
            top_n=top_n,
            safe_search=safe_search,
            language=language,
            category=category,
        )
    except TypeError:
        # Stable helper exists but signature does not match this skill contract.
        return None


def _search_searxng_direct(
    query: str,
    top_n: int,
    safe_search: int,
    language: Optional[str],
    category: Optional[str],
    searxng_url: str,
) -> List[Dict[str, Any]]:
    import requests

    parsed_endpoint = urlparse(searxng_url)
    if parsed_endpoint.scheme not in {"http", "https"}:
        raise ValueError("SEARXNG_URL must use http or https")

    params: Dict[str, Any] = {
        "q": query,
        "format": "json",
        "safesearch": safe_search,
    }
    if language:
        params["language"] = language
    if category:
        params["categories"] = category

    resp = requests.get(
        f"{searxng_url.rstrip('/')}/search",
        params=params,
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    payload = resp.json()

    raw_results: List[Dict[str, Any]] = []
    for r in payload.get("results", [])[:top_n * 2]:
        url = str(r.get("url", "")).strip()
        if not _is_http_url(url):
            continue
        raw_results.append(
            {
                "title": str(r.get("title", "")).strip(),
                "url": url,
                "snippet": str(r.get("content", r.get("snippet", ""))).strip(),
            }
        )
        if len(raw_results) >= top_n:
            break
    return raw_results


def _normalize_results(raw_results: List[Dict[str, Any]], top_n: int) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    seen_urls = set()

    for r in raw_results:
        url = str(r.get("url", "")).strip()
        if not _is_http_url(url) or url in seen_urls:
            continue
        seen_urls.add(url)

        title = str(r.get("title", "")).strip()
        snippet = str(r.get("snippet", r.get("content", ""))).strip()
        classification = _classify_source(url)

        normalized.append(
            {
                "title": title,
                "url": url,
                "domain": _domain_from_url(url),
                "snippet": snippet,
                "source_tier": classification["source_tier"],
                "source_type": classification["source_type"],
            }
        )
        if len(normalized) >= top_n:
            break

    return normalized


# ============================================================
# Convergence extraction
# ============================================================
#
# Lightweight pure-Python detector that flags noun phrases two or
# more credible HTTP/HTTPS sources appear to agree on.
#
# DESIGN
# ------
# - Always runs after normalization; emits a convergent_findings list
#   only when at least one finding crosses the threshold. Otherwise
#   omitted from the response.
# - Operates on title + snippet text per result. Tokenizes to lower-
#   cased word sequences, extracts 2- to 5-grams, filters n-grams
#   that are entirely stopwords or that look like generic boilerplate.
# - "Credible" means HTTP/HTTPS (already enforced upstream) AND
#   non-empty title-or-snippet text. The skill never invents a finding
#   from an empty result.
# - "Independent" means distinct registrable domains. Two pages on
#   the same site cannot converge with themselves.
# - Findings are labeled candidate_convergence and the block carries
#   an explicit caveat string. They are NOT verified facts. Truthseeker
#   remains the authoritative verification layer.
#
# NOT DOING (deliberately, see plan):
# - No is_titular_query / intent gating; convergence runs on every
#   search and only emits when actual convergence is found.
# - No reordering of Operator routing modes.
# - No LLM calls.

_CONVERGENCE_NGRAM_MIN  = 2
_CONVERGENCE_NGRAM_MAX  = 5
_CONVERGENCE_MIN_DOMAINS = 2
_CONVERGENCE_MAX_FINDINGS = 8

# Stopwords kept small and focused. The goal is to suppress phrases
# that would be repeated across unrelated pages (headers, footers,
# generic navigation language), not to do real NLP. n-grams composed
# entirely of stopwords are dropped; n-grams that merely contain
# stopwords are kept.
_CONVERGENCE_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "of", "in", "on",
    "to", "for", "with", "by", "at", "from", "as", "is", "are", "was",
    "were", "be", "been", "being", "this", "that", "these", "those",
    "it", "its", "their", "his", "her", "our", "your", "my", "us",
    "we", "you", "he", "she", "they", "them", "i", "me",
    "will", "would", "can", "could", "should", "may", "might", "must",
    "do", "does", "did", "have", "has", "had", "not", "no", "yes",
    "than", "then", "so", "such", "more", "most", "some", "any",
    "all", "each", "every", "other", "another", "also", "just", "only",
    "into", "out", "up", "down", "over", "under", "after", "before",
    "between", "through", "during", "about",
    # Web boilerplate that turns up across snippets on unrelated topics.
    "home", "page", "search", "menu", "click", "here", "read", "more",
    "skip", "content", "subscribe", "share", "follow", "log", "sign",
    "cookies", "privacy", "policy", "terms", "service", "use", "site",
    "website", "official",
})

# Tokenization regex: lowercased word characters (letters, digits,
# apostrophes for contractions). Hyphenated terms are split into
# their components; "chief-technology" becomes ["chief", "technology"].
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9']*")


def _tokenize_for_convergence(text: str) -> List[str]:
    """Lowercase tokenizer used for n-gram extraction. Returns [] on empty."""
    if not text:
        return []
    return [m.group(0).lower() for m in _TOKEN_RE.finditer(text)]


def _registrable_domain(domain: str) -> str:
    """
    Reduce a fully-qualified domain to a comparable form for the
    "distinct domains" check. Strips a leading "www.". Does not
    attempt a real public-suffix-list reduction; "news.bbc.co.uk"
    and "bbc.co.uk" are treated as distinct here. That asymmetry
    is acceptable for a candidate signal — false-negative on
    convergence is preferable to false-positive.
    """
    d = (domain or "").lower().strip()
    if d.startswith("www."):
        d = d[4:]
    return d


def _ngrams_in_tokens(tokens: List[str], n: int) -> List[Tuple[str, ...]]:
    if len(tokens) < n:
        return []
    return [tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]


def _ngram_is_meaningful(ngram: Tuple[str, ...]) -> bool:
    """
    Decide whether an n-gram is content-bearing enough to consider for
    convergence.

    Rules tuned to suppress generic English while keeping real
    convergent claims:
    - 2-grams must be entirely content tokens (no stopwords at all).
      "chief technology" passes; "the kitchen" does not.
    - 3+ grams must have at least 2 content tokens. "chief technology
      officer" passes; "for example the" (1 content token: "example")
      does not.
    """
    content_tokens = sum(1 for tok in ngram if tok not in _CONVERGENCE_STOPWORDS)
    if len(ngram) <= 2:
        return content_tokens == len(ngram)
    return content_tokens >= 2


def _ngram_is_subspan(short: Tuple[str, ...], long: Tuple[str, ...]) -> bool:
    """True if `short` appears as a contiguous subsequence of `long`."""
    if len(short) >= len(long):
        return False
    L = len(short)
    for i in range(len(long) - L + 1):
        if long[i:i + L] == short:
            return True
    return False


def _extract_convergent_findings(
    results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Scan normalized results for noun phrases shared across >= 2 distinct
    domains. Return a list of finding dicts. Empty list if nothing
    converges.

    Each finding dict has:
      - phrase:           the matched n-gram, joined with spaces
      - distinct_domains: how many distinct registrable domains contain it
      - supporting_urls:  list of result URLs (one per distinct domain,
                          first occurrence wins) where the phrase appears
      - kind:             always "candidate_convergence"
      - confidence_band:  "low" (2 domains) or "medium" (3+ domains).
                          High is intentionally not used because this is
                          a candidate signal, not a verification result.
    """
    if not results or len(results) < _CONVERGENCE_MIN_DOMAINS:
        return []

    # Per-result tokenization. Title and snippet are combined into a
    # single bag of tokens per result; per-result domain is captured
    # for the independence check.
    per_result: List[Dict[str, Any]] = []
    for r in results:
        # Skip results that have no usable text at all. Defensive:
        # _normalize_results already rejects bad URLs, but we re-check
        # the text fields here so empty snippets don't fabricate
        # convergence.
        text = " ".join([
            str(r.get("title", "") or ""),
            str(r.get("snippet", "") or ""),
        ]).strip()
        if not text:
            continue
        tokens = _tokenize_for_convergence(text)
        if not tokens:
            continue
        reg_dom = _registrable_domain(r.get("domain", ""))
        if not reg_dom:
            continue
        per_result.append({
            "registrable_domain": reg_dom,
            "url":                r.get("url", ""),
            "tokens":             tokens,
        })

    if len(per_result) < _CONVERGENCE_MIN_DOMAINS:
        return []

    # Distinct-domain count for each n-gram. We track:
    #   ngram -> {registrable_domain -> first_url_seen}
    # to enforce the "distinct domains" rule and to surface one
    # representative URL per supporting domain.
    ngram_to_domains: Dict[Tuple[str, ...], Dict[str, str]] = {}

    for entry in per_result:
        tokens = entry["tokens"]
        reg_dom = entry["registrable_domain"]
        url = entry["url"]
        # Each result contributes each unique n-gram at most once; we
        # don't want a snippet that repeats a phrase to count as two
        # supporters within the same domain.
        seen_in_this_result: set = set()
        for n in range(_CONVERGENCE_NGRAM_MIN, _CONVERGENCE_NGRAM_MAX + 1):
            for ng in _ngrams_in_tokens(tokens, n):
                if ng in seen_in_this_result:
                    continue
                if not _ngram_is_meaningful(ng):
                    continue
                seen_in_this_result.add(ng)
                slot = ngram_to_domains.setdefault(ng, {})
                # First URL per domain wins; we don't overwrite once a
                # representative is recorded for this domain.
                if reg_dom not in slot:
                    slot[reg_dom] = url

    # Filter to n-grams crossing the distinct-domain threshold.
    candidates: List[Tuple[Tuple[str, ...], Dict[str, str]]] = [
        (ng, doms)
        for ng, doms in ngram_to_domains.items()
        if len(doms) >= _CONVERGENCE_MIN_DOMAINS
    ]
    if not candidates:
        return []

    # De-duplicate overlapping n-grams in two passes.
    #
    # Pass 1: prefer the longest phrase when a shorter n-gram is a
    # subspan of a longer one and the longer one is supported by at
    # least as many distinct domains. This avoids reporting
    # "chief technology", "chief technology officer", and "the chief
    # technology officer" as three separate findings.
    #
    # Pass 2: when a SHORTER phrase has STRICTLY MORE distinct domains
    # than a longer phrase that contains it, drop the longer one. The
    # shorter phrase is the broader signal; the longer one would just
    # be a noisier subset. Avoids "chief technology officer david
    # childress" (2 domains) tagging along behind "chief technology
    # officer" (3 domains).
    candidates.sort(key=lambda x: (-len(x[0]), -len(x[1])))

    kept: List[Tuple[Tuple[str, ...], Dict[str, str]]] = []
    for ng, doms in candidates:
        covered = False
        for kept_ng, kept_doms in kept:
            if (
                _ngram_is_subspan(ng, kept_ng)
                and len(kept_doms) >= len(doms)
            ):
                covered = True
                break
        if not covered:
            kept.append((ng, doms))

    # Pass 2: shorter-dominates-longer cleanup. Walk every pair; if any
    # SHORTER kept finding has STRICTLY MORE domains than a kept finding
    # that contains it, mark the longer one for removal. Pure pairwise
    # comparison keeps the rule easy to reason about; the kept list is
    # bounded by _CONVERGENCE_MAX_FINDINGS so the O(n^2) walk is cheap.
    dominated: set = set()
    for i, (long_ng, long_doms) in enumerate(kept):
        for j, (short_ng, short_doms) in enumerate(kept):
            if i == j:
                continue
            if (
                len(short_ng) < len(long_ng)
                and _ngram_is_subspan(short_ng, long_ng)
                and len(short_doms) > len(long_doms)
            ):
                dominated.add(i)
                break
    kept = [k for idx, k in enumerate(kept) if idx not in dominated]

    # Rank: more distinct domains first, then longer phrase.
    kept.sort(key=lambda x: (-len(x[1]), -len(x[0])))
    kept = kept[:_CONVERGENCE_MAX_FINDINGS]

    findings: List[Dict[str, Any]] = []
    for ng, doms in kept:
        phrase = " ".join(ng)
        n_domains = len(doms)
        band = "medium" if n_domains >= 3 else "low"
        # Stable URL order: by domain name. Keeps output deterministic
        # for tests and for cross-session diffs.
        supporting_urls = [doms[d] for d in sorted(doms.keys())]
        findings.append({
            "phrase":             phrase,
            "distinct_domains":   n_domains,
            "supporting_urls":    supporting_urls,
            "kind":               "candidate_convergence",
            "confidence_band":    band,
        })

    return findings


def handle(action: str, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """
    ADAM skill handler entry point.

    Expected signature:
        handle(action: str, args: dict, context: dict) -> dict
    """
    if action != "search":
        return _fail(
            action=action,
            error_class="disallowed_action",
            error_message=f"Action '{action}' is unrecognized. Supported actions: ['search'].",
        )

    if not isinstance(args, dict):
        return _fail(
            action=action,
            error_class="invalid_args",
            error_message="args must be an object/dictionary.",
        )

    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return _fail(
            action=action,
            error_class="missing_required_args",
            error_message="The 'query' argument is required and must be a non-empty string.",
        )
    query = query.strip()

    # Keep queries bounded so accidental huge prompts do not get sent to search.
    if len(query) > 500:
        query = query[:500].strip()

    top_n = _coerce_int(args.get("top_n", DEFAULT_TOP_N), DEFAULT_TOP_N, MIN_TOP_N, MAX_TOP_N)
    safe_search = _coerce_int(args.get("safe_search", DEFAULT_SAFE_SEARCH), DEFAULT_SAFE_SEARCH, 0, 2)
    language = _clean_optional_string(args.get("language"), max_len=32)
    category = _clean_optional_string(args.get("category"), max_len=64)

    searxng_url = _get_searxng_url()
    if not searxng_url:
        return _fail(
            action=action,
            error_class="environment_configuration_missing",
            error_message="SEARXNG_URL is not configured. Configure ADAM's self-hosted SearXNG endpoint in the environment.",
        )

    try:
        raw_results = _search_with_stable_helper(query, top_n, safe_search, language, category)
        if raw_results is None:
            raw_results = _search_searxng_direct(query, top_n, safe_search, language, category, searxng_url)
        results = _normalize_results(raw_results, top_n)
    except Exception as e:
        return _fail(
            action=action,
            error_class="network_read_exception",
            error_message=f"SearXNG query execution failed: {type(e).__name__}: {e}",
        )

    # Convergence pass. Always runs after normalization; the block is
    # added to the response only when at least one finding crosses the
    # distinct-domains threshold. A defensive try/except keeps the
    # search response well-formed even if convergence extraction blows
    # up on some pathological input: the search results themselves are
    # the contract, the findings are a bonus.
    try:
        convergent_findings = _extract_convergent_findings(results)
    except Exception:
        convergent_findings = []

    response: Dict[str, Any] = {
        "ok": True,
        "status": "success",
        "skill": "websearch",
        "action": "search",
        "query": query,
        "results": results,
        "audit_meta": {
            "io_operation": "external_network_read",
            "endpoint_host": _safe_endpoint_host(searxng_url),
            "results_returned": len(results),
            "convergent_findings_count": len(convergent_findings),
            "write_access_asserted": False,
            "truthseeker_followup_required": True,
        },
        "note": "Search results are candidate sources, not verified claims. Claims based on these results should still be checked by Truthseeker.",
    }

    if convergent_findings:
        response["convergent_findings"] = convergent_findings
        response["convergent_findings_caveat"] = (
            "These are candidate convergent observations across multiple "
            "search snippets, not verified facts. Convergence indicates that "
            "two or more independent sources used similar wording about the "
            "same topic; it does not establish truth. Pass any finding you "
            "intend to rely on to Truthseeker for verification before "
            "treating it as a confirmed claim."
        )

    return response
