"""
ADAM events schema
==================

Typed event records for the ADAM runtime. The runtime emits these to a
session-scoped events.jsonl stream that the GUI consumes. The existing
*_audit.jsonl, *_verification.jsonl, and *_skills.jsonl streams remain
unchanged — events.jsonl is a NEW, ADDITIONAL stream optimized for GUI
consumption, not a replacement for the audit log.

Design principles
-----------------

1. **One record type per event kind.** Each subclass of Event has a fixed
   set of fields. No "extra" or "metadata" dicts. The GUI can rely on
   the shape.

2. **The schema captures verbs, not nouns.** New verbs (a fundamentally
   different kind of thing happening at runtime) require core changes
   here. New nouns (specific skills, agents, roles, document formats)
   do NOT — they slot into existing event types as string-typed values
   that the core treats as opaque.

3. **Strongly typed at the producer; serialized as JSON for consumers.**
   Python emits dataclass instances; serialization is deterministic. The
   GUI deserializes back to typed records (in Python) or parses against
   the declared shape (in TypeScript/HTMX).

4. **Append-only, monotonically ordered.** Every event carries `seq`
   (monotonic per session) and `ts` (wall clock). The GUI can resume
   from a known `seq` after disconnection.

5. **Severable from the runtime.** The events module imports nothing
   from the rest of the runtime. The runtime imports from events. This
   keeps the GUI side import-cheap.

6. **Honest about what's known at emission time.** If a field can't be
   filled when an event fires, the field is Optional and we explain why
   in the docstring rather than padding with None.

Extensibility model
-------------------

The events schema is deliberately structured so that the most common
changes you'll make to ADAM — adding skills, adding agents, adding
artifact formats — require ZERO changes to this file.

**Adding a new skill (no core change):**
  Drop the skill manifest into skills/<name>/SKILL.md + handler.py.
  The runtime discovers it via skill_catalog.discover_skills(). When
  the skill is invoked, the runtime emits a SkillCallEvent with the
  skill's name as a string and the skill's result dict as opaque
  payload. The GUI either has a specialized renderer for that
  (skill, action) pair or falls back to a generic JSON-prettyprint.

**Adding a new agent (no core change):**
  Add an entry to agents.json + a prime file in prompts/primes/.
  The agent's name and role are string-typed in AgentTurnEvent; no
  enumeration to update.

**Adding a new agent role (no core change):**
  agents.json declares the role string. The GUI optionally adds a CSS
  class for that role's color stripe. No events.py change.

**Adding a new artifact format (no core change):**
  The producing skill returns the format string in its result payload
  and emits an ArtifactEvent with the format string. The GUI either
  has a renderer for that format or shows a generic preview.

**Adding a new EVENT KIND (yes, core change):**
  This is the one place where the schema must be extended explicitly.
  A new event kind means a new verb — a fundamentally different thing
  happening at runtime. Examples: SentinelGateEvent when pre-execution
  gating is built; MultiTenantEvent if tenancy scope changes. Adding
  a new kind requires a new dataclass here AND a corresponding
  emission site in the runtime AND a consumer-side handler.

Event categories and their existing audit-log relatives
-------------------------------------------------------

| Event class                | Existing audit kind                          |
|----------------------------|----------------------------------------------|
| SessionStartedEvent        | (none — currently logged via banner)         |
| ContextFileEvent           | "context_file_detected"                      |
| BackgroundInjectedEvent    | "background_context_injected"                |
| SeedEvent                  | "seed"                                       |
| TrustRegistryEvent         | "trust_registry_built"                       |
| AgentTurnEvent             | (turn audit dict)                            |
| DirectorInputEvent         | "director_message"                           |
| VerificationEvent          | (verification.jsonl record)                  |
| SkillCallEvent             | (skills.jsonl record)                        |
| ArtifactEvent              | (currently inferred from skill_call result)  |
| DeliberationStateEvent     | (currently inferred from wrap_up flags)      |
| OperatorContinuationEvent  | "operator_continuation"                      |
| SessionEndedEvent          | (currently logged via final summary)         |
| ErrorEvent                 | "error"                                      |
| ProgressEvent              | (none — pure UI hint)                        |
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union


# ============================================================
# Base event
# ============================================================

@dataclass
class Event:
    """
    Base class for every event the runtime emits.

    All subclasses inherit `seq`, `ts`, `session_id`, and `kind`.
    Subclasses add their own fields.
    """
    # Monotonic sequence number within a session. Starts at 1.
    # The GUI uses this to detect missed events and resume from a known
    # position. Never gaps; if you see seq=5 then seq=7, an event was lost.
    seq: int

    # Wall-clock timestamp, ISO 8601 with seconds precision.
    # Matches the format the existing audit log uses.
    ts: str

    # Session identifier. The existing runtime uses session_id like
    # "4229ce2e-a952-4ac4-934e-4d2b45e83b9d" but truncates to 8 chars
    # for log filenames. Events use the full UUID.
    session_id: str

    # Discriminator field — the GUI dispatches on this. Each subclass
    # sets this to a fixed string in its __post_init__.
    kind: str = field(init=False)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for JSONL emission. Dataclass asdict + flatten enums."""
        d = asdict(self)
        for k, v in list(d.items()):
            if isinstance(v, Enum):
                d[k] = v.value
        return d


# ============================================================
# Session lifecycle
# ============================================================

@dataclass
class SessionStartedEvent(Event):
    """
    Emitted once, at session startup, after config loads and before
    the first turn. The GUI uses this to render the case header.
    """
    seed_text: str
    seed_source: str            # 'cli_flag' | 'seed_file' | 'stdin'
    max_turns: int
    synth_cadence: int
    profile_name: str           # e.g. "Default (Balanced)"
    director_display_name: str  # e.g. "d.childress"
    truthseeker_enabled: bool
    smtp_configured: bool
    runtime_version: str        # e.g. "0.9.4"

    def __post_init__(self):
        self.kind = "session_started"


@dataclass
class SessionEndedEvent(Event):
    """
    Emitted once, at session end. Mirrors values written to
    session_state.json's wrap-up section.
    """
    end_reason: str             # 'wrap-up complete' | 'hard stop' | ...
    total_turns: int
    deliberation_turns: int
    continuation_turns: int
    successes: int              # skill invocation successes
    failures: int               # skill invocation failures
    artifacts_produced: int
    final_decision: Optional[str] = None
    wrap_up_complete: bool = True

    def __post_init__(self):
        self.kind = "session_ended"


# ============================================================
# Context loading
# ============================================================

@dataclass
class ContextFileEvent(Event):
    """
    One per attached context file. Emitted during pre-loop context
    detection. Maps 1:1 to the existing 'context_file_detected'
    audit record.
    """
    context_id: str             # e.g. "CTX-20260523-001"
    filename: str
    source_path: str
    classification: str         # 'text_document' | 'structured_data' | 'unknown'
    sha256: str
    size_bytes: int
    parse_status: str           # 'extracted' | 'skipped' | 'failed'

    def __post_init__(self):
        self.kind = "context_file_attached"


@dataclass
class BackgroundInjectedEvent(Event):
    """
    Emitted once at T0 when the background block is injected into
    history. Maps to existing 'background_context_injected'.
    """
    char_count: int
    token_estimate: int
    files_loaded: List[str]     # list of CTX-IDs

    def __post_init__(self):
        self.kind = "background_injected"


@dataclass
class SeedEvent(Event):
    """
    Emitted once at T0 when the Director's seed enters the deliberation
    history. The GUI may already have this from SessionStartedEvent,
    but emitting separately matches the audit log's structure and
    keeps "what entered history" auditable per turn.
    """
    content: str

    def __post_init__(self):
        self.kind = "seed_injected"


# ============================================================
# Trust registry
# ============================================================

@dataclass
class TrustRegistryEvent(Event):
    """
    Emitted once after the trust registry is built (just before T1).
    Maps to existing 'trust_registry_built' audit record.
    """
    size: int
    source_counts: Dict[str, int]   # keys: seed_tokens, allowlist_entries, etc.
    min_length: int

    def __post_init__(self):
        self.kind = "trust_registry_built"


# ============================================================
# Per-turn events
# ============================================================
#
# Agent roles are NOT a fixed enum. agents.json declares each agent's
# role string. The events module stores whatever role string is in
# agents.json. The GUI optionally has a CSS class per known role for
# color stripes; unknown roles render without a color (and that's fine
# — the agent name and content still render).
#
# Roles currently used in agents.json:
#   advisory       — Logician, Seeker, Visionary
#   synthesizer    — Synthesizer
#   truthseeker    — Truthseeker
#   sentinel       — Sentinel
#   operator       — Operator
#   director       — Director (injected, not declared in agents.json)
#
# Adding a new role: add it to agents.json, optionally give the GUI
# a CSS class for the new role's color stripe. No events.py change.


@dataclass
class AgentTurnEvent(Event):
    """
    One per agent turn. The most-emitted event type by volume.

    Replaces the polymorphic audit dict that today is the turn record:
        {"turn": 7, "agent": "Operator", "model_id": "...", "routing_reason": "...", ...}

    By making it a typed event, the GUI can deserialize without
    runtime field-key guessing. Agent name and role are free-form
    strings — adding a new agent or role requires no changes here.
    """
    turn: int
    agent: str                  # 'Logician' | 'Seeker' | ... | future agent names
    role: str                   # 'advisory' | 'synthesizer' | ... | future role strings
    model_id: str               # e.g. 'claude-sonnet-4-6'
    routing_reason: str         # 'advisory-rotation' | 'wrap-up-synthesizer' | ...
    invocation_note: str        # The system-injected note for this turn
    concern_label: Optional[str]   # For Sentinel turns: the concern category
    max_tokens: int
    temperature: float
    content: str                # The agent's full reply text

    # Verification verdict counts for this turn's content, if Truthseeker ran.
    # None if Truthseeker didn't run on this turn (Director, Sentinel, etc).
    # Denormalized for GUI rendering efficiency — also available via
    # VerificationEvent records joined on turn.
    verification_summary: Optional[Dict[str, int]] = None
    # e.g. {"verified": 1, "partial": 2, "not_web_verifiable": 1, "doc_grounded": 3}

    # Streaming flag: True while the turn is in flight, False once complete.
    # Most turns emit once with streaming=False. For long-running turns
    # the runtime MAY emit a streaming=True placeholder first followed by
    # a streaming=False completion event.
    streaming: bool = False

    def __post_init__(self):
        self.kind = "agent_turn"


@dataclass
class DirectorInputEvent(Event):
    """
    Emitted when the Director addresses the running session. Maps to
    existing 'director_message' audit record.

    Currently the runtime accepts director input via stdin (the '>>'
    prompt). When the GUI's command bar is wired (Phase 2), HTTP-posted
    input becomes another source.
    """
    turn: int
    display_name: str           # 'd.childress'
    raw_text: str
    cleaned_text: str
    target_agent: Optional[str] # If addressed via @Logician, etc.
    warning: Optional[str]
    source: Literal["stdin", "gui", "shared_file"]

    def __post_init__(self):
        self.kind = "director_input"


# ============================================================
# Verification
# ============================================================
#
# VerdictStatus is a FIXED enum — verification outcomes are a closed
# concept defined by Truthseeker's policy code. Adding a new status
# (e.g. PARTIALLY_CONTRADICTED) would be a real core change.

class VerdictStatus(str, Enum):
    """Maps 1:1 to the existing Truthseeker status strings."""
    VERIFIED                          = "VERIFIED"
    PARTIALLY_VERIFIED                = "PARTIALLY_VERIFIED"
    UNSUPPORTED                       = "UNSUPPORTED"
    CONTRADICTED                      = "CONTRADICTED"
    NEEDS_HUMAN_REVIEW                = "NEEDS_HUMAN_REVIEW"
    NOT_WEB_VERIFIABLE                = "NOT_WEB_VERIFIABLE"
    DOCUMENT_GROUNDED_NOT_WEB_VERIFIED = "DOCUMENT_GROUNDED_NOT_WEB_VERIFIED"


@dataclass
class VerificationEvent(Event):
    """
    Emitted per claim, after Truthseeker has produced a verdict.
    Maps to one record in the existing *_verification.jsonl stream.
    """
    turn: int                           # Which turn produced the claim
    claim_text: str                     # The claim as extracted
    claim_category: str                 # 'statistic' | 'named_study' | 'attribution' | ...
    status: VerdictStatus
    confidence: str                     # 'HIGH' | 'MEDIUM' | 'LOW' | 'N/A'
    source_count: int
    highest_source_tier: Optional[str]  # 'tier_1' | ... | 'tier_5' | None
    highest_source_score: int

    sources: List[Dict[str, Any]] = field(default_factory=list)
    note: Optional[str] = None

    def __post_init__(self):
        self.kind = "verification"


# ============================================================
# Skill execution
# ============================================================
#
# SkillStatus is a FIXED enum — skill execution outcomes are a closed
# concept owned by the skill runtime. Adding a new status (e.g.
# AWAITING_HUMAN_APPROVAL when Sentinel pre-execution gating lands)
# would be a real core change.
#
# Skill NAMES, ACTIONS, and RESULT SHAPES are NOT enumerated here.
# They are entirely owned by the individual skills.

class SkillStatus(str, Enum):
    SUCCESS     = "success"
    FAILURE     = "failure"
    REFUSED     = "refused"      # e.g. allowlist refusal, policy denial
    PARSE_ERROR = "parse_error"  # truncated skill_call block


@dataclass
class SkillCallEvent(Event):
    """
    Emitted per skill_call. Captures the invocation envelope — what
    skill was called, with what action and args, and whether it
    succeeded.

    The `result` payload is OPAQUE to the core runtime. Its shape is
    determined entirely by the invoked skill and documented in that
    skill's SKILL.md manifest. The core does not validate or interpret
    result contents.

    Consumers that want to render skill-specific UI (e.g. the GUI's
    artifact preview for document.create, or the email send-receipt
    panel for email.send) should dispatch on (skill, action) and
    either render a specialized view or fall through to a generic
    JSON-prettyprint default.

    This separation is intentional: adding a new skill requires NO
    changes to events.py or the core runtime. The skill drops into
    skills/<name>/ and starts emitting SkillCallEvents the moment
    it is invoked. Whatever the skill returns in its result dict
    flows through this event as opaque payload.
    """
    turn: int
    agent: str                      # Currently always 'Operator'; flexible
    skill: str                      # 'document' | 'email' | 'engineer' | future
    action: str                     # 'create' | 'send' | 'diagnose' | future
    args: Dict[str, Any]            # Args the skill was invoked with
    status: SkillStatus
    error_class: Optional[str]      # 'allowlist_not_configured' | future
    error_detail: Optional[str]

    # Opaque result payload. Shape determined by the invoked skill.
    # The runtime emits it as-is; the GUI renders generically unless
    # it has a specialized renderer for the (skill, action) pair.
    result: Dict[str, Any] = field(default_factory=dict)

    elapsed_ms: int = 0

    def __post_init__(self):
        self.kind = "skill_call"


@dataclass
class ArtifactEvent(Event):
    """
    Emitted when a file lands in the session's artifacts/ directory.

    Today this is inferred from skill_call results. Promoting to a
    first-class event lets the GUI Artifacts panel update directly
    without parsing skill results.

    The `format` field is a free-form string, not an enum. The GUI's
    artifact preview dispatches on format with a generic fallback
    for unknown extensions. Adding support for a new format requires
    no changes here — only a renderer (or fallback) on the GUI side.
    """
    filename: str
    path: str                       # Relative to logs/<session_id>/artifacts/
    format: str                     # 'docx' | 'md' | 'eml' | future
    size_bytes: int
    sha256: str
    created_by_turn: int
    created_by_skill: str           # 'document' | 'email' | future

    def __post_init__(self):
        self.kind = "artifact_created"


# ============================================================
# Deliberation state transitions
# ============================================================
#
# DeliberationState is a FIXED enum — the deliberation lifecycle is
# a core orchestration concept defined by the runtime, not extended
# by skills or agents. Adding a new state (e.g. PAUSED when Director
# halt is implemented, or SENTINEL_GATE_PENDING when pre-execution
# gating lands) would be a real core change.

class DeliberationState(str, Enum):
    """The deliberation lifecycle, used for the status pill rail."""
    DELIBERATING   = "deliberating"   # Advisory + Synthesizer cadence
    RATIFIED       = "ratified"       # Synthesizer produced a Decision Point
    EXECUTING      = "executing"      # Operator turns in flight
    WRAPPING_UP    = "wrapping_up"    # Wrap-up synth/operator phase
    CONTINUATION   = "continuation"   # In a granted continuation turn
    COMPLETE       = "complete"       # Session ended normally


@dataclass
class DeliberationStateEvent(Event):
    """
    Emitted on state transitions of the deliberation lifecycle.
    Drives the Decision / Execution status pills in the GUI.
    """
    new_state: DeliberationState
    previous_state: Optional[DeliberationState]
    triggered_by_turn: int
    triggered_by_agent: str
    detail: Optional[str] = None

    def __post_init__(self):
        self.kind = "deliberation_state"


@dataclass
class OperatorContinuationEvent(Event):
    """
    Emitted when an Operator continuation is requested, granted, or
    refused (cap reached). Maps to existing 'operator_continuation'
    and 'operator_continuation_cap_reached' audit records.
    """
    turn: int
    continuation_index: int         # 1-based, which continuation this is
    max_continuations: int
    continuation_granted: bool
    signal_source: str              # 'wrap_up_block' | 'operator_continue_block'
    reason: Optional[str]
    cap_reached: bool = False

    def __post_init__(self):
        self.kind = "operator_continuation"


# ============================================================
# Errors and progress
# ============================================================

@dataclass
class ErrorEvent(Event):
    """
    Emitted on API failures, parse errors, skill_call dispatch errors,
    runtime exceptions, etc.

    `error_class` is a free-form string. The runtime catalogs known
    error classes (api_timeout, rate_limit, skill_parse_error, ...);
    new skills can introduce their own error classes which surface
    here without a core change.
    """
    turn: int
    agent: str
    error_class: str                # 'api_timeout' | 'skill_parse_error' | future
    error_message: str
    recoverable: bool               # True = runtime continued; False = session ended

    def __post_init__(self):
        self.kind = "error"


@dataclass
class ProgressEvent(Event):
    """
    Emitted incrementally during long-running turns. Pure UI hint —
    the GUI uses these to show liveness indicators.

    Optional emission: a turn that completes in under ~500ms shouldn't
    emit ProgressEvent at all; only emit if the turn would otherwise
    look frozen to a Director watching the UI.

    `phase` is a free-form string. Each emission site documents its
    own phase tokens (e.g. Truthseeker emits 'verifying_claim_3_of_7';
    Operator emits 'streaming_response' or 'skill_executing').
    """
    turn: int
    agent: str
    phase: str                      # free-form, per emission site
    detail: Optional[str] = None

    def __post_init__(self):
        self.kind = "progress"


# ============================================================
# Union type for typed dispatch
# ============================================================

AnyEvent = Union[
    SessionStartedEvent,
    SessionEndedEvent,
    ContextFileEvent,
    BackgroundInjectedEvent,
    SeedEvent,
    TrustRegistryEvent,
    AgentTurnEvent,
    DirectorInputEvent,
    VerificationEvent,
    SkillCallEvent,
    ArtifactEvent,
    DeliberationStateEvent,
    OperatorContinuationEvent,
    ErrorEvent,
    ProgressEvent,
]


# ============================================================
# Emission helper (implementation lands in step 6)
# ============================================================

class EventEmitter:
    """
    The runtime constructs one EventEmitter per session and passes it
    through SessionContext (when step 7 lands) or as a positional
    argument (in the meantime).

    During step 6 implementation, this class will:
      - Open events.jsonl in append mode at session start
      - Maintain the monotonic seq counter
      - Serialize each event to one JSON line + newline + flush
      - Close the file at session end

    During step 7 (SessionContext migration), this lives on the
    SessionContext as ctx.events.
    """
    def __init__(self, session_id: str, events_path: str):
        self.session_id = session_id
        self.events_path = events_path
        self._seq = 0
        # TODO step 6: open file handle

    def emit(self, event: Event) -> None:
        """Assign seq + ts + session_id, then write to events.jsonl."""
        self._seq += 1
        event.seq = self._seq
        event.session_id = self.session_id
        # event.ts is expected to be set by the caller at emission time
        # TODO step 6: serialize and write
        raise NotImplementedError("Event emission lands in step 6 of the refactor")
