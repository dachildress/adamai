/**
 * Event reducer for ADAM sessions.
 *
 * Takes events from events.jsonl (in order) and folds them into a
 * single state object that the UI renders from. Pure function,
 * deterministic, replayable. If you can run a real session through
 * this reducer and get the same state as a session loaded from a
 * cold start of session_state.json, the reducer is correct.
 *
 * Why a reducer instead of N separate hooks: the events stream is
 * inherently sequential and order-sensitive. A reducer makes that
 * obvious in the code. It also means new event types are easy to
 * add (one switch case) and the same logic works for both live SSE
 * streams and replay of completed sessions.
 *
 * Two important properties:
 *
 *   1. Idempotence on seq. If the same event arrives twice (e.g.
 *      SSE reconnect re-replays from start), reducer state must not
 *      double-count. Every reducer case is gated on seq > last_seq.
 *
 *   2. Honesty about absence. State fields that ADAM does not emit
 *      remain null. The UI binds to those nulls and renders "not
 *      configured" indicators rather than fake defaults.
 */

export const EMPTY_STATE = {
  // Session envelope
  session_id:     null,
  schema_version: null,
  last_seq:       0,

  // Director identity (from session_started)
  director: null,                 // {user_id, email, display_name, source}

  // Session config
  seed:            null,
  max_turns:       null,
  synth_cadence:   null,
  history_window:  null,
  agents:          [],            // [{name, role, model_id, provider, max_tokens, temperature}]
  truthseeker_enabled: null,
  searxng_url:     null,

  // Trust registry
  trust_registry: null,           // {size, source_counts, min_length}

  // Context files
  context: {
    files:  [],                   // [{context_id, filename, classification, size_bytes, sha256, ...}]
    counts: null,                 // {total, text_document, structured_data, unknown}
    background_block_chars: null,
  },

  // Skill registry
  skill_registry: {
    enabled: null,
    skills:  [],                  // [{name, version, actions[], allowed_callers[], risk}]
  },

  // Wrap-up state
  wrap_up: {
    triggered:        false,
    triggered_at_turn: null,
    reason:            null,
    triggered_by:      null,
    synth_wrap_turn:   null,
    op_wrap_turn:      null,
  },

  // Turn timeline. Each turn is built up by turn_started → turn_completed
  // (or turn_started → turn_error). The current turn is the highest-
  // numbered turn that has turn_started but not turn_completed/error.
  turns: [],                      // [{turn, agent, routing_reason, model_id, max_tokens,
                                  //   temperature, status: 'running'|'complete'|'error',
                                  //   content, content_preview, content_length, duration_ms,
                                  //   error_type, error_message, started_at, completed_at,
                                  //   verification: {claims_checked, doc_grounded_count, status_counts},
                                  //   skills: [skill_invoked events for this turn]}]
  current_turn: null,             // turn number of the running turn, or null if none

  // Verifications log (flat list, newest first when rendered)
  verifications: [],              // turn_completed:verification_completed events

  // Skill invocations (flat list)
  skill_invocations: [],          // skill_invoked events

  // Director messages
  director_messages: [],          // director_message events

  // Continuations
  continuations: [],              // continuation_granted + continuation_denied events

  // Lifecycle
  session_started_at: null,
  session_ended_at:   null,
  end_reason:         null,
  ended:              false,
  final_summary:      null,       // {turn_counts, truthseeker_errors, skill_summary, wrap_up}

  // Error flags
  truthseeker_error_count: 0,
  turn_error_count:        0,

  // For warning the user if something is structurally off
  unknown_event_types: [],
}


/**
 * Apply a single event to a state object. Returns a new state.
 * Idempotent on seq — events with seq <= state.last_seq are ignored.
 *
 * Mutation-free: we build new objects rather than mutating, so
 * React's reference-equality checks correctly re-render only the
 * subtrees that changed.
 */
export function applyEvent(state, event) {
  if (!event || typeof event !== 'object') return state
  if (event.seq != null && event.seq <= state.last_seq) {
    return state    // already applied
  }

  const next = { ...state, last_seq: event.seq || state.last_seq }
  if (!next.session_id && event.session_id) next.session_id = event.session_id
  if (!next.schema_version && event.schema_version) next.schema_version = event.schema_version

  const payload = event.payload || {}

  switch (event.event_type) {
    case 'session_started':
      next.director              = payload.director || null
      next.seed                  = payload.seed || null
      next.max_turns             = payload.max_turns ?? null
      next.synth_cadence         = payload.synth_cadence ?? null
      next.history_window        = payload.history_window ?? null
      next.agents                = payload.agents || []
      next.truthseeker_enabled   = payload.truthseeker_enabled ?? null
      next.searxng_url           = payload.searxng_url || null
      next.session_started_at    = event.ts || null
      break

    case 'context_loaded':
      next.context = {
        files:  payload.files || [],
        counts: payload.counts || null,
        background_block_chars: payload.background_block_chars ?? null,
      }
      break

    case 'trust_registry_built':
      next.trust_registry = {
        size:          payload.size ?? null,
        source_counts: payload.source_counts || {},
        min_length:    payload.min_length ?? null,
      }
      break

    case 'skill_registry_loaded':
      next.skill_registry = {
        enabled: payload.enabled ?? null,
        skills:  payload.skills || [],
      }
      break

    case 'wrap_up_triggered':
      next.wrap_up = {
        triggered:         true,
        triggered_at_turn: payload.turn ?? null,
        reason:            payload.reason || null,
        triggered_by:      payload.triggered_by || null,
        synth_wrap_turn:   payload.synth_wrap_turn ?? null,
        op_wrap_turn:      payload.op_wrap_turn ?? null,
      }
      break

    case 'turn_started': {
      const turn = {
        turn:           payload.turn,
        agent:          payload.agent,
        routing_reason: payload.routing_reason,
        model_id:       payload.model_id,
        max_tokens:     payload.max_tokens,
        temperature:    payload.temperature,
        invocation_note: payload.invocation_note,
        status:         'running',
        started_at:     event.ts,
        completed_at:   null,
        content:        null,
        content_preview: null,
        content_length: null,
        duration_ms:    null,
        error_type:     null,
        error_message:  null,
        verification:   null,
        skills:         [],
      }
      next.turns = [...next.turns, turn]
      next.current_turn = payload.turn
      break
    }

    case 'turn_completed': {
      next.turns = next.turns.map(t => {
        if (t.turn !== payload.turn) return t
        return {
          ...t,
          status:          'complete',
          content:         payload.content,
          content_preview: payload.content_preview,
          content_length:  payload.content_length,
          duration_ms:     payload.duration_ms,
          completed_at:    event.ts,
          // routing_reason etc may be re-confirmed here; trust payload over turn_started
          routing_reason:  payload.routing_reason ?? t.routing_reason,
          model_id:        payload.model_id ?? t.model_id,
          concern_label:   payload.concern_label ?? t.concern_label,
        }
      })
      if (next.current_turn === payload.turn) next.current_turn = null
      break
    }

    case 'turn_error': {
      next.turns = next.turns.map(t => {
        if (t.turn !== payload.turn) return t
        return {
          ...t,
          status:        'error',
          error_type:    payload.error_type,
          error_message: payload.error_message,
          duration_ms:   payload.duration_ms,
          completed_at:  event.ts,
        }
      })
      if (next.current_turn === payload.turn) next.current_turn = null
      next.turn_error_count += 1
      break
    }

    case 'verification_completed': {
      const verif = {
        turn:               payload.turn,
        agent:              payload.agent,
        regex_hints:        payload.regex_hints,
        claims_checked:     payload.claims_checked,
        doc_grounded_count: payload.doc_grounded_count,
        status_counts:      payload.status_counts || {},
        ts:                 event.ts,
      }
      next.verifications = [...next.verifications, verif]
      // Attach to the turn so the in-line transcript can show the badge
      next.turns = next.turns.map(t => {
        if (t.turn !== payload.turn) return t
        return { ...t, verification: verif }
      })
      break
    }

    case 'verification_error': {
      next.truthseeker_error_count += 1
      break
    }

    case 'skill_invoked': {
      const inv = {
        turn:          payload.turn,
        agent:         payload.agent,
        skill:         payload.skill,
        action:        payload.action,
        status:        payload.status,
        artifact_id:   payload.artifact_id,
        invocation_id: payload.invocation_id,
        // File-producing skill fields (document, slidedeck, etc.)
        path:          payload.path,
        filename:      payload.filename,
        format:        payload.format,
        sha256:        payload.sha256,
        size_bytes:    payload.size_bytes,
        // Part 9.2: relpath is the session-artifacts-relative path used
        // for GUI URL construction. Multi-file workspace skills (coder)
        // emit relpath; flat-file skills (document, slidedeck) emit
        // filename only. The artifact-card UI prefers relpath when
        // present and falls back to filename otherwise. workspace_relpath
        // points at the parent directory of multi-file packages and is
        // currently informational (future UI may surface a "browse"
        // link to the package directory).
        relpath:           payload.relpath,
        workspace_relpath: payload.workspace_relpath,
        // Action skill fields (email.send). Present when the skill
        // produced a recipient list and message metadata. The runtime
        // forwards these in skill_invoked when present; absent for
        // file-producing skills.
        to:            payload.to,
        cc:            payload.cc,
        bcc_count:     payload.bcc_count,
        subject:       payload.subject,
        attachments:   payload.attachments,
        message_id:    payload.message_id,
        sent_at:       payload.sent_at,
        provider:      payload.provider,
        sent:          payload.sent,
        // Error fields
        error_class:   payload.error_class,
        error_message: payload.error_message,
        ts:            event.ts,
      }
      next.skill_invocations = [...next.skill_invocations, inv]
      next.turns = next.turns.map(t => {
        if (t.turn !== payload.turn) return t
        return { ...t, skills: [...(t.skills || []), inv] }
      })
      break
    }

    case 'director_message': {
      next.director_messages = [...next.director_messages, {
        turn:         payload.turn,
        display_name: payload.display_name,
        target_agent: payload.target_agent,
        warning:      payload.warning,
        content:      payload.content,
        // Part 8: provenance and ID. message_id is the GUI-assigned
        // ID for inbox-sourced messages; null for terminal messages.
        // source is "gui_inbox" or "terminal". command is set only
        // for halt commands (>>halt / /halt).
        message_id:   payload.message_id ?? null,
        source:       payload.source ?? 'terminal',
        command:      payload.command ?? null,
        ts:           event.ts,
      }]
      // If this is a GUI-sourced message, mark the locally-queued
      // entry (if any) as consumed by message_id. The App-level
      // local state for queued messages reads next.consumed_message_ids.
      if (payload.message_id) {
        next.consumed_message_ids = {
          ...(next.consumed_message_ids || {}),
          [payload.message_id]: {
            turn:    payload.turn,
            command: payload.command ?? null,
            ts:      event.ts,
          },
        }
      }
      break
    }

    case 'director_message_error': {
      // Failed inbox lines. The GUI surfaces these to the user so
      // they know their message was rejected (e.g. malformed JSON,
      // oversize content). Keyed by message_id when available so the
      // App-level queued-messages display can mark the corresponding
      // local entry as errored.
      next.director_message_errors = [
        ...(next.director_message_errors || []),
        {
          turn:          payload.turn,
          error_type:    payload.error_type,
          error_message: payload.error_message,
          raw_line:      payload.raw_line,
          message_id:    payload.message_id ?? null,
          ts:            event.ts,
        },
      ]
      if (payload.message_id) {
        next.errored_message_ids = {
          ...(next.errored_message_ids || {}),
          [payload.message_id]: {
            error_type:    payload.error_type,
            error_message: payload.error_message,
            ts:            event.ts,
          },
        }
      }
      break
    }

    case 'continuation_granted':
    case 'continuation_denied': {
      next.continuations = [...next.continuations, {
        kind:                event.event_type,
        turn:                payload.turn,
        index:               payload.index,
        max_continuations:   payload.max_continuations,
        signal_source:       payload.signal_source,
        reason:              payload.reason,
        ts:                  event.ts,
      }]
      break
    }

    case 'session_ended':
      next.ended            = true
      next.session_ended_at = payload.ended_at || event.ts
      next.end_reason       = payload.end_reason || null
      next.final_summary    = {
        turn_counts:        payload.turn_counts || {},
        truthseeker_errors: payload.truthseeker_errors || 0,
        skill_summary:      payload.skill_summary || null,
        wrap_up:            payload.wrap_up || null,
      }
      if (next.current_turn != null) next.current_turn = null
      break

    default:
      // Either it has the _unknown_event_type flag set by the backend
      // or it's a type we don't have a case for. Track it so the GUI
      // can surface a "forward-compatibility hint" banner.
      if (!next.unknown_event_types.includes(event.event_type)) {
        next.unknown_event_types = [...next.unknown_event_types, event.event_type]
      }
      break
  }

  return next
}


/**
 * Apply a list of events in order. Convenience wrapper for the
 * catch-up phase of an SSE connection or for replaying a completed
 * session from /api/sessions/<id>/events.
 */
export function applyEvents(state, events) {
  let s = state
  for (const e of events) {
    s = applyEvent(s, e)
  }
  return s
}


/**
 * Derive the progress timeline from a state. Returns a list of
 * step objects suitable for the timeline component:
 *   [{turn, agent, label, status: 'done'|'current'|'future'}, ...]
 *
 * If max_turns is known, the list extends past the executed turns to
 * show the remaining budget. The current turn is whichever turn is
 * `running`, or the last completed turn if none is running.
 */
export function deriveProgress(state) {
  const steps = state.turns.map(t => ({
    turn:   t.turn,
    agent:  t.agent,
    label:  `T${t.turn}`,
    status: t.status === 'running' ? 'current' : 'done',
  }))

  if (state.max_turns && state.turns.length < state.max_turns) {
    const lastTurn = state.turns.length > 0
      ? state.turns[state.turns.length - 1].turn
      : 0
    const remaining = state.max_turns - lastTurn
    if (remaining > 0) {
      steps.push({
        turn:   lastTurn + remaining,
        agent:  null,
        label:  `T${lastTurn + 1}_T${state.max_turns}`,
        status: 'future',
      })
    }
  }

  return steps
}
