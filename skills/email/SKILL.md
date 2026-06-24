---
name: email
description: Send an email via SMTP, or create a local .eml draft. High-risk skill - requires explicit allowlist for send. Attachments must come from the current run's artifacts directory.
version: "1.0"

adam:
  source_type: native_adam
  category: executable
  handler: handler.py
  handler_function: handle
  risk_level: high
  audit_required: true
  llm_access: false
  allowed_callers:
    - Operator
  actions:
    send:
      description: Send an email via SMTP. Requires ADAM_EMAIL_RECIPIENT_ALLOWLIST to be configured. Every recipient is checked against the allowlist before any SMTP connection opens. This is the default action for completing a deliberation that produces an external communication; use draft only when explicitly warranted (see Format guidance in the skill README).
      required_args:
        - to
        - subject
        - body
      optional_args:
        - cc
        - bcc
        - from_name
        - attachment_path
        - attachments
    draft:
      description: Create a local .eml draft file in the current run's artifacts directory. No network activity. Used when human review is explicitly warranted before transmission, when the recipient is not on the allowlist, or when SMTP is not configured. The resulting file can be opened in any mail client and either edited or forwarded manually.
      required_args:
        - to
        - subject
        - body
      optional_args:
        - cc
        - bcc
        - from_name
        - attachment_path
        - attachments
---

# email skill

**Purpose:** Compose email artifacts during ADAM deliberations, either as
actual SMTP sends (governed by environment configuration and a recipient
allowlist) or as local `.eml` drafts (offline; reviewable; used in narrow
circumstances).

**Risk level:** High. Email is an irreversible external communication.
Once sent, it cannot be unsent. The send action's design assumes mistakes
are possible and constrains the blast radius to what the deployment has
explicitly authorized via the recipient allowlist.

**Currently-implemented actions:** `send`, `draft`.

## Format guidance

The default action is `send`. Draft is the exception, not the rule.

- **`send`** — the standard action for completing a deliberation that
  produces an external communication. Transmits via SMTP after every
  recipient passes the allowlist check. If the deployment is configured
  to send (`ADAM_EMAIL_RECIPIENT_ALLOWLIST` and SMTP environment
  variables set), Operator should default to send. Failures are
  structured: an unauthorized recipient or missing configuration
  produces a specific error_class the Operator can reason about and,
  if appropriate, fall back to draft on. Defaulting to draft "to be
  safe" defeats the purpose of the allowlist defense, which exists
  precisely so that send can be the default.

- **`draft`** — appropriate in narrow circumstances only:
  - The Director has explicitly requested a draft for review before
    transmission (e.g., a first run on a new recipient or a particularly
    consequential message).
  - The recipient is not on the configured allowlist and the Operator
    has determined that a draft on disk is the right fallback.
  - SMTP configuration is incomplete and send would fail anyway — in
    this case the Operator should also flag the configuration gap in
    the audit so the deployment can be fixed.
  - The deliberation explicitly involves an unreviewed or sensitive
    artifact where one additional human-in-the-loop check is warranted.

  Producing a draft "just in case" or "for safety" is not appropriate
  when the allowlist is configured. The allowlist IS the safety check.

## When to use

When the deliberation has produced an artifact (e.g., a `.docx` strategic
plan from the document skill) that needs to be delivered to a defined
audience. The standard pattern is:

1. `document.create` produces the artifact in the artifacts directory
2. `email.send` transmits it to allowlisted recipients

Use `email.draft` instead of `send` only when one of the conditions in
the Format guidance section applies.

## When NOT to use

- Mid-deliberation: only invoke email when the deliberation has produced
  a finalized artifact ready for external transmission
- Recipients you haven't confirmed are appropriate for the message
- Recipients you have not added to the configured allowlist (the
  allowlist defense will refuse these, but the right behavior is to
  recognize the gap before invoking, not rely on the runtime refusal)
- Situations where the Director has indicated review is required — in
  those cases use `draft` per Format guidance, not avoid the skill

## Required arguments (both actions)

- `to` (string): Comma-separated email addresses. v1 accepts bare addresses
  only (`user@example.com`), not display-name forms (`First Last <user@example.com>`).
- `subject` (string): Email subject line. Cannot contain newlines or carriage
  returns. Maximum 500 characters.
- `body` (string): Plain text email body. Maximum 200,000 characters
  (configurable via `ADAM_EMAIL_MAX_BODY_CHARS`).

## Optional arguments

- `cc` (string): Comma-separated CC list. Same syntax as `to`.
- `bcc` (string): Comma-separated BCC list. Not exposed in audit records
  beyond the count, to preserve the BCC contract.
- `from_name` (string): Display name for the sender. The sender address
  itself is taken from the `ADAM_EMAIL_FROM` environment variable, not
  from args — so Operator cannot manipulate the sending identity at
  runtime. Maximum 200 characters. Cannot contain newlines.
- `attachment_path` (string): Single attachment. Must be inside the
  current run's artifacts directory.
- `attachments` (list or comma-separated string): Multiple attachments.
  Each path must be inside the current run's artifacts directory.

## Required environment variables

For `draft`:
- `ADAM_EMAIL_FROM` — the sender address.

For `send`:
- `ADAM_EMAIL_FROM` — the sender address.
- `ADAM_SMTP_HOST` — SMTP server hostname.
- `ADAM_EMAIL_RECIPIENT_ALLOWLIST` — comma-separated allowed recipients.
  Accepts exact addresses (`user@example.com`) and domain wildcards
  (`*@example.com`).

## Optional environment variables

- `ADAM_SMTP_PORT` (default `587`)
- `ADAM_SMTP_USERNAME`
- `ADAM_SMTP_PASSWORD` (never logged or audited)
- `ADAM_SMTP_USE_TLS` (default `true` — uses STARTTLS)
- `ADAM_SMTP_USE_SSL` (default `false` — set true to use SMTPS instead)
- `ADAM_EMAIL_MAX_ATTACHMENT_BYTES` (default `15000000`, ~15 MB)
- `ADAM_EMAIL_MAX_TOTAL_ATTACHMENT_BYTES` (default `25000000`, ~25 MB)
- `ADAM_EMAIL_MAX_ATTACHMENT_COUNT` (default `10`)
- `ADAM_EMAIL_MAX_RECIPIENTS` (default `50`)
- `ADAM_EMAIL_MAX_BODY_CHARS` (default `200000`)
- `ADAM_EMAIL_MAX_SUBJECT_CHARS` (default `500`)
- `ADAM_EMAIL_SMTP_TIMEOUT_SECONDS` (default `30`)

## Security model

The email skill implements layered defenses appropriate to its high-risk
classification:

**Recipient allowlist (most important).** The `send` action requires
`ADAM_EMAIL_RECIPIENT_ALLOWLIST` to be configured before any SMTP
connection opens. Every recipient — `to`, `cc`, AND `bcc` — is checked.
Unauthorized recipients fail with `error_class: recipient_not_allowlisted`.
Without an allowlist, `send` fails entirely with
`error_class: allowlist_not_configured`. The `draft` action does NOT
require an allowlist (since drafts don't transmit).

**Sender control.** The `From:` address comes from `ADAM_EMAIL_FROM`
only, never from args. Operator can suggest a `from_name` for display
purposes, but cannot change the underlying sender address.

**Header injection prevention.** Subject, from_name, and recipient
strings are checked for CRLF and null characters before being used in
headers. A poisoned input is rejected with `error_class:
header_injection_attempt` and the attack pattern is visible in the
audit log.

**Path traversal prevention.** Attachment paths are canonicalized
(symlinks followed) and checked against the resolved artifacts_root.
Both `../`-style escapes and symlink-out escapes fail at the same gate.
Null bytes in paths are rejected.

**Resource limits.** Per-attachment size, total attachment size,
attachment count, recipient count, body length, and subject length all
have configurable limits with separate error_class values for audit
precision.

**Credentials never in args.** SMTP password is read from environment
only. The handler never returns or logs the password value, and the
audit metadata for a send records the authentication state (yes/no)
but never the credential.

**Structured SMTP errors.** SMTP failures are caught with specific
error_class values: `smtp_auth_failed`, `smtp_connection_failed`,
`smtp_tls_failed`, `smtp_sender_refused`, `smtp_all_recipients_refused`,
`smtp_delivery_failed`. Audit logs can be filtered by failure mode.

**Lazy network capability.** `smtplib` is imported only inside the SMTP
send function, so simply loading the handler module (e.g., for the
`draft` action, or during skill discovery) does not pull in network
code.

## Example: send a board distribution email (production deployment)

```skill_call
{
  "skill_calls": [
    {
      "skill": "email",
      "action": "send",
      "args": {
        "to": "taskforce-chair@lcps.k12.va.us",
        "cc": "superintendent@lcps.k12.va.us",
        "subject": "ACPS AI Strategic Plan - Draft for Review",
        "body": "The attached strategic plan draft was produced by the ADAM deliberation system. It is provided for taskforce and board review. Please return comments by [date].",
        "attachments": ["ACPS_AI_Strategic_Plan_Framework.docx"]
      }
    }
  ]
}
```

Requires:

```bash
export ADAM_EMAIL_FROM="adam@lcps.k12.va.us"
export ADAM_SMTP_HOST="smtp.lcps.k12.va.us"
export ADAM_SMTP_PORT="587"
export ADAM_SMTP_USERNAME="adam@lcps.k12.va.us"
export ADAM_SMTP_PASSWORD="..."
export ADAM_SMTP_USE_TLS="true"
export ADAM_EMAIL_RECIPIENT_ALLOWLIST="taskforce-chair@lcps.k12.va.us,superintendent@lcps.k12.va.us,*@lcps.k12.va.us"
```

Returns a SkillResult with the message-id, SMTP response, and audit
metadata recording the transmission.

## Example: draft when review is explicitly warranted

Same skill_call as above with `"action": "draft"`. The result is a
`.eml` file in `logs/adam_<stamp>.artifacts/` that the Director can
open in any mail client, review, edit, and either send manually or
re-invoke with `action: "send"` after review.

## Notes for future ADAM features

- **GUI integration:** When the GUI ships, it should expose the same
  configuration via UI fields (allowlist, SMTP settings). All defenses
  in this handler are server-side; a GUI cannot bypass them by skipping
  fields, since required args are validated in the handler. A GUI user
  who doesn't know to configure the allowlist will hit the
  `allowlist_not_configured` error immediately, with a helpful message.
- **Director confirmation gates:** When ADAM's runtime adds the
  per-skill pre-execution confirmation gate (planned for high-risk
  skills), this skill's `risk_level: high` will trigger it
  automatically. No skill code changes will be needed.
- **Sentinel review:** Same. The `risk_level` field is already declared
  in the manifest; the runtime adoption is the future work.
- **Default action inverted in v1.0:** Earlier drafts of this skill
  treated `draft` as the preferred action and `send` as the elevated
  variant. The current architecture inverts this: send is the default
  for any production deployment with the allowlist configured, because
  the allowlist IS the safety check and a draft-first workflow bypasses
  it in favor of ad-hoc manual review. Draft remains as an explicit
  fallback for the narrow cases documented in Format guidance. The
  Operator's gate prompts in the runtime should reinforce this
  preference; see adam_agent_chat.py.
