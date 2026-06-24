import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  fetchGovernanceAdmin,
  validateGovernanceConfig,
  saveGovernanceConfig,
  fetchAdminUsers,
  patchUserGovernanceProfile,
} from '../lib/api'

/**
 * Slice 4.2: governance admin — view and edit profiles + rulesets.
 */
export function GovernanceAdminModal({ onClose }) {
  const [data, setData] = useState(null)
  const [draft, setDraft] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState('profiles')
  const [draftValidation, setDraftValidation] = useState(null)
  const [validating, setValidating] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState(null)

  const load = useCallback(async () => {
    const view = await fetchGovernanceAdmin()
    setData(view)
    setDraft(structuredClone(view.config))
    setDraftValidation(null)
    setSaveError(null)
  }, [])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        await load()
      } catch (e) {
        if (!cancelled) setError(String(e.message || e))
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => { cancelled = true }
  }, [load])

  const dirty = useMemo(() => {
    if (!data?.config || !draft) return false
    return JSON.stringify(data.config) !== JSON.stringify(draft)
  }, [data, draft])

  function requestClose() {
    if (saving || validating) return
    if (dirty) {
      const ok = window.confirm(
        'Discard unsaved governance changes?'
      )
      if (!ok) return
    }
    onClose()
  }

  function handleBackdrop(e) {
    if (e.target === e.currentTarget) requestClose()
  }

  async function handleValidate() {
    if (!draft) return
    setValidating(true)
    setSaveError(null)
    try {
      const result = await validateGovernanceConfig(draft)
      setDraftValidation(result)
    } catch (e) {
      setSaveError(String(e.message || e))
    } finally {
      setValidating(false)
    }
  }

  async function handleSave() {
    if (!draft) return
    setSaving(true)
    setSaveError(null)
    try {
      const view = await saveGovernanceConfig(draft)
      setData(view)
      setDraft(structuredClone(view.config))
      setDraftValidation(view.validation)
    } catch (e) {
      setSaveError(e.errors?.length
        ? e.errors.join('; ')
        : String(e.message || e))
      if (e.errors) {
        setDraftValidation({ valid: false, errors: e.errors, warnings: [] })
      }
    } finally {
      setSaving(false)
    }
  }

  const validation = draftValidation || data?.validation
  const source = data?.source
  const skillUniverse = data?.skill_universe || []
  const rulesetIds = draft ? Object.keys(draft.policy_bounds || {}) : []
  const profileIds = draft ? Object.keys(draft.governance_profiles || {}) : []

  return (
    <div
      className="modal-backdrop"
      onClick={handleBackdrop}
      role="presentation"
    >
      <div
        className="modal modal--governance-admin"
        role="dialog"
        aria-labelledby="governance-admin-title"
        aria-modal="true"
      >
        <div className="modal__header">
          <div>
            <div className="modal__title" id="governance-admin-title">
              Governance configuration
            </div>
            <div className="gov-admin__subtitle">
              Profiles select a ruleset; rulesets define what is allowed.
              {dirty && (
                <span className="gov-admin__dirty-tag"> · unsaved changes</span>
              )}
            </div>
          </div>
          <button type="button" className="modal__close" onClick={requestClose} aria-label="Close">
            ✕
          </button>
        </div>

        <div className="modal__body gov-admin__body">
          {loading && (
            <div className="gov-admin__loading">Loading governance config…</div>
          )}

          {error && (
            <div className="modal__error">{error}</div>
          )}

          {saveError && (
            <div className="modal__error">{saveError}</div>
          )}

          {data && draft && (
            <>
              {validation && (
                <ValidationBanner validation={validation} source={source} />
              )}

              {source && (
                <div className="gov-admin__source">
                  <span className="gov-admin__source-label">Config file</span>
                  <code className="gov-admin__source-path">{source.path || '—'}</code>
                  {source.using_builtin_fallback && (
                    <span className="gov-admin__badge gov-admin__badge--warn">
                      using built-in fallback
                    </span>
                  )}
                  <p className="gov-admin__source-note">{source.reload_note}</p>
                </div>
              )}

              <div className="gov-admin__tabs">
                <button
                  type="button"
                  className={`gov-admin__tab ${tab === 'profiles' ? 'gov-admin__tab--active' : ''}`}
                  onClick={() => setTab('profiles')}
                >
                  Profiles ({profileIds.length})
                </button>
                <button
                  type="button"
                  className={`gov-admin__tab ${tab === 'rulesets' ? 'gov-admin__tab--active' : ''}`}
                  onClick={() => setTab('rulesets')}
                >
                  Rulesets ({rulesetIds.length})
                </button>
                <button
                  type="button"
                  className={`gov-admin__tab ${tab === 'fields' ? 'gov-admin__tab--active' : ''}`}
                  onClick={() => setTab('fields')}
                >
                  Field reference
                </button>
                <button
                  type="button"
                  className={`gov-admin__tab ${tab === 'pilots' ? 'gov-admin__tab--active' : ''}`}
                  onClick={() => setTab('pilots')}
                >
                  Users
                </button>
              </div>

              {tab === 'profiles' && (
                <ProfilesEditor
                  draft={draft}
                  setDraft={setDraft}
                  rulesetIds={rulesetIds}
                  reviewModes={data.review_modes}
                  reviewConditions={data.review_conditions}
                />
              )}
              {tab === 'rulesets' && (
                <RulesetsEditor
                  draft={draft}
                  setDraft={setDraft}
                  skillUniverse={skillUniverse}
                />
              )}
              {tab === 'fields' && (
                <FieldReferencePanel
                  fieldEnforcement={data.field_enforcement}
                  skillUniverse={skillUniverse}
                />
              )}
              {tab === 'pilots' && (
                <UsersAssignmentPanel profileIds={profileIds} />
              )}
            </>
          )}
        </div>

        <div className="modal__footer">
          <div className="modal__footer-hint">
            Changes apply to new sessions immediately after save.
          </div>
          <div className="modal__footer-actions">
            <button
              type="button"
              className="btn btn--ghost"
              onClick={requestClose}
              disabled={saving || validating}
            >
              Close
            </button>
            <button
              type="button"
              className="btn btn--ghost"
              onClick={handleValidate}
              disabled={saving || validating || !draft}
            >
              {validating ? 'Validating…' : 'Validate'}
            </button>
            <button
              type="button"
              className="btn btn--primary"
              onClick={handleSave}
              disabled={saving || validating || !dirty}
            >
              {saving ? 'Saving…' : 'Save changes'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}


function ValidationBanner({ validation, source }) {
  const { valid, errors = [], warnings = [] } = validation
  if (valid && warnings.length === 0) {
    return (
      <div className="gov-admin__validation gov-admin__validation--ok">
        Configuration is valid
        {source?.file_exists && !source?.using_builtin_fallback && (
          <span className="gov-admin__validation-detail"> — loaded from disk</span>
        )}
      </div>
    )
  }
  return (
    <div className={`gov-admin__validation ${valid ? 'gov-admin__validation--warn' : 'gov-admin__validation--error'}`}>
      {!valid && (
        <div className="gov-admin__validation-title">
          Configuration has errors — save is blocked until fixed
        </div>
      )}
      {valid && warnings.length > 0 && (
        <div className="gov-admin__validation-title">Warnings</div>
      )}
      <ul className="gov-admin__validation-list">
        {errors.map((msg) => (
          <li key={`e-${msg}`}>{msg}</li>
        ))}
        {warnings.map((msg) => (
          <li key={`w-${msg}`}>{msg}</li>
        ))}
      </ul>
    </div>
  )
}


function ProfilesEditor({ draft, setDraft, rulesetIds, reviewModes, reviewConditions }) {
  const profiles = draft.governance_profiles || {}
  const profileIds = Object.keys(profiles)

  function updateProfile(pid, patch) {
    setDraft((prev) => ({
      ...prev,
      governance_profiles: {
        ...prev.governance_profiles,
        [pid]: { ...prev.governance_profiles[pid], ...patch },
      },
    }))
  }

  function toggleReviewCondition(pid, cond) {
    const current = profiles[pid]?.review_required_for || []
    const next = current.includes(cond)
      ? current.filter((c) => c !== cond)
      : [...current, cond]
    updateProfile(pid, { review_required_for: next })
  }

  function setDefaultProfile(pid) {
    setDraft((prev) => ({ ...prev, default_profile_id: pid }))
  }

  if (!profileIds.length) {
    return <div className="gov-admin__empty">No profiles defined.</div>
  }

  return (
    <div className="gov-admin__cards">
      {profileIds.map((pid) => {
        const p = profiles[pid]
        const isDefault = draft.default_profile_id === pid
        return (
          <div key={pid} className="gov-admin__card gov-admin__card--edit">
            <div className="gov-admin__card-header">
              <span className="gov-admin__card-title">{p.name || pid}</span>
              <span className="gov-admin__card-id">{pid}</span>
              {isDefault ? (
                <span className="gov-admin__badge gov-admin__badge--default">default</span>
              ) : (
                <button
                  type="button"
                  className="gov-admin__link-btn"
                  onClick={() => setDefaultProfile(pid)}
                >
                  Set as default
                </button>
              )}
            </div>

            <label className="gov-admin__field">
              <span className="gov-admin__field-label">Display name</span>
              <input
                className="gov-admin__input"
                value={p.name || ''}
                onChange={(e) => updateProfile(pid, { name: e.target.value })}
              />
            </label>

            <label className="gov-admin__field">
              <span className="gov-admin__field-label">Description</span>
              <textarea
                className="gov-admin__textarea"
                rows={2}
                value={p.description || ''}
                onChange={(e) => updateProfile(pid, { description: e.target.value })}
              />
            </label>

            <label className="gov-admin__field">
              <span className="gov-admin__field-label">
                Ruleset <span className="gov-admin__badge gov-admin__badge--live">live</span>
              </span>
              <select
                className="gov-admin__select"
                value={p.policy_bounds_id || ''}
                onChange={(e) => updateProfile(pid, { policy_bounds_id: e.target.value })}
              >
                {rulesetIds.map((rid) => (
                  <option key={rid} value={rid}>{rid}</option>
                ))}
              </select>
            </label>

            <label className="gov-admin__field">
              <span className="gov-admin__field-label">
                Human review mode <span className="gov-admin__badge gov-admin__badge--live">live</span>
              </span>
              <select
                className="gov-admin__select"
                value={p.human_review_mode || 'none'}
                onChange={(e) => updateProfile(pid, { human_review_mode: e.target.value })}
              >
                {(reviewModes || ['none', 'conditional', 'required']).map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
            </label>

            <fieldset className="gov-admin__fieldset">
              <legend>
                Review required for{' '}
                <span className="gov-admin__badge gov-admin__badge--live">live</span>
              </legend>
              <div className="gov-admin__checks">
                {(reviewConditions || []).map((cond) => (
                  <label key={cond} className="gov-admin__check">
                    <input
                      type="checkbox"
                      checked={(p.review_required_for || []).includes(cond)}
                      onChange={() => toggleReviewCondition(pid, cond)}
                    />
                    <span>{cond}</span>
                  </label>
                ))}
              </div>
            </fieldset>
          </div>
        )
      })}
    </div>
  )
}


function RulesetsEditor({ draft, setDraft, skillUniverse }) {
  const rulesets = draft.policy_bounds || {}
  const rulesetIds = Object.keys(rulesets)

  function updateRuleset(rid, patch) {
    setDraft((prev) => ({
      ...prev,
      policy_bounds: {
        ...prev.policy_bounds,
        [rid]: { ...prev.policy_bounds[rid], ...patch },
      },
    }))
  }

  function usesAllowList(bounds) {
    return Array.isArray(bounds.allowed_skills) && bounds.allowed_skills.length > 0
  }

  function setAllowListMode(rid, enabled) {
    const bounds = rulesets[rid]
    if (enabled) {
      updateRuleset(rid, {
        allowed_skills: bounds.allowed_skills?.length
          ? bounds.allowed_skills
          : [...skillUniverse],
      })
    } else {
      const next = { ...bounds }
      delete next.allowed_skills
      setDraft((prev) => ({
        ...prev,
        policy_bounds: { ...prev.policy_bounds, [rid]: next },
      }))
    }
  }

  function toggleSkill(rid, field, skill) {
    const bounds = rulesets[rid]
    const current = bounds[field] || []
    const next = current.includes(skill)
      ? current.filter((s) => s !== skill)
      : [...current, skill].sort()
    updateRuleset(rid, { [field]: next })
  }

  if (!rulesetIds.length) {
    return <div className="gov-admin__empty">No rulesets defined.</div>
  }

  return (
    <div className="gov-admin__cards">
      {rulesetIds.map((rid) => {
        const b = rulesets[rid]
        const allowList = usesAllowList(b)
        return (
          <div key={rid} className="gov-admin__card gov-admin__card--edit">
            <div className="gov-admin__card-header">
              <span className="gov-admin__card-title">{b.name || rid}</span>
              <span className="gov-admin__card-id">{rid}</span>
            </div>

            <label className="gov-admin__field">
              <span className="gov-admin__field-label">Display name</span>
              <input
                className="gov-admin__input"
                value={b.name || ''}
                onChange={(e) => updateRuleset(rid, { name: e.target.value })}
              />
            </label>

            <label className="gov-admin__field">
              <span className="gov-admin__field-label">Description</span>
              <textarea
                className="gov-admin__textarea"
                rows={2}
                value={b.description || ''}
                onChange={(e) => updateRuleset(rid, { description: e.target.value })}
              />
            </label>

            <fieldset className="gov-admin__fieldset">
              <legend>
                Skill policy{' '}
                <span className="gov-admin__badge gov-admin__badge--live">live</span>
              </legend>
              <label className="gov-admin__check gov-admin__check--block">
                <input
                  type="radio"
                  name={`allow-mode-${rid}`}
                  checked={!allowList}
                  onChange={() => setAllowListMode(rid, false)}
                />
                <span>Default allow — only the blocked list disables skills</span>
              </label>
              <label className="gov-admin__check gov-admin__check--block">
                <input
                  type="radio"
                  name={`allow-mode-${rid}`}
                  checked={allowList}
                  onChange={() => setAllowListMode(rid, true)}
                />
                <span>Allow-list only — deny any skill not checked below</span>
              </label>

              {allowList && (
                <div className="gov-admin__skill-group">
                  <div className="gov-admin__skill-label">Allowed skills</div>
                  <div className="gov-admin__checks">
                    {skillUniverse.map((skill) => (
                      <label key={`a-${skill}`} className="gov-admin__check">
                        <input
                          type="checkbox"
                          checked={(b.allowed_skills || []).includes(skill)}
                          onChange={() => toggleSkill(rid, 'allowed_skills', skill)}
                        />
                        <span>{skill}</span>
                      </label>
                    ))}
                  </div>
                </div>
              )}

              <div className="gov-admin__skill-group">
                <div className="gov-admin__skill-label">Blocked skills</div>
                <div className="gov-admin__checks">
                  {skillUniverse.map((skill) => (
                    <label key={`b-${skill}`} className="gov-admin__check">
                      <input
                        type="checkbox"
                        checked={(b.blocked_skills || []).includes(skill)}
                        onChange={() => toggleSkill(rid, 'blocked_skills', skill)}
                      />
                      <span>{skill}</span>
                    </label>
                  ))}
                </div>
              </div>
            </fieldset>

            <fieldset className="gov-admin__fieldset">
              <legend>Action bounds</legend>
              <label className="gov-admin__check gov-admin__check--block">
                <input
                  type="checkbox"
                  checked={b.external_actions_allowed !== false}
                  onChange={(e) => updateRuleset(rid, {
                    external_actions_allowed: e.target.checked,
                  })}
                />
                <span>
                  External actions allowed{' '}
                  <span className="gov-admin__badge gov-admin__badge--live">live</span>
                </span>
              </label>
              <label className="gov-admin__check gov-admin__check--block">
                <input
                  type="checkbox"
                  checked={b.email_send_allowed !== false}
                  onChange={(e) => updateRuleset(rid, {
                    email_send_allowed: e.target.checked,
                  })}
                />
                <span>
                  Email send allowed{' '}
                  <span className="gov-admin__badge gov-admin__badge--live">live</span>
                </span>
              </label>
              <label className="gov-admin__check gov-admin__check--block">
                <input
                  type="checkbox"
                  checked={b.file_write_allowed !== false}
                  onChange={(e) => updateRuleset(rid, {
                    file_write_allowed: e.target.checked,
                  })}
                />
                <span>
                  File write allowed{' '}
                  <span className="gov-admin__badge gov-admin__badge--declarative">declarative</span>
                </span>
              </label>
            </fieldset>
          </div>
        )
      })}
    </div>
  )
}


function FieldReferencePanel({ fieldEnforcement, skillUniverse }) {
  if (!fieldEnforcement) return null
  return (
    <div className="gov-admin__field-ref">
      <p className="gov-admin__field-intro">
        Only fields marked <strong>live</strong> affect running sessions today.
        Declarative fields are stored for future enforcement.
      </p>
      {skillUniverse?.length > 0 && (
        <p className="gov-admin__field-skills">
          Known skills: <code>{skillUniverse.join(', ')}</code>
        </p>
      )}
      <h4 className="gov-admin__field-section">Ruleset fields (policy_bounds)</h4>
      <FieldTable fields={fieldEnforcement.policy_bounds} />
      <h4 className="gov-admin__field-section">Profile fields (governance_profiles)</h4>
      <FieldTable fields={fieldEnforcement.governance_profiles} />
    </div>
  )
}


function FieldTable({ fields }) {
  if (!fields) return null
  return (
    <table className="gov-admin__table">
      <thead>
        <tr>
          <th>Field</th>
          <th>Status</th>
          <th>Description</th>
        </tr>
      </thead>
      <tbody>
        {Object.entries(fields).map(([key, meta]) => (
          <tr key={key}>
            <td><code>{key}</code></td>
            <td>
              {meta.enforced ? (
                <span className="gov-admin__badge gov-admin__badge--live">
                  live{meta.slice ? ` · slice ${meta.slice}` : ''}
                </span>
              ) : (
                <span className="gov-admin__badge gov-admin__badge--declarative">
                  declarative
                </span>
              )}
            </td>
            <td>{meta.description}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}


function UsersAssignmentPanel({ profileIds }) {
  const [usersData, setUsersData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [savingUser, setSavingUser] = useState(null)
  const [rowError, setRowError] = useState(null)

  const loadUsers = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await fetchAdminUsers()
      setUsersData(data)
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadUsers()
  }, [loadUsers])

  async function handleAssign(username, value) {
    setSavingUser(username)
    setRowError(null)
    try {
      await patchUserGovernanceProfile(username, value || null)
      await loadUsers()
    } catch (e) {
      setRowError(`${username}: ${e.message || e}`)
    } finally {
      setSavingUser(null)
    }
  }

  if (loading) {
    return <div className="gov-admin__loading">Loading users…</div>
  }
  if (error) {
    return <div className="modal__error">{error}</div>
  }

  const users = usersData?.users || []
  const defaultId = usersData?.default_profile_id || 'general'
  const profiles = usersData?.profiles || []
  const profileOptions = profiles.length
    ? profiles.map((p) => ({ id: p.id, name: p.name || p.id }))
    : profileIds.map((id) => ({ id, name: id }))

  if (!users.length) {
    return <div className="gov-admin__empty">No users in users.json.</div>
  }

  return (
    <div className="gov-admin__users">
      <p className="gov-admin__field-intro">
        Pilots are locked to their assigned profile at session start (server-enforced).
        Admins choose a profile per session in the New Session modal.
        Clearing a pilot&apos;s assignment uses the role default, then{' '}
        <code>{defaultId}</code>.
      </p>
      {rowError && <div className="modal__error">{rowError}</div>}
      <table className="gov-admin__table gov-admin__table--users">
        <thead>
          <tr>
            <th>User</th>
            <th>Role</th>
            <th>Status</th>
            <th>Assigned profile</th>
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.username}>
              <td>
                <div className="gov-admin__user-name">{u.display_name}</div>
                <div className="gov-admin__user-id">{u.username}</div>
              </td>
              <td>{u.role}</td>
              <td>{u.status}</td>
              <td>
                {u.governance_profile_locked ? (
                  <select
                    className="gov-admin__select gov-admin__select--inline"
                    value={u.governance_profile || ''}
                    disabled={savingUser === u.username}
                    onChange={(e) => handleAssign(u.username, e.target.value || null)}
                  >
                    <option value="">
                      {u.role_governance_profile
                        ? `(role: ${u.role_governance_profile})`
                        : `(system default: ${defaultId})`}
                    </option>
                    {profileOptions.map((p) => (
                      <option key={p.id} value={p.id}>{p.name}</option>
                    ))}
                  </select>
                ) : (
                  <span className="gov-admin__admin-note">chooses per session</span>
                )}
                {u.governance_profile_locked && u.role_governance_profile && !u.governance_profile && (
                  <div className="gov-admin__user-hint">
                    role fallback: {u.role_governance_profile}
                  </div>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
