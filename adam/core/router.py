"""
Predicate-based agent router for the deliberation loop.

Responsibilities:

1. **Sentinel/Operator predicate detection**: scan agent output for
   trigger phrases (FERPA, "decision point:", etc.) and decide if a
   gate agent should run next.

2. **Per-concern cooldown tracking** (SentinelRegistry): once a
   Sentinel concern has fired on a label like "equity exposure", that
   label enters a cooldown period before it can re-fire. Prevents
   cascade re-firing on the gate's own output.

3. **Speaker selection** (select_next_speaker): the routing priority
   ladder. Wrap-up forced sequence > Director addressing > Operator
   gate > Sentinel gate > Synthesizer cadence > advisory rotation.

4. **Advisory cycle derivation**: build the rotation list of
   advisory-role agents from the agents.json config.

Behavior-preserving extraction. The only structural change from the
inline version: ADVISORY_CYCLE is now exposed via set_advisory_cycle()
/ get_advisory_cycle() accessors instead of being a directly-mutated
module global. The runtime calls set_advisory_cycle() once at startup
after derive_advisory_cycle() runs; select_next_speaker reads back via
get_advisory_cycle(). Same observable behavior, but importers stay
consistent across modules.

Type references to WrapUpState, DirectorState, and SkillCatalog stay
as forward-reference strings so this module doesn't need to import
them. Those classes are still session-state in the runtime until
step 5b-3 introduces SessionContext.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from adam.core.config_loader import _RUNTIME_CONFIG
from adam.skills_runtime import build_operator_skill_args_note


# ============================================================
# Sentinel / Operator predicates
# ============================================================
# Each predicate: (compiled regex, short concern/decision label)

SENTINEL_TRIGGERS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\b(student data|student records|individual student|student-level)\b", re.I), "student data handling"),
    (re.compile(r"\b(FERPA|COPPA|HIPAA|IDEA Part B)\b"),                                       "regulatory compliance"),
    (re.compile(r"\b(without consent|no opt[- ]?out|mandatory data|surveillance)\b", re.I),    "consent and surveillance"),
    (re.compile(r"\b(bias|biased|underserved|under[- ]?resourced|equity gap|disparate impact)\b", re.I), "equity exposure"),
    (re.compile(r"\b(mandate|require all|district[- ]?wide rollout|ban\b|prohibit)\b", re.I),  "irreversible policy action"),
    (re.compile(r"\b(vendor lock|sole[- ]?source|proprietary platform|exclusive contract)\b", re.I), "vendor lock-in"),
    (re.compile(r"\b(facial recognition|biometric|emotion detection|behavior prediction)\b", re.I), "high-risk surveillance technology"),
]

# NOTE: Operator has NO mid-debate predicate triggers. Operator is the
# terminal execution authority and runs exactly once, during wrap-up, after
# the final Synthesizer pass (see select_next_speaker's wrap-up branch). A
# former OPERATOR_TRIGGERS table ("decision point:", "ratified:", "we should
# proceed with:") plus an operator_decision() helper and an
# OPERATOR_COOLDOWN_TURNS gate used to route to Operator mid-deliberation;
# they were removed because that path only ever produced premature artifacts
# (an artifact written before the debate closed could not reflect later
# conclusions). Conclusion language in advisory output now simply continues
# the debate. Do not re-introduce a mid-debate Operator trigger: durable
# artifacts may only be produced after final synthesis.

GATE_AGENTS              = {"Sentinel", "Operator"}
NON_TRIGGERING_SPEAKERS  = {"Sentinel", "Operator", "Truthseeker", "System", "Director"}

# Once a Sentinel concern label has fired, it cannot re-fire until this many
# advisory turns have elapsed (counting only post-fire advisory output that
# does NOT contain the same trigger). See router for exact semantics.
SENTINEL_CONCERN_COOLDOWN_TURNS = 4


def sentinel_concern(text: str) -> Optional[str]:
    for pattern, label in SENTINEL_TRIGGERS:
        if pattern.search(text):
            return label
    return None


# ============================================================
# Router  (FIX #1: Sentinel cascade)
# ============================================================
#
# The cascade fix has two parts:
#
#   (a) SCOPE RESTRICTION:
#       Sentinel/Operator triggers are checked ONLY against the most recent
#       NON-gate, NON-Truthseeker, NON-System agent message. They do not scan
#       the full history window. This prevents trigger words from older
#       messages persisting in the visible window and re-firing the gate.
#
#   (b) CONCERN REGISTRY:
#       Once Sentinel fires on a concern label (e.g. "equity exposure"), that
#       specific concern enters a per-concern cooldown. It cannot re-fire on
#       the same label until SENTINEL_CONCERN_COOLDOWN_TURNS advisory turns
#       have produced output that does NOT match the same trigger. A new,
#       distinct concern can still fire normally.
#
# This combination breaks the cascade you observed: Sentinel's own equity
# vocabulary cannot retrigger it (scope), and even if a later advisory turn
# uses equity vocabulary briefly, the concern-specific cooldown prevents
# duplicate flags on the same already-mitigated risk.


class SentinelRegistry:
    """Tracks per-concern cooldown state."""

    def __init__(self) -> None:
        # concern_label -> turn index when it last fired
        self._fired_at: Dict[str, int] = {}

    def can_fire(self, concern_label: str, current_turn: int) -> bool:
        last_fired = self._fired_at.get(concern_label)
        if last_fired is None:
            return True
        return (current_turn - last_fired) >= SENTINEL_CONCERN_COOLDOWN_TURNS

    def record_fire(self, concern_label: str, current_turn: int) -> None:
        self._fired_at[concern_label] = current_turn

    def known_concerns(self) -> Set[str]:
        return set(self._fired_at.keys())


def _last_advisory_message(history: List[Dict]) -> Optional[Dict]:
    """Find the most recent message from an agent that can trigger predicates."""
    for m in reversed(history):
        if m.get("agent") not in NON_TRIGGERING_SPEAKERS:
            return m
    return None


# ============================================================
# Advisory cycle state (set once at startup)
# ============================================================
#
# ADVISORY_CYCLE is the ordered rotation list of advisory agents.
# Populated by derive_advisory_cycle() from agents.json and committed
# via set_advisory_cycle() during session startup. select_next_speaker
# reads back via get_advisory_cycle() so multiple importers stay
# consistent (same pattern as _RUNTIME_CONFIG in config_loader).

_ADVISORY_CYCLE: List[str] = []


def set_advisory_cycle(cycle: List[str]) -> None:
    """Replace the advisory cycle in place. Called once at startup."""
    _ADVISORY_CYCLE.clear()
    _ADVISORY_CYCLE.extend(cycle)


def get_advisory_cycle() -> List[str]:
    """Read-only view of the advisory cycle. Returns the live list."""
    return _ADVISORY_CYCLE


# Backward-compat alias: the runtime's main() reads ADVISORY_CYCLE
# directly in a few places (for tally formatting, banner output).
# It points at the same list object that set_advisory_cycle mutates.
ADVISORY_CYCLE = _ADVISORY_CYCLE


def build_artifact_skill_block(skill_catalog: Optional["SkillCatalog"]) -> str:
    """
    Return a short note listing currently-implemented formats for the
    executable document skill, suitable for appending to an Operator
    invocation note. Returns empty string if no executable document skill
    is loaded (so callers can concatenate unconditionally).

    This block previously lived inline inside select_next_speaker's
    wrap-up branch. It's been hoisted out so the non-wrap-up Operator
    gate (operator-triggered) can present the same format guidance and
    avoid emitting skill_calls for formats whose renderer isn't built.
    """
    if skill_catalog is None or "document" not in skill_catalog.executable:
        return ""
    doc_manifest = skill_catalog.executable["document"]
    create_spec = doc_manifest.actions.get("create", {})
    manifest_formats = create_spec.get("supported_formats", [])
    IMPLEMENTED_FORMATS_THIS_BUILD = {"md", "txt", "html", "docx"}
    supported_now = [f for f in manifest_formats if f in IMPLEMENTED_FORMATS_THIS_BUILD]
    supported_str = ", ".join(supported_now) if supported_now else "(none)"
    return (
        f"\n\nFILE ARTIFACT AVAILABILITY"
        f"\nThe document skill is loaded and executable. Currently "
        f"implemented formats in this build: {supported_str}. "
        f"For board-facing deliverables, prefer 'docx' (full "
        f"cover page, styled headings, page numbers, document "
        f"properties). For editable drafts, prefer 'md'. For "
        f"web review, prefer 'html'."
    )


def build_artifact_mode_rule(skill_catalog: Optional["SkillCatalog"]) -> str:
    """
    Return the artifact-delivery-mode discipline text. This is the rule
    that prevents Operator from emitting a full structured plan in prose
    AND a skill_call with the same content inlined into its 'content' arg
    in the same response. Half-content from a max_tokens truncation mid-
    JSON-string is worse than no content: the parser rejects the block,
    the artifact never lands, and the audit trail records a misleading
    "Operator attempted but failed" record.

    The T6 truncation in the 2026-05-23 superintendent-search session was
    a direct consequence of this rule being absent from the operator-gate
    invocation note. The rule was present on the wrap-up Operator at T10,
    which is why T10 succeeded with a 42KB artifact in the same session.

    Returns empty string when no executable artifact skill is loaded
    (no skill_call to emit means no mode-discipline question to answer).
    """
    if skill_catalog is None or not skill_catalog.executable:
        return ""
    return (
        "\n\nARTIFACT DELIVERY MODE -- IMPORTANT"
        "\nIf the final deliverable should be saved as a file artifact, "
        "do NOT also print the full artifact body in prose. The "
        "skill_call IS the artifact. Duplicating the artifact content "
        "outside the skill_call wastes the token budget and can cause "
        "the skill_call block to truncate mid-string, which means the "
        "file is never created."
        "\n\nChoose ONE of these two modes:"
        "\n\n  MODE 1 (transcript artifact): produce the closing artifact "
        "in prose IN the transcript. Do not emit a skill_call. Use this "
        "mode when there is no executable file-artifact skill, or when "
        "the artifact is intentionally meant to be read inline."
        "\n\n  MODE 2 (file artifact): emit a skill_call block invoking "
        "the document skill (or another executable artifact skill). Do "
        "NOT also print the artifact content in prose. A brief sentence "
        "('See attached <filename> for the full plan') is fine; the full "
        "content is not. The skill_call's 'content' arg carries the "
        "artifact."
        "\n\nIf the artifact content would not fit safely inside one "
        "skill_call (the skill runtime's max_content_size_bytes is "
        "enforced), produce a shorter executive artifact in MODE 2 and "
        "note that a full version requires chunked artifact generation, "
        "which is not yet available in this runtime."
    )


def select_next_speaker(
    history:           List[Dict],
    synth_cadence:     int,
    current_turn:      int,
    sentinel_reg:      SentinelRegistry,
    wrap_up:           "WrapUpState",
    director:          Optional["DirectorState"] = None,
    skill_catalog:     Optional["SkillCatalog"] = None,
    skill_args_parsed: Optional[Dict[str, Dict[str, Dict[str, str]]]] = None,
) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Decide who speaks next.

    Returns (agent_name, optional_invocation_note, optional_concern_label).
    The concern_label is returned when Sentinel fires so the registry can
    record the fire after the turn completes successfully.

    Routing priority order (when wrap-up is NOT active):
      1. Director-addressed override (one turn, then resumes normally)
      2. Operator gate (Decision Points)
      3. Sentinel gate (predicate-triggered concerns)
      4. Synthesizer cadence
      5. Advisory rotation

    Routing during wrap-up:
      Director messages are advisory context only. They do not alter
      forced wrap-up routing. >>halt is the exception, but that comes
      through WrapUpState, not addressing.

    Wrap-up priority order:
      1. If wrap-up is active and Synthesizer hasn't run its forced turn yet
         -> force Synthesizer with closing directive
      2. If wrap-up is active and Synthesizer has done its forced turn but
         Operator hasn't -> force Operator with final-crystallization directive
      3. Otherwise, normal routing (predicate gates, scheduled Synthesizer,
         advisory rotation)

    Turn-budget entry: at the start of each turn, the main loop checks
    whether current_turn >= synth_wrap_up_turn and calls wrap_up.trigger()
    accordingly. The router itself doesn't decide when to enter wrap-up;
    it only handles routing given the wrap-up state.
    """
    # --- Wrap-up phase: forced sequence ---
    if wrap_up.is_active():
        if not wrap_up.synth_done:
            verif_summary = _summarize_recent_verifications(history)
            note = (
                f"WRAP-UP PHASE: this deliberation is closing (reason: {wrap_up.reason}). "
                f"Produce a final synthesis that consolidates ratified decisions, names "
                f"open questions and unresolved tensions, and integrates verification "
                f"findings. Do not introduce new exploratory threads. Preserve uncertainty "
                f"where evidence remains incomplete. The next turn will be the final "
                f"Operator pass that crystallizes this synthesis into the session artifact."
            )
            if verif_summary:
                note += f"\n\nRecent verification findings:\n{verif_summary}"
            return "Synthesizer", note, None

        if not wrap_up.operator_done:
            verif_summary = _summarize_recent_verifications(history)

            # Build the available-artifact-skill block from the live catalog.
            # If the document skill is loaded and executable, list its
            # currently-implemented formats explicitly so Operator doesn't
            # request formats that aren't ready (e.g. docx before Pass 3).
            # If no executable artifact skill is available, Operator defaults
            # to transcript-artifact mode.
            artifact_skill_block = ""
            has_executable_doc_skill = (
                skill_catalog is not None
                and "document" in skill_catalog.executable
            )
            if has_executable_doc_skill:
                # Read the document manifest to find which formats are
                # currently supported. Pass 2 supports md, txt, html;
                # Pass 3 will add docx. The handler-side renderers are
                # the ground truth, but we list what the manifest says.
                doc_manifest = skill_catalog.executable["document"]
                create_spec = doc_manifest.actions.get("create", {})
                manifest_formats = create_spec.get("supported_formats", [])
                # Conservative whitelist: only list formats whose renderer
                # is known to be implemented in this build. Pass 3 added docx.
                IMPLEMENTED_FORMATS_THIS_BUILD = {"md", "txt", "html", "docx"}
                supported_now = [
                    f for f in manifest_formats
                    if f in IMPLEMENTED_FORMATS_THIS_BUILD
                ]
                supported_str = ", ".join(supported_now) if supported_now else "(none)"
                artifact_skill_block = (
                    f"\n\nFILE ARTIFACT AVAILABILITY"
                    f"\nThe document skill is loaded and executable. Currently "
                    f"implemented formats in this build: {supported_str}. "
                    f"For board-facing deliverables, prefer 'docx' (full "
                    f"cover page, styled headings, page numbers, document "
                    f"properties). For editable drafts, prefer 'md'. For "
                    f"web review, prefer 'html'."
                )

            artifact_mode_rule = (
                "\n\nARTIFACT DELIVERY MODE -- IMPORTANT"
                "\nIf the final deliverable should be saved as a file artifact, "
                "do NOT also print the full artifact body in prose. The "
                "skill_call IS the artifact. Duplicating the artifact content "
                "outside the skill_call wastes the token budget and can cause "
                "the skill_call block to truncate mid-string, which means the "
                "file is never created."
                "\n\nChoose ONE of these two modes:"
                "\n\n  MODE 1 (transcript artifact): emit the wrap_up block, "
                "then produce the closing artifact in prose IN the transcript. "
                "Do not emit a skill_call. Use this mode when there is no "
                "executable file-artifact skill, or when the artifact is "
                "intentionally meant to be read inline."
                "\n\n  MODE 2 (file artifact): emit the wrap_up block, then "
                "emit a skill_call block invoking the document skill (or "
                "another executable artifact skill). Do NOT also print the "
                "artifact content in prose. A brief sentence ('See attached "
                "<filename> for the full plan') is fine; the full content "
                "is not. The skill_call's 'content' arg carries the artifact."
                "\n\nIf the artifact content would not fit safely inside one "
                "skill_call (the skill runtime's max_content_size_bytes is "
                "enforced), produce a shorter executive artifact in MODE 2 "
                "and note in your wrap_up.next_session_recommendation that a "
                "full version requires chunked artifact generation, which "
                "is not yet available in this runtime."
            )

            # Build a continuation availability block. If the runtime
            # supports continuations, tell Operator how to request one.
            # If continuations are disabled (e.g., max=0 in runtime.json),
            # explicitly tell Operator that this is the only execution
            # turn so it knows to bundle everything in one block.
            oc_cfg = _RUNTIME_CONFIG.get("operator_continuations", {})
            oc_enabled = bool(oc_cfg.get("enabled", True))
            oc_max     = int(oc_cfg.get("max_operator_continuations", 4))
            if oc_enabled and oc_max > 0:
                continuation_block = (
                    f"\n\nOPERATOR CONTINUATION (multi-skill execution chains)"
                    f"\nIf you need to invoke MORE than one skill_call and those "
                    f"calls depend on each other (e.g., create a document then "
                    f"email it using the resulting filename), you may request "
                    f"a continuation turn AFTER this wrap-up turn. Up to "
                    f"{oc_max} continuation turn(s) are available."
                    f"\n\nHow to request continuation: include the field "
                    f"\"continuation_requested\": true inside your wrap_up "
                    f"JSON block. On the continuation turn, prior skill "
                    f"results will be visible in the transcript, and you can "
                    f"emit additional skill_call blocks that reference the "
                    f"actual artifact paths produced by this turn's skills."
                    f"\n\nContinuation turns are EXECUTION ONLY:"
                    f"\n  - No new deliberation, no new analysis."
                    f"\n  - Prior skill results from this turn will be in the "
                    f"transcript when the continuation turn starts."
                    f"\n  - Emit only the additional skill_call blocks needed "
                    f"to complete finalization."
                    f"\n  - To request yet another continuation, emit a "
                    f"```operator_continue``` JSON block with "
                    f"continuation_requested: true."
                    f"\n  - To end after the continuation, set "
                    f"continuation_requested: false (or omit the block)."
                    f"\n  - Do NOT retry the same failing skill.action with "
                    f"the same args repeatedly. If a live external action "
                    f"is refused (e.g., recipient not on allowlist), use a "
                    f"safer fallback such as draft."
                    f"\n\nWhen NOT to request continuation:"
                    f"\n  - If a single skill_call (or a single skill_call "
                    f"block with multiple entries in the skill_calls array) "
                    f"fits in this turn, use that instead. Continuations "
                    f"are for chains where you need to see one skill's "
                    f"result before invoking the next."
                )
            else:
                continuation_block = (
                    f"\n\nThis is the ONLY execution turn (continuations are "
                    f"disabled in this deployment). Emit all skill_calls now."
                )

            note = (
                f"WRAP-UP PHASE - FINAL OPERATOR TURN: this is the closing "
                f"artifact pass. No further deliberation will occur after "
                f"your response."
                f"\n\nYOUR RESPONSE STRUCTURE (in this exact order):"
                f"\n\n1. FIRST, emit a fenced ```wrap_up``` JSON block at the "
                f"START of your response with these exact fields:"
                f"\n  - narrative_summary (string): one-paragraph plain-prose "
                f"summary of what this session accomplished, suitable for "
                f"someone reading the session_state file weeks later without "
                f"other context"
                f"\n  - open_questions (list of strings): unresolved items "
                f"worth carrying into a future session"
                f"\n  - next_session_recommendation (string): what the next "
                f"session should focus on, given where this one ended"
                f"\n  - notable_risks (list of strings): risks named during "
                f"deliberation that remain open or worth flagging"
                f"\n  - continuation_requested (bool, optional): set to true "
                f"if you need an execution-only continuation turn for "
                f"additional skill_calls; false or omitted otherwise"
                f"\n\n2. THEN, deliver the closing artifact in EXACTLY ONE "
                f"mode (see ARTIFACT DELIVERY MODE below)."
                f"\n\nThe wrap_up block MUST appear at the start so it lands "
                f"reliably regardless of total response length. The block "
                f"populates the operator_summary in session_state.json, "
                f"which is how future sessions resume from where this one "
                f"left off."
                f"\n\nVERIFICATION POLICY: omit any specific citations "
                f"marked UNSUPPORTED or CONTRADICTED. Mark PARTIALLY_VERIFIED "
                f"items as 'pending verification'. NOT_WEB_VERIFIABLE means "
                f"the verifier shouldn't have checked this kind of claim "
                f"(an identifier, address, file path, or configuration "
                f"value, not a factual assertion) -- ignore the verdict "
                f"and treat the item as authoritative per its source. "
                f"In particular: email recipient addresses are gated by "
                f"the allowlist, not the verifier. If a recipient is on "
                f"the configured allowlist, it is authorized regardless "
                f"of any Truthseeker verdict you may see referring to it. "
                f"Do not use a verifier verdict as a basis for choosing "
                f"draft over send."
                f"{artifact_mode_rule}"
                f"{artifact_skill_block}"
                f"{continuation_block}"
            )
            if verif_summary:
                note += f"\n\nRecent verification findings:\n{verif_summary}"
            # Append CLI-provided skill args as suggestions for Operator to
            # consider. These are not commands; the suggestion-not-command
            # rule is restated inside the note so Operator doesn't auto-
            # invoke skills just because args were supplied.
            skill_args_note = build_operator_skill_args_note(skill_args_parsed or {})
            if skill_args_note:
                note += f"\n\n{skill_args_note}"
            return "Operator", note, None

        # Operator continuation: the first wrap-up turn is done, Operator
        # requested another execution turn, and the cap allows it. This
        # branch is entered when wrap_up.continuation_active is True.
        if wrap_up.continuation_active:
            oc_cfg = _RUNTIME_CONFIG.get("operator_continuations", {})
            oc_max = int(oc_cfg.get("max_operator_continuations", 4))
            continuation_idx = wrap_up.continuation_count + 1
            note = (
                f"OPERATOR CONTINUATION TURN {continuation_idx} of max {oc_max}"
                f"\n\nThe deliberation is COMPLETE. The wrap-up turn has been "
                f"emitted. This is an execution-only continuation turn that "
                f"exists so you can complete multi-step skill execution chains "
                f"(e.g., create a document on the prior turn, then email it "
                f"on this turn using the actual artifact path that document.create "
                f"produced)."
                f"\n\nWHAT YOU CAN DO ON THIS TURN:"
                f"\n  - Emit one or more skill_call blocks with additional "
                f"skill invocations."
                f"\n  - Reference artifact paths and metadata from prior "
                f"skill results (visible in the transcript as System messages)."
                f"\n  - Request yet another continuation by emitting a fenced "
                f"```operator_continue``` JSON block with continuation_requested "
                f"set to true. Optionally include a 'reason' field explaining "
                f"why."
                f"\n\nWHAT YOU MUST NOT DO ON THIS TURN:"
                f"\n  - Do NOT re-open deliberation, introduce new analysis, "
                f"or reconsider the ratified decisions."
                f"\n  - Do NOT re-emit the wrap_up block (it was emitted on "
                f"the prior turn and is already in session_state)."
                f"\n  - Do NOT re-emit the full artifact body. The artifact "
                f"was either saved as a file via skill_call (visible in the "
                f"transcript) or rendered to the transcript on the prior turn."
                f"\n  - Do NOT retry the same skill.action with the same args "
                f"if it just failed. If a live external action was refused "
                f"(e.g., recipient not on allowlist, allowlist not configured), "
                f"switch to a safer alternative such as the 'draft' action."
                f"\n\nTO END THE SESSION AFTER THIS TURN:"
                f"\n  - Set continuation_requested to false in your "
                f"```operator_continue``` block, OR omit the block entirely. "
                f"Either ends the session cleanly after any skill_calls in "
                f"this turn execute."
                f"\n\nTO REQUEST ANOTHER CONTINUATION:"
                f"\n  - Emit ```operator_continue``` with continuation_requested "
                f"true. You have used {wrap_up.continuation_count} of {oc_max} "
                f"continuation turns so far."
            )
            return "Operator", note, None

        # Wrap-up complete but loop hasn't exited yet - this is a defensive
        # branch; should not normally be reached
        return ADVISORY_CYCLE[0], None, None

    # --- Normal routing (Director addressing override > predicates > rotation) ---
    if not history:
        return ADVISORY_CYCLE[0], None, None

    # Compute the routing decision first, then apply Director engagement
    # note as a wrapper if there are pending broadcasts. This way, both
    # the addressed-agent case AND the normal-rotation case correctly
    # tell the next agent to engage with Director broadcast input.
    selected_agent:   Optional[str] = None
    selected_note:    Optional[str] = None
    selected_concern: Optional[str] = None

    # Director addressing override (consumed once, then routing resumes normally)
    if director is not None:
        addr_target = director.consume_address_target()
        if addr_target is not None:
            selected_agent = addr_target
            selected_note = (
                f"The Director ({director.display_name}) addressed this turn to you. "
                f"Read the Director's message in the transcript above and respond directly. "
                f"After your turn, normal routing will resume."
            )

    if selected_agent is None:
        # FIX #1a: only scan the most recent advisory-class message for triggers
        last_advisory = _last_advisory_message(history)

        if last_advisory is not None:
            text = last_advisory["content"]

            # ADAM lifecycle rule: Operator is terminal-only.
            # ----------------------------------------------------------------
            # There is deliberately NO mid-debate Operator gate here. Operator
            # is the terminal execution authority: it runs exactly once, during
            # wrap-up, after the final Synthesizer pass, on settled input. See
            # the wrap-up branch at the top of select_next_speaker.
            #
            # A former gate routed to Operator mid-debate when advisory output
            # contained phrases like "Decision Point", "ratified", or "we
            # should proceed with". In practice that path only produced
            # premature artifacts: Operator would emit a full plan/document at,
            # say, T7, and the debate would then continue through Sentinel,
            # Synthesizer, and a second Operator pass at T10 -- so the early
            # artifact could never reflect conclusions reached after it was
            # written. It was removed because no durable artifact may be
            # produced before final synthesis.
            #
            # Mid-debate evidence needs are already covered without Operator:
            #   - Seeker is an authorized caller of the websearch skill
            #     (skills/websearch/SKILL.md: allowed_callers [Operator, Seeker])
            #     and can search during normal advisory rotation.
            #   - Truthseeker verification runs automatically on every non-gate
            #     advisory turn (see loop.py), with its own SearXNG access.
            # Artifact/action skills (document, coder, email, slidedeck) remain
            # Operator-only, which is why they can only fire at wrap-up.
            #
            # Conclusion language therefore just continues the debate; it falls
            # through to the Sentinel gate below and then to normal rotation.

            # Sentinel gate: per-concern registry (FIX #1b). Sentinel remains
            # predicate-triggered mid-debate -- only Operator was made terminal.
            concern = sentinel_concern(text)
            if concern and sentinel_reg.can_fire(concern, current_turn):
                selected_note = (
                    f"The most recent message triggered the '{concern}' risk predicate. "
                    f"Flag it precisely and propose a concrete mitigation."
                )
                selected_agent   = "Sentinel"
                selected_concern = concern

    if selected_agent is None:
        # Synthesizer cadence
        last_synth_idx = -1
        for i in range(len(history) - 1, -1, -1):
            if history[i].get("agent") == "Synthesizer":
                last_synth_idx = i
                break
        advisory_since_synth = sum(
            1 for m in history[last_synth_idx + 1:] if m.get("agent") in ADVISORY_CYCLE
        )
        if advisory_since_synth >= synth_cadence:
            selected_agent = "Synthesizer"

    if selected_agent is None:
        # Default rotation
        rotation_messages = [m for m in history if m.get("agent") in ADVISORY_CYCLE]
        next_idx = len(rotation_messages) % len(ADVISORY_CYCLE)
        selected_agent = ADVISORY_CYCLE[next_idx]

    # Director engagement: if any broadcast (non-addressed) Director
    # message was just drained, prepend an engagement instruction that
    # QUOTES the actual text and DEMANDS explicit acknowledgment. The
    # earlier "address their question" phrasing was too soft -- agents
    # with rigid role prompts (Sentinel especially) would comply by
    # omission. Quoting the text inline removes deniability.
    if director is not None:
        broadcast_texts = director.consume_broadcast_texts()
        if broadcast_texts:
            if len(broadcast_texts) == 1:
                quoted = f'"{broadcast_texts[0]}"'
            else:
                quoted = "\n".join(f'  - "{t}"' for t in broadcast_texts)

            engagement_note = (
                f"DIRECTOR INPUT (must be acknowledged before your normal role):\n\n"
                f"The Director ({director.display_name}) just said: {quoted}\n\n"
                f"You MUST begin your response with an explicit acknowledgment of "
                f"this input. Quote or paraphrase what was said, then state how it "
                f"affects your reasoning. If the Director corrected an assumption "
                f"in the deliberation, adjust your contribution accordingly. Do not "
                f"skip this acknowledgment -- it is required for audit traceability."
            )
            if selected_note:
                selected_note = engagement_note + "\n\n---\n\n" + selected_note
            else:
                selected_note = engagement_note

    return selected_agent, selected_note, selected_concern


def _summarize_recent_verifications(history: List[Dict], lookback: int = 8) -> str:
    recent = history[-lookback:] if len(history) > lookback else history
    truth_msgs = [m for m in recent if m.get("agent") == "Truthseeker"]
    if not truth_msgs:
        return ""
    return "\n".join(m["content"] for m in truth_msgs[-2:])


def derive_advisory_cycle(agents_config: Dict[str, Any]) -> List[str]:
    """Build the rotation list from agents whose role is 'advisory'."""
    # Preserve config-file order
    return [name for name, a in agents_config.items() if a["role"] == "advisory"]

