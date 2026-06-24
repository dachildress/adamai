import { useState, useRef, useEffect } from 'react'
import { resumeSession } from '../lib/api'

/**
 * ResumeModal — resolve a session paused at the human-review gate (Slice 4a).
 *
 * Three zones, matching the governance design:
 *   1. Why it paused — the agent's reason (read-only), plus the settled
 *      plan it's waiting to act on.
 *   2. Your guidance — free-text direction the agent applies on resume.
 *   3. Documents — optional files to hand the agent (e.g. the privacy
 *      policy it asked for), injected as context on resume.
 *
 * The director picks a decision (Approve / Redirect / Decline) and resumes.
 * Resuming spawns a NEW session that routes straight to Operator with the
 * guidance and documents composed in; onResumed hands that new session id
 * back to the caller.
 */
export function ResumeModal({ paused, onClose, onResumed }) {
  // `paused` carries: session_id, review_reason, and (optionally) the
  // settled synthesis preview from session state.
  const [decision, setDecision]   = useState('approve')
  const [guidance, setGuidance]   = useState('')
  const [files, setFiles]         = useState([])
  const [submitting, setSubmitting] = useState(false)
  const [error, setError]         = useState(null)
  const fileInputRef = useRef(null)
  const guidanceRef = useRef(null)

  const MAX_CONTEXT_FILES      = 20
  const MAX_CONTEXT_FILE_BYTES = 10 * 1024 * 1024

  useEffect(() => {
    if (guidanceRef.current) guidanceRef.current.focus()
  }, [])

  useEffect(() => {
    function onKeyDown(e) {
      if (submitting) return
      if (e.key === 'Escape') { e.preventDefault(); onClose() }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [submitting])     // eslint-disable-line

  function handleFileSelect(ev) {
    const picked = Array.from(ev.target.files || [])
    const room = MAX_CONTEXT_FILES - files.length
    const accepted = []
    for (const f of picked.slice(0, room)) {
      if (f.size <= MAX_CONTEXT_FILE_BYTES) accepted.push(f)
    }
    if (accepted.length) setFiles(prev => [...prev, ...accepted])
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  function removeFile(i) {
    setFiles(prev => prev.filter((_, idx) => idx !== i))
  }

  async function submit() {
    if (submitting) return
    // Redirect and Decline want a reason; nudge but don't hard-block.
    if (decision === 'redirect' && !guidance.trim()) {
      setError('Add a line of guidance so the agent knows how to redirect.')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      const result = await resumeSession(paused.session_id, {
        decision,
        guidance: guidance.trim(),
        contextFiles: files,
      })
      onResumed(result)
    } catch (e) {
      setError(e.message || 'failed to resume session')
      setSubmitting(false)
    }
  }

  function fmtBytes(n) {
    if (n < 1024) return `${n} B`
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
    return `${(n / (1024 * 1024)).toFixed(1)} MB`
  }

  const decisions = [
    { id: 'approve',  label: 'Approve',  hint: 'Produce the deliverable as planned.' },
    { id: 'redirect', label: 'Redirect', hint: 'Apply your guidance, then produce it.' },
    { id: 'reject',   label: 'Decline',  hint: 'Don\u2019t produce it; record the decision.' },
  ]

  return (
    <div className="modal-backdrop" onClick={() => !submitting && onClose()}>
      <div className="modal modal--review" role="dialog" aria-modal="true"
           onClick={e => e.stopPropagation()}>
        <div className="modal__header">
          <h2 className="modal__title">Review required</h2>
          <button className="modal__close" onClick={onClose} disabled={submitting}>✕</button>
        </div>

        {/* Zone 1 — why it paused */}
        <div className="review-zone">
          <div className="review-zone__label">Why this paused</div>
          <div className="review-zone__reason">
            {paused.review_reason || 'This session is waiting for your review before it acts.'}
          </div>
          {paused.synthesis_preview && (
            <details className="review-zone__plan">
              <summary>The plan it\u2019s waiting to act on</summary>
              <div className="review-zone__plan-text">{paused.synthesis_preview}</div>
            </details>
          )}
        </div>

        {/* Decision */}
        <div className="review-zone">
          <div className="review-zone__label">Your decision</div>
          <div className="review-decisions">
            {decisions.map(d => (
              <button
                key={d.id}
                className={`review-decision ${decision === d.id ? 'review-decision--active' : ''}`}
                onClick={() => setDecision(d.id)}
                disabled={submitting}
                type="button"
              >
                <span className="review-decision__label">{d.label}</span>
                <span className="review-decision__hint">{d.hint}</span>
              </button>
            ))}
          </div>
        </div>

        {/* Zone 2 — guidance */}
        <div className="review-zone">
          <div className="review-zone__label">
            Guidance {decision === 'approve' ? '(optional)' : ''}
          </div>
          <textarea
            ref={guidanceRef}
            className="review-zone__input"
            rows={3}
            value={guidance}
            onChange={e => setGuidance(e.target.value)}
            placeholder={
              decision === 'reject'
                ? 'Optional: note why you\u2019re declining.'
                : 'Anything the agent should apply before producing the deliverable.'
            }
            disabled={submitting}
          />
        </div>

        {/* Zone 3 — documents */}
        <div className="review-zone">
          <div className="review-zone__label">Documents (optional)</div>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            style={{ display: 'none' }}
            onChange={handleFileSelect}
            disabled={submitting}
          />
          <button
            className="upload-btn"
            onClick={() => fileInputRef.current?.click()}
            disabled={submitting}
            type="button"
          >
            + Add files
          </button>
          <span className="form-field__hint">
            {files.length === 0
              ? 'e.g. a privacy policy or template the agent needs'
              : `${files.length} file${files.length === 1 ? '' : 's'} attached`}
          </span>
          {files.length > 0 && (
            <ul className="upload-list">
              {files.map((f, i) => (
                <li key={`${f.name}::${f.size}::${i}`} className="upload-list__item">
                  <span className="upload-list__name">{f.name}</span>
                  <span className="upload-list__size">{fmtBytes(f.size)}</span>
                  <button className="upload-list__remove" onClick={() => removeFile(i)}
                          disabled={submitting} title="remove">✕</button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {error && <div className="modal__error">✕ {error}</div>}

        <div className="modal__actions">
          <button className="btn btn--ghost" onClick={onClose} disabled={submitting}>
            Cancel
          </button>
          <button className="btn btn--primary" onClick={submit} disabled={submitting}>
            {submitting ? 'Resuming…' : 'Resume session'}
          </button>
        </div>
      </div>
    </div>
  )
}
