import { useCallback, useEffect, useState } from 'react'
import {
  testMysqlConnection,
  introspectMysqlSource,
  fetchSourceModelCandidates,
  approveSourceModelCandidate,
  rejectSourceModelCandidate,
  fetchSourceModels,
  removeSourceConnection,
} from '../lib/api'

/**
 * Admin → Data Sources: configure a MySQL connection, test it, introspect a
 * candidate source model, review it, and approve/reject. Admin-gated the same
 * way the governance admin modal is. Passwords are entered here (admin) and
 * never rendered back from any response.
 */
const COARSE_LABEL = {
  ok: 'Connected',
  connection_failed: 'Connection failed',
  authentication_failed: 'Authentication failed',
  no_tables_found: 'Connected, but no tables found',
}

const EMPTY_FORM = {
  host: '', port: 3306, user: '', password: '', database: '', source_name: '',
}

export function DataSourcesModal({ onClose }) {
  const [form, setForm] = useState(EMPTY_FORM)
  const [testResult, setTestResult] = useState(null)   // {status, ok, table_count}
  const [candidate, setCandidate] = useState(null)     // pending candidate under review
  const [candidates, setCandidates] = useState([])
  const [models, setModels] = useState([])
  const [busy, setBusy] = useState('')                 // '' | 'test' | 'introspect' | 'approve' | 'reject'
  const [error, setError] = useState(null)

  function set(k, v) { setForm(f => ({ ...f, [k]: v })); setError(null) }

  const refresh = useCallback(async () => {
    try {
      const [c, m] = await Promise.all([fetchSourceModelCandidates(), fetchSourceModels()])
      setCandidates(c.candidates || [])
      setModels(m.source_models || [])
    } catch (e) { setError(e.message || String(e)) }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  async function handleTest() {
    setBusy('test'); setError(null); setTestResult(null)
    try {
      const res = await testMysqlConnection({
        host: form.host, port: Number(form.port) || 3306, user: form.user,
        password: form.password, database: form.database,
      })
      setTestResult(res)
    } catch (e) { setError(e.message || String(e)) }
    finally { setBusy('') }
  }

  async function handleIntrospect() {
    if (!form.source_name.trim()) { setError('source name is required'); return }
    setBusy('introspect'); setError(null)
    try {
      const cand = await introspectMysqlSource({
        host: form.host, port: Number(form.port) || 3306, user: form.user,
        password: form.password, database: form.database, source_name: form.source_name.trim(),
      })
      setCandidate(cand)
      await refresh()
    } catch (e) { setError(e.message || String(e)) }
    finally { setBusy('') }
  }

  async function handleApprove(id) {
    setBusy('approve'); setError(null)
    try {
      // Send the connection fields so the backend writes the encrypted profile
      // that makes the source queryable. Field names match the backend body.
      await approveSourceModelCandidate(id, {
        host: form.host,
        port: Number(form.port) || 3306,
        user: form.user,
        password: form.password,
        database: form.database,
        display_name: form.source_name?.trim() || undefined,
      })
      setCandidate(null)
      await refresh()
    } catch (e) { setError(e.message || String(e)) }
    finally { setBusy('') }
  }

  async function handleRemoveConnection(version) {
    if (!window.confirm(
      `Remove the stored connection for ${version}?\n\n` +
      `This deletes the encrypted credential only. The approved schema and its ` +
      `governance history are kept, but the source can't be queried until it is ` +
      `re-onboarded with connection details.`,
    )) return
    setBusy('remove-connection'); setError(null)
    try {
      await removeSourceConnection(version)
      await refresh()
    } catch (e) { setError(e.message || String(e)) }
    finally { setBusy('') }
  }

  async function handleReject(id) {
    setBusy('reject'); setError(null)
    try {
      await rejectSourceModelCandidate(id)
      setCandidate(null)
      await refresh()
    } catch (e) { setError(e.message || String(e)) }
    finally { setBusy('') }
  }

  function handleBackdrop(e) { if (e.target === e.currentTarget) onClose() }

  // Test must succeed with >0 tables before introspect is allowed.
  const canIntrospect = testResult && testResult.ok && testResult.table_count > 0 && !busy
  const review = candidate?.schema_detail

  return (
    <div className="modal-backdrop" onClick={handleBackdrop} role="presentation">
      <div className="modal modal--governance-admin" role="dialog" aria-modal="true"
           aria-labelledby="data-sources-title">
        <div className="modal__header">
          <div>
            <div className="modal__title" id="data-sources-title">Data Sources</div>
            <div className="gov-admin__subtitle">
              Connect a read-only MySQL source, introspect its schema, and approve a
              governed source model.
            </div>
          </div>
          <button type="button" className="modal__close" onClick={onClose} aria-label="Close">✕</button>
        </div>

        <div className="modal__body gov-admin__body">
          {error && <div className="modal__error">{error}</div>}

          <div className="gov-admin__card gov-admin__card--edit">
            <div className="gov-admin__card-header">
              <span className="gov-admin__card-title">Configure connection</span>
            </div>
            <div className="gov-admin__form-grid">
              <label className="gov-admin__field"><span className="gov-admin__field-label">Host</span>
                <input className="gov-admin__input" value={form.host} onChange={e => set('host', e.target.value)} /></label>
              <label className="gov-admin__field"><span className="gov-admin__field-label">Port</span>
                <input type="number" className="gov-admin__input" value={form.port} onChange={e => set('port', e.target.value)} /></label>
              <label className="gov-admin__field"><span className="gov-admin__field-label">User</span>
                <input className="gov-admin__input" value={form.user} onChange={e => set('user', e.target.value)} /></label>
              <label className="gov-admin__field"><span className="gov-admin__field-label">Password</span>
                <input type="password" className="gov-admin__input" value={form.password}
                       autoComplete="off" onChange={e => set('password', e.target.value)} /></label>
              <label className="gov-admin__field"><span className="gov-admin__field-label">Database</span>
                <input className="gov-admin__input" value={form.database} onChange={e => set('database', e.target.value)} /></label>
              <label className="gov-admin__field"><span className="gov-admin__field-label">Source name</span>
                <input className="gov-admin__input" value={form.source_name} onChange={e => set('source_name', e.target.value)} /></label>
            </div>
            <div className="gov-admin__form-actions">
              <button type="button" className="btn btn--ghost btn--small" onClick={handleTest}
                      disabled={!!busy || !form.host || !form.user || !form.database}>
                {busy === 'test' ? 'Testing…' : 'Test connection'}
              </button>
              <button type="button" className="btn btn--primary btn--small" onClick={handleIntrospect}
                      disabled={!canIntrospect}
                      title={canIntrospect ? '' : 'Test connection first (needs ≥1 table)'}>
                {busy === 'introspect' ? 'Introspecting…' : 'Introspect'}
              </button>
            </div>
            {testResult && (
              <div className={`gov-admin__validation ${testResult.ok && testResult.table_count > 0
                ? 'gov-admin__validation--ok' : 'gov-admin__validation--warn'}`}>
                {COARSE_LABEL[testResult.status] || testResult.status}
                {testResult.status === 'ok' && ` — ${testResult.table_count} tables`}
                {testResult.status === 'no_tables_found' && ' — cannot introspect an empty schema'}
              </div>
            )}
          </div>

          {review && (
            <div className="gov-admin__card gov-admin__card--edit">
              <div className="gov-admin__card-header">
                <span className="gov-admin__card-title">Review candidate — {candidate.source_name}</span>
                <span className="gov-admin__card-id">pending</span>
              </div>
              <div className="gov-admin__temp-pw-note">
                fingerprint: <code>{candidate.schema_fingerprint?.slice(0, 16)}…</code>
              </div>
              {(review.entities || []).map(ent => (
                <div key={ent.name} className="gov-admin__skill-group">
                  <div className="gov-admin__skill-label">{ent.name}</div>
                  <table className="gov-admin__table">
                    <thead><tr><th>Field</th><th>Type</th><th>Nullable</th><th>PK</th></tr></thead>
                    <tbody>
                      {(ent.fields || []).map(f => (
                        <tr key={f.name}>
                          <td><code>{f.name}</code></td>
                          <td>{f.source_type || '—'}</td>
                          <td>{f.nullable ? 'yes' : 'no'}</td>
                          <td>{f.primary_key ? '✓' : ''}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ))}
              {(review.relationships || []).length > 0 && (
                <div className="gov-admin__skill-group">
                  <div className="gov-admin__skill-label">Relationships</div>
                  <ul className="gov-admin__validation-list">
                    {review.relationships.map((r, i) => (
                      <li key={i}>{r.from_entity}.{r.from_field} → {r.to_entity}.{r.to_field} ({r.relationship_type})</li>
                    ))}
                  </ul>
                </div>
              )}
              <div className="gov-admin__form-actions">
                <button type="button" className="btn btn--danger btn--small"
                        onClick={() => handleReject(candidate.candidate_id)} disabled={!!busy}>
                  {busy === 'reject' ? 'Rejecting…' : 'Reject'}
                </button>
                <button type="button" className="btn btn--primary btn--small"
                        onClick={() => handleApprove(candidate.candidate_id)} disabled={!!busy}>
                  {busy === 'approve' ? 'Approving…' : 'Approve'}
                </button>
              </div>
            </div>
          )}

          <div className="gov-admin__users">
            <h4 className="gov-admin__field-section">Pending candidates ({candidates.length})</h4>
            {candidates.length === 0 ? (
              <div className="gov-admin__empty">No candidates.</div>
            ) : (
              <table className="gov-admin__table gov-admin__table--users">
                <thead><tr><th>Source</th><th>Status</th><th>Fingerprint</th><th></th></tr></thead>
                <tbody>
                  {candidates.map(cd => (
                    <tr key={cd.candidate_id}>
                      <td>{cd.source_name}</td>
                      <td><span className={`gov-admin__status gov-admin__status--${cd.status === 'pending' ? 'active' : 'suspended'}`}>{cd.status}</span></td>
                      <td><code>{(cd.schema_fingerprint || '').slice(0, 12)}…</code></td>
                      <td>
                        {cd.status === 'pending' && (
                          <div className="gov-admin__row-actions">
                            <button className="btn btn--ghost btn--small"
                                    onClick={() => setCandidate(cd)} disabled={!!busy}>Review</button>
                            <button className="btn btn--primary btn--small"
                                    onClick={() => handleApprove(cd.candidate_id)} disabled={!!busy}>Approve</button>
                            <button className="btn btn--danger btn--small"
                                    onClick={() => handleReject(cd.candidate_id)} disabled={!!busy}>Reject</button>
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            <h4 className="gov-admin__field-section">Ratified source models ({models.length})</h4>
            {models.length === 0 ? (
              <div className="gov-admin__empty">No ratified models yet.</div>
            ) : (
              <table className="gov-admin__table gov-admin__table--users">
                <thead><tr><th>Source</th><th>Version</th><th>Entities</th><th>Connection</th><th>Approved by</th><th>Approved at</th><th></th></tr></thead>
                <tbody>
                  {models.map(m => (
                    <tr key={m.version}>
                      <td>{m.source_name}</td>
                      <td><code>{m.version}</code></td>
                      <td>{m.entity_count}</td>
                      <td>
                        <span className={`gov-admin__status gov-admin__status--${m.has_connection ? 'active' : 'suspended'}`}>
                          {m.has_connection ? 'connected' : 'no connection'}
                        </span>
                      </td>
                      <td>{m.approved_by}</td>
                      <td>{m.approved_at}</td>
                      <td>
                        {m.has_connection && (
                          <button type="button" className="btn btn--danger btn--small"
                                  onClick={() => handleRemoveConnection(m.version)} disabled={!!busy}>
                            {busy === 'remove-connection' ? 'Removing…' : 'Remove connection'}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>

        <div className="modal__footer">
          <div className="modal__footer-hint">Connections are read-only; passwords are never stored or echoed.</div>
          <div className="modal__footer-actions">
            <button type="button" className="btn btn--ghost" onClick={onClose}>Close</button>
          </div>
        </div>
      </div>
    </div>
  )
}
