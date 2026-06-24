import { useState, useRef, useEffect } from 'react'
import { resumeSession } from '../lib/api'

/**
 * ResumeModal — resolve a paused session (Slice 4a gate review or Slice 4b
 * information pause).
 */
export function ResumeModal({ paused, onClose, onResumed }) {
  const isInformation = paused.pause_type === 'information'
      || paused.pause_type === 'awaiting_information'

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
    if (isInformation) {
      if (!guidance.trim() && files.length === 0) {
        setError('Add guidance or at least one document the agents requested.')
        return
      }
    } else if (decision === 'redirect' && !guidance.trim()) {
      setError('Add a line of guidance so the agent knows how to redirect.')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      const result = await resumeSession(paused.session_id, {
        decision: isInformation ? 'approve' : decision,
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

  const pauseReason = isInformation
    ? (paused.information_reason || 'Deliberation paused until missing input is provided.')
    : (paused.review_reason || 'This session is waiting for your review before it acts.')

  return (
    <div className="modal-backdrop" onClick={() => !submitting && onClose()}>
      <div className="modal modal--review" role="dialog" aria-modal="true"
           onClick={e => e.stopPropagation()}>
        <div className="modal__header">
          <h2 className="modal__title">
            {isInformation ? 'Provide information' : 'Review required'}
          </h2>
          <button className="modal__close" onClick={onClose} disabled={submitting}>✕</button>
        </div>

        <div className="review-zone">
          <div className="review-zone__label">Why this paused</div>
          <div className="review-zone__reason">{pauseReason}</div>
          {!isInformation && paused.synthesis_preview && (
            <details className="review-zone__plan">
              <summary>The plan it\u2019s waiting to act on</summary>
              <div className="review-zone__plan-text">{paused.synthesis_preview}</div>
            </details>
          )}
        </div>

        {!isInformation && (
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
        )}

        <div className="review-zone">
          <div className="review-zone__label">
            {isInformation ? 'Your answer' : `Guidance ${decision === 'approve' ? '(optional)' : ''}`}
          </div>
          <textarea
            ref={guidanceRef}
            className="review-zone__input"
            rows={3}
            value={guidance}
            onChange={e => setGuidance(e.target.value)}
            placeholder={
              isInformation
                ? 'Paste the policy, data, or direction the agents asked for.'
                : decision === 'reject'
                  ? 'Optional: note why you\u2019re declining.'
                  : 'Anything the agent should apply before producing the deliverable.'
            }
            disabled={submitting}
          />
        </div>

        <div className="review-zone">
          <div className="review-zone__label">
            Documents {isInformation ? '' : '(optional)'}
          </div>
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
            {submitting
              ? 'Resuming…'
              : isInformation ? 'Continue deliberation' : 'Resume session'}
          </button>
        </div>
      </div>
    </div>
  )
}
