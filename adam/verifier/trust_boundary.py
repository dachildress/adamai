"""
Trust boundary for Director-provided inputs.

Architectural principle: operational parameters supplied by the Director
(seed contents, allowlist entries, env config values, CLI skill args,
context file metadata) are AUTHORITATIVE BY DIRECTOR INTENT. They are
not factual claims about the external world and must not be routed
through web verification.

Truthseeker exists to keep advisory agents honest about external factual
claims they introduce -- "research consistently shows X," "Y is verified
by Z source." It does not exist to second-guess the operating environment.

Before this trust boundary existed, Truthseeker's regex extractor would
pull substrings like "dachildress@amherst.k12.va.us" out of advisory
turns and web-search them. Email addresses are not the kind of thing the
web indexes systematically, so the verifier would return UNSUPPORTED.
Downstream agents (Operator, Synthesizer) would then treat the address
as "unconfirmed" -- a category error that, in one observed session,
caused the wrap-up Operator to pick action: draft instead of action: send
because the recipient (which was on the configured allowlist) had been
marked UNSUPPORTED by the verifier.

The trust registry fixes this by category, not by pattern. Any string
the Director provided as input is in the registry. Claim candidates
whose text appears in the registry are skipped before verification ever
runs.

Multi-instance note (step 7): The active registry is currently held in a
module-level variable, mirroring the pre-refactor pattern. This works
for single-process single-session usage (the current ADAM CLI mode) but
will need to migrate to SessionContext when ADAM runs multiple concurrent
sessions in one process. The set_active_registry / get_active_registry
accessors make that migration a single-file change.
"""
from __future__ import annotations

import os
import re
from typing import Dict, Optional, Set


# Minimum length for a trust-registry entry to be checked against claim
# candidates. Prevents very short strings (CTX-IDs prefixes, single
# words) from causing false-positive filtering when they happen to
# appear inside unrelated text.
_TRUST_REGISTRY_MIN_LENGTH = 6


class TrustRegistry:
    """
    Read-only registry of strings the Director provided as session
    input. Built once at session startup; checked against every claim
    candidate before verification runs.

    Membership semantics: case-insensitive substring presence. A
    candidate claim with text "the address dachildress@amherst.k12.va.us
    has been confirmed" is filtered if the registry contains
    "dachildress@amherst.k12.va.us" -- the candidate's text contains a
    trusted string. This catches the common case where the regex
    extractor pulls fragments of sentences that include trusted
    identifiers.

    The registry is built from these sources:
      - Seed text (the full Director instruction)
      - Recipient allowlist entries (ADAM_EMAIL_RECIPIENT_ALLOWLIST)
      - CLI-provided skill argument values (--skill-arg)
      - Context file IDs and filenames
      - A curated set of env config values (SMTP host, FROM address)

    Each source contributes its strings to a single frozenset of
    normalized lowercase strings, with entries shorter than
    _TRUST_REGISTRY_MIN_LENGTH excluded to prevent false positives.
    """
    __slots__ = ("_trusted", "_source_counts")

    def __init__(self, trusted_strings: Set[str], source_counts: Dict[str, int]):
        self._trusted = frozenset(trusted_strings)
        self._source_counts = dict(source_counts)

    @property
    def size(self) -> int:
        return len(self._trusted)

    @property
    def source_counts(self) -> Dict[str, int]:
        return dict(self._source_counts)

    def contains(self, text: str) -> bool:
        """Return True if any trusted string appears in text (case-insensitive)."""
        if not text or not self._trusted:
            return False
        lowered = text.lower()
        for trusted in self._trusted:
            if trusted in lowered:
                return True
        return False


def build_trust_registry(
    seed_text:                  str,
    skill_args_parsed:          Optional[Dict[str, Dict[str, Dict[str, str]]]],
    context_files_by_id:        Dict[str, object],     # ContextFile, kept opaque
    context_files_by_filename:  Dict[str, object],
) -> TrustRegistry:
    """
    Collect Director-provided strings into a TrustRegistry built once
    per session. Called from main() just before the deliberation loop
    begins, after seed loading, context file detection, and skill args
    parsing have all completed.

    The env-derived entries are read at construction time. Re-reading
    them later would let mid-session env changes affect filtering,
    which is not a supported workflow.

    `context_files_by_id` and `context_files_by_filename` are typed as
    Dict[str, object] to avoid forcing a circular import on
    adam.context.file_extractor (which holds the ContextFile dataclass).
    Only the keys are consumed; the values are not introspected.
    """
    trusted: Set[str] = set()
    counts: Dict[str, int] = {
        "seed_tokens":         0,
        "allowlist_entries":   0,
        "skill_arg_values":    0,
        "context_identifiers": 0,
        "env_config_values":   0,
    }

    def _add(value: str, source_key: str) -> None:
        if not value:
            return
        normalized = value.strip().lower()
        if len(normalized) < _TRUST_REGISTRY_MIN_LENGTH:
            return
        if normalized in trusted:
            return
        trusted.add(normalized)
        counts[source_key] = counts.get(source_key, 0) + 1

    # ---- seed text ----
    if seed_text:
        _add(seed_text, "seed_tokens")
        EMAIL_RE  = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
        URL_RE    = re.compile(r"https?://\S+")
        QUOTED_RE = re.compile(r"[\"']([^\"'\n]{6,200})[\"']")
        for m in EMAIL_RE.finditer(seed_text):
            _add(m.group(0), "seed_tokens")
        for m in URL_RE.finditer(seed_text):
            _add(m.group(0), "seed_tokens")
        for m in QUOTED_RE.finditer(seed_text):
            _add(m.group(1), "seed_tokens")

    # ---- recipient allowlist ----
    allowlist_raw = os.environ.get("ADAM_EMAIL_RECIPIENT_ALLOWLIST", "")
    if allowlist_raw:
        for entry in allowlist_raw.split(","):
            entry = entry.strip()
            if entry:
                _add(entry, "allowlist_entries")
                # Wildcard entries imply trust in the domain part.
                if entry.startswith("*@"):
                    _add(entry[2:], "allowlist_entries")

    # ---- CLI-provided skill args ----
    if skill_args_parsed:
        for skill_block in skill_args_parsed.values():
            if not isinstance(skill_block, dict):
                continue
            for action_block in skill_block.values():
                if not isinstance(action_block, dict):
                    continue
                for value in action_block.values():
                    if isinstance(value, str):
                        _add(value, "skill_arg_values")

    # ---- context file identifiers and filenames ----
    for ctx_id in context_files_by_id.keys():
        _add(ctx_id, "context_identifiers")
    for filename in context_files_by_filename.keys():
        _add(filename, "context_identifiers")

    # ---- env config values ----
    for env_var in (
        "ADAM_EMAIL_FROM",
        "ADAM_SMTP_HOST",
        "ADAM_SMTP_USERNAME",
        "SEARXNG_URL",
    ):
        value = os.environ.get(env_var, "").strip()
        if value:
            _add(value, "env_config_values")

    return TrustRegistry(trusted, counts)


# ============================================================
# Active-registry accessor pattern
# ============================================================
#
# The session's TrustRegistry is set once at startup via
# set_active_registry() and consumed by extract_claim_candidates()
# and verify_claim() via get_active_registry(). The accessor pattern
# (rather than direct global access) gives us a clean migration path
# to SessionContext in step 7 -- only these two functions need to
# change at that point.

_active_registry: Optional[TrustRegistry] = None


def set_active_registry(registry: Optional[TrustRegistry]) -> None:
    """
    Called once during session startup to register the session's
    TrustRegistry. Pass None to disable trust-boundary filtering
    (e.g. when --no-verify is set).
    """
    global _active_registry
    _active_registry = registry


def get_active_registry() -> Optional[TrustRegistry]:
    """
    Used by claim_extractor and policy_rules to check the active
    registry. Returns None when verification is disabled or the
    registry has not been built.
    """
    return _active_registry
