"""
Main deliberation loop and its supporting helpers.

Public entry point:
  - run_deliberation_loop(ctx, args, ...) -> None
      The main loop body. Drives turn iteration, history management,
      wrap-up handling, kill-notice handling, continuation budget.
      Replaces the inline while-loop that was inside main().

Helpers (also public so tests and the runtime can reach them):
  - load_agent_primes: read each agent's prime file
  - resolve_agent_call_params: tier the max_tokens / temperature lookup
  - build_transcript_messages: build the agent-side message stack
  - _extract_wrap_up_block, _extract_operator_continue_block,
    extract_continuation_signal: parse Operator output for wrap-up
    and continuation signals
  - _extract_decisions_from_audit, _summarize_verification_audit,
    _summarize_sentinel_concerns, _build_session_state: serialize
    the final session_state.json artifact

This module imports from every previously-extracted subsystem:
  adam.core.client_dispatch    (call_model)
  adam.core.config_loader      (_RUNTIME_CONFIG, _rt)
  adam.core.router             (select_next_speaker, SentinelRegistry,
                                derive_advisory_cycle, set_advisory_cycle,
                                sentinel_concern)
  adam.core.session            (SessionContext, WrapUpState, DirectorState,
                                StopState, compute_wrap_up_triggers,
                                format_director_transcript_entry,
                                handle_sigint)
  adam.verifier                (the verification pipeline)
  adam.context                 (context loader)
  adam.skills_runtime          (SkillRuntime, parse_skill_calls, etc.)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import signal
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Adam subsystem imports
from adam.core.exceptions import ConfigError, ContextLoadAborted
from adam.core.client_dispatch import call_model
from adam.core.config_loader import _RUNTIME_CONFIG, _rt
from adam.core.router import (
    SentinelRegistry,
    select_next_speaker,
    sentinel_concern,
    derive_advisory_cycle,
    set_advisory_cycle,
    ADVISORY_CYCLE,
    NON_TRIGGERING_SPEAKERS,
)
from adam.core.session import (
    SessionContext,
    WrapUpState,
    DirectorState,
    StopState,
    compute_wrap_up_triggers,
    format_director_transcript_entry,
    build_content_preview,
)
from adam.verifier import (
    TRUTHSEEKER_MODEL_ID,
    TRUTHSEEKER_TEMPERATURE,
    TrustRegistry,
    build_trust_registry,
    set_active_registry,
    get_active_registry,
    extract_claim_candidates,
    extract_document_grounded_claims,
    extract_structured_claims,
    verify_claim,
    apply_verification_policy,
    format_verification_summary,
    format_verification_for_transcript,
)
from adam.verifier.web_search import _extract_first_json_value
from adam.context import (
    ContextFile,
    build_context_state,
    DOCUMENT_GROUNDED_CLAIM_RULE,
)
from adam.context.budget_manager import _estimate_tokens
from adam.skills_runtime import (
    SkillRuntime,
    parse_skill_calls,
    discover_skills,
)
from adam.core.governance_invariants import (
    GOVERNANCE_BOUNDARY_END_REASON,
    evaluate_self_modification_boundary,
)
from adam.core.empty_termination import (
    REFUSAL_TERMINATED_END_REASON,
    evaluate_refusal_termination,
    evaluate_unsafe_execution_boundary,
)
from adam.core.information_pause import (
    INFORMATION_PAUSE_END_REASON,
    evaluate_information_pause,
)


# ============================================================
# Agent loading
# ============================================================

def load_agent_primes(agents_config: Dict[str, Any]) -> Dict[str, str]:
    """Read each agent's prime file from disk into a dict."""
    primes: Dict[str, str] = {}
    for name, a in agents_config.items():
        with open(a["prime_file"], encoding="utf-8") as f:
            primes[name] = f.read().strip()
    return primes



def resolve_agent_call_params(
    agent_name:   str,
    agents:       Dict[str, Any],
    models:       Dict[str, Any],
    is_wrap_up:   bool = False,
    is_artifact:  bool = False,
) -> Tuple[str, int, float]:
    """
    Return (model_id, max_tokens, temperature) for an agent.

    Three tiers of max_tokens, in resolution order:

      1. is_wrap_up=True and max_tokens_wrap_up is configured -> use that.
         This is how wrap-up Synthesizer/Operator turns get the headroom
         (typically 16000) to produce a complete closing artifact.

      2. is_artifact=True and max_tokens_artifact is configured -> use that.
         This is for NON-wrap-up Operator turns that are about to emit a
         skill_call whose 'content' arg carries a multi-KB artifact body
         (e.g. an operator-gate turn that's been ratified to produce a
         document). The standard max_tokens (1500 for Operator in the
         default config) is too small for any meaningful artifact; without
         this tier, the skill_call truncates mid-JSON-string and the
         artifact is dropped by the parser. Sized smaller than wrap-up
         since these turns don't also carry a wrap_up JSON block.

      3. Otherwise, use max_tokens (with model default_max_tokens as
         fallback). This is the standard advisory-turn budget.

    Both max_tokens_wrap_up and max_tokens_artifact are optional fields;
    agents without them in their config simply use max_tokens for all
    turn types. The caller decides which flag to pass based on routing.
    """
    a = agents[agent_name]
    model_id = a["model_id"]
    m = models[model_id]

    if is_wrap_up and "max_tokens_wrap_up" in a:
        max_tokens = a["max_tokens_wrap_up"]
    elif is_artifact and "max_tokens_artifact" in a:
        max_tokens = a["max_tokens_artifact"]
    else:
        max_tokens = a.get("max_tokens", m.get("default_max_tokens", 1024))

    temperature = a.get("temperature", 0.7)
    return model_id, max_tokens, temperature


# ============================================================
# Message construction
# ============================================================

def build_transcript_messages(
    history:         List[Dict],
    history_limit:   int,
    current_agent:   str,
    invocation_note: Optional[str],
    current_turn:    int,
    max_turns:       int,
    wrap_up_active:  bool,
) -> List[Dict[str, str]]:
    """
    Build the per-turn message list sent to an agent.

    Now includes turn-awareness header so agents know their place in the
    session, and a wrap-up phase banner when applicable. The wrap-up banner
    is binary (active or not), not a turn-countdown -- countdowns invite the
    model to "save its best for last" which produces worse intermediate
    output. Binary signaling produces cleaner behavior.
    """
    if len(history) > history_limit:
        visible = [history[0]] + history[-(history_limit - 1):]
    else:
        visible = history
    transcript = "\n\n".join(f"[{m['agent']}] {m['content']}" for m in visible)

    # Turn awareness header (always present)
    awareness = f"SESSION STATUS: turn {current_turn} of {max_turns} total."

    # Wrap-up banner (only when active)
    if wrap_up_active:
        awareness += (
            "\n\nSESSION STATUS: WRAP-UP PHASE\n"
            "\n"
            "This deliberation is closing. Your remaining turns should focus on:\n"
            "  - Consolidating and integrating decisions already ratified\n"
            "  - Identifying open questions and unresolved tensions worth carrying forward\n"
            "  - Producing final recommendations grounded in this session's verified findings\n"
            "  - Avoiding new exploratory threads unless they materially change the outcome\n"
            "  - Preserving uncertainty where evidence remains incomplete\n"
            "\n"
            "The final Operator turn will produce the closing session artifact."
        )

    instruction = (
        f"{awareness}\n\n"
        f"Conversation so far:\n\n{transcript}\n\n"
        f"You are {current_agent}. Respond next, staying strictly in your role."
    )
    if invocation_note:
        instruction += f"\n\nRouter note: {invocation_note}"
    return [{"role": "user", "content": instruction}]


# ============================================================
# Session State Construction (wrap-up phase output)
# ============================================================
#
# session_state.json is the deterministic continuity artifact produced at
# session end. It has four authority domains:
#
#   runtime_state       (orchestrator-owned)   - times, counts, paths, identity
#   governance_state    (orchestrator-owned)   - decisions, sentinel concerns, verification
#   deliberation_state  (Synthesizer-influenced) - open questions, framing assumptions
#   operator_summary    (Operator-influenced)  - narrative, next-session recommendation
#
# Orchestrator-owned fields are constructed from audit logs and never depend
# on model output. Influenced fields are populated from optional structured
# blocks the wrap-up agents emit; if those blocks are absent or malformed,
# the corresponding fields stay null with summary_quality flagged.
#
# This artifact is what makes ADAM stateful across sessions. The next-session
# code path can load a prior session_state.json as a context document and
# treat its ratified_decisions as binding background.


import uuid


def _write_pause_state(*, ctx, turn: int, synthesis_text: str,
                       review_reason: str,
                       governance_profile_id: Optional[str]) -> None:
    """
    Slice 4a: persist the minimal state needed to resume a session that
    paused at the human-review gate. Because the gate pause is TERMINAL
    (the deliberation is already complete -- Synthesizer's final pass is
    done, only Operator remains), the resumable state is small and
    well-defined: the final synthesis, why review was required, and the
    turn it paused at. Written as pause_state.json in the session dir.

    (The mid-loop information pause in 4b will need a much larger snapshot;
    this helper is intentionally scoped to the terminal gate only.)
    """
    try:
        pause = {
            "schema_version":         "1.0",
            "pause_type":             "gate_review",   # 4b adds "information"
            "paused_at_turn":         turn,
            "governance_profile_id":  governance_profile_id,
            "review_reason":          review_reason,
            "final_synthesis_text":   synthesis_text,
            "status":                 "awaiting_human_review",
            "paused_ts":              datetime.now().isoformat(timespec="seconds"),
        }
        out = ctx.session_dir / "pause_state.json"
        out.write_text(json.dumps(pause, indent=2), encoding="utf-8")
    except Exception as e:
        # Never let pause-state persistence crash the run; the session
        # still ends awaiting_human_review even if the file write fails,
        # but resume would be degraded. Log it.
        ctx.log(f"[GOVERNANCE] WARNING: could not write pause_state.json: {e}")


def _write_information_pause_state(
    *,
    ctx,
    turn: int,
    agent_name: str,
    agent_text: str,
    information_reason: str,
    history: List[Dict[str, str]],
    wrap_up: "WrapUpState",
    governance_profile_id: Optional[str],
    consecutive_synth_convergence: int,
) -> None:
    """Slice 4b: persist deliberation snapshot for mid-loop resume."""
    try:
        pause = {
            "schema_version":              "1.0",
            "pause_type":                  "information",
            "status":                      INFORMATION_PAUSE_END_REASON,
            "paused_at_turn":              turn,
            "paused_agent":                agent_name,
            "information_reason":        information_reason,
            "agent_text":                  agent_text,
            "history_snapshot":            list(history),
            "wrap_up_snapshot": {
                "requested":              wrap_up.requested,
                "reason":                 wrap_up.reason,
                "synth_done":             wrap_up.synth_done,
                "operator_done":          wrap_up.operator_done,
                "synth_wrap_up_turn":     wrap_up.synth_wrap_up_turn,
                "operator_wrap_up_turn":  wrap_up.operator_wrap_up_turn,
                "continuation_count":     wrap_up.continuation_count,
            },
            "consecutive_synth_convergence": consecutive_synth_convergence,
            "governance_profile_id":       governance_profile_id,
            "paused_ts":                   datetime.now().isoformat(timespec="seconds"),
        }
        out = ctx.session_dir / "pause_state.json"
        out.write_text(json.dumps(pause, indent=2), encoding="utf-8")
    except Exception as e:
        ctx.log(f"[GOVERNANCE] WARNING: could not write information pause_state.json: {e}")


def _restore_information_pause_resume(
    *,
    ctx,
    args: argparse.Namespace,
    history: List[Dict[str, str]],
    wrap_up: "WrapUpState",
    state: "_LoopState",
    log,
) -> None:
    """Slice 4b: restore deliberation from parent pause_state.json."""
    parent_id = getattr(args, "parent_session_id", None)
    if not parent_id:
        log("[RESUME] WARNING: --resume-after-information without parent id; "
            "starting fresh deliberation.")
        return

    parent_dir = ctx.session_dir.parent / parent_id
    pause_path = parent_dir / "pause_state.json"
    if not pause_path.is_file():
        log(f"[RESUME] WARNING: parent pause_state missing at {pause_path}; "
            "starting fresh deliberation.")
        return

    try:
        pause = json.loads(pause_path.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"[RESUME] WARNING: could not read parent pause_state: {e}")
        return

    if pause.get("pause_type") != "information":
        log("[RESUME] WARNING: parent pause is not information-type; "
            "starting fresh deliberation.")
        return

    snapshot = pause.get("history_snapshot") or []
    if not isinstance(snapshot, list):
        snapshot = []

    history.clear()
    history.extend(snapshot)

    paused_turn = int(pause.get("paused_at_turn") or 0)
    state.turn = paused_turn
    state.consecutive_synth_convergence = int(
        pause.get("consecutive_synth_convergence") or 0
    )

    wu = pause.get("wrap_up_snapshot") or {}
    if wu.get("requested"):
        wrap_up.requested = True
        wrap_up.reason = wu.get("reason")
    wrap_up.synth_done = bool(wu.get("synth_done"))
    wrap_up.operator_done = bool(wu.get("operator_done"))
    wrap_up.continuation_count = int(wu.get("continuation_count") or 0)

    resume_note = (
        "[Deliberation resumed after an information pause. The Director has "
        "provided the requested information — see the session seed and any "
        "new context documents. Continue deliberation from where you left off.]"
    )
    history.append({
        "role":    "user",
        "content": resume_note,
        "agent":   "Director",
    })

    log(f"[RESUME] Deliberation resumed after information pause "
        f"(parent {parent_id}, continuing after turn {paused_turn}).")
    log(f"         Reason at pause: {pause.get('information_reason', '')}")
    log()


def evaluate_review_gate(synthesis_text: str,
                         policy_bounds: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    Slice 4a human-review gate. Decide whether the planned Operator action
    requires a HUMAN to approve it before Operator runs. Runs at the same
    wrap-up chokepoint as the policy gate (after synth_done, before
    Operator), but AFTER the policy gate -- a hard policy block takes
    precedence over a review pause (no point asking a human to approve
    something the bounds forbid outright).

    Returns None if no review is required (Operator runs normally), or a
    short human-readable description of WHY review is required (the
    deliberation pauses with status 'awaiting_human_review'; the director
    approves / redirects before Operator executes).

    Driven by the profile's review settings, threaded in via the same
    bounds object the policy gate uses:
      - human_review_mode: "none" | "conditional" | "required"
      - review_required_for: list of conditions, e.g.
          "public_facing_artifact", "student_data_output",
          "external_action", "email_send", "file_write"

    "none"      -> never pauses (behavior-neutral; the default profile).
    "required"  -> pause whenever ANY listed condition is detected.
    "conditional" -> same detection, but only the listed conditions; this
                  is the K-12 default (pause for public-facing or
                  student-data output, let routine internal work through).

    Detection is COARSE and conservative, like the policy gate: it keys on
    intent signalled in the synthesis. The cost of an unnecessary pause is
    a director click; the cost of a false "no review needed" is an
    un-reviewed external/public artifact -- so when a listed condition is
    plausibly present, we pause.
    """
    if not policy_bounds:
        return None
    mode = (policy_bounds.get("human_review_mode") or "none").lower()
    if mode == "none":
        return None
    conditions = policy_bounds.get("review_required_for") or []
    if not conditions:
        return None

    text = (synthesis_text or "").lower()
    reasons = []

    def _wants(cond):
        return cond in conditions

    # public_facing_artifact: the planned output goes to an external/public
    # audience (parents, the public, a board packet for distribution).
    if _wants("public_facing_artifact"):
        if re.search(r"\b(parent|parents|public|community|press|newsletter|"
                     r"distribut|publish|website|families|staff[- ]wide|"
                     r"all[- ]staff|notification to)\b", text):
            reasons.append("the planned output is public- or parent-facing")

    # student_data_output: the artifact involves student PII / records.
    if _wants("student_data_output"):
        if re.search(r"\b(student data|student record|student pii|"
                     r"individual student|named student|grades?|iep|"
                     r"504 plan|disciplinary record|ferpa)\b", text):
            reasons.append("the planned output may involve student data")

    # external_action / email_send: an action leaves the system.
    if _wants("external_action") or _wants("email_send"):
        if re.search(r"\b(send|email|e-mail|submit|file with|report to|"
                     r"transmit|dispatch|notify|post to|upload to)\b", text):
            reasons.append("the plan involves an external action")

    # file_write: writing files (rarely a review trigger, but supported).
    if _wants("file_write"):
        if re.search(r"\b(write|save|create) (a |the )?(file|document|report)\b", text):
            reasons.append("the plan writes a file")

    if not reasons:
        return None

    # De-dup while preserving order.
    seen = []
    for r in reasons:
        if r not in seen:
            seen.append(r)
    return ("Human review is required before Operator executes because "
            + "; ".join(seen) + ".")


def evaluate_policy_gate(synthesis_text: str,
                         policy_bounds: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    Slice 3 policy gate. Inspect the ratified synthesis against the
    session's policy bounds and decide whether Operator's planned action
    is permitted. Runs at the wrap-up chokepoint, AFTER the Synthesizer's
    final pass and BEFORE Operator executes.

    Returns None if permitted (Operator runs as normal), or a short
    human-readable reason string if BLOCKED (Operator does not run; the
    session ends with status 'policy_blocked').

    Slice 3 is intentionally COARSE: it checks for external-action intent
    (notably email sending) signalled in the synthesis against the
    bounds' boolean flags. It does NOT attempt LLM-grade interpretation of
    nuanced policy language -- that fragility is exactly what the design
    note warns against. The deterministic, conservative checks here are:

      - If the synthesis plans to SEND EMAIL and the bounds set
        email_send_allowed=false  -> BLOCK.
      - If the synthesis plans an EXTERNAL ACTION and the bounds set
        external_actions_allowed=false -> BLOCK.

    A permissive bounds object (e.g. the default 'standard' ruleset) sets
    these flags true and so never blocks -- the gate is behavior-neutral
    for the default profile, matching Slice 2's design.

    If policy_bounds is None/empty, the gate is OFF (returns None): a
    session spawned without governance behaves exactly as before.
    """
    if not policy_bounds:
        return None
    text = (synthesis_text or "").lower()

    # --- Email-send intent ---
    email_allowed = policy_bounds.get("email_send_allowed", True)
    if not email_allowed:
        # Detect intent for ADAM/Operator to SEND an email as the planned
        # action -- not incidental mentions of email (e.g. "staff received
        # a phishing email"). We require an explicit send-construction
        # tying a send verb to an email object, with the actor being the
        # system. This is deliberately narrow: a false block on an
        # ordinary document is worse than missing an edge phrasing, and
        # the spawn-time skill denial (Slice 2) already removes the email
        # skill under these bounds as the primary defense -- this gate is
        # the secondary, content-aware check.
        send_email = bool(
            re.search(r"\boperator\s+(?:will\s+)?sends?\b[^.]{0,60}\b(email|e-mail)\b", text)
            or re.search(r"\bsend(?:s|ing)?\s+(?:an?\s+|the\s+)?(?:email|e-mail)\b", text)
            or re.search(r"\b(?:email|e-mail)\s+(?:the\s+\w+\s+)?to\s+\w", text)
            or re.search(r"\bdispatch(?:es|ing)?\s+(?:an?\s+)?(?:email|e-mail)\b", text)
        )
        if send_email:
            return ("Policy bounds for this session do not allow sending email "
                    "(email_send_allowed=false). Operator was prevented from "
                    "executing a send action.")

    # --- General external-action intent ---
    external_allowed = policy_bounds.get("external_actions_allowed", True)
    if not external_allowed:
        # Tightened detection. The earlier version fired whenever an
        # external-ish verb (incl. "publish") co-occurred anywhere with an
        # external-ish noun (incl. "public"). That caught DISCUSSION of a
        # future, human-controlled step -- e.g. a synthesis saying "the
        # Director fills placeholders before publication" -- and wrongly
        # blocked a session that was only planning to produce a reviewable
        # document. That false block also masked the human-review gate
        # (which runs after this one).
        #
        # We now require an explicit construction where OPERATOR / the
        # system performs an external transmission as the planned action:
        # sending/submitting/transmitting/posting TO an external recipient.
        # Words like "publish"/"publication" alone no longer trigger -- they
        # describe a downstream human step as often as an ADAM action, and
        # the artifact this turn is a document for review, not a transmission.
        external = bool(
            re.search(
                r"\boperator\s+(?:will\s+)?"
                r"(send|sends|submit|submits|transmit|transmits|post|posts|"
                r"upload|uploads|file|files|dispatch|dispatches)\b"
                r"[^.]{0,60}\b(to|with)\b",
                text,
            )
            or re.search(
                r"\b(send|submit|transmit|post|upload|dispatch)\b[^.]{0,40}"
                r"\b(nrc|agency|regulator|portal|recipient|distribution list|"
                r"external (system|party|recipient))\b",
                text,
            )
        )
        if external:
            return ("Policy bounds for this session do not allow external actions "
                    "(external_actions_allowed=false). Operator was prevented from "
                    "executing an external action.")

    return None


def detect_synth_convergence(synth_text: str) -> bool:
    """
    Conservative early-wrap-up signal.

    Returns True when the Synthesizer's turn indicates genuine,
    high-confidence convergence: it has reached a decisive recommendation
    and rates its own confidence High. This only ever ends a session
    EARLIER than the turn budget; the turn_budget trigger remains the
    backstop, and it never extends a session.

    The Synthesizer prompt (prompts/synthesizer.md) defines the markers:
      - a decisive ending: "Decision Point:" or
        "Synthesized recommendation:"   (NOT "Not ready for decision:")
      - "Confidence: High"              (NOT Medium / Low / absent)

    Design note on the Unresolved line: we intentionally do NOT require
    "Unresolved: None". A well-behaved Synthesizer routinely notes a
    residual caveat even when it has clearly converged (that is good
    synthesis, not non-convergence). Requiring "Unresolved: None" set the
    bar so high the trigger almost never fired in practice -- on a real
    20-turn session the Synthesizer hit "Decision Point + Confidence:
    High" four times, each with a non-blocking caveat noted, and the
    session still ran to the budget. So the decisive ending plus High
    confidence is the convergence signal; the "Not ready for decision"
    ending and Medium/Low confidence remain the vetoes that keep this
    conservative.
    """
    if not synth_text:
        return False
    text = synth_text

    # An explicit "not ready" ending vetoes convergence outright.
    if re.search(r"not\s+ready\s+for\s+decision\s*:", text, re.IGNORECASE):
        return False

    # Require a decisive ending.
    has_decision = bool(
        re.search(r"decision\s+point\s*:", text, re.IGNORECASE)
        or re.search(r"synthesized\s+recommendation\s*:", text, re.IGNORECASE)
    )
    if not has_decision:
        return False

    # Require explicit high confidence. Absence of a Confidence line, or
    # Medium/Low, does not qualify (conservative).
    if not re.search(r"confidence\s*:\s*high\b", text, re.IGNORECASE):
        return False

    return True


def _extract_wrap_up_block(operator_text: str) -> Optional[Dict[str, Any]]:
    """
    Find and parse a fenced ```wrap_up``` JSON block in Operator's final turn.

    Returns the parsed dict or None if absent / malformed. Tolerates
    surrounding prose and other fenced blocks; only extracts from the
    ```wrap_up``` fence specifically.
    """
    # Match ```wrap_up ... ``` with the inner content captured
    fence_pattern = re.compile(
        r"```wrap_up\s*\n?(.*?)\n?```", re.DOTALL | re.IGNORECASE,
    )
    match = fence_pattern.search(operator_text)
    if not match:
        return None
    inner = match.group(1).strip()

    # Reuse the balanced-bracket parser for resilient extraction
    json_str = _extract_first_json_value(inner)
    if json_str is None:
        sys.stderr.write("[SESSION_STATE] wrap_up block present but no balanced JSON found\n")
        return None
    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"[SESSION_STATE] wrap_up block JSON parse failed: {e}\n")
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _extract_operator_continue_block(operator_text: str) -> Optional[Dict[str, Any]]:
    """
    Find and parse a fenced ```operator_continue``` JSON block.

    Operator emits this block on continuation turns (turns AFTER the
    initial wrap-up) when more skill_calls are needed beyond what the
    wrap-up turn could fit. The block is smaller than a full wrap_up
    block since the narrative was already captured at the wrap-up turn.

    Expected schema:
      {
        "continuation_requested": bool,
        "reason": str (optional, free-text justification)
      }

    Returns the parsed dict or None if absent / malformed.
    """
    fence_pattern = re.compile(
        r"```operator_continue\s*\n?(.*?)\n?```", re.DOTALL | re.IGNORECASE,
    )
    match = fence_pattern.search(operator_text)
    if not match:
        return None
    inner = match.group(1).strip()

    json_str = _extract_first_json_value(inner)
    if json_str is None:
        sys.stderr.write(
            "[CONTINUATION] operator_continue block present but no balanced JSON found\n"
        )
        return None
    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"[CONTINUATION] operator_continue block JSON parse failed: {e}\n")
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def extract_continuation_signal(
    operator_text:           str,
    is_first_wrap_up:        bool,
) -> Tuple[bool, Optional[str], str]:
    """
    Determine whether Operator is requesting another execution turn after
    completing the current one.

    Resolution rules:
      - On the FIRST wrap-up turn: look for continuation_requested inside
        the wrap_up block. (Operator already emits wrap_up, so layering
        the signal there avoids requiring a second block.)
      - On CONTINUATION turns: look for the operator_continue block. The
        wrap_up block is not re-emitted on continuation turns.

    Returns (requested, reason, source):
      - requested: True if continuation requested, False otherwise
      - reason: optional free-text justification from operator_continue.reason
      - source: "wrap_up" | "operator_continue" | "absent" (for audit)

    Missing or malformed signals default to requested=False. This is the
    safe default -- if Operator forgets the signal entirely, the session
    ends cleanly rather than looping ambiguously.
    """
    if is_first_wrap_up:
        wrap = _extract_wrap_up_block(operator_text)
        if wrap is None:
            return False, None, "absent"
        flag = wrap.get("continuation_requested")
        if not isinstance(flag, bool):
            return False, None, "absent"
        return flag, None, "wrap_up"

    # Continuation turn: only the operator_continue block matters
    cont = _extract_operator_continue_block(operator_text)
    if cont is None:
        return False, None, "absent"
    flag = cont.get("continuation_requested")
    if not isinstance(flag, bool):
        return False, None, "absent"
    reason = cont.get("reason")
    if reason is not None and not isinstance(reason, str):
        reason = None
    return flag, reason, "operator_continue"


def _extract_decisions_from_audit(audit_path: Path) -> List[Dict[str, Any]]:
    """
    Scan the turn audit log for Synthesizer Decision Points and Operator
    acknowledgments. Returns a list of ratified decisions with IDs in the
    DEC-YYYYMMDD-NNN format.

    Decision detection: Synthesizer turns containing "Decision Point:" are
    treated as RATIFIED decisions. The next Operator turn (if any) is
    recorded as operator_ack_turn.
    """
    if not audit_path.exists():
        return []

    turns: List[Dict[str, Any]] = []
    try:
        with open(audit_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    turns.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        sys.stderr.write(f"[SESSION_STATE] failed to read audit log: {e}\n")
        return []

    decisions: List[Dict[str, Any]] = []
    date_str = datetime.now().strftime("%Y%m%d")
    sequence = 0

    for i, t in enumerate(turns):
        if t.get("agent") != "Synthesizer":
            continue
        content = t.get("content", "")
        dp_match = re.search(r"decision point:\s*(.+?)(?:\.|$|\n)", content, re.I)
        if not dp_match:
            continue

        sequence += 1
        decision_id = f"DEC-{date_str}-{sequence:03d}"

        # Find next Operator turn after this one
        operator_ack_turn = None
        for later in turns[i + 1:]:
            if later.get("agent") == "Operator":
                operator_ack_turn = later.get("turn")
                break

        decisions.append({
            "decision_id":       decision_id,
            "status":            "RATIFIED",
            "decision":          dp_match.group(1).strip(),
            "turn":              t.get("turn"),
            "synthesizer_text":  content,
            "operator_ack_turn": operator_ack_turn,
            "supersedes":        None,  # v1: always null; v2: prior decision_id if revising
        })

    return decisions


def _summarize_verification_audit(verification_path: Path) -> Dict[str, Any]:
    """
    Aggregate the per-claim verification.jsonl into status counts.
    Returns the counts dict for inclusion in governance_state.
    """
    counts = {
        "total_claims_checked": 0,
        "verified":             0,
        "partially_verified":   0,
        "unsupported":          0,
        "contradicted":         0,
        "needs_human_review":   0,
    }
    if not verification_path.exists():
        return counts

    try:
        with open(verification_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                counts["total_claims_checked"] += 1
                status = record.get("status", "")
                if status == "VERIFIED":
                    counts["verified"] += 1
                elif status == "PARTIALLY_VERIFIED":
                    counts["partially_verified"] += 1
                elif status == "UNSUPPORTED":
                    counts["unsupported"] += 1
                elif status == "CONTRADICTED":
                    counts["contradicted"] += 1
                elif status == "NEEDS_HUMAN_REVIEW":
                    counts["needs_human_review"] += 1
    except Exception as e:
        sys.stderr.write(f"[SESSION_STATE] failed to summarize verification: {e}\n")

    return counts


def _summarize_sentinel_concerns(sentinel_reg: SentinelRegistry) -> List[Dict[str, Any]]:
    """Snapshot of which concerns fired and when, from the registry."""
    return [
        {"label": label, "first_turn": turn_fired}
        for label, turn_fired in sentinel_reg._fired_at.items()
    ]


# ============================================================
# Deliberation loop
# ============================================================
#
# Step 5b-4: the deliberation loop body was lifted out of main() into
# run_deliberation_loop() in this module. main() now constructs the
# context, configures subsystems, and hands a single call to this
# function. The loop's mutable state (turn counter, continuation budget,
# end_reason, the wrap-up text captures, and the Truthseeker error
# accumulator) is returned to the caller via the _LoopState dataclass
# so main() can read it for the end-of-run summary log and
# session_state.json construction.
#
# Behavior preservation: every observable behavior of the previous
# inline loop is preserved. Variables that were mutated in main()'s
# scope (history list, wrap_up state, sentinel registry, director
# state, skill runtime invocations, _RUNTIME_CONFIG dict) are still
# mutated in place via the references passed in; they were never
# rebound, only their internal state was changed, so the function
# boundary makes no difference. Scalar mutations (turn counter,
# continuation_budget, end_reason) are now in _LoopState, but the
# control flow they govern is unchanged.

@dataclass
class _LoopState:
    """
    Mutable state for one deliberation run. Returned by
    run_deliberation_loop so the caller can build the session_state
    and emit the end-of-run summary.

    Internal to loop.py. Callers should treat it as a read-only
    snapshot once the loop returns.
    """
    end_reason:                str
    turn:                      int = 0
    continuation_budget:       int = 0
    synthesizer_wrap_up_text:  Optional[str] = None
    operator_wrap_up_text:     Optional[str] = None
    truthseeker_errors:        List[Tuple[int, str, str]] = field(default_factory=list)
    # Count of consecutive Synthesizer turns that signalled high-confidence
    # convergence. Resets to 0 on any Synthesizer turn that does not. Used
    # by the early-wrap-up trigger, which fires at >= 2.
    consecutive_synth_convergence: int = 0
    # Slice 3: set when the policy gate blocks Operator. policy_blocked
    # drives a first-class terminal session status; the reason is shown
    # to the director (not buried in logs).
    policy_blocked:      bool = False
    policy_block_reason: Optional[str] = None
    # Slice 4a: set when the human-review gate pauses the session before
    # Operator. awaiting_human_review drives a resumable terminal status;
    # the reason is the agent-facing explanation shown to the director.
    awaiting_human_review: bool = False
    review_reason:         Optional[str] = None
    # Hard invariant: self-modification / capability-acquisition requests.
    governance_boundary_blocked: bool = False
    governance_boundary_reason:  Optional[str] = None
    refusal_terminated:        bool = False
    refusal_reason:            Optional[str] = None
    awaiting_information:      bool = False
    information_reason:        Optional[str] = None


def _inject_director_messages(
    *,
    ctx:        SessionContext,
    director:   DirectorState,
    history:    List[Dict[str, str]],
    turn_idx:   int,
) -> None:
    """
    Drain queued Director messages and inject them into history/audit.

    Called at each turn boundary BEFORE the speaker selection. Each
    message becomes its own history entry with agent="Director" and a
    cleaned content string; the raw input is preserved in the audit
    log. Director messages are non-triggering (Sentinel and
    Truthseeker skip them via NON_TRIGGERING_SPEAKERS).

    Was a closure inside main() in the pre-5b-4 layout. Now a free
    function that takes the references it needs as explicit
    parameters; the runtime calls it via the loop and tests can call
    it directly without standing up a full main().
    """
    pending_msgs = director.drain_pending()
    for msg in pending_msgs:
        cleaned = format_director_transcript_entry(msg, director.display_name)
        history.append({
            "role":    "user",
            "content": cleaned,
            "agent":   "Director",
        })
        ctx.audit({
            "turn":         turn_idx,
            "agent":        "Director",
            "kind":         "director_message",
            "display_name": director.display_name,
            "raw_text":     msg.raw_text,
            "cleaned_text": cleaned,
            "target_agent": msg.target_agent,
            "warning":      msg.warning,
            "ts":           msg.ts,
        })
        ctx.emit_event("director_message", {
            "turn":          turn_idx,
            "display_name":  director.display_name,
            "target_agent":  msg.target_agent,
            "warning":       msg.warning,
            "content":       cleaned,
            # Part 8: provenance fields. message_id is non-null only
            # when the message originated from director_inbox.jsonl
            # (the GUI). The GUI uses this to mark queued messages
            # as consumed.
            "source":        getattr(msg, "source", "terminal"),
            "message_id":    getattr(msg, "message_id", None),
        })
        boundary_reason = evaluate_self_modification_boundary(msg.raw_text or cleaned)
        if boundary_reason:
            StopState.governance_boundary = boundary_reason
            ctx.log(f"[T{turn_idx}] >>> GOVERNANCE BOUNDARY: Director request blocked")
            ctx.log(f"           Reason: {boundary_reason}")
            ctx.log()
            ctx.emit_event("governance_boundary_blocked", {
                "turn":   turn_idx,
                "source": "director_message",
                "reason": boundary_reason,
            })
            ctx.audit({
                "turn":   turn_idx,
                "event":  "governance_boundary_blocked",
                "source": "director_message",
                "reason": boundary_reason,
                "ts":     datetime.now().isoformat(timespec="seconds"),
            })
        else:
            unsafe_reason = evaluate_unsafe_execution_boundary(msg.raw_text or cleaned)
            if unsafe_reason:
                StopState.refusal_termination = unsafe_reason
                ctx.log(f"[T{turn_idx}] >>> REFUSAL TERMINATION: unsafe request blocked")
                ctx.log(f"           Reason: {unsafe_reason}")
                ctx.log()
                ctx.emit_event("refusal_terminated", {
                    "turn":   turn_idx,
                    "source": "director_message",
                    "reason": unsafe_reason,
                })
                ctx.audit({
                    "turn":   turn_idx,
                    "event":  "refusal_terminated",
                    "source": "director_message",
                    "reason": unsafe_reason,
                    "ts":     datetime.now().isoformat(timespec="seconds"),
                })
        ctx.log(f"[T{turn_idx}] {cleaned}")
    if pending_msgs:
        ctx.log()


def consume_director_inbox(
    *,
    ctx:      SessionContext,
    director: DirectorState,
    turn_idx: int,
) -> None:
    """
    Part 8: read new lines from director_inbox.jsonl and enqueue them as
    Director messages, in the same way the stdin polling thread does for
    terminal input. Both input surfaces feed the same DirectorState queue;
    _inject_director_messages drains the queue once per turn boundary,
    making this method effectively a writer where the stdin thread is
    another writer.

    Called from the deliberation loop at the top of each iteration.
    Must NEVER raise: an exception here would crash the session and
    take the deliberation with it. All failure paths log a stderr
    message and emit a director_message_error event, then continue.

    Inbox line format (one JSON object per line):

        {
          "message_id":  "<gui-assigned id>",
          "ts":          "<isoformat>",
          "content":     "<raw director text, including >> prefix if any>"
        }

    Malformed lines (bad JSON, missing fields, oversize content) are
    skipped with a director_message_error event so the GUI can show
    the user what was rejected. The byte offset advances past every
    line we attempt to process, malformed or not — we never re-read
    a line that already triggered an error event.
    """
    # Inbox is optional. A session running without the GUI never has
    # one; a session running WITH the GUI may not have one yet if no
    # message has been submitted. Both are normal; treat as empty.
    path = ctx.director_inbox_path
    if not path.exists():
        return

    try:
        stat = path.stat()
    except OSError:
        return

    if stat.st_size <= ctx._inbox_pos:
        # No new bytes since last poll (or file was truncated; rare but
        # if it happens we reset to start). In the truncation case the
        # GUI would have to have unlinked + recreated the file, which
        # is unusual but safe to recover from.
        if stat.st_size < ctx._inbox_pos:
            ctx._inbox_pos = 0
        else:
            return

    # Read new bytes from the last position to current EOF, then advance
    # the position past every complete line. Partial last lines (file
    # mid-write by the GUI) are deferred to the next poll.
    try:
        with open(path, "rb") as f:
            f.seek(ctx._inbox_pos)
            chunk = f.read(stat.st_size - ctx._inbox_pos)
    except OSError as e:
        # File became unreadable between stat and open. Skip this poll.
        import sys as _sys
        _sys.stderr.write(f"[director_inbox.jsonl read failed: {e}]\n")
        return

    last_nl = chunk.rfind(b"\n")
    if last_nl == -1:
        # No complete line yet; leave the position where it is, try again
        # next iteration.
        return

    complete = chunk[: last_nl + 1].decode("utf-8", errors="replace")
    ctx._inbox_pos += last_nl + 1

    for raw_line in complete.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # JSON parse
        try:
            obj = json.loads(line)
        except Exception as e:
            ctx.emit_event("director_message_error", {
                "turn":         turn_idx,
                "error_type":   "malformed_json",
                "error_message": f"{type(e).__name__}: {e}",
                "raw_line":     line[:500],
                "message_id":   None,
            })
            ctx.log(f"[T{turn_idx}] director_inbox: malformed JSON, skipped")
            continue

        if not isinstance(obj, dict):
            ctx.emit_event("director_message_error", {
                "turn":         turn_idx,
                "error_type":   "not_a_json_object",
                "error_message": f"line was a {type(obj).__name__}, expected object",
                "raw_line":     line[:500],
                "message_id":   None,
            })
            continue

        message_id = obj.get("message_id")
        ts         = obj.get("ts")
        content    = obj.get("content")

        if not message_id or not isinstance(message_id, str):
            ctx.emit_event("director_message_error", {
                "turn":         turn_idx,
                "error_type":   "missing_message_id",
                "error_message": "inbox line has no message_id (required for consume tracking)",
                "raw_line":     line[:500],
                "message_id":   message_id,
            })
            continue

        if not isinstance(content, str) or not content.strip():
            ctx.emit_event("director_message_error", {
                "turn":         turn_idx,
                "error_type":   "missing_content",
                "error_message": "inbox line has empty or non-string content",
                "raw_line":     line[:500],
                "message_id":   message_id,
            })
            continue

        # Size cap (8000 chars, matching the GUI-side cap). The cap is
        # enforced here too so a misbehaving inbox writer can't get
        # around it. Director messages longer than 8000 chars almost
        # certainly indicate an error in the calling client; truncating
        # silently would hide the problem.
        if len(content) > 8000:
            ctx.emit_event("director_message_error", {
                "turn":         turn_idx,
                "error_type":   "content_too_long",
                "error_message": f"content is {len(content)} chars; max is 8000",
                "raw_line":     line[:200] + "...",
                "message_id":   message_id,
            })
            continue

        # Hand off to DirectorState.enqueue_inbox_message, which runs
        # the same parser used by the stdin thread (>>AGENT addressing,
        # >>halt detection, etc.) and pushes a DirectorMessage onto the
        # queue. The injection at _inject_director_messages drains the
        # queue normally — it doesn't know or care whether the messages
        # came from terminal or GUI.
        msg, error_reason, halt_triggered = director.enqueue_inbox_message(
            raw_text   = content,
            message_id = message_id,
            ts         = ts,
        )

        if halt_triggered:
            # Halt is a state-change command, not a message. The
            # director.halt_requested flag is already set; the loop
            # picks it up on the next turn. We still emit a
            # director_message event so the GUI can confirm the halt
            # request was received (mapping message_id -> consumed).
            ctx.emit_event("director_message", {
                "turn":          turn_idx,
                "display_name":  director.display_name,
                "target_agent":  None,
                "warning":       None,
                "content":       f"[{director.display_name}] halt requested",
                "source":        "gui_inbox",
                "message_id":    message_id,
                "command":       "halt",
            })
            ctx.audit({
                "turn":         turn_idx,
                "agent":        "Director",
                "kind":         "director_command",
                "command":      "halt",
                "display_name": director.display_name,
                "source":       "gui_inbox",
                "message_id":   message_id,
                "ts":           datetime.now().isoformat(timespec='seconds'),
            })
            ctx.log(f"[T{turn_idx}] [{director.display_name}] halt requested via GUI inbox")
            continue

        if error_reason is not None:
            ctx.emit_event("director_message_error", {
                "turn":         turn_idx,
                "error_type":   "parser_rejected",
                "error_message": error_reason,
                "raw_line":     line[:500],
                "message_id":   message_id,
            })
            continue

        # Successful enqueue. The message is now on director.pending
        # and will be drained by _inject_director_messages on the next
        # call. No event yet — director_message fires from the
        # injector, with the full transcript-formatted content.


def _emit_skill_events_from_results(
    *,
    ctx:    SessionContext,
    turn:   int,
    agent:  str,
    results: List[Dict[str, Any]],
) -> None:
    """
    Emit one skill_invoked event per result returned by
    skill_runtime.process_agent_output. Centralized here because the
    skill-processing block runs from two places in the loop (the
    advisory/post-turn site and the wrap-up/continuation site) and we
    want identical event shapes from both.

    Result dicts come from the skill runtime and carry at least
    `status`, `skill`, `action`. Optional fields are picked up when
    present and omitted when absent; the GUI binds to whatever it
    finds and renders accordingly.

    Field categories forwarded:

      - Universal:        artifact_id, invocation_id (every skill)
      - File-producing:   path, filename, format, sha256, size_bytes
                          (document.create, slidedeck.create)
      - Action skills:    to, cc, bcc_count, subject, attachments,
                          message_id, sent_at, provider, sent
                          (email.send)
      - Error:            error_class, error_message (failure paths)

    This is intentionally a union of every field shape we know any
    skill might emit. A skill that doesn't emit a field just doesn't
    have it in its result dict, so the field doesn't appear in the
    event. There's no per-skill switch here -- when a new skill type
    needs a new field surfaced, add the field name to the appropriate
    tuple below and the runtime forwards it automatically.
    """
    # File-producing skill fields (document.create, slidedeck.create, etc).
    # The path is normalized to a session-relative string by the skill
    # runtime, so the GUI can construct artifact URLs from it.
    #
    # Part 9.2: added relpath and workspace_relpath for multi-file
    # workspace skills (coder). relpath is the session-artifacts-
    # relative path used by the GUI to build artifact URLs:
    #
    #     /api/sessions/<id>/artifacts/<relpath>
    #
    # workspace_relpath points at the parent directory of multi-file
    # packages; informational for now. The flat-file skills (document,
    # slidedeck) don't emit these fields; the GUI falls back to
    # filename when relpath is absent.
    FILE_FIELDS = (
        "path", "filename", "format", "sha256", "size_bytes",
        "relpath", "workspace_relpath",
    )
    # Email-specific fields (email.send). cc and to are arrays of
    # recipient addresses; bcc_count is just a count for privacy.
    # message_id is the SMTP server's assigned ID; sent_at is the
    # actual transmission timestamp.
    EMAIL_FIELDS = (
        "to", "cc", "bcc_count", "subject", "attachments",
        "message_id", "sent_at", "provider", "sent",
    )
    # Identity fields shared by all skill invocations.
    IDENT_FIELDS = (
        "artifact_id", "invocation_id",
    )
    # Error info -- present only on failure paths.
    ERROR_FIELDS = (
        "error_class", "error_message",
    )

    for r in results or []:
        payload: Dict[str, Any] = {
            "turn":    turn,
            "agent":   agent,
            "skill":   r.get("skill"),
            "action":  r.get("action"),
            "status":  r.get("status"),
        }
        # Forward any field that the skill produced. We chain through
        # the categories rather than picking by skill name so a new
        # skill that produces (e.g.) document-style fields gets them
        # surfaced without any code change here.
        for opt_field in IDENT_FIELDS + FILE_FIELDS + EMAIL_FIELDS + ERROR_FIELDS:
            if opt_field in r:
                payload[opt_field] = r[opt_field]
        ctx.emit_event("skill_invoked", payload)


def run_deliberation_loop(
    ctx:                          SessionContext,
    args:                         argparse.Namespace,
    *,
    agents:                       Dict[str, Any],
    models:                       Dict[str, Any],
    providers:                    Dict[str, Any],
    primes:                       Dict[str, str],
    history:                      List[Dict[str, str]],
    wrap_up:                      WrapUpState,
    sentinel_reg:                 SentinelRegistry,
    director:                     DirectorState,
    skill_catalog:                Any,
    skill_runtime:                SkillRuntime,
    searxng_url:                  str,
    context_files_by_id:          Dict[str, "ContextFile"],
    context_files_by_filename:    Dict[str, "ContextFile"],
) -> _LoopState:
    """
    Execute the main deliberation loop. Returns a _LoopState carrying
    the values main() needs for the end-of-run summary and
    session_state.json construction.

    The previous inline loop lived in main() at roughly lines
    907-1308 of adam_agent_chat.py (pre-5b-4). This function is a
    structural lift, not a semantic redesign: every branch, audit
    record, log line, and break path that existed in main()'s loop
    body is reproduced here.

    Mutated arguments (all in place):
      - history:       loop appends agent/system/Director/Truthseeker
                       entries on every turn
      - wrap_up:       trigger(), synth_done, operator_done,
                       continuation_* fields are updated
      - sentinel_reg:  record_fire() is called when Sentinel speaks
      - director:      drain_pending() consumes queued messages
      - skill_runtime: process_agent_output() may add to .invocations

    Read-only references:
      - ctx, args, agents, models, providers, primes, skill_catalog,
        searxng_url, context_files_by_id, context_files_by_filename
    """
    # Import call_model lazily at function level. It is already imported
    # at module top, but the lazy alias here matches the original loop's
    # local-name binding so any future swap can be done in one place.

    # --- Local writer aliases ---
    # The original loop used `log`, `audit`, `verification_audit` as
    # local names (introduced by `log = ctx.log` etc. earlier in main).
    # Re-establish those here so the body below stays line-identical to
    # the original wherever practical.
    log                = ctx.log
    audit              = ctx.audit
    verification_audit = ctx.verification_audit

    # Slice 3: parse the policy-bounds JSON arg once. The backend passes
    # the resolved ruleset as a compact JSON string via --policy-bounds.
    # If absent or unparseable, the policy gate is OFF (permissive) and
    # the session behaves exactly as a pre-governance run.
    policy_bounds: Optional[Dict[str, Any]] = None
    _pb_raw = getattr(args, "policy_bounds", None)
    if _pb_raw:
        try:
            parsed_pb = json.loads(_pb_raw)
            if isinstance(parsed_pb, dict):
                policy_bounds = parsed_pb
        except (ValueError, TypeError):
            log("[GOVERNANCE] --policy-bounds could not be parsed; policy gate OFF")

    # --- Loop state ---
    # end_reason is the deliberation default; every exit path that
    # makes sense overwrites this. The default fires only if the loop
    # falls through without an explicit exit (e.g. extremely short
    # sessions where wrap-up never triggers), matching the prior
    # behavior of the inline default at the top of main().
    state = _LoopState(
        end_reason = f"deliberation cap of {args.max_turns} reached without wrap-up",
    )

    deliberation_cap = args.max_turns

    # Slice 4a RESUME MODE. When the session was relaunched after a human-
    # review pause (the director approved/redirected via the resume
    # endpoint), we do NOT re-run the advisory deliberation. The synthesis
    # was already settled at pause time, and the GUI has composed the
    # original synthesis + the director's guidance into the seed (using the
    # continuation builder). We jump straight to wrap-up so Operator runs
    # once and produces the artifact. wrap_up.synth_done is pre-set so the
    # router's wrap-up branch routes to Operator on the first turn; the
    # in-loop review gate is skipped on resumed runs (guarded by
    # args.resume_after_review where it is evaluated) so we don't pause
    # again.
    if getattr(args, "resume_after_review", False):
        wrap_up.trigger("human_review_resumed")
        wrap_up.synth_done = True
        # The settled synthesis text is carried in the seed/context that the
        # GUI composed; record a marker so session_state reflects the resume.
        state.synthesizer_wrap_up_text = (
            state.synthesizer_wrap_up_text
            or "[resumed after human review — synthesis settled at pause; "
               "director guidance applied]"
        )
        log("[RESUME] Session resumed after human review; routing directly "
            "to Operator (deliberation already settled at pause).")
        log()

    elif getattr(args, "resume_after_information", False):
        _restore_information_pause_resume(
            ctx=ctx,
            args=args,
            history=history,
            wrap_up=wrap_up,
            state=state,
            log=log,
        )

    while state.turn < deliberation_cap + state.continuation_budget:
        state.turn += 1
        turn = state.turn

        if StopState.hard_stop:
            state.end_reason = "hard stop requested"
            break

        # Part 8: poll director_inbox.jsonl for GUI-submitted messages
        # and push them onto the same queue the stdin thread uses.
        # Runs BEFORE _inject_director_messages so messages submitted
        # between the previous turn boundary and this one land in the
        # same drain. Never raises.
        consume_director_inbox(
            ctx=ctx, director=director, turn_idx=turn,
        )

        # Drain Director messages BEFORE turn-budget check, so a director
        # halt that arrived before turn N is honored at turn N
        _inject_director_messages(
            ctx=ctx, director=director, history=history, turn_idx=turn,
        )

        if StopState.governance_boundary:
            state.governance_boundary_blocked = True
            state.governance_boundary_reason = StopState.governance_boundary
            state.end_reason = GOVERNANCE_BOUNDARY_END_REASON
            log(f"[T{turn}] >>> GOVERNANCE BOUNDARY: session stopped")
            log(f"           Reason: {StopState.governance_boundary}")
            log()
            break

        if StopState.refusal_termination:
            state.refusal_terminated = True
            state.refusal_reason = StopState.refusal_termination
            state.end_reason = REFUSAL_TERMINATED_END_REASON
            log(f"[T{turn}] >>> REFUSAL TERMINATION: session stopped")
            log(f"           Reason: {StopState.refusal_termination}")
            log()
            break

        # Director halt: trigger wrap-up if requested
        if director.halt_requested and not wrap_up.requested:
            wrap_up.trigger("director_halt")
            log(f"[T{turn}] >>> Entering WRAP-UP PHASE (reason: director_halt by {director.display_name})")
            log()
            ctx.emit_event("wrap_up_triggered", {
                "turn":             turn,
                "reason":           "director_halt",
                "triggered_by":     director.display_name,
                "synth_wrap_turn":  wrap_up.synth_wrap_up_turn,
                "op_wrap_turn":     wrap_up.operator_wrap_up_turn,
            })

        # Turn-budget trigger: at synth_wrap_up_turn, enter wrap-up phase
        # if not already triggered by another source
        if turn >= wrap_up.synth_wrap_up_turn and not wrap_up.requested:
            wrap_up.trigger("turn_budget")
            log(f"[T{turn}] >>> Entering WRAP-UP PHASE (reason: turn_budget)")
            log()
            ctx.emit_event("wrap_up_triggered", {
                "turn":             turn,
                "reason":           "turn_budget",
                "triggered_by":     None,
                "synth_wrap_turn":  wrap_up.synth_wrap_up_turn,
                "op_wrap_turn":     wrap_up.operator_wrap_up_turn,
            })

        agent_name, invocation_note, concern_label = select_next_speaker(
            history=history,
            synth_cadence=args.synth_cadence,
            current_turn=turn,
            sentinel_reg=sentinel_reg,
            wrap_up=wrap_up,
            director=director,
            skill_catalog=skill_catalog,
            skill_args_parsed=args.skill_args_parsed,
        )

        # Detect Director participation in this turn's routing.
        # - "addressed this turn to you" marker => true addressing override
        #   (Director typed >>AgentName: ...)
        # - "DIRECTOR INPUT" marker => broadcast engagement added on top
        #   of whatever the base routing was
        has_director_address    = bool(invocation_note and "addressed this turn to you" in invocation_note)
        has_director_engagement = bool(invocation_note and "DIRECTOR INPUT" in invocation_note)

        base_reason = (
            "wrap-up-synthesizer"     if wrap_up.is_active() and agent_name == "Synthesizer" and not wrap_up.synth_done
            else "wrap-up-operator"   if wrap_up.is_active() and agent_name == "Operator"    and not wrap_up.operator_done
            else "operator-continuation" if wrap_up.is_active() and agent_name == "Operator" and wrap_up.continuation_active
            else "director-addressed" if has_director_address
            else "operator-triggered"   if agent_name == "Operator"    and invocation_note
            else "sentinel-triggered"   if agent_name == "Sentinel"    and invocation_note
            else "synthesizer-cadence"  if agent_name == "Synthesizer"
            else "advisory-rotation"
        )
        # Broadcast engagement is a modifier on top of the base reason.
        # When addressing was the primary driver, the addressed label
        # already implies Director involvement; no suffix needed.
        if has_director_engagement and not has_director_address:
            routing_reason = f"{base_reason}+director-engaged"
        else:
            routing_reason = base_reason

        # Token-budget tier selection (three tiers, in priority order):
        #
        #   1. is_wrap_up_turn -> max_tokens_wrap_up (largest, ~16000)
        #      The wrap-up Synthesizer/Operator and continuation turns
        #      that carry full closing artifacts.
        #
        #   2. is_artifact_turn -> max_tokens_artifact (mid-size, ~8000)
        #      A non-wrap-up Operator turn triggered by a ratified Decision
        #      Point, with an executable artifact skill in the catalog.
        #      Without this tier, the T6-style truncation observed in the
        #      2026-05-23 superintendent-search session recurs: a 1500-
        #      token Operator turn cannot hold both an implementation plan
        #      AND a skill_call whose 'content' arg carries an artifact.
        #
        #   3. otherwise -> max_tokens (smallest, agent default)
        #      Advisory rotation, Sentinel concerns, etc.
        is_wrap_up_turn = (
            routing_reason.startswith("wrap-up-")
            or routing_reason.startswith("operator-continuation")
        )
        is_artifact_turn = (
            not is_wrap_up_turn
            and routing_reason == "operator-triggered"
            and skill_catalog is not None
            and bool(skill_catalog.executable)
        )
        model_id, max_tokens, temperature = resolve_agent_call_params(
            agent_name, agents, models,
            is_wrap_up=is_wrap_up_turn,
            is_artifact=is_artifact_turn,
        )

        messages = build_transcript_messages(
            history=history,
            history_limit=args.history_messages,
            current_agent=agent_name,
            invocation_note=invocation_note,
            current_turn=turn,
            max_turns=args.max_turns,
            wrap_up_active=wrap_up.is_active(),
        )

        # Emit turn_started before call_model. This is the event the
        # GUI uses to render "Logician (executing)" while the LLM is
        # generating. If call_model raises, turn_error fires with no
        # turn_completed — the GUI must handle that state.
        _turn_start_time = time.perf_counter()
        ctx.emit_event("turn_started", {
            "turn":            turn,
            "agent":           agent_name,
            "routing_reason":  routing_reason,
            "model_id":        model_id,
            "max_tokens":      max_tokens,
            "temperature":     temperature,
            "invocation_note": invocation_note,
        })

        try:
            reply = call_model(
                model_id=model_id,
                system_prompt=primes[agent_name],
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                models=models,
                providers=providers,
            )
        except Exception as e:
            log(f"[ERROR] {agent_name} call failed at T{turn}: {type(e).__name__}: {e}")
            audit({"turn": turn, "agent": agent_name, "kind": "error", "error": str(e),
                   "ts": datetime.now().isoformat(timespec='seconds')})
            ctx.emit_event("turn_error", {
                "turn":           turn,
                "agent":          agent_name,
                "routing_reason": routing_reason,
                "error_type":     type(e).__name__,
                "error_message":  str(e),
                "duration_ms":    int((time.perf_counter() - _turn_start_time) * 1000),
            })
            state.end_reason = f"{agent_name} API error"
            break

        # Record successful Sentinel fire in the registry only after the call
        # succeeded - we don't want a failed call to consume the cooldown slot
        if agent_name == "Sentinel" and concern_label:
            sentinel_reg.record_fire(concern_label, turn)

        ts = datetime.now().strftime("%H:%M:%S")
        log(f"[T{turn} {ts}] {agent_name} ({routing_reason}): {reply}")
        if invocation_note:
            preview = invocation_note[:200] + ('...' if len(invocation_note) > 200 else '')
            log(f"           Router note: {preview}")
        log()

        history.append({"role": "assistant", "content": reply, "agent": agent_name})

        # Slice 4b: mid-loop information pause. When the Synthesizer signals
        # missing input ("Not ready for decision:" etc.), pause resumably
        # instead of cycling to the turn budget or running Operator.
        if (
            agent_name == "Synthesizer"
            and not getattr(args, "resume_after_review", False)
        ):
            info_reason = evaluate_information_pause(reply, agent_name)
            if info_reason is not None:
                audit({
                    "turn":             turn,
                    "agent":            agent_name,
                    "model_id":         model_id,
                    "routing_reason":   routing_reason,
                    "invocation_note":  invocation_note,
                    "concern_label":    concern_label,
                    "max_tokens":       max_tokens,
                    "temperature":      temperature,
                    "content":          reply,
                    "ts":               datetime.now().isoformat(timespec="seconds"),
                })
                ctx.emit_event("turn_completed", {
                    "turn":             turn,
                    "agent":            agent_name,
                    "routing_reason":   routing_reason,
                    "model_id":         model_id,
                    "concern_label":    concern_label,
                    "content":          reply,
                    "content_preview":  build_content_preview(reply),
                    "content_length":   len(reply) if reply else 0,
                    "duration_ms":      int((time.perf_counter() - _turn_start_time) * 1000),
                })
                state.awaiting_information = True
                state.information_reason = info_reason
                state.end_reason = INFORMATION_PAUSE_END_REASON
                _write_information_pause_state(
                    ctx=ctx,
                    turn=turn,
                    agent_name=agent_name,
                    agent_text=reply,
                    information_reason=info_reason,
                    history=history,
                    wrap_up=wrap_up,
                    governance_profile_id=getattr(args, "governance_profile_id", None),
                    consecutive_synth_convergence=state.consecutive_synth_convergence,
                )
                log(f"[T{turn}] >>> INFORMATION PAUSE: deliberation suspended")
                log(f"           Reason: {info_reason}")
                log()
                ctx.emit_event("awaiting_information", {
                    "turn":                  turn,
                    "governance_profile_id": getattr(args, "governance_profile_id", None),
                    "reason":                info_reason,
                    "agent":                 agent_name,
                })
                audit({
                    "turn":   turn,
                    "event":  "awaiting_information",
                    "reason": info_reason,
                    "ts":     datetime.now().isoformat(timespec="seconds"),
                })
                break

        # Early-wrap-up trigger (conservative convergence detection).
        # If the Synthesizer signals genuine, high-confidence convergence
        # on TWO CONSECUTIVE Synthesizer passes, enter wrap-up now rather
        # than cycling the advisory rotation to the turn budget. Requiring
        # two consecutive signals (not one) guards against an eager first
        # Synthesizer pass that sounds confident but still flags a
        # critical unknown -- a single confident pass can be premature;
        # the same high-confidence Decision Point surviving the next
        # Synthesizer turn (~synth_cadence turns later, after more
        # advisory input) is a trustworthy "genuinely settled" signal.
        # This only ends a session EARLIER than the budget; the
        # turn_budget trigger remains the backstop.
        if agent_name == "Synthesizer" and not wrap_up.requested:
            if detect_synth_convergence(reply):
                state.consecutive_synth_convergence += 1
            else:
                state.consecutive_synth_convergence = 0

            if state.consecutive_synth_convergence >= 2:
                wrap_up.trigger("consensus_reached")
                log(f"[T{turn}] >>> Entering WRAP-UP PHASE (reason: consensus_reached)")
                log()
                ctx.emit_event("wrap_up_triggered", {
                    "turn":             turn,
                    "reason":           "consensus_reached",
                    "triggered_by":     "Synthesizer",
                    "synth_wrap_turn":  wrap_up.synth_wrap_up_turn,
                    "op_wrap_turn":     wrap_up.operator_wrap_up_turn,
                })

        audit({
            "turn":             turn,
            "agent":            agent_name,
            "model_id":         model_id,
            "routing_reason":   routing_reason,
            "invocation_note":  invocation_note,
            "concern_label":    concern_label,
            "max_tokens":       max_tokens,
            "temperature":      temperature,
            "content":          reply,
            "ts":               datetime.now().isoformat(timespec='seconds'),
        })
        ctx.emit_event("turn_completed", {
            "turn":             turn,
            "agent":            agent_name,
            "routing_reason":   routing_reason,
            "model_id":         model_id,
            "concern_label":    concern_label,
            "content":          reply,
            "content_preview":  build_content_preview(reply),
            "content_length":   len(reply) if reply else 0,
            "duration_ms":      int((time.perf_counter() - _turn_start_time) * 1000),
        })

        # If this turn was part of the forced wrap-up sequence, mark it and
        # capture the text for session_state construction. If both wrap-up
        # turns are complete, exit the loop with end_reason reflecting how
        # wrap-up was triggered.
        #
        # IMPORTANT: SkillRuntime MUST run on wrap-up turns. The wrap-up turn
        # is exactly where Operator is most likely to invoke document.create
        # to produce the final artifact. Truthseeker is correctly skipped
        # for wrap-up turns (closing artifacts don't introduce new claims
        # that need verification beyond what was already deliberated), but
        # skills are different -- they're the execution layer, not the
        # verification layer.
        if routing_reason == "wrap-up-synthesizer":
            wrap_up.synth_done = True
            state.synthesizer_wrap_up_text = reply

            # Slice 3 POLICY GATE. The ratified synthesis now exists and
            # Operator has not run. Check the planned action against this
            # session's policy bounds BEFORE Operator executes. This is
            # the wrap-up chokepoint the Operator-terminal-only change
            # created. If the gate blocks, the session ends here with
            # status 'policy_blocked' and a visible reason -- Operator
            # never runs, no artifact is produced. (Policy check runs
            # before any human review, per the design: fail fast and
            # deterministically before spending human attention.)
            block_reason = evaluate_policy_gate(reply, policy_bounds)
            if block_reason is not None:
                state.policy_blocked = True
                state.policy_block_reason = block_reason
                state.end_reason = "policy_blocked"
                log(f"[T{turn}] >>> POLICY GATE BLOCKED Operator "
                    f"(profile: {getattr(args, 'governance_profile_id', None)})")
                log(f"           Reason: {block_reason}")
                log()
                ctx.emit_event("policy_blocked", {
                    "turn":                  turn,
                    "governance_profile_id": getattr(args, "governance_profile_id", None),
                    "reason":                block_reason,
                })
                audit({
                    "turn":   turn,
                    "event":  "policy_blocked",
                    "governance_profile_id": getattr(args, "governance_profile_id", None),
                    "reason": block_reason,
                    "ts":     datetime.now().isoformat(timespec="seconds"),
                })
                # Mark wrap-up "complete" so no further routing occurs,
                # and end the loop. Operator is intentionally NOT run.
                wrap_up.operator_done = True
                break

            # Empty-termination gate. If the synthesis refused the requested
            # action, Operator must not run and no substitute artifact may be
            # produced (the helpfulness reflex that created incident logs and
            # stray files after a refusal).
            refusal_reason = evaluate_refusal_termination(reply)
            if refusal_reason is not None:
                state.refusal_terminated = True
                state.refusal_reason = refusal_reason
                state.end_reason = REFUSAL_TERMINATED_END_REASON
                log(f"[T{turn}] >>> REFUSAL TERMINATION: Operator skipped")
                log(f"           Reason: {refusal_reason}")
                log()
                ctx.emit_event("refusal_terminated", {
                    "turn":                  turn,
                    "governance_profile_id": getattr(args, "governance_profile_id", None),
                    "reason":                refusal_reason,
                })
                audit({
                    "turn":   turn,
                    "event":  "refusal_terminated",
                    "governance_profile_id": getattr(args, "governance_profile_id", None),
                    "reason": refusal_reason,
                    "ts":     datetime.now().isoformat(timespec="seconds"),
                })
                wrap_up.operator_done = True
                break

            # Slice 4a HUMAN-REVIEW GATE. Runs only if the policy gate did
            # not block (a hard policy block takes precedence -- no point
            # asking a human to approve something the bounds forbid). If
            # the profile requires review for this planned action, PAUSE:
            # persist the state needed to resume, surface
            # 'awaiting_human_review', and exit cleanly WITHOUT running
            # Operator. The director approves/redirects via the resume
            # endpoint, which relaunches the session in resume mode with
            # the guidance composed into context and Operator then runs.
            #
            # On a RESUMED run (args.resume_after_review set), we have
            # already been approved -- skip the gate so we don't pause
            # again in an infinite loop.
            if not getattr(args, "resume_after_review", False):
                review_reason = evaluate_review_gate(reply, policy_bounds)
                if review_reason is not None:
                    state.awaiting_human_review = True
                    state.review_reason = review_reason
                    state.end_reason = "awaiting_human_review"
                    _write_pause_state(
                        ctx=ctx,
                        turn=turn,
                        synthesis_text=reply,
                        review_reason=review_reason,
                        governance_profile_id=getattr(args, "governance_profile_id", None),
                    )
                    log(f"[T{turn}] >>> HUMAN-REVIEW PAUSE before Operator "
                        f"(profile: {getattr(args, 'governance_profile_id', None)})")
                    log(f"           Reason: {review_reason}")
                    log()
                    ctx.emit_event("awaiting_human_review", {
                        "turn":                  turn,
                        "governance_profile_id": getattr(args, "governance_profile_id", None),
                        "reason":                review_reason,
                        "synthesis_preview":     build_content_preview(reply),
                    })
                    audit({
                        "turn":   turn,
                        "event":  "awaiting_human_review",
                        "governance_profile_id": getattr(args, "governance_profile_id", None),
                        "reason": review_reason,
                        "ts":     datetime.now().isoformat(timespec="seconds"),
                    })
                    # Pause = clean exit. Operator NOT run; the session is
                    # resumable. Mark operator_done so the loop terminates
                    # rather than routing to Operator.
                    wrap_up.operator_done = True
                    break
        elif routing_reason == "wrap-up-operator" or routing_reason == "operator-continuation":
            is_first_wrap_up = (routing_reason == "wrap-up-operator")
            if is_first_wrap_up:
                wrap_up.operator_done = True
                state.operator_wrap_up_text  = reply
            else:
                # Continuation turn: increment counter. The wrap-up text
                # captured for session_state remains the FIRST wrap-up's
                # text (which contains the narrative_summary, open_questions,
                # etc.). Continuation turns are pure execution and don't
                # contribute to the operator_summary.
                wrap_up.continuation_count += 1

            # Process any skill_call blocks in this turn's output. For
            # continuation turns, this is the whole point -- the prior
            # turn's results are already in history, so this turn's
            # skill_calls can reference them.
            if skill_catalog.enabled:
                try:
                    results, transcript_text = skill_runtime.process_agent_output(
                        agent=agent_name, turn=turn, agent_output=reply,
                    )
                    _emit_skill_events_from_results(
                        ctx=ctx, turn=turn, agent=agent_name, results=results,
                    )
                    if transcript_text:
                        log(transcript_text)
                        log()
                        # On continuation turns, we DO inject skill results
                        # into history so subsequent continuation turns (if
                        # granted) can see them. On the final continuation
                        # before break, this is harmless (no agent will
                        # consume it). On the first wrap-up turn that's
                        # going to continue, this is critical.
                        history.append({
                            "role":    "user",
                            "content": transcript_text,
                            "agent":   "System",
                        })
                        audit({
                            "turn":         turn,
                            "agent":        "System",
                            "kind":         "skill_results_injected_wrap_up"
                                            if is_first_wrap_up
                                            else "skill_results_injected_continuation",
                            "result_count": len(results),
                            "successes":    sum(1 for r in results if r["status"] == "success"),
                            "failures":     sum(1 for r in results if r["status"] != "success"),
                            "ts":           datetime.now().isoformat(timespec='seconds'),
                        })
                except Exception as e:
                    log(f"[SKILL_RUNTIME ERROR] T{turn} "
                        f"({routing_reason}): {type(e).__name__}: {e}")
                    log()

            # Check whether Operator wants a continuation turn.
            oc_cfg = _RUNTIME_CONFIG.get("operator_continuations", {})
            oc_enabled = bool(oc_cfg.get("enabled", True))
            oc_max     = int(oc_cfg.get("max_operator_continuations", 4))
            oc_hard_cap = int(oc_cfg.get("hard_cap", 10))
            effective_max = min(oc_max, oc_hard_cap)  # defense in depth

            cont_requested, cont_reason, cont_source = extract_continuation_signal(
                reply, is_first_wrap_up=is_first_wrap_up,
            )

            grant_continuation = False
            if oc_enabled and cont_requested:
                if wrap_up.continuation_count < effective_max:
                    grant_continuation = True
                else:
                    # Cap reached. Audit it but don't grant.
                    wrap_up.continuation_cap_reached = True
                    audit({
                        "turn":               turn,
                        "agent":              "System",
                        "kind":               "operator_continuation_cap_reached",
                        "max_continuations":  effective_max,
                        "additional_requested": True,
                        "signal_source":      cont_source,
                        "ts":                 datetime.now().isoformat(timespec='seconds'),
                    })
                    log(f"[T{turn}] >>> Operator requested another continuation "
                        f"but cap ({effective_max}) is reached. Ending session.")
                    log()
                    ctx.emit_event("continuation_denied", {
                        "turn":              turn,
                        "reason":            "cap_reached",
                        "max_continuations": effective_max,
                        "signal_source":     cont_source,
                    })
            elif cont_requested and not oc_enabled:
                # Operator asked for continuation but the runtime has them
                # disabled. Audit and proceed to end.
                audit({
                    "turn":          turn,
                    "agent":         "System",
                    "kind":          "operator_continuation_requested_but_disabled",
                    "signal_source": cont_source,
                    "ts":            datetime.now().isoformat(timespec='seconds'),
                })
                ctx.emit_event("continuation_denied", {
                    "turn":              turn,
                    "reason":            "disabled",
                    "max_continuations": effective_max,
                    "signal_source":     cont_source,
                })

            if grant_continuation:
                wrap_up.continuation_requested = True
                wrap_up.continuation_active    = True
                # Extend the loop's effective ceiling by one so the next
                # iteration actually runs. continuation_budget is the
                # mechanism that converts a grant into an executable turn;
                # without this increment, the grant is audited but never
                # consumed when the grant happens on the final deliberation
                # turn (the common case for wrap-up Operator).
                state.continuation_budget += 1
                audit({
                    "turn":                    turn,
                    "agent":                   "System",
                    "kind":                    "operator_continuation",
                    "continuation_index":      wrap_up.continuation_count + 1,
                    "max_continuations":       effective_max,
                    "continuation_granted":    True,
                    "signal_source":           cont_source,
                    "reason":                  cont_reason or "",
                    "ts":                      datetime.now().isoformat(timespec='seconds'),
                })
                ctx.emit_event("continuation_granted", {
                    "turn":              turn,
                    "index":             wrap_up.continuation_count + 1,
                    "max_continuations": effective_max,
                    "signal_source":     cont_source,
                    "reason":            cont_reason or "",
                })
                log(f"[T{turn}] >>> Operator continuation granted "
                    f"(#{wrap_up.continuation_count + 1} of {effective_max}). "
                    f"Loop continues for execution-only turn.")
                if cont_reason:
                    log(f"       Reason: {cont_reason}")
                log()
                # Do NOT break. Loop continues; next iteration's router
                # will see continuation_active and route to Operator with
                # the continuation note.
                continue

            # No continuation (or denied). End the session.
            # Defensive: explicitly clear continuation_active so the loop
            # state is unambiguous on break (the loop won't re-enter, but
            # the state is read elsewhere for session_state.json).
            wrap_up.continuation_active   = False
            wrap_up.continuation_requested = False
            state.end_reason = f"wrap-up complete ({wrap_up.reason})"
            log(f"[T{turn}] >>> Wrap-up sequence complete. Ending session.")
            log()
            break

        # Truthseeker verification pass (only on non-gate, non-Truthseeker turns).
        # Always logs a diagnostic line so the .log file shows what Truthseeker did
        # or didn't do every turn -- no more silent failures.
        if not args.no_verify and agent_name not in NON_TRIGGERING_SPEAKERS:
            try:
                # First pass: identify document-grounded claims (those containing
                # [CTX-...] or (per "...") markers). These are routed straight to
                # DOCUMENT_GROUNDED_NOT_WEB_VERIFIED without invoking SearXNG.
                doc_grounded_records = extract_document_grounded_claims(
                    reply, context_files_by_id, context_files_by_filename,
                    turn=turn, source_agent=agent_name,
                )
                for record in doc_grounded_records:
                    verification_audit(record)

                # Second pass: normal verification on the unmarked portion. We
                # don't strip the marked sentences from the reply -- agents may
                # weave marked and unmarked claims together. Instead, the normal
                # extractor finds web-checkable claims, and if a candidate
                # happens to overlap with a marked region, that's fine: it goes
                # through both paths and the operator audit shows both records.
                candidates = extract_claim_candidates(reply)
                claims     = extract_structured_claims(models, providers, reply, candidates)

                verifications: List[Dict[str, Any]] = []
                for c in claims:
                    v = verify_claim(models, providers, c, searxng_url)
                    v["source_turn"]  = turn
                    v["source_agent"] = agent_name
                    verifications.append(v)
                    verification_audit(v)

                ts2 = datetime.now().strftime("%H:%M:%S")
                dg_count = len(doc_grounded_records)
                if verifications:
                    summary = format_verification_summary(verifications)
                    extra = f", {dg_count} document-grounded" if dg_count else ""
                    log(f"[T{turn}.v {ts2}] Truthseeker: regex={len(candidates)} hints, "
                        f"claims={len(claims)}, {summary}{extra}")
                    log()

                    injected = format_verification_for_transcript(verifications)
                    history.append({
                        "role":    "user",
                        "content": injected,
                        "agent":   "Truthseeker",
                    })
                elif claims:
                    # Should not happen (verify_claim always returns a record), but log if it does
                    log(f"[T{turn}.v {ts2}] Truthseeker: regex={len(candidates)} hints, "
                        f"claims={len(claims)}, verification produced no records")
                    log()
                elif dg_count:
                    log(f"[T{turn}.v {ts2}] Truthseeker: regex={len(candidates)} hints, "
                        f"claims=0 web-verifiable, {dg_count} document-grounded "
                        f"(logged as DOCUMENT_GROUNDED_NOT_WEB_VERIFIED)")
                    log()
                else:
                    # The honest "nothing to verify" case - extractor returned no claims
                    log(f"[T{turn}.v {ts2}] Truthseeker: regex={len(candidates)} hints, "
                        f"claims=0 (extractor found nothing verifiable)")
                    log()

                # Single verification_completed event for all four log
                # paths above. The GUI doesn't care which message we
                # logged; it cares about the counts. Tally the verdicts
                # so the GUI can render "1 VERIFIED, 2 UNSUPPORTED, ..."
                # without re-parsing the per-claim records itself.
                _status_counts: Dict[str, int] = {}
                for v in verifications:
                    status = str(v.get("status", "UNKNOWN"))
                    _status_counts[status] = _status_counts.get(status, 0) + 1
                ctx.emit_event("verification_completed", {
                    "turn":               turn,
                    "agent":              agent_name,
                    "regex_hints":        len(candidates),
                    "claims_checked":     len(claims),
                    "doc_grounded_count": dg_count,
                    "status_counts":      _status_counts,
                })
            except Exception as e:
                log(f"[TRUTHSEEKER ERROR] T{turn}: {type(e).__name__}: {e}")
                log()
                state.truthseeker_errors.append((turn, type(e).__name__, str(e)))
                ctx.emit_event("verification_error", {
                    "turn":          turn,
                    "agent":         agent_name,
                    "error_type":    type(e).__name__,
                    "error_message": str(e),
                })

        # Skill Runtime: process any ```skill_call blocks the agent emitted.
        # Each call is validated, executed, audited to skills.jsonl, and an
        # abbreviated result summary is injected into history so subsequent
        # agents see what happened. Skill results never trigger Sentinel/
        # Truthseeker (System role is in NON_TRIGGERING_SPEAKERS).
        if skill_catalog.enabled:
            try:
                results, transcript_text = skill_runtime.process_agent_output(
                    agent=agent_name, turn=turn, agent_output=reply,
                )
                _emit_skill_events_from_results(
                    ctx=ctx, turn=turn, agent=agent_name, results=results,
                )
                if transcript_text:
                    log(transcript_text)
                    log()
                    history.append({
                        "role":    "user",
                        "content": transcript_text,
                        "agent":   "System",
                    })
                    audit({
                        "turn":         turn,
                        "agent":        "System",
                        "kind":         "skill_results_injected",
                        "result_count": len(results),
                        "successes":    sum(1 for r in results if r["status"] == "success"),
                        "failures":     sum(1 for r in results if r["status"] != "success"),
                        "ts":           datetime.now().isoformat(timespec='seconds'),
                    })
            except Exception as e:
                log(f"[SKILL_RUNTIME ERROR] T{turn}: {type(e).__name__}: {e}")
                log()

        if StopState.wrap_up:
            state.end_reason = "graceful stop (kill notice)"
            break

        time.sleep(args.delay)

    return state


def _build_session_state(
    session_id:          str,
    started_at:          str,
    ended_at:            str,
    end_reason:          str,
    seed:                str,
    max_turns:           int,
    args:                argparse.Namespace,
    history:             List[Dict],
    audit_path:          Path,
    verification_path:   Path,
    sentinel_reg:        SentinelRegistry,
    operator_wrap_up_text: Optional[str],
    synthesizer_wrap_up_text: Optional[str],
    agents:              Dict[str, Any],
    providers:           Dict[str, Any],
    models:              Dict[str, Any],
    director:            Optional["DirectorState"] = None,
    director_user_id:    Optional[str] = None,
    director_email:      Optional[str] = None,
    context_files:       Optional[List["ContextFile"]] = None,
    budget_assessment:   Optional[Dict[str, Any]] = None,
    background_block_chars: Optional[int] = None,
    skill_runtime:       Optional["SkillRuntime"] = None,
    wrap_up_state:       Optional["WrapUpState"] = None,
    policy_blocked:      bool = False,
    policy_block_reason: Optional[str] = None,
    awaiting_human_review: bool = False,
    review_reason:         Optional[str] = None,
    awaiting_information:  bool = False,
    information_reason:    Optional[str] = None,
    governance_boundary_blocked: bool = False,
    governance_boundary_reason:  Optional[str] = None,
    refusal_terminated:        bool = False,
    refusal_reason:            Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build the four-domain session_state dict from authoritative sources.
    Orchestrator owns runtime_state and governance_state entirely. Operator's
    optional wrap_up block populates operator_summary if present.
    """
    # Turn counts per agent (from history, not audit, for accuracy)
    turn_counts: Dict[str, int] = {}
    for m in history:
        agent_name = m.get("agent")
        if agent_name and agent_name != "System":
            turn_counts[agent_name] = turn_counts.get(agent_name, 0) + 1

    # Parse Operator's optional wrap_up block
    operator_summary: Dict[str, Any] = {
        "narrative_summary":           None,
        "open_questions":              [],
        "next_session_recommendation": None,
        "notable_risks":               [],
        "summary_quality":             "missing",  # missing | partial | structured
        "source":                      None,        # wrap_up_block | text_only | none
    }
    if operator_wrap_up_text:
        block = _extract_wrap_up_block(operator_wrap_up_text)
        if block is not None:
            operator_summary["narrative_summary"]           = block.get("narrative_summary")
            operator_summary["open_questions"]              = block.get("open_questions", []) or []
            operator_summary["next_session_recommendation"] = block.get("next_session_recommendation")
            operator_summary["notable_risks"]               = block.get("notable_risks", []) or []
            operator_summary["summary_quality"]             = "structured"
            operator_summary["source"]                      = "wrap_up_block"
        else:
            # Block absent or malformed - record that Operator did produce a
            # wrap-up turn but the structured block was unusable
            operator_summary["summary_quality"] = "partial"
            operator_summary["source"]          = "text_only"
            # Use first ~500 chars of Operator's text as a fallback narrative
            operator_summary["narrative_summary"] = (
                operator_wrap_up_text[:500] + ("..." if len(operator_wrap_up_text) > 500 else "")
            )

    # Identify agent-model bindings as deployed for this run
    agent_bindings = {}
    for name, a in agents.items():
        model_id = a.get("model_id")
        provider_id = models.get(model_id, {}).get("provider")
        agent_bindings[name] = {
            "model_id":    model_id,
            "provider_id": provider_id,
            "role":        a.get("role"),
        }

    return {
        "schema_version": "1.0",
        "runtime_state": {
            "session_id":          session_id,
            "started_at":          started_at,
            "ended_at":            ended_at,
            "end_reason":          end_reason,
            "max_turns":           max_turns,
            "synth_cadence":       args.synth_cadence,
            "history_window":      args.history_messages,
            "truthseeker_enabled": not args.no_verify,
            "turn_counts":         turn_counts,
            # Slice 1/3: governance status for this session. profile_id is
            # the profile that governed the run; policy_blocked is a
            # first-class terminal signal that the policy gate prevented
            # Operator from executing, with a human-readable reason. The
            # GUI surfaces these so a blocked session is visible, not
            # buried in logs.
            "governance": {
                "profile_id":     getattr(args, "governance_profile_id", None),
                "policy_blocked": policy_blocked,
                "policy_block_reason": policy_block_reason,
                "awaiting_human_review": awaiting_human_review,
                "review_reason":         review_reason,
                "awaiting_information":  awaiting_information,
                "information_reason":    information_reason,
                "governance_boundary_blocked": governance_boundary_blocked,
                "governance_boundary_reason":  governance_boundary_reason,
                "refusal_terminated":        refusal_terminated,
                "refusal_reason":            refusal_reason,
            },
            "audit_log_path":      str(audit_path),
            "verification_log_path": str(verification_path),
            "director_identity":   (
                {
                    "user_id":      director_user_id,
                    "email":        director_email,
                    "display_name": director.display_name,
                    "source":       director.source,
                }
                if director is not None else None
            ),
            "agent_bindings":      agent_bindings,
            # CLI-provided generic skill args from --skill-arg. Stored as
            # the parsed nested dict so future readers can reconstruct
            # what was suggested at launch time. Empty {} when no args
            # were provided.
            "requested_skill_args": getattr(args, "skill_args_parsed", {}) or {},
            # Operator continuation tracking. Records how many continuation
            # turns Operator requested and whether the cap was reached.
            "operator_continuations": {
                "count":       wrap_up_state.continuation_count if wrap_up_state else 0,
                "max":         int(_RUNTIME_CONFIG.get("operator_continuations", {}).get("max_operator_continuations", 4)),
                "cap_reached": wrap_up_state.continuation_cap_reached if wrap_up_state else False,
                "enabled":     bool(_RUNTIME_CONFIG.get("operator_continuations", {}).get("enabled", True)),
            },
        },
        "governance_state": {
            "seed":                  seed,
            "ratified_decisions":    _extract_decisions_from_audit(audit_path),
            "sentinel_concerns_fired": _summarize_sentinel_concerns(sentinel_reg),
            "verification_summary":  _summarize_verification_audit(verification_path),
        },
        "deliberation_state": {
            "final_synthesis_text":  synthesizer_wrap_up_text,
            "open_questions":        operator_summary["open_questions"],
            "notable_risks":         operator_summary["notable_risks"],
        },
        "operator_summary": operator_summary,
        "context_state":    (
            {**build_context_state(context_files),
             "context_dir":         args.context_dir,
             "context_file_args":   args.context_file or [],
             "budget_assessment":   budget_assessment,
             "context_block_chars": background_block_chars,
             "context_block_tokens": (
                 _estimate_tokens(background_block_chars) if background_block_chars else None
             )}
            if context_files else
            {"context_enabled":     _rt("context", "enabled"),
             "context_dir":         None,
             "context_file_args":   [],
             "text_documents":      [],
             "structured_data":     [],
             "unknown_files":       [],
             "budget_assessment":   None,
             "context_block_chars": None,
             "context_block_tokens": None}
        ),
        "skill_state": (
            {
                "skills_enabled":    bool(_RUNTIME_CONFIG.get("skills", {}).get("enabled", False)),
                "catalog": {
                    "executable": [
                        {
                            "name":            m.name,
                            "version":         m.version,
                            "actions":         list(m.actions.keys()),
                            "allowed_callers": m.allowed_callers,
                            "risk_level":      m.risk_level,
                        }
                        for m in skill_runtime.catalog.list_executable()
                    ],
                    "documentation_only": [
                        {
                            "name":    m.name,
                            "version": m.version,
                        }
                        for m in skill_runtime.catalog.list_documentation_only()
                    ],
                    "disabled":    [{"name": n, "reason": r} for n, r in skill_runtime.catalog.disabled],
                    "unsupported": [{"name": n, "reason": r} for n, r in skill_runtime.catalog.unsupported],
                },
                "invocations": skill_runtime.invocations,
                "summary": {
                    "total":     len(skill_runtime.invocations),
                    "successes": sum(1 for r in skill_runtime.invocations if r.get("status") == "success"),
                    "failures":  sum(1 for r in skill_runtime.invocations if r.get("status") == "failed"),
                    "skills_log_path": str(skill_runtime.skills_log_path),
                },
            } if skill_runtime is not None else None
        ),
    }

