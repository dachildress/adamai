import { useEffect, useState } from 'react'
import { fetchQuerySourceModels, runDataIntelligenceQuery } from '../lib/api'

/**
 * Query Data Source (any authenticated user): pick a ratified source by
 * version, ask one objective, and render the governed SkillResult with the
 * fact/judgment separation visible — runtime observations in one region;
 * model inferences / recommendations / assumptions / confidence clearly
 * marked as judgment; source lineage shown. When the pipeline blocks, the
 * stage/reason is shown. The form never collects connection credentials.
 */
const BLOCK_LABEL = {
  policy_denied: 'Blocked by policy (Sentinel)',
  approval_required: 'Requires human approval',
  validation_error: 'Plan rejected at validation',
  adapter_unavailable: 'Data source unavailable',
  plan_parse_error: 'The planner returned an unusable plan',
  interpretation_error: 'The interpretation could not be parsed',
  empty: 'No matching data',
}

export function QueryDataSourceModal({ onClose }) {
  const [models, setModels] = useState([])
  const [version, setVersion] = useState('')
  const [objective, setObjective] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [outcome, setOutcome] = useState(null)   // {result} | {error}

  useEffect(() => {
    (async () => {
      try {
        const m = await fetchQuerySourceModels()
        const list = m.source_models || []
        setModels(list)
        // Only default to a source that can actually answer (has a connection).
        const firstQueryable = list.find(s => s.has_connection)
        if (firstQueryable) setVersion(firstQueryable.version)
      } catch (e) { setError(e.message || String(e)) }
    })()
  }, [])

  async function handleRun() {
    if (!version || !objective.trim()) { setError('pick a source and enter an objective'); return }
    setBusy(true); setError(null); setOutcome(null)
    try {
      const res = await runDataIntelligenceQuery(version, objective.trim())
      setOutcome(res)
    } catch (e) { setError(e.message || String(e)) }
    finally { setBusy(false) }
  }

  function handleBackdrop(e) { if (e.target === e.currentTarget) onClose() }

  const result = outcome?.result
  const configError = outcome?.error   // MODEL_NOT_CONFIGURED / CONNECTION_NOT_CONFIGURED
  const blocked = result && result.status !== 'ok'

  return (
    <div className="modal-backdrop" onClick={handleBackdrop} role="presentation">
      <div className="modal modal--governance-admin" role="dialog" aria-modal="true"
           aria-labelledby="query-ds-title">
        <div className="modal__header">
          <div>
            <div className="modal__title" id="query-ds-title">Query Data Source</div>
            <div className="gov-admin__subtitle">
              Ask a question of an approved source. Answers separate machine-computed
              facts from model interpretation.
            </div>
          </div>
          <button type="button" className="modal__close" onClick={onClose} aria-label="Close">✕</button>
        </div>

        <div className="modal__body gov-admin__body">
          {error && <div className="modal__error">{error}</div>}

          <div className="gov-admin__card gov-admin__card--edit">
            <label className="gov-admin__field">
              <span className="gov-admin__field-label">Source</span>
              <select className="gov-admin__select" value={version}
                      onChange={e => setVersion(e.target.value)}>
                {models.length === 0 && <option value="">No approved sources</option>}
                {models.map(m => (
                  <option key={m.version} value={m.version} disabled={!m.has_connection}>
                    {m.source_name} · {m.version} · approved {m.approved_at}
                    {m.has_connection ? '' : ' (no connection)'}
                  </option>
                ))}
              </select>
            </label>
            <label className="gov-admin__field">
              <span className="gov-admin__field-label">Objective</span>
              <textarea className="gov-admin__textarea" rows={3} value={objective}
                        placeholder="e.g. which schools have the highest absenteeism?"
                        onChange={e => setObjective(e.target.value)} />
            </label>
            <div className="gov-admin__form-actions">
              <button type="button" className="btn btn--primary btn--small" onClick={handleRun}
                      disabled={busy || !version || !objective.trim()}>
                {busy ? 'Running…' : 'Ask'}
              </button>
            </div>
          </div>

          {configError && (
            <div className="modal__error">
              {configError === 'MODEL_NOT_CONFIGURED'
                ? 'No model is configured for queries in this deployment yet.'
                : configError === 'CONNECTION_NOT_CONFIGURED'
                ? 'No read-only connection is configured for this source yet.'
                : configError}
            </div>
          )}

          {result && (
            <div className="gov-admin__card gov-admin__card--edit">
              <div className="gov-admin__card-header">
                <span className="gov-admin__card-title">Answer</span>
                <span className={`gov-admin__status gov-admin__status--${result.status === 'ok' ? 'active' : 'suspended'}`}>
                  {result.status}
                </span>
              </div>

              {blocked && (
                <div className="gov-admin__validation gov-admin__validation--warn">
                  {BLOCK_LABEL[result.status] || result.status}
                  {result.limitations?.length ? ` — ${result.limitations[0]}` : ''}
                </div>
              )}

              {/* FACTS — runtime-computed observations */}
              <div className="gov-admin__skill-group">
                <div className="gov-admin__skill-label">Observations (computed from the data)</div>
                {result.observations?.length ? (
                  <ul className="gov-admin__validation-list">
                    {result.observations.map((o, i) => (
                      <li key={i}><code>{o.label}</code>: {String(o.value)}{o.detail ? ` (${o.detail})` : ''}</li>
                    ))}
                  </ul>
                ) : <div className="gov-admin__empty">No observations.</div>}
              </div>

              {/* JUDGMENT — model interpretation, clearly marked */}
              {result.status === 'ok' && (
                <>
                  <div className="gov-admin__skill-group">
                    <div className="gov-admin__skill-label">Inferences (model interpretation)</div>
                    <ul className="gov-admin__validation-list">
                      {(result.inferences || []).map((x, i) => <li key={i}>{x}</li>)}
                    </ul>
                  </div>
                  <div className="gov-admin__skill-group">
                    <div className="gov-admin__skill-label">Recommendations (model judgment)</div>
                    <ul className="gov-admin__validation-list">
                      {(result.recommendations || []).map((x, i) => <li key={i}>{x}</li>)}
                    </ul>
                  </div>
                  <div className="gov-admin__skill-group">
                    <div className="gov-admin__skill-label">Assumptions</div>
                    <ul className="gov-admin__validation-list">
                      {(result.assumptions || []).map((x, i) => <li key={i}>{x}</li>)}
                    </ul>
                  </div>
                  <div className="gov-admin__temp-pw-note">
                    Confidence (model self-report): <strong>{result.confidence || '—'}</strong>
                    {result.confidence_rationale ? ` — ${result.confidence_rationale}` : ''}
                  </div>
                </>
              )}

              {result.limitations?.length > 0 && (
                <div className="gov-admin__skill-group">
                  <div className="gov-admin__skill-label">Limitations</div>
                  <ul className="gov-admin__validation-list">
                    {result.limitations.map((x, i) => <li key={i}>{x}</li>)}
                  </ul>
                </div>
              )}

              <div className="gov-admin__temp-pw-note">
                Source: <code>{result.source_lineage?.source_model_version}</code>
                {' · plan '}<code>{(result.source_lineage?.plan_id || '').slice(0, 12)}…</code>
              </div>
            </div>
          )}
        </div>

        <div className="modal__footer">
          <div className="modal__footer-hint">Queries are read-only and governed by the pipeline.</div>
          <div className="modal__footer-actions">
            <button type="button" className="btn btn--ghost" onClick={onClose}>Close</button>
          </div>
        </div>
      </div>
    </div>
  )
}
