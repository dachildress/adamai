"""
Per-session state and writers.

Owns:
  - SessionContext: the migration target for per-session module state.
    Holds paths, identity, writers, history, and the misc state classes
    that were previously scattered across main()'s namespace.

  - State classes:
      * WrapUpState: wrap-up phase tracking + continuation budget
      * DirectorMessage / DirectorState: human-in-the-loop polling
      * StopState: signal-handler flags
  - Director input parsing: _parse_director_input, format_director_transcript_entry
  - Lifecycle helpers: compute_wrap_up_triggers, derive_user_id,
    validate_user_id, load_dotenv, handle_sigint

The SessionContext bundles the per-session paths and provides the
log/audit/verification_audit writer methods that were previously
closures inside main(). After step 5b-3, the deliberation loop
receives a SessionContext as its primary handle on session state
rather than reaching into module globals.

Behavior preserved exactly. The audit writer methods produce the
same JSON-lines output to the same paths; the signal handler does
the same thing on Ctrl+C; Director input parsing follows the same
grammar.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import queue as queue_module
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from adam.core.exceptions import ConfigError


# ============================================================
# Path constants used by SessionContext
# ============================================================

DOTENV_PATH = Path(".env")
LOG_DIR     = Path("logs")


# ============================================================
# Events stream (additive, Part 4 — for live GUI consumers)
# ============================================================
#
# The events stream is an ordered, machine-readable record of what
# happened in a session, written to events.jsonl in parallel with the
# existing log/audit/verification/skills outputs. It exists for one
# consumer: a live GUI (or any other subscriber) that wants to render
# session state as it changes, rather than reconstructing it after the
# fact from the per-subsystem logs.
#
# Design discipline:
#   - Additive only. Nothing in the existing logs or session_state.json
#     changes. emit_event is a new writer, not a replacement.
#   - Closed catalog. The GUI knows what event_type values to expect.
#     Adding a new event type requires bumping EVENT_SCHEMA_VERSION.
#   - emit_event NEVER raises. The events stream is auxiliary; nothing
#     in the deliberation loop should be load-bearing on it.
#   - Disk-only. No stdout echo, no in-process pub/sub yet. (If
#     in-process subscribers are added later, register_subscriber()
#     becomes the natural extension point on SessionContext.)
#
# EVENT_SCHEMA_VERSION follows semver-ish: bump major when the event
# envelope changes shape (e.g. seq becomes a string), bump minor when
# new event types are added, bump patch for payload-additive changes
# to existing event types.
#
# Version history:
#   1.0  Initial Part 4 ship: 15 event types covering the full session
#        lifecycle.
#   1.1  Part 8 additive change: new event type director_message_error
#        for malformed director_inbox.jsonl lines, and the existing
#        director_message event gains an optional message_id field
#        (populated when the message originated from the GUI inbox).
#        Pre-1.1 consumers see the new event type as "unknown" and
#        the new field as absent; both are backward-compatible.

EVENT_SCHEMA_VERSION = "1.1"

# Catalog of supported event types. The GUI binds to these strings;
# changing one is a breaking schema change. Adding a new one is
# additive (bump the minor of EVENT_SCHEMA_VERSION).
EVENT_TYPES = frozenset({
    "session_started",
    "context_loaded",
    "trust_registry_built",
    "skill_registry_loaded",
    "wrap_up_triggered",
    "turn_started",
    "turn_completed",
    "turn_error",
    "verification_completed",
    "verification_error",
    "skill_invoked",
    "director_message",
    "director_message_error",
    "continuation_granted",
    "continuation_denied",
    "session_ended",
})

# Cap on inline content length in turn_completed.content_preview. The
# full content is still included in turn_completed.content; the preview
# is a separate field for GUIs that want to render a snippet without
# loading the whole turn body.
_TURN_CONTENT_PREVIEW_CHARS = 280


def build_content_preview(content: str) -> str:
    """
    Truncate content to _TURN_CONTENT_PREVIEW_CHARS at a word boundary
    where possible, appending an ellipsis if truncated. Used by the
    turn_completed event payload so GUIs can render a snippet without
    loading the whole reply body.
    """
    if not content:
        return ""
    if len(content) <= _TURN_CONTENT_PREVIEW_CHARS:
        return content
    # Truncate at the last whitespace before the cap so the preview
    # ends on a word boundary. Falls back to a hard cut if there's no
    # whitespace in the prefix.
    cut = content[:_TURN_CONTENT_PREVIEW_CHARS]
    last_ws = cut.rfind(" ")
    if last_ws > _TURN_CONTENT_PREVIEW_CHARS // 2:
        cut = cut[:last_ws]
    return cut.rstrip() + "..."


# ============================================================
# Wrap-up timing
# ============================================================

def compute_wrap_up_triggers(max_turns: int) -> Tuple[int, int]:
    """
    Compute the turn indices that trigger forced Synthesizer (convergence)
    and forced Operator (finalization) in the session wrap-up sequence.

    Uses proportional-with-floor logic so the wrap-up sequence adapts to
    short runs (e.g. a 10-turn smoke test) without consuming a fixed 5-turn
    budget that would dominate the deliberation.

    For max_turns >= 50:  triggers at 90% / 95% (e.g. 45 / 47 for 50)
    For max_turns in 10-49: triggers at max_turns - 5 / max_turns - 3
    For max_turns < 10:   triggers at max_turns - 2 / max_turns - 1
                          (with a floor of 1 to avoid negative turn numbers)

    Returns (synth_wrap_up_turn, operator_wrap_up_turn). The two must be
    distinct: Synthesizer fires first to consolidate, Operator fires second
    to crystallize.
    """
    if max_turns >= 10:
        synth_turn    = max(int(0.9 * max_turns), max_turns - 5)
        operator_turn = max(int(0.95 * max_turns), max_turns - 3)
    else:
        # For very short runs, give at most 2 turns of wrap-up
        synth_turn    = max(1, max_turns - 2)
        operator_turn = max(2, max_turns - 1)

    # Guarantee Synthesizer fires before Operator
    if operator_turn <= synth_turn:
        operator_turn = synth_turn + 1

    return synth_turn, operator_turn


# Truthseeker architectural choices (NOT user-configurable):
# These are deliberate design decisions, not tuning knobs. Changing them
# would alter Truthseeker's behavioral guarantees. Operational tuning
# values (timeouts, parallelism, claim cap, etc.) live in runtime.json.
  # TRUTHSEEKER_MODEL_ID and TRUTHSEEKER_TEMPERATURE imported from
# adam.verifier at the top of this file.
# _RUNTIME_CONFIG and _rt() imported from adam.core.config_loader at
# the top of this file (refactor step 5b-1).


# ============================================================
# Director identity normalization
# ============================================================
#
# Director identity has two parts:
#   - user_id: short, path-safe, lowercase. Used as a directory name
#              in logs/<user_id>/<session_id>/. Derived from the env
#              var ADAM_DEFAULT_DIRECTOR or eventually from upstream
#              auth (Google Workspace / Microsoft / SAML).
#   - email:   full email address with domain. Captured for metadata
#              and audit, never used in paths.
#
# The runtime refuses to start without both. There is no anonymous
# mode -- "no username, no use" is the rule. This protects API spend
# and forces upstream auth to be wired before anyone can run ADAM.

# Allowed characters in a user_id. Matches the Active Directory /
# Google Workspace username conventions: lowercase letters, digits,
# dot, hyphen, underscore. No spaces, slashes, or other path-unsafe
# characters.
_USER_ID_RE = re.compile(r"^[a-z0-9._-]+$")


def derive_user_id(raw: str) -> str:
    """
    Normalize a raw director identifier to a path-safe user_id.

    Accepts either 'childrda' or 'childrda@lcps.k12.va.us' (in case the
    env var was set with the full email by mistake) and returns
    'childrda'. Always lowercased; downstream code never sees mixed case.
    """
    return raw.split("@")[0].strip().lower()


def validate_user_id(user_id: str) -> str:
    """
    Validate a normalized user_id against the path-safety regex.
    Returns the user_id if valid; raises ConfigError if not.

    This is paranoia for the single-user demo (where the env var is
    set by the operator) but the right paranoia for when OAuth lands
    -- a malformed claim shouldn't be allowed to create directories
    outside logs/.
    """
    if not user_id:
        raise ConfigError(
            "ADAM_DEFAULT_DIRECTOR resolved to empty user_id. "
            "Set ADAM_DEFAULT_DIRECTOR=<username> in .env (e.g. "
            "ADAM_DEFAULT_DIRECTOR=childrda)."
        )
    if not _USER_ID_RE.match(user_id):
        raise ConfigError(
            f"ADAM_DEFAULT_DIRECTOR contains characters not allowed in "
            f"a user_id: {user_id!r}. Allowed: lowercase letters, digits, "
            f"dot, hyphen, underscore. Got something else."
        )
    return user_id


# ============================================================
# .env loader
# ============================================================

def load_dotenv(path: Path = DOTENV_PATH) -> None:
    """Minimal .env loader. Existing environment variables take precedence."""
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value





# ============================================================
# Wrap-up state
# ============================================================

class WrapUpState:
    """
    Tracks whether the session has entered the forced wrap-up sequence
    and what stage of wrap-up it's in.

    Three entry points trigger wrap-up:
      1. Turn-budget: current_turn reaches synth_wrap_up_turn (computed
         from max_turns by compute_wrap_up_triggers)
      2. Director command: >>halt typed by the human (v2 - not yet wired)
      3. (future) Sentinel-ratified stop: Sentinel produces a halt-grade
         recommendation (v2)

    Once requested=True, the router stops normal rotation and runs the
    forced sequence (Synthesizer convergence -> Operator finalization),
    then the main loop ends after Operator's final turn.
    """

    def __init__(self, synth_wrap_up_turn: int, operator_wrap_up_turn: int) -> None:
        self.synth_wrap_up_turn    = synth_wrap_up_turn
        self.operator_wrap_up_turn = operator_wrap_up_turn

        # Set when any trigger fires
        self.requested: bool          = False
        self.reason:    Optional[str] = None     # "turn_budget" | "director_halt" | future...

        # Set as the wrap-up sequence progresses
        self.synth_done:    bool = False
        self.operator_done: bool = False

        # Operator continuation tracking. After Operator's first wrap-up
        # turn, Operator may request additional turns to complete
        # multi-skill execution chains. These are execution-only turns:
        # no deliberation, no advisory agents, no Sentinel re-engagement.
        # The runtime grants continuations up to max_operator_continuations
        # (configured in runtime.json). Each continuation increments the
        # count; cap_reached is set when the cap blocks a request.
        self.continuation_count:        int  = 0      # how many granted so far
        self.continuation_cap_reached:  bool = False  # set if a request was denied due to cap
        self.continuation_requested:    bool = False  # Operator's most-recent signal
        self.continuation_active:       bool = False  # True while in continuation phase

    def trigger(self, reason: str) -> None:
        """Idempotent. First trigger wins (reason and timing are preserved)."""
        if not self.requested:
            self.requested = True
            self.reason    = reason

    def is_active(self) -> bool:
        return self.requested

    def is_complete(self) -> bool:
        """True once both forced turns have run."""
        return self.synth_done and self.operator_done

# ============================================================
# Director (human-in-the-loop)
# ============================================================
#
# The Director is the human operator's voice during a deliberation.
# A background thread polls stdin while the main loop runs API calls;
# messages are queued and drained at turn boundaries (never mid-turn).

DIRECTOR_HELP_TEXT = """
Director commands:
  >>AgentName: message   Send one targeted message to an agent
  message                Broadcast message to all agents
  >>halt                 Clean wrap-up and end session
  >>help                 Show this help
"""


class DirectorMessage:
    """A single Director input, parsed into its semantic parts."""

    def __init__(
        self,
        raw_text:     str,
        cleaned_text: str,
        target_agent: Optional[str],
        ts:           str,
        warning:      Optional[str] = None,
        source:       str = "terminal",
        message_id:   Optional[str] = None,
    ) -> None:
        self.raw_text     = raw_text          # original input including >>prefix
        self.cleaned_text = cleaned_text      # prefix stripped, for transcript display
        self.target_agent = target_agent      # None = broadcast
        self.ts           = ts
        self.warning      = warning           # populated if parsing surfaced an issue
        # Provenance fields added in Part 8 (Director inbox / live GUI).
        # source is "terminal" for stdin >> input, "gui_inbox" for
        # messages consumed from director_inbox.jsonl. message_id is
        # the GUI-assigned ID for inbox-sourced messages; None for
        # terminal messages.
        self.source       = source
        self.message_id   = message_id


def _parse_director_input(raw: str, known_agents: List[str]) -> Optional[Tuple[Optional[str], str, Optional[str]]]:
    """
    Parse one line of Director input.

    Returns (target_agent_or_None, cleaned_text, warning_or_None) for a
    normal message, or None if the input was empty or a command that
    should not be queued (>>help, blank lines).

    Multiple addresses (>>Logician,Seeker: ...) are flagged with a warning
    and the first valid target is used.
    """
    raw = raw.strip()
    if not raw:
        return None

    # >>AgentName: message  OR  >>AgentName,Other: message
    if raw.startswith(">>"):
        body = raw[2:].lstrip()
        # Look for the colon that separates address from message
        if ":" in body:
            addr_part, msg_part = body.split(":", 1)
            addr_part = addr_part.strip()
            msg_part  = msg_part.strip()

            if not msg_part:
                return None  # >>AgentName: with no message -> ignore

            # Handle multiple addresses
            target_names = [n.strip() for n in addr_part.split(",")]
            warning: Optional[str] = None

            if len(target_names) > 1:
                warning = (
                    f"multiple addresses ({addr_part}) - using first valid only "
                    f"(multi-address support is reserved for a future version)"
                )

            # Match against known agents (case-insensitive)
            target: Optional[str] = None
            for candidate in target_names:
                for known in known_agents:
                    if candidate.lower() == known.lower():
                        target = known
                        break
                if target:
                    break

            if target is None:
                warning = (
                    f"unknown agent name '{addr_part}' - "
                    f"known agents: {', '.join(known_agents)}. "
                    f"Treating as broadcast."
                )
                return (None, msg_part, warning)

            return (target, msg_part, warning)

        # >>X without colon: not a valid addressing pattern. Strip the >>
        # prefix and treat the remaining text as a broadcast. The warning
        # tells the operator we did this so they don't think the prefix
        # had any effect.
        broadcast_text = body.strip() if body.strip() else raw
        return (None, broadcast_text,
                "unrecognized '>>' usage (no colon); '>>' prefix stripped, treating as broadcast")

    # Plain text -> broadcast
    return (None, raw, None)


class DirectorState:
    """
    Tracks the Director's session-level state. Queue is thread-safe.

    The stdin polling thread enqueues parsed messages; the main loop
    drains them at turn boundaries via drain_pending().
    """

    def __init__(self, display_name: str, source: str, known_agents: List[str]) -> None:
        self.display_name = display_name      # "David" or "Director"
        self.source       = source            # "cli_flag" or "default"
        self.known_agents = known_agents
        self.pending: "queue_module.Queue[DirectorMessage]" = queue_module.Queue()
        self.thread:  Optional[threading.Thread] = None
        self.stop_event = threading.Event()

        # Halt-request flag set by the polling thread. Main loop checks
        # at each turn boundary and triggers WrapUpState accordingly.
        self.halt_requested = False

        # Tracks any addressing override requested by the most recent
        # drain. Consumed by select_next_speaker; cleared after use.
        self.pending_address_target: Optional[str] = None

        # Set to the cleaned text(s) of any broadcast (non-addressed)
        # message in the most recent drain. The next agent's invocation
        # note quotes these texts directly and demands explicit
        # acknowledgment so the model can't comply-by-omission. Cleared
        # after consumption.
        self.pending_broadcast_texts: List[str] = []

    def start_polling(self, audit_cb: callable) -> None:
        """Start the background stdin polling thread."""
        if self.thread is not None:
            return
        self.thread = threading.Thread(
            target=self._poll_loop,
            args=(audit_cb,),
            daemon=True,
            name="director-stdin-poller",
        )
        self.thread.start()

    def stop_polling(self) -> None:
        self.stop_event.set()
        # Thread is a daemon so it dies with the process; don't join.

    def _poll_loop(self, audit_cb: callable) -> None:
        """
        Read lines from stdin until the main loop signals stop.

        On Windows, sys.stdin.readline() will block. That's fine because
        this is a daemon thread - it dies when the main process exits.
        """
        while not self.stop_event.is_set():
            try:
                line = sys.stdin.readline()
            except Exception:
                break  # stdin closed or unreadable; quietly end polling
            if not line:
                # EOF (Ctrl+D on Unix, Ctrl+Z+Enter on Windows)
                break

            raw = line.rstrip("\r\n")
            stripped = raw.strip()
            stripped_lc = stripped.lower()

            # Built-in commands handled in-thread (don't go on the queue)
            # Help: accept >>help, help, or ? as the entire input. We're
            # permissive here because "help" is the first thing someone
            # reaches for when unsure how to use a system.
            if stripped_lc in (">>help", "help", "?"):
                sys.stderr.write(DIRECTOR_HELP_TEXT)
                sys.stderr.flush()
                continue

            # Halt: STRICT - requires >>halt explicitly. The cost of a
            # missed halt is "type it again"; the cost of accidentally
            # halting a session because someone said "halt" in passing
            # is real lost work.
            if stripped_lc == ">>halt":
                self.halt_requested = True
                sys.stderr.write(
                    f"[{self.display_name}] halt requested. "
                    f"ADAM will enter clean wrap-up after the current turn.\n"
                )
                sys.stderr.flush()
                audit_cb({
                    "kind":         "director_command",
                    "command":      "halt",
                    "display_name": self.display_name,
                    "ts":           datetime.now().isoformat(timespec='seconds'),
                })
                continue

            # Normal message - parse and queue
            parsed = _parse_director_input(raw, self.known_agents)
            if parsed is None:
                continue
            target, cleaned, warning = parsed
            msg = DirectorMessage(
                raw_text=raw,
                cleaned_text=cleaned,
                target_agent=target,
                ts=datetime.now().isoformat(timespec='seconds'),
                warning=warning,
            )
            self.pending.put(msg)

            # Operator feedback to the human
            if target:
                sys.stderr.write(
                    f"[{self.display_name} \u2192 {target}] message queued for next turn boundary.\n"
                )
            else:
                sys.stderr.write(
                    f"[{self.display_name}] broadcast message queued for next turn boundary.\n"
                )
            if warning:
                sys.stderr.write(f"  warning: {warning}\n")
            sys.stderr.flush()

    def drain_pending(self) -> List[DirectorMessage]:
        """Drain all queued messages in order. Called at turn boundaries."""
        drained: List[DirectorMessage] = []
        while True:
            try:
                drained.append(self.pending.get_nowait())
            except queue_module.Empty:
                break
        # If any drained message had an address target, the most recent
        # one becomes the pending override target. (Oldest-first ordering
        # of injection is preserved separately; this only affects which
        # agent gets forced.)
        # If any drained message was a broadcast (no target), capture its
        # cleaned text so the next agent's invocation note can quote it
        # directly and demand explicit acknowledgment.
        for msg in drained:
            if msg.target_agent is not None:
                self.pending_address_target = msg.target_agent
            else:
                self.pending_broadcast_texts.append(msg.cleaned_text)
        return drained

    def consume_address_target(self) -> Optional[str]:
        """
        Return and clear the pending address target.
        Called by the router after using the override for one turn.
        """
        target = self.pending_address_target
        self.pending_address_target = None
        return target

    def consume_broadcast_texts(self) -> List[str]:
        """
        Return and clear the pending broadcast texts.
        Called by the router; if non-empty, the next agent's invocation
        note quotes them inline with an explicit-acknowledgment instruction.
        """
        texts = self.pending_broadcast_texts
        self.pending_broadcast_texts = []
        return texts

    # ----------------------------------------------------------------
    # Part 8: inbox-sourced messages (from the live GUI)
    # ----------------------------------------------------------------

    def enqueue_inbox_message(
        self,
        raw_text:   str,
        message_id: str,
        ts:         Optional[str] = None,
    ) -> Tuple[Optional["DirectorMessage"], Optional[str], bool]:
        """
        Process one line from director_inbox.jsonl. Mirrors the per-line
        logic in the stdin poller above, but for GUI-submitted messages.
        Returns (msg, error_reason, halt_triggered) where:
          - msg is the queued DirectorMessage (or None if the line was a
            command like >>halt or invalid).
          - error_reason is set when the line could not be parsed as
            valid Director input; emitted by the caller as a
            director_message_error event.
          - halt_triggered is True if the line was >>halt or /halt.

        The same parser used by the stdin thread is used here, so all
        addressing semantics (>>Logician:, multi-address warnings,
        unknown agents, etc.) are identical regardless of which input
        surface produced the message.

        IMPORTANT: this method NEVER raises. The deliberation loop calls
        it from the loop body; an exception here would crash the session.
        """
        if ts is None:
            ts = datetime.now().isoformat(timespec='seconds')

        if not isinstance(raw_text, str):
            return (None, f"content is not a string (got {type(raw_text).__name__})", False)

        stripped = raw_text.strip()
        if not stripped:
            return (None, "empty content", False)

        stripped_lc = stripped.lower()

        # Halt: canonical is >>halt. Accept /halt as an alias since the
        # GUI HALT button is documented to map to this canonical form,
        # but a director typing /halt should also succeed. Both routes
        # produce identical state.
        if stripped_lc in (">>halt", "/halt"):
            self.halt_requested = True
            return (None, None, True)

        # Help is meaningless from the GUI; ignore quietly.
        if stripped_lc in (">>help", "help", "?"):
            return (None, "help command has no effect from GUI inbox", False)

        # Normal message — parse and queue
        parsed = _parse_director_input(raw_text, self.known_agents)
        if parsed is None:
            return (None, "parser rejected message (empty or address-only)", False)
        target, cleaned, warning = parsed

        msg = DirectorMessage(
            raw_text     = raw_text,
            cleaned_text = cleaned,
            target_agent = target,
            ts           = ts,
            warning      = warning,
            source       = "gui_inbox",
            message_id   = message_id,
        )
        self.pending.put(msg)
        return (msg, None, False)


def format_director_transcript_entry(msg: DirectorMessage, display_name: str) -> str:
    """
    Format a Director message for the transcript shown to agents.
    Cleaned form: [David, to Logician] message  OR  [David] message
    """
    if msg.target_agent:
        return f"[{display_name}, to {msg.target_agent}] {msg.cleaned_text}"
    return f"[{display_name}] {msg.cleaned_text}"


# ============================================================
# Stop / kill-notice handling
# ============================================================

class StopState:
    wrap_up = False
    hard_stop = False


def handle_sigint(signum: int, frame: Any) -> None:
    if StopState.hard_stop:
        os._exit(1)
    if not StopState.wrap_up:
        StopState.wrap_up = True
        sys.stderr.write(
            "\n\n[KILL NOTICE] Will stop after the current agent finishes. "
            "Press Ctrl+C again to exit immediately.\n\n"
        )
        sys.stderr.flush()
    else:
        StopState.hard_stop = True
        sys.stderr.write("\n[KILL NOTICE] Hard stop.\n\n")
        sys.stderr.flush()


# ============================================================
# SessionContext
# ============================================================
#
# Holds per-session state that was previously scattered across main()
# closures and module globals:
#
#   - Director identity (user_id, email, display_name, source)
#   - Session metadata (session_id, started_at)
#   - Log paths (session_dir + per-file paths)
#   - Writer methods: log, audit, verification_audit
#
# The state classes (WrapUpState, DirectorState, SentinelRegistry,
# SkillRuntime) are NOT held on the context. They retain their own
# encapsulation and are passed alongside the context to the
# deliberation loop. That keeps SessionContext focused on the I/O
# and identity layer.

class SessionContext:
    """
    Per-session I/O context. Holds paths, identity, writers.

    Construction:
        ctx = SessionContext.create(
            director_user_id=...,
            director_email=...,
            director_display_name=...,
            director_source=...,
        )

    This creates the session directory, opens no files (writers
    use append mode and close after each write), and sets the
    started_at timestamp.

    Identity fields (user_id, email, display_name, source) are read
    by audit writers and session_state serialization. Director_source
    distinguishes 'env' (from .env vars) from 'cli_flag' (--director-name
    override).
    """

    def __init__(
        self,
        director_user_id:     str,
        director_email:       str,
        director_display_name: str,
        director_source:      str,
        session_id:           str,
        started_at:           str,
        session_dir:          Path,
    ) -> None:
        # Identity
        self.user_id      = director_user_id
        self.email        = director_email
        self.display_name = director_display_name
        self.source       = director_source

        # Session metadata
        self.session_id = session_id
        self.started_at = started_at
        self.session_dir = session_dir

        # Per-file paths (stable across the session)
        self.log_path           = session_dir / "session.log"
        self.audit_path         = session_dir / "audit.jsonl"
        self.verification_path  = session_dir / "verification.jsonl"
        self.skills_log_path    = session_dir / "skills.jsonl"
        self.events_path        = session_dir / "events.jsonl"
        # Part 8 (live GUI director input): GUI appends director
        # messages here; ADAM reads new lines at each loop iteration.
        self.director_inbox_path = session_dir / "director_inbox.jsonl"
        self.session_state_path = session_dir / "session_state.json"
        self.artifacts_root     = session_dir / "artifacts"

        # Monotonic per-session event sequence number. Incremented on
        # every successful emit_event call so the GUI can detect dropped
        # events when polling and impose a stable order on events that
        # share a timestamp.
        self._event_seq = 0

        # Part 8: byte offset into director_inbox.jsonl for incremental
        # reading. The loop polls the file at each iteration; this
        # tracker lets us re-read only the bytes that arrived since
        # the last poll, in the same way the GUI's SSE tailer reads
        # events.jsonl. Tail-from-zero on startup so any inbox
        # entries that arrived before the session loop began also
        # get processed (the GUI starts the session and may queue a
        # message before ADAM's first loop iteration).
        self._inbox_pos = 0

    @classmethod
    def create(
        cls,
        director_user_id:     str,
        director_email:       str,
        director_display_name: str,
        director_source:      str,
        session_id:           Optional[str] = None,
    ) -> "SessionContext":
        """
        Create a fresh SessionContext. Generates a new session id
        and timestamp, creates the per-user/per-session log directory.

        New layout: logs/<user_id>/<session_id>/<file>.<ext>
        Old layout (pre-v0.9.4): logs/adam_<stamp>.<ext>
        Existing logs in the old layout are left untouched.

        Part 9: when session_id is provided (e.g., from --session-id by
        a GUI launcher that pre-created the session directory), use
        that ID instead of generating a new UUID. The session directory
        is tolerated as already-existing (exist_ok=True) since the GUI
        will have written seed.md, input_context/, and .process_info.json
        before spawning ADAM.
        """
        if session_id is None:
            session_id = str(uuid.uuid4())
        started_at = datetime.now().isoformat(timespec='seconds')

        LOG_DIR.mkdir(exist_ok=True)
        session_dir = LOG_DIR / director_user_id / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        return cls(
            director_user_id      = director_user_id,
            director_email        = director_email,
            director_display_name = director_display_name,
            director_source       = director_source,
            session_id            = session_id,
            started_at            = started_at,
            session_dir           = session_dir,
        )

    # --- Writers ---
    # These replace the log/audit/verification_audit closures that
    # previously lived inside main(). Same observable behavior: open
    # the per-file path in append mode, write, close. The session log
    # also echoes the line to stdout.

    def log(self, line: str = "") -> None:
        """Echo to stdout and append to session.log."""
        print(line)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def audit(self, event: Dict[str, Any]) -> None:
        """Append a JSON line to audit.jsonl."""
        with open(self.audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")

    def verification_audit(self, record: Dict[str, Any]) -> None:
        """Append a JSON line to verification.jsonl."""
        with open(self.verification_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def emit_event(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """
        Append an event to events.jsonl. Auxiliary stream for live
        consumers (GUI, monitors); existing logs are unaffected.

        Each event line is a self-contained envelope:
            {
              "event_type":     <str, one of EVENT_TYPES>,
              "schema_version": EVENT_SCHEMA_VERSION,
              "session_id":     <session uuid>,
              "ts":             <isoformat seconds>,
              "seq":            <monotonic per-session counter>,
              "payload":        { ... event-type-specific ... }
            }

        Discipline:
          - NEVER raises. If the file can't be written, the failure is
            logged to stderr and the deliberation continues. The events
            stream is auxiliary; nothing in the loop is load-bearing on
            it. (Existing audit/verification writers DO raise on write
            failure because the audit trail is load-bearing for
            session_state.json reconstruction; events are not.)
          - Unknown event_type values are still written but tagged with
            `_unknown_event_type: true` in the envelope, so the GUI can
            surface them as a forward-compatibility hint rather than
            silently dropping them. The catalog should be the source of
            truth, but a misspelling at a call site should be visible,
            not invisible.
          - payload=None is allowed and serialized as {} so the GUI can
            count on payload always being a dict.
        """
        try:
            self._event_seq += 1
            envelope: Dict[str, Any] = {
                "event_type":     event_type,
                "schema_version": EVENT_SCHEMA_VERSION,
                "session_id":     self.session_id,
                "ts":             datetime.now().isoformat(timespec='seconds'),
                "seq":            self._event_seq,
                "payload":        payload if payload is not None else {},
            }
            if event_type not in EVENT_TYPES:
                envelope["_unknown_event_type"] = True
            with open(self.events_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(envelope, default=str) + "\n")
        except Exception as e:
            # Auxiliary stream — never let it crash the session. Emit a
            # single stderr line so the operator knows the GUI feed is
            # broken without flooding the main log.
            sys.stderr.write(
                f"[events.jsonl write failed: {type(e).__name__}: {e}]\n"
            )
