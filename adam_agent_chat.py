#!/usr/bin/env python3
"""
adam_agent_chat.py

Multi-agent ADAM simulation with config-driven providers, external-grounded
verification, and a hardened predicate router.

ARCHITECTURE
============
Six conversational agents collaborate in deliberation:
  - Logician, Seeker, Visionary  -- advisory rotation
  - Synthesizer                  -- scheduled integration on cadence
  - Sentinel                     -- predicate-triggered risk gate
  - Operator                     -- predicate-triggered execution gate

All agent <-> model bindings live in config/agents.json + config/models.json
+ config/providers.json. Swapping a provider or model for any agent is a
config-file change; no Python touched.

Truthseeker is NOT in the conversational rotation. It is an automatic
external-grounded verification service that runs after every content turn:
  1. Claim extraction: regex first-pass + Haiku LLM classification
  2. Per claim: SearXNG search -> trafilatura page fetch -> Haiku per-source
     judgment -> deterministic policy rules -> structured status
  3. Verification summary is injected into the transcript as a [Truthseeker]
     message so subsequent agents see the findings.
  4. Operator is told via router-note to omit UNSUPPORTED/CONTRADICTED claims
     and mark PARTIALLY_VERIFIED claims as 'pending verification'.

SETUP
=====
    pip install openai anthropic requests trafilatura
    cp .env.example .env  # then fill in keys + SEARXNG_URL

KILL NOTICE
===========
    Ctrl+C once  -> finish current agent, then stop
    Ctrl+C twice -> exit immediately
"""

import argparse
import hashlib
import importlib
import json
import os
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

# ============================================================
# Verifier subsystem (extracted in refactor step 2)
# ============================================================
#
# The Truthseeker verification engine lives in adam.verifier. The
# names below are re-exported into this module's namespace so the
# existing call sites (verify_claim, extract_claim_candidates, etc.)
# work without modification. The single-file inline definitions that
# used to live in this module have been removed.
#
# Two registration calls run later in main(): set_runtime_config and
# set_active_registry. They wire the verifier's session-scoped state.
from adam.verifier import (
    TRUTHSEEKER_MODEL_ID,
    TRUTHSEEKER_TEMPERATURE,
    TrustRegistry,
    build_trust_registry,
    set_active_registry,
    get_active_registry,
    set_runtime_config as _verifier_set_runtime_config,
    CLAIM_CANDIDATE_PATTERNS,
    extract_claim_candidates,
    extract_document_grounded_claims,
    extract_structured_claims,
    searxng_search,
    trafilatura_extract,
    classify_source_tier,
    verify_claim,
    apply_verification_policy,
    format_verification_summary,
    format_verification_for_transcript,
)
# _extract_first_json_value is the JSON-tolerant value extractor used
# by the verifier internally AND by _extract_wrap_up_block in this
# module's wrap-up parsing logic. It lives in adam.verifier.web_search
# until a later refactor step gives it a more neutral home.
from adam.verifier.web_search import _extract_first_json_value

# ============================================================
# Context subsystem (extracted in refactor step 3)
# ============================================================
#
# The Context Loader lives in adam.context. The names below are
# re-exported into this module's namespace so existing call sites
# (detect_context_files, build_context_state, load_context_block,
# extract_text_for_file, etc.) work without modification.
#
# One registration call runs later in main(): set_runtime_config
# wires the context config block. The load_context_block raises
# ContextLoadAborted instead of calling fatal() directly.
from adam.context import (
    TEXT_DOCUMENT_EXTENSIONS,
    STRUCTURED_DATA_EXTENSIONS,
    ContextFile,
    detect_context_files,
    extract_text_for_file,
    build_context_state,
    classify_budget_status,
    summarize_file,
    ContextLoadDecision,
    build_background_block,
    load_context_block,
    DOCUMENT_GROUNDED_CLAIM_RULE,
    PROMPT_VERSION_RE,
    set_runtime_config as _context_set_runtime_config,
)
# Internal helpers re-exported for backward compatibility with call sites
# in this file (mostly _estimate_tokens which is used outside the context
# block too).
from adam.context.budget_manager import _estimate_tokens
from adam.context.file_extractor import (
    _classify_file, _hash_file, _enumerate_context_files,
)

# ============================================================
# Skills runtime subsystem (extracted in refactor step 4)
# ============================================================
#
# The Skills runtime lives in adam.skills_runtime. The names below
# are re-exported into this module's namespace so existing call sites
# (discover_skills, parse_skill_calls, SkillRuntime, parse_skill_args,
# etc.) work without modification.
#
# One registration call runs later in main(): set_runtime_config wires
# the skills config block. The package has no LLM dependencies, so no
# lazy imports are needed.
from adam.skills_runtime import (
    SkillManifestError,
    SkillManifest,
    SkillCatalog,
    discover_skills,
    build_skill_manifest_block,
    SKILL_CALL_FENCE_RE,
    SKILL_CALL_OPEN_FENCE_RE,
    ParsedSkillCall,
    parse_skill_calls,
    SkillRuntime,
    parse_skill_args,
    format_skill_args_for_display,
    build_operator_skill_args_note,
    set_runtime_config as _skills_set_runtime_config,
)

# ============================================================
# Generic LLM dispatch (extracted in refactor step 5a)
# ============================================================
#
# Provider-agnostic call_model() and its supporting helpers live in
# adam.core.client_dispatch. This is the single entry point for all
# LLM invocations across the runtime, the verifier, the context
# subsystem's summarizer, and any future skill that needs a model.
#
# Step 5a removed the lazy-import shims that adam.verifier.web_search
# and adam.context.budget_manager were using to defer this import:
# now they import call_model normally at module-load time.
from adam.core.client_dispatch import (
    call_model,
    get_provider_client,
)

# ============================================================
# Config loader (extracted in refactor step 5b-1)
# ============================================================
#
# Config loading and validation moved to adam.core.config_loader.
# The runtime imports _RUNTIME_CONFIG and _rt() back so existing call
# sites work without changes. Critical: _RUNTIME_CONFIG is mutated
# in place by load_and_validate_runtime_config (clear + update), not
# rebound, so all importers share the same dict object.
from adam.core.config_loader import (
    # State
    _RUNTIME_CONFIG,
    # Accessor
    _rt,
    get_runtime_config,
    # Loaders
    _load_json,
    load_and_validate_config,
    load_and_validate_runtime_config,
    # Path constants
    CONFIG_DIR,
    PROVIDERS_PATH,
    MODELS_PATH,
    AGENTS_PATH,
    RUNTIME_PATH,
)

# ============================================================
# Predicate router (extracted in refactor step 5b-2)
# ============================================================
#
# The Sentinel/Operator predicate router and the advisory rotation
# logic moved to adam.core.router. The runtime imports the public
# names back so existing call sites work unchanged.
#
# ADVISORY_CYCLE uses the same mutate-in-place pattern as _RUNTIME_CONFIG:
# set_advisory_cycle() updates the list owned by adam.core.router, and
# the runtime accesses the same list object via the ADVISORY_CYCLE alias.
from adam.core.router import (
    # Constants
    SENTINEL_TRIGGERS,
    GATE_AGENTS,
    NON_TRIGGERING_SPEAKERS,
    SENTINEL_CONCERN_COOLDOWN_TURNS,
    # Predicate helpers
    sentinel_concern,
    # Registry + speaker selection
    SentinelRegistry,
    select_next_speaker,
    # Advisory cycle
    derive_advisory_cycle,
    set_advisory_cycle,
    get_advisory_cycle,
    ADVISORY_CYCLE,
    # Artifact-block helpers (called by select_next_speaker for the
    # wrap-up Operator turn)
    build_artifact_skill_block,
    build_artifact_mode_rule,
)

# ============================================================
# Session lifecycle (extracted in refactor step 5b-3)
# ============================================================
#
# Session-scoped state, signal handlers, .env loader, user-id
# validation, wrap-up timing, and the new SessionContext class.
from adam.core.session import (
    # Path constants
    DOTENV_PATH,
    LOG_DIR,
    # Lifecycle helpers
    compute_wrap_up_triggers,
    derive_user_id,
    validate_user_id,
    load_dotenv,
    # State classes
    WrapUpState,
    DirectorMessage,
    DirectorState,
    StopState,
    # Director input parsing
    DIRECTOR_HELP_TEXT,
    format_director_transcript_entry,
    # Signal handling
    handle_sigint,
    # The migration target
    SessionContext,
)

# ============================================================
# CLI (extracted in refactor step 5b-3)
# ============================================================
from adam.core.cli import (
    parse_args,
    apply_runtime_defaults,
    load_seed,
    fatal,
)
# _seed_source_label is private to cli but the runtime banner needs it
from adam.core.cli import _seed_source_label

# ============================================================
# Loop helpers (extracted in refactor step 5b-3)
# Deliberation loop (extracted in refactor step 5b-4)
# ============================================================
#
# Step 5b-3 lifted the supporting helpers (prime loading, transcript
# message construction, wrap-up block parsing, session_state builder)
# into adam.core.loop while keeping the loop body inline in main().
#
# Step 5b-4 lifted the loop body itself into run_deliberation_loop()
# in the same module. main() now constructs the per-session state,
# hands it to run_deliberation_loop(), and reads the returned
# _LoopState for the end-of-run summary and session_state.json.
from adam.core.loop import (
    load_agent_primes,
    resolve_agent_call_params,
    build_transcript_messages,
    _extract_wrap_up_block,
    _extract_operator_continue_block,
    extract_continuation_signal,
    _extract_decisions_from_audit,
    _summarize_verification_audit,
    _summarize_sentinel_concerns,
    _build_session_state,
    run_deliberation_loop,
    _LoopState,
)
from adam.core.governance_invariants import (
    GOVERNANCE_BOUNDARY_END_REASON,
    evaluate_self_modification_boundary,
)
from adam.core.empty_termination import (
    REFUSAL_TERMINATED_END_REASON,
    evaluate_unsafe_execution_boundary,
)

# ============================================================
# Shared exceptions (extracted in refactor step 3)
# ============================================================
#
# ConfigError moved from this file to adam.core.exceptions so the
# context subpackage can raise it without an upward import dependency
# on adam_agent_chat. ContextLoadAborted is new in step 3.
from adam.core.exceptions import ConfigError, ContextLoadAborted
# Backward-compatibility shim: a couple of call sites in this file still
# reference _TRUST_REGISTRY directly (e.g. for the "registry built" audit
# event). Those sites are migrated to get_active_registry() below.


# ============================================================
# Paths & defaults
# ============================================================

# CONFIG_DIR, PROVIDERS_PATH, MODELS_PATH, AGENTS_PATH, RUNTIME_PATH
# moved to adam.core.config_loader in refactor step 5b-1.
# DOTENV_PATH and LOG_DIR moved to adam.core.session in step 5b-3.
# All five are imported at the top of this file.

# Session defaults (max_turns, delay_seconds, history_messages, synth_cadence)
# live in config/runtime.json under "session_defaults". The seed text lives
# in prompts/seed.md (path also configurable via runtime.json:
# session_defaults.seed_file). CLI flags override both.
# Resolution order at startup:
#   max_turns / delay / history_messages / synth_cadence:
#     1. CLI flag (--max-turns etc.) if provided
#     2. runtime.json session_defaults.<key>
#   seed:
#     1. --seed "..." CLI flag if provided
#     2. Contents of file at session_defaults.seed_file (default: prompts/seed.md)
#     3. Fail with clear error if neither




# Truthseeker architectural choices (NOT user-configurable):
# These are deliberate design decisions, not tuning knobs. Changing them
# would alter Truthseeker's behavioral guarantees. Operational tuning
# values (timeouts, parallelism, claim cap, etc.) live in runtime.json.
  # TRUTHSEEKER_MODEL_ID and TRUTHSEEKER_TEMPERATURE imported from
# adam.verifier at the top of this file.
# _RUNTIME_CONFIG and _rt() imported from adam.core.config_loader at
# the top of this file (refactor step 5b-1).
















# ============================================================
# Truthseeker: claim extraction (regex first-pass)
# ============================================================








# ============================================================
# CLI
# ============================================================







# ============================================================
# Agent loading
# ============================================================


def main() -> None:
    load_dotenv()
    args = parse_args()
    signal.signal(signal.SIGINT, handle_sigint)

    # --- Load and validate config ---
    try:
        providers, models, agents = load_and_validate_config()
        load_and_validate_runtime_config()
    except ConfigError as e:
        fatal(str(e))

    # Register the truthseeker config block with the verifier subsystem.
    # The verifier reads its tunables (search_top_n, parallel_workers,
    # judgment_cache_enabled, etc.) via its own _rt_truthseeker helper
    # rather than reaching into this module's _RUNTIME_CONFIG global.
    _verifier_set_runtime_config(_RUNTIME_CONFIG.get("truthseeker", {}))

    # Register the context config block with the context subsystem.
    # Same pattern as the verifier: the context subsystem reads its
    # tunables (target_context_tokens, soft_warning_tokens, etc.) via
    # its own _rt_context helper.
    _context_set_runtime_config(_RUNTIME_CONFIG.get("context", {}))

    # Register the skills config block with the skills runtime subsystem.
    # Same pattern as verifier and context: the skills runtime reads
    # its tunables (max_content_size_bytes, etc.) via its own
    # _rt_skills helper.
    _skills_set_runtime_config(_RUNTIME_CONFIG.get("skills", {}))

    # Apply runtime.json session_defaults to any CLI args that were not
    # explicitly provided. Then resolve the seed from --seed or the
    # configured seed file.
    apply_runtime_defaults(args)
    try:
        args.seed = load_seed(args)
    except ConfigError as e:
        fatal(str(e))

    # Parse generic --skill-arg flags (skill.action.arg=value, repeatable).
    # ConfigError propagates a clear message that names the offending value
    # so the operator can fix the CLI invocation. audit_fn is None at this
    # stage (the audit file path is established later, after we know the
    # session stamp); duplicate-override warnings get re-audited downstream
    # when we have an audit function available.
    try:
        args.skill_args_parsed = parse_skill_args(args.skill_arg, audit_fn=None)
    except ConfigError as e:
        fatal(str(e))

    # Context Loader Pass 1: enumerate, classify, hash files referenced by
    # --context-dir / --context-file. The files are detected and audit-logged
    # but their content is NOT yet read or injected (that's Pass 2). This is
    # the foundation so Pass 2 can plug in cleanly.
    try:
        context_files: List[ContextFile] = detect_context_files(args)
    except ConfigError as e:
        fatal(str(e))

    primes         = load_agent_primes(agents)
    advisory_cycle = derive_advisory_cycle(agents)
    set_advisory_cycle(advisory_cycle)

    if not advisory_cycle:
        fatal("No advisory-role agents defined in agents.json. At least one is required.")

    # --- Skill discovery (Pass 1) ---
    # Discover skills under skills/ per runtime.json. Build the manifest
    # block once and append it to every allowed-caller agent's prime so
    # they see the same skill catalog. agents NOT in any skill's
    # allowed_callers don't get the manifest, which keeps the dialectic
    # clean (advisory agents don't try to invoke skills).
    #
    # v5 multi-user: --disable-skill flags merge into runtime.json's
    # disabled_skills list. This is how per-user role policy reaches
    # ADAM -- the GUI translates the user's role -> skills_denied
    # into CLI flags at spawn time. Effect is identical to permanent
    # disabling: agents never see the skill, runtime rejects invocations.
    # Future: replace with a per-session policy file (see design doc).
    #
    # Note on ordering: we cannot log() here because the session
    # context (which owns the log function) is bound ~100 lines below.
    # The message is stashed in a local and emitted right after
    # `log = ctx.log` so it lands in the proper session log file in
    # turn-zero order rather than going to bare stdout. An earlier
    # version of this code used log() directly here and crashed with
    # an UnboundLocalError on every spawn that received a
    # --disable-skill flag (i.e., every pilot session).
    skills_cfg = dict(_RUNTIME_CONFIG.get("skills", {}))
    cli_disabled = getattr(args, "disable_skill", None) or []
    _deferred_skill_denial_log: Optional[str] = None
    if cli_disabled:
        cfg_disabled = list(skills_cfg.get("disabled_skills", []))
        merged = sorted(set(cfg_disabled) | set(cli_disabled))
        skills_cfg["disabled_skills"] = merged
        _deferred_skill_denial_log = (
            f"Per-session skill denial (from --disable-skill): {sorted(cli_disabled)}"
        )
    skill_catalog = discover_skills(skills_cfg)
    # Caller-filtered advertisement: tell each agent ONLY about the executable
    # skills its role is allowed to call (matching the runtime's allowed_callers
    # enforcement), so it never emits a call the runtime would reject. Agents
    # with no invocable skills get no skills section at all. This is the
    # INITIATION half only — every actual call is still fully governed by the
    # runtime (allowed_callers) and the skill handler (profile capability, source
    # scope, denied_fields/aggregate-only, budgets).
    for agent_name in primes:
        block = build_skill_manifest_block(skill_catalog, agent_name)
        if block:
            primes[agent_name] = primes[agent_name] + "\n\n" + block

    searxng_url = os.environ.get("SEARXNG_URL", "http://localhost:8080").strip()

    # --- Director identity (required) ---
    # ADAM refuses to start without an identified Director. This protects API
    # spend and forces upstream auth (Google/Microsoft OAuth, SAML) to be
    # wired before anyone can run ADAM in production. For single-user CLI
    # use, the operator sets these in .env. When the GUI eventually wires
    # OAuth, these env vars become irrelevant -- the authenticated session
    # supplies the same fields from the auth token.
    # v5 multi-user: the GUI passes the authenticated user's identity via
    # --director-user-id / --director-email / --director-name. When the
    # GUI is the caller, those flags override anything in .env, so each
    # session's logs land under the authenticated user's directory
    # (logs/<username>/<session_id>/) regardless of what .env says.
    #
    # When ADAM is invoked from the CLI directly (no GUI), the flags are
    # absent and we fall back to .env. This preserves the v4 single-user
    # workflow for command-line invocations.
    #
    # The .env values are also kept as a fallback if the GUI passes only
    # a partial identity (e.g. user_id and name but no explicit email --
    # though the GUI in fact passes all three). This belt-and-suspenders
    # behavior avoids surprises during transitions.
    raw_director_id    = (getattr(args, "director_user_id", None) or
                          os.environ.get("ADAM_DEFAULT_DIRECTOR", "")).strip()
    raw_director_email = (getattr(args, "director_email", None) or
                          os.environ.get("ADAM_DEFAULT_DIRECTOR_EMAIL", "")).strip()
    raw_director_name  = (getattr(args, "director_name", None) or
                          os.environ.get("ADAM_DEFAULT_DIRECTOR_DISPLAY_NAME", "")).strip()

    if not raw_director_id:
        raise ConfigError(
            "Director user_id is not set. ADAM refuses to start "
            "without an identified Director. Either set ADAM_DEFAULT_DIRECTOR "
            "in your .env file (e.g. ADAM_DEFAULT_DIRECTOR=childrda) or pass "
            "--director-user-id on the command line. The GUI passes this "
            "automatically based on the authenticated user."
        )
    if not raw_director_email:
        raise ConfigError(
            "Director email is not set. The full email address is required "
            "for audit and metadata. Either set ADAM_DEFAULT_DIRECTOR_EMAIL "
            "in your .env file (e.g. ADAM_DEFAULT_DIRECTOR_EMAIL="
            "childrda@lcps.k12.va.us) or pass --director-email on the "
            "command line."
        )

    director_user_id = validate_user_id(derive_user_id(raw_director_id))
    director_email   = raw_director_email
    director_display = raw_director_name if raw_director_name else director_user_id

    # --- Session context (refactor step 5b-3) ---
    # SessionContext owns the session id, started_at, per-session log
    # directory, the seven log/state/artifact paths, and the audit-writer
    # methods (ctx.log, ctx.audit, ctx.verification_audit). We bind local
    # names to its attributes so the rest of main()'s code can keep
    # referring to log_path, audit_path, etc. without rewrites.
    #
    # Director source resolution still happens below (after this block)
    # because --director-name CLI flag may override the env-derived display
    # name. The CLI override is applied via ctx.display_name reassignment
    # before the banner prints.
    # Part 9: validate --session-id if provided. Must be a clean string
    # safe to use as a directory name on every platform we deploy to.
    # The GUI is expected to send UUID-style strings, but other clients
    # could call ADAM directly with this flag, so validate here too.
    requested_session_id = getattr(args, "session_id", None)
    if requested_session_id is not None:
        if not requested_session_id or len(requested_session_id) > 64:
            print("ERROR: --session-id must be 1-64 characters", file=sys.stderr)
            sys.exit(1)
        if any(c in requested_session_id for c in "/\\:.* \t\n\r"):
            print("ERROR: --session-id contains forbidden characters "
                  "(path separators, whitespace, or '.')", file=sys.stderr)
            sys.exit(1)

    ctx = SessionContext.create(
        director_user_id      = director_user_id,
        director_email        = director_email,
        director_display_name = director_display,
        director_source       = "env",   # may be overridden by --director-name below
        session_id            = requested_session_id,
    )

    session_id         = ctx.session_id
    session_started_at = ctx.started_at
    session_dir        = ctx.session_dir
    log_path           = ctx.log_path
    audit_path         = ctx.audit_path
    verification_path  = ctx.verification_path
    skills_log_path    = ctx.skills_log_path
    events_path        = ctx.events_path
    session_state_path = ctx.session_state_path
    artifacts_root     = ctx.artifacts_root

    # Bind the writer methods as bare names so existing call sites
    # (log("..."), audit({...}), verification_audit(...)) work unchanged.
    log                = ctx.log
    audit              = ctx.audit
    verification_audit = ctx.verification_audit

    # v5 multi-user: emit any deferred messages from the skill-config
    # block above. These were stashed because `log` wasn't bound yet
    # when the skill catalog was being assembled. Order matters --
    # this message should appear before the session banner so the
    # log reads top-down chronologically.
    if _deferred_skill_denial_log:
        log(_deferred_skill_denial_log)

    log("=" * 72)
    log("ADAM MULTI-AGENT SIMULATION")
    log(f"Started:              {datetime.now().isoformat(timespec='seconds')}")
    log(f"Director:             {director_display} ({director_email})")
    log(f"Director user_id:     {director_user_id}")
    log(f"Session id:           {session_id}")
    log(f"Session directory:    {session_dir}")
    log(f"Max turns:            {args.max_turns}")
    log(f"Synth cadence:        every {args.synth_cadence} advisory turns")
    log(f"History window:       {args.history_messages} messages")
    log(f"Delay between turns:  {args.delay}s")
    log(f"Truthseeker:          {'DISABLED (--no-verify)' if args.no_verify else 'ENABLED'}")
    log(f"SearXNG URL:          {searxng_url}")
    log(f"Truthseeker model:    {TRUTHSEEKER_MODEL_ID}")
    log(f"Seed source:          {_seed_source_label(args)}")
    log(f"Log file:             {log_path}")
    log(f"Audit log:            {audit_path}")
    log(f"Verification log:     {verification_path}")
    log("Session defaults (config/runtime.json, overridable by CLI flags):")
    log(f"  max_turns                = {_rt('session_defaults', 'max_turns')}")
    log(f"  delay_seconds            = {_rt('session_defaults', 'delay_seconds')}")
    log(f"  history_messages         = {_rt('session_defaults', 'history_messages')}")
    log(f"  synth_cadence            = {_rt('session_defaults', 'synth_cadence')}")
    log(f"  seed_file                = {_rt('session_defaults', 'seed_file')}")
    log("Truthseeker runtime settings (config/runtime.json):")
    log(f"  search_top_n             = {_rt('truthseeker', 'search_top_n')}")
    log(f"  max_claims_per_turn      = {_rt('truthseeker', 'max_claims_per_turn')}")
    log(f"  parallel_workers         = {_rt('truthseeker', 'parallel_workers')}")
    log(f"  page_fetch_timeout       = {_rt('truthseeker', 'page_fetch_timeout_seconds')}s")
    log(f"  search_http_timeout      = {_rt('truthseeker', 'search_http_timeout_seconds')}s")
    log(f"  source_excerpt_chars     = {_rt('truthseeker', 'source_excerpt_chars')}")
    log(f"  skip_tier_5_sources      = {_rt('truthseeker', 'skip_tier_5_sources')}")
    log(f"  judgment_cache_enabled   = {_rt('truthseeker', 'judgment_cache_enabled')}")
    log("Provider retry settings (config/providers.json):")
    for pid, p in providers.items():
        r = p["retry"]
        log(f"  {pid:<10} max_attempts={r['max_attempts']} "
            f"backoff={r['initial_backoff_seconds']}s..{r['max_backoff_seconds']}s "
            f"(x{r['backoff_multiplier']}) retry_after_header={r['respect_retry_after_header']}")
    log("Agents:")
    for name, a in agents.items():
        model_id, max_tokens, temperature = resolve_agent_call_params(name, agents, models)
        provider = models[model_id]["provider"]
        log(f"  - {name:<12} role={a['role']:<22} {provider:<10} {model_id:<28} max={max_tokens:<5} temp={temperature}")
    log(f"  - {'Truthseeker':<12} role={'service':<22} {models[TRUTHSEEKER_MODEL_ID]['provider']:<10} {TRUTHSEEKER_MODEL_ID:<28} max=varies  temp={TRUTHSEEKER_TEMPERATURE}")
    log("=" * 72)
    log()
    log("Press Ctrl+C once for graceful stop, twice for hard stop.")
    log()

    # ============================================================
    # Events stream: session-level setup events
    # ============================================================
    #
    # Three events are emitted here to give GUI subscribers the initial
    # state they need to render the session: the skill catalog, then a
    # session_started "header" event with director identity and basic
    # parameters. context_loaded fires later (after detection) and
    # trust_registry_built fires when the registry is constructed.
    #
    # These are emitted AFTER the banner so the live GUI mirrors the
    # information the human operator sees at session start. Order
    # matters for live subscribers; skill_registry_loaded fires before
    # session_started so the Skills panel populates first.
    ctx.emit_event("skill_registry_loaded", {
        "enabled": bool(skill_catalog.enabled),
        "skills": [
            {
                "name":            m.name,
                "version":         getattr(m, "version", None),
                "actions":         list(m.actions.keys()),
                "allowed_callers": list(m.allowed_callers),
                "risk":            getattr(m, "risk", None),
            }
            for m in skill_catalog.list_enabled()
        ],
    })

    _agents_summary = [
        {
            "name":         name,
            "role":         a["role"],
            "model_id":     a["model_id"],
            "provider":     models[a["model_id"]]["provider"],
            "max_tokens":   a.get("max_tokens"),
            "temperature":  a.get("temperature"),
        }
        for name, a in agents.items()
    ]
    ctx.emit_event("session_started", {
        "director": {
            "user_id":      director_user_id,
            "email":        director_email,
            "display_name": director_display,
            "source":       "env",  # may be overridden below; informational only
        },
        "seed":          args.seed,
        "max_turns":     args.max_turns,
        "synth_cadence": args.synth_cadence,
        "history_window": args.history_messages,
        "agents":        _agents_summary,
        "truthseeker_enabled": not args.no_verify,
        "searxng_url":   searxng_url,
    })

    history: List[Dict[str, str]] = []

    # Pass 2: Context Loader full flow - extract, assess, summarize, inject.
    # Runs only if context files were detected (Pass 1 captured them).
    # Background block is injected as System (already in NON_TRIGGERING_SPEAKERS)
    # BEFORE the seed so agents see context first, deliberation question second.
    background_block: Optional[str] = None
    budget_assessment: Optional[Dict[str, Any]] = None
    context_files_by_id:       Dict[str, ContextFile] = {}
    context_files_by_filename: Dict[str, ContextFile] = {}
    if context_files:
        try:
            background_block, budget_assessment = load_context_block(
                args=args,
                context_files=context_files,
                providers=providers,
                models=models,
                agents=agents,
                primes=primes,
                audit_fn=audit,
            )
        except ConfigError as e:
            fatal(str(e))
        except ContextLoadAborted as e:
            # The context subsystem raises this when the operator aborts
            # the budget assessment (or when stdin is non-interactive
            # without --yes-context-risk). Cleanly terminate with the
            # same fatal() exit code as a config error.
            fatal(str(e))

        # Build lookup tables so Truthseeker's document-grounded extractor
        # can resolve markers to known files. Only text_document and
        # structured_data files get entries; unknown files don't.
        for cf in context_files:
            if cf.classification in ("text_document", "structured_data"):
                context_files_by_id[cf.context_id] = cf
                context_files_by_filename[cf.filename] = cf

        if background_block:
            history.append({
                "role":    "user",
                "content": background_block,
                "agent":   "System",
            })
            log("[T0] System (Background): "
                f"{len(background_block)} chars of context loaded "
                f"({len(context_files_by_id)} files referenced)")
            log()
            audit({
                "turn":          0,
                "agent":         "System",
                "kind":          "background_context_injected",
                "char_count":    len(background_block),
                "token_estimate": _estimate_tokens(len(background_block)),
                "files_loaded":  list(context_files_by_id.keys()),
                "ts":            datetime.now().isoformat(timespec='seconds'),
            })

    history.append({"role": "user", "content": args.seed, "agent": "System"})
    log(f"[T0] System: {args.seed}")
    log()
    audit({"turn": 0, "agent": "System", "kind": "seed", "content": args.seed,
           "ts": datetime.now().isoformat(timespec='seconds')})

    sentinel_reg = SentinelRegistry()

    # --- Trust registry construction ---
    # Build the Director-provided trust registry once, here, after
    # seed loading, context file detection, and skill args parsing
    # have all completed. The registry is read-only for the rest of
    # the session and is consulted by extract_claim_candidates and
    # verify_claim on every verification pass. See TrustRegistry
    # docstring (adam.verifier.trust_boundary) for the architectural
    # reasoning.
    #
    # set_active_registry() replaces the old _TRUST_REGISTRY module
    # global. The verifier reads back via get_active_registry().
    if args.no_verify:
        # Verification disabled, registry construction is moot.
        set_active_registry(None)
        trust_registry = None
    else:
        trust_registry = build_trust_registry(
            seed_text=args.seed,
            skill_args_parsed=getattr(args, "skill_args_parsed", None),
            context_files_by_id=context_files_by_id,
            context_files_by_filename=context_files_by_filename,
        )
        set_active_registry(trust_registry)
        # Min-length value lives in adam.verifier.trust_boundary now.
        # Import here (not at top of file) because it's a single audit
        # value we don't need elsewhere.
        from adam.verifier.trust_boundary import _TRUST_REGISTRY_MIN_LENGTH
        audit({
            "kind":           "trust_registry_built",
            "size":           trust_registry.size,
            "source_counts":  trust_registry.source_counts,
            "min_length":     _TRUST_REGISTRY_MIN_LENGTH,
            "ts":             datetime.now().isoformat(timespec='seconds'),
        })
        ctx.emit_event("trust_registry_built", {
            "size":          trust_registry.size,
            "source_counts": trust_registry.source_counts,
            "min_length":    _TRUST_REGISTRY_MIN_LENGTH,
        })
        log(f"Trust registry:       {trust_registry.size} entries "
            f"(seed={trust_registry.source_counts.get('seed_tokens', 0)}, "
            f"allowlist={trust_registry.source_counts.get('allowlist_entries', 0)}, "
            f"skill_args={trust_registry.source_counts.get('skill_arg_values', 0)}, "
            f"context_ids={trust_registry.source_counts.get('context_identifiers', 0)}, "
            f"env_config={trust_registry.source_counts.get('env_config_values', 0)})")
        log()

    # --- Wrap-up state ---
    # session_id, session_started_at, and session_state_path were
    # established earlier (alongside the per-user session directory
    # layout) so that all log paths could be constructed before any
    # writes happened. The wrap-up state below still belongs here
    # because it depends on args.max_turns.
    synth_wrap_turn, op_wrap_turn = compute_wrap_up_triggers(args.max_turns)
    wrap_up = WrapUpState(
        synth_wrap_up_turn    = synth_wrap_turn,
        operator_wrap_up_turn = op_wrap_turn,
    )

    # --- Director (human-in-the-loop) setup ---
    # Always create a DirectorState - the polling thread runs whether or
    # not the human actually types anything.
    #
    # Director display name resolution:
    #   1. ADAM_DEFAULT_DIRECTOR_DISPLAY_NAME env var (preferred)
    #      -- defaults to director_user_id when DISPLAY_NAME is unset
    #   2. --director-name CLI flag overrides for testing only
    #
    # NOTE: anonymous sessions are no longer supported. The interactive
    # prompt for "press Enter to use 'Director'" and the "Director"
    # literal fallback have been removed. The user_id and email are
    # required (validated above); the display name has a safe default.
    # When upstream OAuth (Google/Microsoft) lands, the env vars become
    # unused -- the authenticated session supplies the same fields.
    if args.director_name and args.director_name.strip():
        director_display = args.director_name.strip()
        director_source  = "cli_flag"
    else:
        # director_display was set from ADAM_DEFAULT_DIRECTOR_DISPLAY_NAME
        # earlier (falling back to director_user_id if unset).
        director_source  = "env"

    director = DirectorState(
        display_name=director_display,
        source=director_source,
        known_agents=list(agents.keys()),
    )
    director.start_polling(audit_cb=audit)

    log(f"Wrap-up sequence:     Synthesizer at T{synth_wrap_turn}, Operator at T{op_wrap_turn}")
    log(f"Session state path:   {session_state_path}")
    log(f"Director:             {director.display_name} (source: {director.source})")
    log(f"Director help:        type '>>help' at any time during the run")

    # Skill Runtime: catalog + invocation orchestrator. Pass 1 = runtime
    # foundation; Pass 2 = document skill + local filesystem backend.
    # artifacts_root is the per-session directory where artifact-producing
    # skills write their outputs (established earlier alongside the
    # session log paths). Lazily created by the backend on first use;
    # no harm if no skill ever produces an artifact this run.
    skill_runtime = SkillRuntime(
        catalog=skill_catalog,
        skills_log_path=skills_log_path,
        session_id=session_id,
        artifacts_root=artifacts_root,
        requested_skill_args=args.skill_args_parsed,
    )
    if (skill_catalog.executable or skill_catalog.documentation_only
            or skill_catalog.disabled or skill_catalog.unsupported):
        log("")
        log("Skills:")
        if skill_catalog.executable:
            log(f"  executable ({len(skill_catalog.executable)}):")
            for m in skill_catalog.list_executable():
                actions = ",".join(m.actions.keys())
                callers = ",".join(m.allowed_callers)
                log(f"    - {m.name} v{m.version}  actions={actions}  "
                    f"risk={m.risk_level}  callers={callers}")
        if skill_catalog.documentation_only:
            log(f"  documentation-only ({len(skill_catalog.documentation_only)}):")
            for m in skill_catalog.list_documentation_only():
                log(f"    - {m.name} v{m.version}  reason=no handler.py or no adam.actions")
        if skill_catalog.disabled:
            log(f"  disabled ({len(skill_catalog.disabled)}):")
            for name, reason in skill_catalog.disabled:
                log(f"    - {name}  reason={reason}")
        if skill_catalog.unsupported:
            log(f"  unsupported ({len(skill_catalog.unsupported)}):")
            for name, reason in skill_catalog.unsupported:
                log(f"    - {name}  reason={reason}")
        log(f"  Skills log:         {skills_log_path}")

    # Generic CLI skill args (the --skill-arg extension point). Shown in
    # the startup banner so the operator can verify what was parsed, and
    # injected into the Operator wrap-up note later as suggestions.
    if args.skill_args_parsed:
        log("")
        log("Skill arguments provided:")
        for line in format_skill_args_for_display(args.skill_args_parsed):
            log(line)
        log("  (Note: these are suggestions made available to Operator, not "
            "commands. Operator decides whether to invoke a skill.)")
        # Re-run the parser now that audit() is available so any
        # duplicate-override events get recorded. Parser is idempotent.
        try:
            parse_skill_args(args.skill_arg, audit_fn=audit)
        except ConfigError:
            # Already validated above; should not error this time. Defensive.
            pass
        # Record the full structure once as an audit event so downstream
        # readers can reconstruct what was provided without rerunning the parser.
        audit({
            "kind":              "skill_args_provided",
            "skill_args":        args.skill_args_parsed,
            "ts":                datetime.now().isoformat(timespec='seconds'),
        })

    # Context Loader summary (Pass 1: detect-only). Files are listed but
    # no loading or injection happens yet. Pass 2 will add the budget
    # assessment, privacy confirmation, and [T0] Background injection.
    if context_files:
        text_count   = sum(1 for cf in context_files if cf.classification == "text_document")
        struct_count = sum(1 for cf in context_files if cf.classification == "structured_data")
        unk_count    = sum(1 for cf in context_files if cf.classification == "unknown")
        log("")
        log(f"Context files detected: {len(context_files)} total "
            f"({text_count} text, {struct_count} structured-data, {unk_count} unknown)")
        log("  NOTE: Pass 1 detection only - files are audit-logged but not "
            "loaded into deliberation yet.")
        log("  Pass 2 will add text-document loading, summarization, and "
            "[T0] Background injection.")
        for cf in context_files:
            tag = {
                "text_document":   "[text]",
                "structured_data": "[data]",
                "unknown":         "[unkn]",
            }.get(cf.classification, "[????]")
            size_kb = cf.size_bytes / 1024.0 if cf.size_bytes else 0.0
            log(f"  {tag} {cf.context_id:<20} {cf.filename:<40} {size_kb:>8.1f} KB")
            audit({
                "kind":       "context_file_detected",
                **cf.to_audit_dict(),
                "ts":         datetime.now().isoformat(timespec='seconds'),
            })
        # Emit a single context_loaded event with the full file list so
        # the GUI's CONTEXT row can render in one paint instead of one
        # event per file.
        ctx.emit_event("context_loaded", {
            "files": [cf.to_audit_dict() for cf in context_files],
            "counts": {
                "total":           len(context_files),
                "text_document":   text_count,
                "structured_data": struct_count,
                "unknown":         unk_count,
            },
            "background_block_chars": len(background_block) if background_block else 0,
        })
    log()

    # ============================================================
    # Deliberation loop (extracted in step 5b-4)
    # ============================================================
    #
    # Drive the deliberation. run_deliberation_loop owns:
    #   - turn iteration and budget
    #   - wrap-up triggering (turn budget + director halt)
    #   - speaker selection and per-turn LLM dispatch
    #   - Truthseeker verification pass
    #   - skill_call processing on advisory and wrap-up turns
    #   - Operator continuation handling
    #   - kill-notice handling (Ctrl+C once)
    #
    # All mutable subsystems (history, wrap_up, sentinel_reg, director,
    # skill_runtime) are passed by reference and mutated in place; this
    # is identical to the pre-5b-4 behavior where they were mutated in
    # main()'s scope. The function returns a _LoopState whose scalar
    # fields populate the local names below for the post-loop summary
    # logging and session_state.json construction.
    StopState.governance_boundary = None
    StopState.refusal_termination = None
    _seed_boundary_reason = evaluate_self_modification_boundary(args.seed)
    if not _seed_boundary_reason and background_block:
        _seed_boundary_reason = evaluate_self_modification_boundary(background_block)

    _unsafe_reason = None
    if not _seed_boundary_reason:
        _unsafe_reason = evaluate_unsafe_execution_boundary(args.seed)
        if not _unsafe_reason and background_block:
            _unsafe_reason = evaluate_unsafe_execution_boundary(background_block)

    if _seed_boundary_reason:
        log(">>> GOVERNANCE BOUNDARY: session stopped before deliberation")
        log(f"    Reason: {_seed_boundary_reason}")
        log()
        ctx.emit_event("governance_boundary_blocked", {
            "turn":   0,
            "source": "seed",
            "reason": _seed_boundary_reason,
        })
        audit({
            "turn":   0,
            "event":  "governance_boundary_blocked",
            "source": "seed",
            "reason": _seed_boundary_reason,
            "ts":     datetime.now().isoformat(timespec="seconds"),
        })
        state = _LoopState(
            end_reason=GOVERNANCE_BOUNDARY_END_REASON,
            governance_boundary_blocked=True,
            governance_boundary_reason=_seed_boundary_reason,
        )
        director.stop_polling()
    elif _unsafe_reason:
        log(">>> REFUSAL TERMINATION: session stopped before deliberation")
        log(f"    Reason: {_unsafe_reason}")
        log()
        ctx.emit_event("refusal_terminated", {
            "turn":   0,
            "source": "seed",
            "reason": _unsafe_reason,
        })
        audit({
            "turn":   0,
            "event":  "refusal_terminated",
            "source": "seed",
            "reason": _unsafe_reason,
            "ts":     datetime.now().isoformat(timespec="seconds"),
        })
        state = _LoopState(
            end_reason=REFUSAL_TERMINATED_END_REASON,
            refusal_terminated=True,
            refusal_reason=_unsafe_reason,
        )
        director.stop_polling()
    else:
        state = run_deliberation_loop(
            ctx, args,
            agents=agents,
            models=models,
            providers=providers,
            primes=primes,
            history=history,
            wrap_up=wrap_up,
            sentinel_reg=sentinel_reg,
            director=director,
            skill_catalog=skill_catalog,
            skill_runtime=skill_runtime,
            searxng_url=searxng_url,
            context_files_by_id=context_files_by_id,
            context_files_by_filename=context_files_by_filename,
        )
    end_reason               = state.end_reason
    synthesizer_wrap_up_text = state.synthesizer_wrap_up_text
    operator_wrap_up_text    = state.operator_wrap_up_text
    truthseeker_errors       = state.truthseeker_errors

    log("=" * 72)
    ended_at = datetime.now().isoformat(timespec='seconds')
    log(f"Ended:        {ended_at}")
    log(f"End reason:   {end_reason}")
    counts: Dict[str, int] = {}
    for m in history:
        if m.get("agent") not in (None, "System"):
            counts[m["agent"]] = counts.get(m["agent"], 0) + 1
    log("Turns per agent:")
    for agent_name in list(agents.keys()) + ["Truthseeker"]:
        log(f"  - {agent_name:<12} {counts.get(agent_name, 0)}")
    log(f"Audit log:        {audit_path}")
    log(f"Verification log: {verification_path}")
    if skill_catalog.enabled:
        log(f"Skills log:       {skills_log_path}")

    # Emit session_ended for GUI subscribers. session_state.json is
    # still authoritative for everything; this event just gives the GUI
    # the closing "session is done, here's a quick summary" signal
    # without requiring a JSON re-read.
    _skill_summary = None
    if skill_runtime and skill_runtime.invocations:
        _skill_summary = {
            "total":     len(skill_runtime.invocations),
            "successes": sum(1 for i in skill_runtime.invocations if i.get("status") == "success"),
            "failures":  sum(1 for i in skill_runtime.invocations if i.get("status") != "success"),
        }
    ctx.emit_event("session_ended", {
        "end_reason":           end_reason,
        "ended_at":             ended_at,
        "turn_counts":          counts,
        "truthseeker_errors":   len(truthseeker_errors),
        "skill_summary":        _skill_summary,
        "wrap_up": {
            "synth_done":     wrap_up.synth_done,
            "operator_done":  wrap_up.operator_done,
            "continuations":  wrap_up.continuation_count,
        },
    })

    # Truthseeker error summary. Quiet, no errors -> no output. Otherwise
    # surface the count and the dominant error type so the operator can't
    # miss that verification was broken for some/all of the run. Grouped
    # by error type because the same underlying problem (missing dependency,
    # network outage) typically produces the same exception across all turns.
    if truthseeker_errors:
        error_types: Dict[str, int] = {}
        first_message_by_type: Dict[str, str] = {}
        for turn, err_type, err_msg in truthseeker_errors:
            error_types[err_type] = error_types.get(err_type, 0) + 1
            first_message_by_type.setdefault(err_type, err_msg)

        log("")
        log("!" * 72)
        log(f"WARNING: Truthseeker errored on {len(truthseeker_errors)} turn(s). "
            f"Claims on those turns were NOT verified.")
        for err_type, count in sorted(error_types.items(), key=lambda x: -x[1]):
            log(f"  {count}x {err_type}: {first_message_by_type[err_type]}")
        # Targeted hint for the most common cause (missing dep) so the
        # operator gets a direct path to fix.
        if "ModuleNotFoundError" in error_types or "ImportError" in error_types:
            log("  HINT: Run `pip install -r requirements.txt` to install all "
                "ADAM dependencies, then re-run.")
        log("!" * 72)
        log("")

    # Stop Director polling thread cleanly (daemon thread; this is best-effort)
    director.stop_polling()

    # Count Director interjections from audit log entries we just wrote
    director_interjections = sum(
        1 for m in history if m.get("agent") == "Director"
    )
    log(f"Director:         {director.display_name} (source: {director.source}); "
        f"interjections: {director_interjections}")

    # --- Build and write session_state.json ---
    # This is the deterministic continuity artifact. It is constructed even
    # if the session ended abruptly (hard stop, API error) so that the
    # audit trail always includes a final summary file. Some fields will be
    # null in non-wrap-up endings; that is correct behavior.
    try:
        session_state = _build_session_state(
            session_id=session_id,
            started_at=session_started_at,
            ended_at=ended_at,
            end_reason=end_reason,
            seed=args.seed,
            max_turns=args.max_turns,
            args=args,
            history=history,
            audit_path=audit_path,
            verification_path=verification_path,
            sentinel_reg=sentinel_reg,
            operator_wrap_up_text=operator_wrap_up_text,
            synthesizer_wrap_up_text=synthesizer_wrap_up_text,
            agents=agents,
            providers=providers,
            models=models,
            director=director,
            director_user_id=director_user_id,
            director_email=director_email,
            context_files=context_files,
            budget_assessment=budget_assessment,
            background_block_chars=(len(background_block) if background_block else None),
            skill_runtime=skill_runtime,
            wrap_up_state=wrap_up,
            policy_blocked=state.policy_blocked,
            policy_block_reason=state.policy_block_reason,
            awaiting_human_review=state.awaiting_human_review,
            review_reason=state.review_reason,
            awaiting_information=state.awaiting_information,
            information_reason=state.information_reason,
            governance_boundary_blocked=state.governance_boundary_blocked,
            governance_boundary_reason=state.governance_boundary_reason,
            refusal_terminated=state.refusal_terminated,
            refusal_reason=state.refusal_reason,
        )
        with open(session_state_path, "w", encoding="utf-8") as f:
            json.dump(session_state, f, indent=2, default=str)
        log(f"Session state:    {session_state_path}")
        log(f"  ratified decisions: {len(session_state['governance_state']['ratified_decisions'])}")
        log(f"  wrap-up status:     synth_done={wrap_up.synth_done} operator_done={wrap_up.operator_done}")
        if skill_runtime and skill_runtime.invocations:
            ss = session_state["skill_state"]["summary"]
            log(f"  skill invocations:  total={ss['total']} "
                f"successes={ss['successes']} failures={ss['failures']}")
        log(f"  operator_summary:   quality={session_state['operator_summary']['summary_quality']} "
            f"source={session_state['operator_summary']['source']}")
    except Exception as e:
        log(f"[SESSION_STATE ERROR] failed to write session_state.json: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
