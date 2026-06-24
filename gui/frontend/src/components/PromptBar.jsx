import { useState, useRef, useEffect } from 'react'
import { submitDirectorMessage } from '../lib/api'

/**
 * Part 8: the Director prompt bar is now live.
 *
 * The user types a message and presses Enter (or clicks Send). The
 * component POSTs to /api/sessions/<id>/director_message, which appends
 * to director_inbox.jsonl. ADAM consumes the inbox at the next turn
 * boundary and emits a director_message event. The reducer tracks
 * consumed message_ids, and this component looks at state.consumed_message_ids
 * (passed in via props) to mark queued messages as "consumed".
 *
 * Disabled when:
 *   - no session is selected
 *   - session.ended is true
 *   - a submit is in flight (briefly)
 *
 * Quick-action buttons (@ Agent, /skill, Request Approval, Export Audit)
 * remain disabled with tooltips -- those are Phase C features.
 * The HALT button is handled by MainPanel's status bar; not duplicated here.
 */
export function PromptBar({
  sessionId,
  ended,
  consumedMessageIds,
  erroredMessageIds,
}) {
  const [text, setText] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)
  const [queued, setQueued] = useState([])   // {message_id, content, queued_at}
  const inputRef = useRef(null)

  // Promote consumed queued messages out of the local list once we see
  // them in events. Keep errored ones visible briefly with a status
  // so the user knows why they vanished.
  useEffect(() => {
    if (!consumedMessageIds && !erroredMessageIds) return
    setQueued(qs =>
      qs.filter(q => !consumedMessageIds?.[q.message_id])
        .map(q => erroredMessageIds?.[q.message_id]
          ? { ...q, status: 'errored', error: erroredMessageIds[q.message_id] }
          : q
        )
    )
  }, [consumedMessageIds, erroredMessageIds])

  const isDisabled = !sessionId || ended || submitting

  const placeholder = !sessionId
    ? 'no session selected'
    : ended
      ? 'session has ended — new messages cannot be queued'
      : 'address the deliberation, >>Agent: to direct a specific role, or >>halt to stop...'

  async function submit() {
    const content = text.trim()
    if (!content || isDisabled) return
    setError(null)
    setSubmitting(true)
    try {
      const result = await submitDirectorMessage(sessionId, content)
      // Push to the local queued list. It'll be removed when the
      // matching director_message event arrives.
      setQueued(qs => [
        ...qs,
        {
          message_id: result.message_id,
          content,
          queued_at: result.queued_at,
          status: 'queued',
        },
      ])
      setText('')
    } catch (e) {
      setError(e.message || 'submission failed')
    } finally {
      setSubmitting(false)
      // Keep focus on the input so the user can continue typing
      if (inputRef.current) inputRef.current.focus()
    }
  }

  function onKeyDown(e) {
    // Enter submits; Shift+Enter is reserved for future multi-line support
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  return (
    <div className="prompt">
      {/* Local queued-message list. Shown only when there's something
          to show. Each row indicates the local-state status (queued,
          errored) of a recently-submitted message. Items disappear
          when their matching director_message event arrives. */}
      {queued.length > 0 && (
        <div className="prompt__queued">
          {queued.map(q => (
            <div
              key={q.message_id}
              className={`prompt__queued-item prompt__queued-item--${q.status}`}
              title={q.message_id}
            >
              <span className="prompt__queued-icon">
                {q.status === 'errored' ? '✕' : '⋯'}
              </span>
              <span className="prompt__queued-content">{q.content}</span>
              <span className="prompt__queued-status">
                {q.status === 'errored'
                  ? `rejected: ${q.error?.error_type || 'error'}`
                  : 'queued — waiting for next turn boundary'}
              </span>
            </div>
          ))}
        </div>
      )}

      {error && (
        <div className="prompt__error">
          <span>✕ {error}</span>
          <button onClick={() => setError(null)} className="prompt__error-dismiss">
            dismiss
          </button>
        </div>
      )}

      <div className={`prompt__bar ${isDisabled ? 'prompt__bar--disabled' : ''}`}>
        <span className="prompt__caret">›</span>
        <input
          ref={inputRef}
          className="prompt__input"
          placeholder={placeholder}
          disabled={isDisabled}
          value={text}
          onChange={e => setText(e.target.value)}
          onKeyDown={onKeyDown}
          maxLength={8000}
        />
        <button
          className={`prompt__send ${(!text.trim() || isDisabled) ? 'prompt__send--inactive' : 'prompt__send--active'}`}
          onClick={submit}
          disabled={!text.trim() || isDisabled}
          title={submitting ? 'submitting...' : 'send to director inbox'}
        >
          {submitting ? '...' : '→ Send'}
        </button>
      </div>

      <div className="prompt__footer">
        <div className="prompt__examples">
          <span style={{ marginRight: 4 }}>Examples:</span>
          <button
            className="prompt__example prompt__example--clickable"
            onClick={() => !isDisabled && setText('verify all remaining claims')}
            disabled={isDisabled}
          >
            verify all remaining claims
          </button>
          <button
            className="prompt__example prompt__example--clickable"
            onClick={() => !isDisabled && setText('>>halt')}
            disabled={isDisabled}
          >
            &gt;&gt;halt
          </button>
          <button
            className="prompt__example prompt__example--clickable"
            onClick={() => !isDisabled && setText('>>Logician: ')}
            disabled={isDisabled}
          >
            &gt;&gt;Logician: …
          </button>
        </div>

        <div className="prompt__quickactions">
          <button className="prompt__action" disabled data-tooltip="Phase C: @AGENT targeting via picker">
            @ Agent
          </button>
          <button className="prompt__action" disabled data-tooltip="Phase C: /SKILL commands">
            /skill
          </button>
          <button className="prompt__action" disabled data-tooltip="Phase C: human-approval gates">
            ✓ Request Approval
          </button>
          <button className="prompt__action" disabled data-tooltip="Phase C: audit export">
            ↓ Export Audit
          </button>
        </div>
      </div>
    </div>
  )
}
