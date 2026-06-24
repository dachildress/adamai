"""
Context budget management, summarization, and [T0] Background block.

Four responsibilities:

1. **Token budget assessment** (_estimate_tokens, classify_budget_status):
   given the total character count of extracted context files, estimate
   the token cost and classify against runtime-configured thresholds:
   OK / WARNING / HIGH / BLOCKED.

2. **Summarizer service hook** (summarize_file): invoke the Summarizer
   service agent on a file's extracted text, with a per-(file_hash,
   model_id, prompt_version) cache to avoid paying for the same summary
   twice. Cache hits and misses both emit audit records.

3. **Operator confirmation flow** (_print_assessment, _prompt_assessment_choice,
   ContextLoadDecision): show the operator what's about to be sent to
   external API providers, get explicit consent, honor --yes-context-risk
   for automation and --override-context-limit for the BLOCKED case.

4. **[T0] Background block construction** (build_background_block,
   load_context_block): assemble the per-file labeled content, prepend
   the document-grounded claim rule, and return the block that gets
   injected at T0 of the deliberation.

EXCEPTION CONTRACT: load_context_block raises ContextLoadAborted if the
operator declines to proceed (rather than calling fatal() directly).
This keeps the context module free of upward dependencies on the runtime.
The runtime's main() catches ContextLoadAborted and calls fatal() the
same way it does for ConfigError.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from adam.core.exceptions import ContextLoadAborted
from adam.core.client_dispatch import call_model
from adam.context._config import _rt_context
from adam.context.file_extractor import (
    ContextFile,
    extract_text_for_file,
)


# ============================================================
# Summarizer prompt-version lookup
# ============================================================

# Regex used to read the prompt_version marker from prompts/summarizer.md.
# The marker is an HTML comment like "<!-- prompt_version: v3 -->".
PROMPT_VERSION_RE = re.compile(r"<!--\s*prompt_version:\s*([A-Za-z0-9_.\-]+)\s*-->")


def _read_summarizer_prompt_version() -> str:
    """
    Read prompts/summarizer.md and extract its prompt_version marker.
    Returns the version string (e.g. 'summarizer_v1'). Falls back to
    'unversioned' if no marker is found; that fallback would defeat
    cache invalidation on prompt changes, so the operator should always
    keep a marker in the file.
    """
    path = Path("prompts/summarizer.md")
    if not path.exists():
        return "unversioned"
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return "unversioned"
    match = PROMPT_VERSION_RE.search(text)
    if match:
        return match.group(1)
    return "unversioned"


# ============================================================
# Token estimation
# ============================================================

def _estimate_tokens(char_count: int) -> int:
    """Cheap token estimate: chars / runtime divisor (default 4)."""
    divisor = _rt_context("estimate_tokens_by_chars_divisor")
    return max(1, char_count // divisor)


def classify_budget_status(total_tokens: int) -> str:
    """
    Classify the total token count against the runtime-configured
    thresholds. Status values:
      OK         under target
      WARNING    >= soft_warning_tokens (recommend summarization)
      HIGH       >= target, < hard_refusal (recommend summarization, allow override)
      BLOCKED    >= hard_refusal_tokens (operator must override explicitly)
    """
    target = _rt_context("target_context_tokens")
    soft   = _rt_context("soft_warning_tokens")
    hard   = _rt_context("hard_refusal_tokens")
    if total_tokens >= hard:
        return "BLOCKED"
    if total_tokens >= target:
        return "HIGH"
    if total_tokens >= soft:
        return "WARNING"
    return "OK"


# ============================================================
# Summarizer cache
# ============================================================

def _cache_path_for(cf: ContextFile, model_id: str, prompt_version: str) -> Path:
    """
    Compute the on-disk cache path for a summary, keyed by file hash,
    summarizer model, and prompt version. A change to any of the three
    components produces a different cache path, so cache invalidation
    is automatic.
    """
    cache_dir = Path(_rt_context("cache_dir"))
    return cache_dir / f"{cf.sha256}.{model_id}.{prompt_version}.summary.txt"


def summarize_file(
    cf: ContextFile,
    text: str,
    providers: Dict[str, Any],
    models: Dict[str, Any],
    agents: Dict[str, Any],
    primes: Dict[str, str],
    audit_fn: Callable,
) -> Tuple[str, str]:
    """
    Produce a summary for the given text using the Summarizer service agent.
    Caches by (file_hash, model_id, prompt_version). Returns (summary_text,
    summary_path_or_empty).

    Failure modes are soft: if summarization fails, returns ("", "") and
    the caller falls back to truncated raw text or skips the file (its
    audit entry will reflect the outcome).
    """
    summarizer = agents.get("Summarizer")
    if summarizer is None:
        return "", ""

    model_id = summarizer["model_id"]
    prompt_version = _read_summarizer_prompt_version()
    cache_path = _cache_path_for(cf, model_id, prompt_version)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    # Cache hit
    if cache_path.exists():
        try:
            cached = cache_path.read_text(encoding="utf-8")
            audit_fn({
                "kind":             "context_summary_cache_hit",
                "context_id":       cf.context_id,
                "filename":         cf.filename,
                "summary_path":     str(cache_path),
                "model_id":         model_id,
                "prompt_version":   prompt_version,
                "ts":               datetime.now().isoformat(timespec='seconds'),
            })
            return cached, str(cache_path)
        except Exception:
            # Cache file unreadable; fall through to regeneration
            pass

    # Cache miss
    system_prompt = primes.get("Summarizer", "")
    user_content = (
        f"Document filename: {cf.filename}\n"
        f"Context ID: {cf.context_id}\n"
        f"Document type: {cf.classification}\n\n"
        f"Document text:\n\"\"\"\n{text}\n\"\"\""
    )

    try:
        summary = call_model(
            model_id=model_id,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_content}],
            max_tokens=summarizer.get("max_tokens", 2000),
            temperature=summarizer.get("temperature", 0.3),
            models=models,
            providers=providers,
        )
    except Exception as e:
        sys.stderr.write(f"[CONTEXT_LOADER] Summarizer failed for {cf.filename}: "
                         f"{type(e).__name__}: {e}\n")
        audit_fn({
            "kind":             "context_summary_failed",
            "context_id":       cf.context_id,
            "filename":         cf.filename,
            "model_id":         model_id,
            "prompt_version":   prompt_version,
            "error":            f"{type(e).__name__}: {e}",
            "ts":               datetime.now().isoformat(timespec='seconds'),
        })
        return "", ""

    summary = (summary or "").strip()
    if not summary:
        audit_fn({
            "kind":             "context_summary_empty",
            "context_id":       cf.context_id,
            "filename":         cf.filename,
            "model_id":         model_id,
            "prompt_version":   prompt_version,
            "ts":               datetime.now().isoformat(timespec='seconds'),
        })
        return "", ""

    # Persist to cache
    try:
        cache_path.write_text(summary, encoding="utf-8")
    except Exception as e:
        sys.stderr.write(f"[CONTEXT_LOADER] Failed to write summary cache "
                         f"{cache_path}: {type(e).__name__}: {e}\n")

    audit_fn({
        "kind":             "context_summary_generated",
        "context_id":       cf.context_id,
        "filename":         cf.filename,
        "summary_path":     str(cache_path),
        "model_id":         model_id,
        "prompt_version":   prompt_version,
        "original_chars":   len(text),
        "summary_chars":    len(summary),
        "ts":               datetime.now().isoformat(timespec='seconds'),
    })
    return summary, str(cache_path)


# ============================================================
# Operator confirmation flow
# ============================================================

class ContextLoadDecision:
    """
    Result of the budget assessment + operator confirmation flow.
    Held by the loader so it knows what to do per file.
    """
    def __init__(self) -> None:
        self.abort:                bool = False
        self.summarize_all:        bool = False
        self.pass_through:         bool = False
        self.override_hard_limit:  bool = False


def _format_kb(n_bytes: int) -> str:
    return f"{n_bytes / 1024.0:.1f}"


def _print_assessment(
    context_files: List[ContextFile],
    extraction_results: Dict[str, Tuple[str, Optional[str]]],
    providers_in_use: List[str],
) -> Tuple[int, int, str]:
    """
    Print the budget assessment screen (without the prompt). Returns
    (total_chars, total_tokens, status). The caller drives the prompt
    based on status and CLI flags.
    """
    text_files = [cf for cf in context_files if cf.classification == "text_document"]
    data_files = [cf for cf in context_files if cf.classification == "structured_data"]
    unk_files  = [cf for cf in context_files if cf.classification == "unknown"]

    total_chars = 0
    total_tokens = 0
    print()
    print("=" * 72, file=sys.stderr)
    print("CONTEXT BUDGET ASSESSMENT", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    print(file=sys.stderr)

    if text_files:
        print("Text documents (will be loaded):", file=sys.stderr)
        for cf in text_files:
            text, fail_reason = extraction_results.get(cf.context_id, ("", "not extracted"))
            if fail_reason:
                print(f"  - {cf.context_id} {cf.filename:<40} "
                      f"FAILED: {fail_reason[:60]}", file=sys.stderr)
            else:
                chars = len(text)
                tokens = _estimate_tokens(chars)
                total_chars += chars
                total_tokens += tokens
                print(f"  - {cf.context_id} {cf.filename:<40} "
                      f"{chars:>8} chars / ~{tokens:>6} tokens", file=sys.stderr)
        print(file=sys.stderr)

    if data_files:
        print("Structured data (DETECTED but NOT LOADED in v1):", file=sys.stderr)
        for cf in data_files:
            print(f"  - {cf.context_id} {cf.filename:<40} "
                  f"{_format_kb(cf.size_bytes):>6} KB  [unsupported in v1]",
                  file=sys.stderr)
        print(file=sys.stderr)

    if unk_files:
        print("Unknown / unsupported file types (skipped):", file=sys.stderr)
        for cf in unk_files:
            print(f"  - {cf.context_id} {cf.filename:<40}", file=sys.stderr)
        print(file=sys.stderr)

    target = _rt_context("target_context_tokens")
    status = classify_budget_status(total_tokens)
    print(f"Estimated context block: ~{total_tokens} tokens "
          f"(target: {target})  STATUS: {status}", file=sys.stderr)
    print(file=sys.stderr)
    print(f"Total documents to be sent to API providers: "
          f"{len(text_files)} text document(s)", file=sys.stderr)
    print(f"Provider(s) configured: {', '.join(providers_in_use)}", file=sys.stderr)
    print(file=sys.stderr)
    print("WARNING: Do not continue if these contain student PII, confidential",
          file=sys.stderr)
    print("personnel information, medical information, legal-privileged content,",
          file=sys.stderr)
    print("or other sensitive data that should not be sent to external AI providers.",
          file=sys.stderr)
    print(file=sys.stderr)

    return total_chars, total_tokens, status


def _prompt_assessment_choice(status: str, args: argparse.Namespace) -> ContextLoadDecision:
    """
    Show the operator their choices and return their decision. Honors
    --yes-context-risk (skip privacy prompt) and --override-context-limit
    (allow BLOCKED to proceed).
    """
    decision = ContextLoadDecision()

    if status == "BLOCKED":
        if args.override_context_limit:
            print("Status: BLOCKED, but --override-context-limit was provided.",
                  file=sys.stderr)
            print("Proceeding with full context regardless of size. "
                  "Per-turn cost will be elevated.",
                  file=sys.stderr)
            print("=" * 72, file=sys.stderr)
            print(file=sys.stderr)
            decision.override_hard_limit = True
            decision.pass_through = True
            return decision
        print("Status: BLOCKED. Context exceeds hard refusal limit "
              "(context.hard_refusal_tokens).", file=sys.stderr)
        print("To proceed anyway, re-run with --override-context-limit.",
              file=sys.stderr)
        print("Otherwise, reduce context size by removing files or pre-summarizing.",
              file=sys.stderr)
        print("=" * 72, file=sys.stderr)
        decision.abort = True
        return decision

    can_pass_through = (status in ("OK", "WARNING", "HIGH"))
    recommend_summarize = (status in ("WARNING", "HIGH"))

    if args.yes_context_risk:
        # Automation path: skip the prompt entirely, apply recommended plan
        if recommend_summarize:
            decision.summarize_all = True
        else:
            decision.pass_through = True
        print(f"--yes-context-risk: skipping operator confirmation. "
              f"Applying recommended plan ("
              f"{'summarize' if recommend_summarize else 'pass through'}).",
              file=sys.stderr)
        print("=" * 72, file=sys.stderr)
        print(file=sys.stderr)
        return decision

    # Interactive prompt
    print("Choose:", file=sys.stderr)
    if recommend_summarize:
        print("  [1] Continue with recommended plan (summarize to fit target)",
              file=sys.stderr)
    else:
        print("  [1] Continue with recommended plan (pass through verbatim)",
              file=sys.stderr)
    print("  [2] Abort", file=sys.stderr)
    if can_pass_through and recommend_summarize:
        print("  [3] Continue WITHOUT summarization (use raw extracted text)",
              file=sys.stderr)
    print("=" * 72, file=sys.stderr)

    if not sys.stdin.isatty():
        print("Non-interactive stdin and --yes-context-risk not set; aborting.",
              file=sys.stderr)
        decision.abort = True
        return decision

    while True:
        try:
            choice = input("Enter choice [1/2"
                           + ("/3" if (can_pass_through and recommend_summarize) else "")
                           + "]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            decision.abort = True
            return decision

        if choice == "1":
            if recommend_summarize:
                decision.summarize_all = True
            else:
                decision.pass_through = True
            return decision
        if choice == "2":
            decision.abort = True
            return decision
        if choice == "3" and can_pass_through and recommend_summarize:
            decision.pass_through = True
            return decision
        print("Invalid choice. Try again.", file=sys.stderr)


# ============================================================
# [T0] Background block construction
# ============================================================

DOCUMENT_GROUNDED_CLAIM_RULE = (
    "DOCUMENT-GROUNDED CLAIM RULE:\n"
    "When citing information from the uploaded context documents below, "
    "you MUST mark the citation using one of these formats:\n"
    "  - [CTX-YYYYMMDD-NNN]   (machine-precise, preferred for audit)\n"
    '  - (per "filename.ext") (natural in prose)\n\n'
    "Claims using these markers will be classified as document-grounded and "
    "will NOT be web-verified by Truthseeker; they will be logged as "
    "DOCUMENT_GROUNDED_NOT_WEB_VERIFIED in the verification log.\n\n"
    "Claims that do NOT use these markers will be treated as normal factual "
    "claims and may be checked against public web sources. If your claim "
    "comes only from uploaded context and you fail to mark it, Truthseeker "
    "may classify it as UNSUPPORTED, which weakens the final artifact.\n\n"
    "Use the marker liberally and consistently."
)


def build_background_block(
    context_files: List[ContextFile],
    extracted_or_summary: Dict[str, str],
) -> str:
    """
    Construct the [T0] Background block content shown to all agents.
    Structure: rule preamble, then one labeled section per text document.
    Structured data files are listed with a note that they're available
    for future skill access but not loaded into this block.
    """
    parts: List[str] = []
    parts.append("# UPLOADED CONTEXT FOR THIS SESSION")
    parts.append("")
    parts.append(DOCUMENT_GROUNDED_CLAIM_RULE)
    parts.append("")

    text_files = [cf for cf in context_files if cf.classification == "text_document"]
    data_files = [cf for cf in context_files if cf.classification == "structured_data"]

    if text_files:
        parts.append("## Loaded text documents")
        parts.append("")
        for cf in text_files:
            body = extracted_or_summary.get(cf.context_id, "")
            if not body.strip():
                parts.append(f"### {cf.context_id} - {cf.filename}")
                parts.append("")
                parts.append(f"_Content unavailable (extraction or summarization "
                             f"failed; see audit log for details)._")
                parts.append("")
                continue
            tag = "SUMMARY" if cf.summary_used else "VERBATIM"
            parts.append(f"### {cf.context_id} - {cf.filename}  [{tag}]")
            parts.append("")
            parts.append(body.strip())
            parts.append("")

    if data_files:
        parts.append("## Detected structured-data files (not loaded in v1)")
        parts.append("")
        for cf in data_files:
            parts.append(f"- **{cf.context_id}** - `{cf.filename}` "
                         f"({_format_kb(cf.size_bytes)} KB)")
        parts.append("")
        parts.append("_These files are recorded for audit but their contents "
                     "are not in this Background block. Agents cannot directly "
                     "cite their contents; future versions will expose them via "
                     "data skills._")
        parts.append("")

    return "\n".join(parts)


# ============================================================
# Top-level orchestrator
# ============================================================

def load_context_block(
    args:           argparse.Namespace,
    context_files:  List[ContextFile],
    providers:      Dict[str, Any],
    models:         Dict[str, Any],
    agents:         Dict[str, Any],
    primes:         Dict[str, str],
    audit_fn:       Callable,
) -> Tuple[Optional[str], Dict[str, Any]]:
    """
    Run the full Pass 2 flow: extraction, assessment, operator confirmation,
    optional summarization, [T0] Background block construction.

    Returns:
      (background_block_text, assessment_dict)

    background_block_text is None if no files were loaded, all files
    failed, or the operator aborted.

    On abort, raises ContextLoadAborted with an explanatory message.
    The runtime catches and converts to fatal().
    """
    if not context_files:
        return None, {}

    # Extract text from all text_document files
    extraction_results: Dict[str, Tuple[str, Optional[str]]] = {}
    text_files = [cf for cf in context_files if cf.classification == "text_document"]
    for cf in text_files:
        text, fail_reason = extract_text_for_file(cf)
        extraction_results[cf.context_id] = (text, fail_reason)
        cf.original_chars = len(text)
        if fail_reason:
            cf.parse_status = "extraction_failed"
            cf.failure_reason = fail_reason
            audit_fn({
                "kind":           "context_extraction_failed",
                "context_id":     cf.context_id,
                "filename":       cf.filename,
                "reason":         fail_reason,
                "ts":             datetime.now().isoformat(timespec='seconds'),
            })
        else:
            cf.parse_status = "extracted"
            cf.token_estimate = _estimate_tokens(len(text))

    # Assessment + decision
    providers_in_use = sorted({models[a["model_id"]]["provider"] for a in agents.values()})
    total_chars, total_tokens, status = _print_assessment(
        context_files, extraction_results, providers_in_use,
    )
    decision = _prompt_assessment_choice(status, args)
    assessment = {
        "status":           status,
        "total_chars":      total_chars,
        "total_tokens":     total_tokens,
        "target_tokens":    _rt_context("target_context_tokens"),
        "soft_warning":     _rt_context("soft_warning_tokens"),
        "hard_refusal":     _rt_context("hard_refusal_tokens"),
        "decision":         (
            "abort"        if decision.abort
            else "summarize" if decision.summarize_all
            else "pass_through" if decision.pass_through
            else "unknown"
        ),
        "override_hard_limit": decision.override_hard_limit,
        "ts":               datetime.now().isoformat(timespec='seconds'),
    }
    audit_fn({
        "kind":              "context_budget_assessment",
        **assessment,
    })

    if decision.abort:
        raise ContextLoadAborted(
            "Context load aborted by operator (or non-interactive without "
            "--yes-context-risk)."
        )

    # Summarize if the decision calls for it
    extracted_or_summary: Dict[str, str] = {}
    if decision.summarize_all:
        print("Summarizing context documents...", file=sys.stderr)
        for cf in text_files:
            raw_text, fail_reason = extraction_results[cf.context_id]
            if fail_reason or not raw_text.strip():
                extracted_or_summary[cf.context_id] = ""
                cf.summary_used = False
                continue
            summary, summary_path = summarize_file(
                cf, raw_text, providers, models, agents, primes, audit_fn,
            )
            if summary.strip():
                extracted_or_summary[cf.context_id] = summary
                cf.summary_used = True
                cf.summary_path = summary_path
                cf.injected_chars = len(summary)
                print(f"  {cf.context_id} {cf.filename}: "
                      f"{cf.original_chars} -> {cf.injected_chars} chars",
                      file=sys.stderr)
            else:
                fallback = raw_text
                extracted_or_summary[cf.context_id] = fallback
                cf.summary_used = False
                cf.injected_chars = len(fallback)
                print(f"  {cf.context_id} {cf.filename}: "
                      f"summarization failed; using raw text",
                      file=sys.stderr)
        print(file=sys.stderr)
    else:
        # Pass-through path: use raw text for each file
        for cf in text_files:
            raw_text, fail_reason = extraction_results[cf.context_id]
            if fail_reason:
                extracted_or_summary[cf.context_id] = ""
                cf.summary_used = False
            else:
                extracted_or_summary[cf.context_id] = raw_text
                cf.summary_used = False
                cf.injected_chars = len(raw_text)

    background_text = build_background_block(context_files, extracted_or_summary)
    audit_fn({
        "kind":              "context_block_built",
        "char_count":        len(background_text),
        "token_estimate":    _estimate_tokens(len(background_text)),
        "text_doc_count":    len(text_files),
        "summarized":        decision.summarize_all,
        "ts":                datetime.now().isoformat(timespec='seconds'),
    })
    return background_text, assessment
