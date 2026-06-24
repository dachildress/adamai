/**
 * Agent metadata. Used for rendering agent badges, role descriptions,
 * and color binding. Source of truth for agent display in the UI.
 *
 * NOTE: This is GUI-side display metadata only. The actual roles
 * (advisory, scheduled, predicate-triggered, service) are emitted
 * by ADAM in session_started.payload.agents. We use whatever ADAM
 * says, not what's hardcoded here. The hardcoding is only for visual
 * affordances: icon, color class, short description.
 */

export const AGENT_META = {
  Logician: {
    short: 'L',
    desc:  'Adversarial reasoning, premise scrutiny, internal consistency checks',
  },
  Seeker: {
    short: 'Se',
    desc:  'Empirical context, comparative cases, real-world benchmarks',
  },
  Visionary: {
    short: 'V',
    desc:  'Long-horizon framing, downstream consequences, opportunity space',
  },
  Synthesizer: {
    short: 'Sy',
    desc:  'Convergent consolidation, decision points, ratification proposals',
  },
  Sentinel: {
    short: 'St',
    desc:  'Procedural and ethical concerns, predicate-triggered',
  },
  Operator: {
    short: 'O',
    desc:  'Skill execution, artifact creation, finalization',
  },
  Truthseeker: {
    short: 'Tk',
    desc:  'Claim verification against web sources and context documents',
  },
  Summarizer: {
    short: 'Sm',
    desc:  'Service: turn summarization and context compression',
  },
  System: {
    short: 'Sx',
    desc:  'Infrastructure messages: background context, skill results, audit',
  },
  Director: {
    short: 'D',
    desc:  'Human-in-the-loop: interjections and governance halts',
  },
}

/**
 * Routing reason → human-readable description. Used for the
 * transcript and the tooltip on agent turns.
 */
export const ROUTING_REASON_LABELS = {
  'advisory-rotation':            'Advisory rotation',
  'sentinel-triggered':           'Sentinel concern',
  'operator-triggered':           'Operator turn (skill execution)',
  'synthesizer-cadence':          'Synthesizer convergence',
  'wrap-up-synthesizer':          'Wrap-up: convergence',
  'wrap-up-operator':             'Wrap-up: finalization',
  'operator-continuation':        'Operator continuation',
  'director-addressed':           'Director-addressed turn',
}

/**
 * Highlight provenance markers in transcript text:
 *   [CTX-...]  → mint pill
 *   [DOC]      → slate pill
 *   {!docN.M}  → faint marker
 *
 * Returns an array of segments suitable for React rendering:
 *   [{type: 'text', value: '...'}, {type: 'ctx', value: 'CTX-...'}]
 */
export function tokenizeContent(text) {
  if (!text) return []
  const segments = []
  // Pattern matches: [CTX-...], [DOC], [doc], or any [TAG]-shaped marker
  const re = /(\[CTX-[A-Z0-9-]+\])|(\[DOC\]|\[doc\])|(\{![\w\.]+\})|(\[[A-Z_]+\])/g
  let last = 0
  let m
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) {
      segments.push({ type: 'text', value: text.slice(last, m.index) })
    }
    if (m[1]) {
      segments.push({ type: 'ctx', value: m[1] })
    } else if (m[2]) {
      segments.push({ type: 'doc', value: m[2] })
    } else if (m[3]) {
      segments.push({ type: 'doc', value: m[3] })
    } else if (m[4]) {
      segments.push({ type: 'tag', value: m[4] })
    }
    last = m.index + m[0].length
  }
  if (last < text.length) {
    segments.push({ type: 'text', value: text.slice(last) })
  }
  return segments
}

/**
 * Format a duration in milliseconds to a compact display string:
 *   < 1s: "950ms"
 *   < 60s: "3.5s"
 *   >= 60s: "1m 37s"
 */
export function formatDuration(ms) {
  if (ms == null) return ''
  if (ms < 1000) return `${ms}ms`
  const seconds = ms / 1000
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const mins = Math.floor(seconds / 60)
  const rem = Math.floor(seconds % 60)
  return `${mins}m ${rem}s`
}

/**
 * Format a wall-clock timestamp for display in the header/footer/turn
 * timestamps. Defensive against null/invalid input.
 */
export function formatTimestamp(ts, opts = {}) {
  if (!ts) return ''
  try {
    const d = new Date(ts)
    if (isNaN(d.getTime())) return ts
    if (opts.timeOnly) {
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
    }
    return d.toLocaleString([], { dateStyle: 'short', timeStyle: 'medium', hour12: false })
  } catch {
    return ts
  }
}

/**
 * Format elapsed time between two timestamps as "MMm SSs".
 * Returns the empty string if either is missing.
 */
export function formatElapsed(startTs, endTs) {
  if (!startTs) return ''
  try {
    const start = new Date(startTs).getTime()
    const end = endTs ? new Date(endTs).getTime() : Date.now()
    const ms = end - start
    if (ms < 0) return ''
    const seconds = Math.floor(ms / 1000)
    const mins = Math.floor(seconds / 60)
    const rem = seconds % 60
    return `${String(mins).padStart(2, '0')}m ${String(rem).padStart(2, '0')}s`
  } catch {
    return ''
  }
}

/**
 * Pretty-print a session age (started_at → now) for the sidebar.
 */
export function formatAge(ts) {
  if (!ts) return ''
  try {
    const then = new Date(ts).getTime()
    const now  = Date.now()
    const diff = now - then
    const minutes = Math.floor(diff / 60_000)
    const hours   = Math.floor(diff / 3_600_000)
    const days    = Math.floor(diff / 86_400_000)
    if (days >= 1) {
      const d = new Date(ts)
      return d.toLocaleDateString([], { month: 'short', day: 'numeric' }).toLowerCase()
    }
    if (hours >= 1) return `${hours}h`
    if (minutes >= 1) return `${minutes}m`
    return 'now'
  } catch {
    return ''
  }
}
