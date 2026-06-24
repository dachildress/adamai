import { useMemo, useState } from 'react'
import { artifactUrl } from '../lib/api'

export function RightBar({ state }) {
  return (
    <aside className="rightbar">
      <VerifierPanel state={state} />
      <SkillRegistryPanel state={state} />
    </aside>
  )
}

function VerifierPanel({ state }) {
  const verifications = state.verifications
  const total = verifications.reduce((s, v) => s + (v.claims_checked || 0), 0)

  return (
    <div className="panel">
      <div className="panel__header">
        <div className="panel__title">Verifier</div>
        <div className="panel__meta">{total} / session</div>
      </div>

      {verifications.length === 0 ? (
        <div style={{ fontSize: 12, color: 'var(--text-faint)', padding: '8px 0' }}>
          No verifications yet. Truthseeker fires after advisory and
          non-Operator wrap-up turns.
        </div>
      ) : (
        <>
          {verifications.flatMap(v => {
            // Render a card per status-count line, since each is a
            // distinct claim with a different verdict
            const cards = []
            const statuses = v.status_counts || {}
            for (const [status, count] of Object.entries(statuses)) {
              cards.push(
                <VerificationRow
                  key={`${v.turn}-${status}`}
                  turn={v.turn}
                  agent={v.agent}
                  status={status}
                  count={count}
                />
              )
            }
            if (v.doc_grounded_count > 0) {
              cards.push(
                <VerificationRow
                  key={`${v.turn}-doc`}
                  turn={v.turn}
                  agent={v.agent}
                  status="DOCUMENT_GROUNDED_NOT_WEB_VERIFIED"
                  count={v.doc_grounded_count}
                />
              )
            }
            return cards
          })}
          <a className="verif__view-all">View all verifications →</a>
        </>
      )}
    </div>
  )
}

function VerificationRow({ turn, agent, status, count }) {
  return (
    <div className="verif">
      <div className="verif__claim">
        {count} claim{count === 1 ? '' : 's'} from {agent}
      </div>
      <div className="verif__meta">
        <span className={`verif__status verif__status--${status}`}>
          {status.replace(/_/g, ' ')}
        </span>
        <span className="verif__turn">T{turn}</span>
      </div>
    </div>
  )
}

function SkillRegistryPanel({ state }) {
  const skills = state.skill_registry?.skills || []
  const invocations = state.skill_invocations
  const [searchTerm, setSearchTerm] = useState('')
  const [selectedSkill, setSelectedSkill] = useState(null)

  // For each registered skill, count invocations
  const skillsAugmented = useMemo(() => {
    return skills.map(s => {
      const invs = invocations.filter(i => i.skill === s.name)
      const successes = invs.filter(i => i.status === 'success').length
      return {
        ...s,
        invocations: invs,
        used: invs.length,
        successes,
        last_invocation: invs[invs.length - 1] || null,
      }
    })
  }, [skills, invocations])

  const filtered = useMemo(() => {
    if (!searchTerm) return skillsAugmented
    const q = searchTerm.toLowerCase()
    return skillsAugmented.filter(s =>
      s.name.toLowerCase().includes(q) ||
      (s.actions || []).some(a => a.toLowerCase().includes(q))
    )
  }, [skillsAugmented, searchTerm])

  // Auto-select the first skill on first render, or any skill that
  // was just used (for live focus on relevant skills).
  const effectiveSelected = selectedSkill || filtered[0]?.name

  return (
    <div className="panel">
      <div className="panel__header">
        <div className="panel__title">Skill Registry</div>
        <div className="panel__meta">{skills.length} loaded</div>
      </div>

      <div className="skill-search">
        <span className="skill-search__icon">⌕</span>
        <input
          className="skill-search__input"
          placeholder="search skills..."
          value={searchTerm}
          onChange={e => setSearchTerm(e.target.value)}
        />
      </div>

      {filtered.length === 0 ? (
        <div style={{ fontSize: 12, color: 'var(--text-faint)', padding: '8px 0' }}>
          {skills.length === 0
            ? 'No skills registered.'
            : 'No matching skills.'}
        </div>
      ) : (
        filtered.map(s => (
          <div
            key={s.name}
            className={`skill-card ${s.name === effectiveSelected ? 'skill-card--selected' : ''}`}
            onClick={() => setSelectedSkill(s.name)}
          >
            <div className="skill-card__header">
              <span className="skill-card__name">{s.name}</span>
              {s.used > 0
                ? <span className="skill-card__badge skill-card__badge--used">
                    USED · {s.used}
                  </span>
                : <span className="skill-card__badge skill-card__badge--ready">READY</span>
              }
            </div>
          </div>
        ))
      )}

      {effectiveSelected && (
        <SkillDetail
          skill={skillsAugmented.find(s => s.name === effectiveSelected)}
          sessionId={state.session_id}
        />
      )}
    </div>
  )
}

function SkillDetail({ skill, sessionId }) {
  if (!skill) return null

  // Risk normalization for CSS class. ADAM doesn't reliably emit risk
  // today (skill_registry_loaded.skills[].risk was null in the smoke
  // test), so we render "—" if not provided rather than a fake value.
  const riskRaw = skill.risk || null
  const riskClass = riskRaw ? riskRaw.toLowerCase().replace(/[ -]/g, '_') : ''

  return (
    <div className="skill-detail">
      <div className="skill-detail__title-row">
        <span className="skill-detail__name">{skill.name}</span>
        {riskRaw
          ? <span className={`skill-detail__risk skill-detail__risk--${riskClass}`}>
              RISK · {riskRaw}
            </span>
          : <span className="skill-detail__risk skill-detail__risk--low" style={{ color: 'var(--text-faint)', background: 'transparent', borderBottom: '1px dashed var(--line-base)' }}>
              RISK · —
            </span>
        }
      </div>

      <div className="skill-detail__description">
        Actions: <code>{(skill.actions || []).join(', ')}</code>
        {skill.version && <span> · version {skill.version}</span>}
      </div>

      <div className="skill-detail__section">
        <div className="skill-detail__section-label">Allowed Callers</div>
        <div className="skill-detail__args">
          {(skill.allowed_callers || []).map(c => (
            <span key={c} style={{ marginRight: 8 }}>{c}</span>
          ))}
        </div>
      </div>

      {skill.last_invocation && (
        <div className="skill-detail__section">
          <div className="skill-detail__section-label">Last Invocation</div>
          <div className="skill-detail__last-invocation">
            T{skill.last_invocation.turn}
            {skill.last_invocation.size_bytes && <> · {(skill.last_invocation.size_bytes / 1024).toFixed(1)} KB</>}
            {' · '}{skill.last_invocation.status}
          </div>
          {/*
            Download link for the artifact produced by this skill's
            last successful invocation. Renders only when the skill
            actually wrote a file (filename or relpath is set) AND
            the invocation succeeded AND we have a sessionId.
            Skills like email.send don't produce files; nothing to link.

            Part 9.2: prefer relpath (nested-path skills like coder)
            over filename (flat-path skills like document/slidedeck).
            artifactUrl() handles both shapes.
          */}
          {sessionId
            && (skill.last_invocation.relpath || skill.last_invocation.filename)
            && skill.last_invocation.status === 'success' && (
            <a
              className="skill-detail__artifact-link"
              href={artifactUrl(
                sessionId,
                skill.last_invocation.relpath || skill.last_invocation.filename
              )}
              target="_blank"
              rel="noopener noreferrer"
            >
              <span className="skill-detail__artifact-link-icon">↗</span>
              <span>open {skill.last_invocation.filename}</span>
            </a>
          )}
        </div>
      )}
    </div>
  )
}
