"""
ADAM email skill handler.

Actions:
- draft : produce a local .eml file in the run's artifacts directory.
          No network activity. Always safe.
- send  : transmit via SMTP. Gated by environment configuration AND
          a recipient allowlist. Logs the SMTP transaction for audit.

Handler contract:
    handle(action, args, context) -> dict (SkillResult body)

The SkillRuntime wraps this into a full SkillResult; this handler
just produces the action-specific body.

Threat model defenses implemented (see SKILL.md for the full discussion):
  1. Recipient allowlist (ADAM_EMAIL_RECIPIENT_ALLOWLIST). Enforced on
     every recipient including BCC. A send to an address outside the
     allowlist fails before the SMTP connection opens.
  2. Header injection: CRLF and similar protocol-boundary characters
     are rejected in every header field (subject, from_name, recipients).
     EmailMessage's API also normalizes these, but we reject explicitly
     so a poisoned input is audited as an attack pattern, not silently
     scrubbed.
  3. Path traversal: attachments are resolved (resolving symlinks) and
     checked against the canonical artifacts_root. Symlink-escape and
     '..'-traversal both fail at the same gate.
  4. Resource limits: per-attachment size cap, max total attachment
     bytes, max attachment count, max body length, max recipient count.
     Each has a separate error_class for audit precision.
  5. Send-only-when-explicit: send action requires multiple env vars
     to be present. draft works with only ADAM_EMAIL_FROM. Operators
     can use draft for everything except the final send.
  6. Credential surface: SMTP password is read from env only, never
     accepted via args. The full SMTP transaction is logged but the
     password is never recorded in any output or audit field.
  7. SMTP errors are caught and re-raised with structured error_class
     values so the audit log distinguishes auth-failed, connection-failed,
     delivery-failed, and unknown failures.
  8. Lazy SMTP import: smtplib only imports inside _send_smtp(), so
     simply importing the handler module (e.g., for the draft action,
     or during skill discovery) doesn't bring in network code.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import uuid
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ============================================================
# Validation constants
# ============================================================

# Email syntax check. Deliberately conservative: this is for invocation-
# time validation, not for accepting weird-but-RFC-legal addresses. The
# real validation happens at the SMTP server. We just want to catch
# obvious mistakes and injection patterns at the handler layer.
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# CRLF and bare CR/LF in header values are the classic header-injection
# vectors. Reject them explicitly even though EmailMessage normalizes
# many of them. We want the rejection to surface in the audit log so
# poisoned inputs are visible as a detectable pattern.
HEADER_INJECTION_RE = re.compile(r"[\r\n\x00]")

# Limits. All overridable via env so deployments can tune. Defaults
# chosen to be permissive enough for real K-12 use but not so loose
# that resource exhaustion is easy.
DEFAULTS = {
    "max_attachment_bytes":       15_000_000,    # 15 MB per attachment
    "max_total_attachment_bytes": 25_000_000,    # 25 MB total per email
    "max_attachment_count":       10,            # max files per email
    "max_recipients":             50,            # combined to/cc/bcc
    "max_body_chars":             200_000,       # ~50 pages of plain text
    "max_subject_chars":          500,           # generous but bounded
    "smtp_timeout_seconds":       30,
}


# ============================================================
# Public entrypoint
# ============================================================

def handle(action: str, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Entry point called by SkillRuntime.

    The runtime has already validated:
      - caller is in allowed_callers (Operator only for this skill)
      - action exists in the manifest (draft or send)
      - all required_args are present in args
      - arg values don't exceed the runtime's max_content_size_bytes

    This handler validates:
      - arg TYPES (strings, lists)
      - email address SYNTAX for all recipients
      - header field SAFETY (no CRLF injection)
      - attachment paths are inside artifacts_root and exist
      - resource limits are not exceeded
      - environment is configured correctly for the action
      - for 'send': every recipient is on the allowlist
    """
    if action not in ("draft", "send"):
        raise ValueError(f"Unsupported email action: {action!r}")

    if not isinstance(args, dict):
        raise TypeError("args must be a dict")
    if not isinstance(context, dict):
        raise TypeError("context must be a dict")

    # ---- artifacts_root resolution + canonicalization ----
    artifacts_root_raw = context.get("artifacts_root", "")
    if not isinstance(artifacts_root_raw, str) or not artifacts_root_raw:
        raise ValueError("context.artifacts_root is required and must be a non-empty string")
    artifacts_root = Path(artifacts_root_raw).resolve()
    # Create the dir if it doesn't exist yet (first invocation in a session).
    artifacts_root.mkdir(parents=True, exist_ok=True)

    # ---- merge skill_call args with CLI-provided suggestions ----
    # The locked architectural rule: explicit skill_call args win.
    # requested_skill_args is a fallback only. We never override an
    # explicit arg with a CLI suggestion.
    requested_skill_args = context.get("requested_skill_args") or {}
    suggested = {}
    if isinstance(requested_skill_args, dict):
        skill_block = requested_skill_args.get("email", {})
        if isinstance(skill_block, dict):
            action_block = skill_block.get(action, {})
            if isinstance(action_block, dict):
                suggested = dict(action_block)
    merged = dict(suggested)  # start with suggestions
    for k, v in args.items():
        if v is not None:
            merged[k] = v  # explicit args win

    # ---- limits from env (overrideable per deployment) ----
    limits = _resolve_limits()

    # ---- extract + validate fields ----
    to_raw     = _require_text(merged, "to",      limit=10_000)
    subject    = _require_text(merged, "subject", limit=limits["max_subject_chars"])
    body       = _require_text(merged, "body",    limit=limits["max_body_chars"])
    cc_raw     = _optional_text(merged, "cc",     limit=10_000)
    bcc_raw    = _optional_text(merged, "bcc",    limit=10_000)
    from_name  = _optional_text(merged, "from_name", limit=200)

    # Header injection check on every field that ends up in a header
    _reject_header_injection("subject", subject)
    if from_name is not None:
        _reject_header_injection("from_name", from_name)

    # Recipient parsing. Note: we reject display-name addresses entirely
    # for v1 to keep the parser simple and the injection surface small.
    # "First Last <user@example.com>" is NOT accepted. This is a
    # deliberate scope reduction, documented in SKILL.md.
    to_list  = _parse_recipients(to_raw,  "to")
    cc_list  = _parse_recipients(cc_raw,  "cc")  if cc_raw  else []
    bcc_list = _parse_recipients(bcc_raw, "bcc") if bcc_raw else []

    total_recipients = len(to_list) + len(cc_list) + len(bcc_list)
    if total_recipients == 0:
        raise ValueError("at least one recipient is required (to/cc/bcc)")
    if total_recipients > limits["max_recipients"]:
        raise _make_error(
            "too_many_recipients",
            f"recipient count {total_recipients} exceeds limit "
            f"{limits['max_recipients']}",
        )

    # ---- attachment validation ----
    attachment_paths = _collect_attachment_paths(merged, artifacts_root, limits)

    # ---- FROM header (env-only, never from args) ----
    from_address = _resolve_from_address()
    from_header = _build_from_header(from_address, from_name)

    # ---- recipient allowlist (send action only) ----
    # The allowlist is the single most important security defense. It
    # turns "any syntactically valid email" into "only the addresses
    # this deployment has explicitly authorized." Without it, a
    # misjudging Operator can reach anyone on the Internet.
    if action == "send":
        allowlist = _resolve_recipient_allowlist()
        if allowlist is None:
            raise _make_error(
                "allowlist_not_configured",
                "ADAM_EMAIL_RECIPIENT_ALLOWLIST is not set. The send "
                "action requires an explicit recipient allowlist to be "
                "configured. Set ADAM_EMAIL_RECIPIENT_ALLOWLIST to a "
                "comma-separated list of permitted addresses, or use "
                "the 'draft' action which produces a local .eml file "
                "with no allowlist requirement."
            )
        all_recipients = to_list + cc_list + bcc_list
        unauthorized = [r for r in all_recipients if not _matches_allowlist(r, allowlist)]
        if unauthorized:
            raise _make_error(
                "recipient_not_allowlisted",
                f"the following recipients are not on the configured "
                f"allowlist: {unauthorized}. To allow these addresses, "
                f"add them to ADAM_EMAIL_RECIPIENT_ALLOWLIST. The "
                f"allowlist accepts exact addresses (user@example.com) "
                f"and domain wildcards (*@example.com)."
            )

    # ---- build EmailMessage ----
    msg = EmailMessage()
    msg["From"]    = from_header
    msg["To"]      = ", ".join(to_list)
    if cc_list:
        msg["Cc"]  = ", ".join(cc_list)
    # BCC is intentionally NOT set as a header -- only used at SMTP envelope level
    msg["Subject"] = subject
    msg["Date"]    = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)

    # Attach files
    for path in attachment_paths:
        _attach_file(msg, path)

    # ---- dispatch by action ----
    if action == "draft":
        return _produce_draft(
            msg=msg,
            artifacts_root=artifacts_root,
            context=context,
            to_list=to_list, cc_list=cc_list, bcc_list=bcc_list,
            subject=subject,
            attachment_paths=attachment_paths,
        )
    # action == "send"
    return _produce_send(
        msg=msg,
        to_list=to_list, cc_list=cc_list, bcc_list=bcc_list,
        subject=subject,
        attachment_paths=attachment_paths,
        timeout_seconds=limits["smtp_timeout_seconds"],
    )


# ============================================================
# draft action
# ============================================================

def _produce_draft(
    msg:              EmailMessage,
    artifacts_root:   Path,
    context:          Dict[str, Any],
    to_list:          List[str],
    cc_list:          List[str],
    bcc_list:         List[str],
    subject:          str,
    attachment_paths: List[Path],
) -> Dict[str, Any]:
    """Write the EmailMessage as a .eml file in artifacts_root."""
    artifact_id = str(uuid.uuid4())
    invocation_short = str(context.get("invocation_id", ""))[:8] or "draft"
    # The filename is deterministic in pattern but unique per invocation,
    # mirroring how the document skill names artifacts.
    draft_filename = f"email_draft_{invocation_short}.eml"
    draft_path = artifacts_root / draft_filename

    # Collision handling matches LocalFilesystemBackend's pattern
    if draft_path.exists():
        n = 2
        while True:
            candidate = artifacts_root / f"email_draft_{invocation_short}-{n}.eml"
            if not candidate.exists():
                draft_path = candidate
                break
            n += 1

    draft_bytes = bytes(msg)
    draft_path.write_bytes(draft_bytes)
    sha = _sha256_bytes(draft_bytes)

    return {
        "artifact_id":  artifact_id,
        "path":         str(draft_path),
        "filename":     draft_path.name,
        "format":       "eml",
        "sha256":       sha,
        "size_bytes":   len(draft_bytes),
        "backend":      "local_filesystem",
        "mime_type":    "message/rfc822",
        "provider":     "local_eml_draft",
        "sent":         False,
        "to":           to_list,
        "cc":           cc_list,
        "bcc_count":    len(bcc_list),  # don't reveal BCC addresses even in audit
        "subject":      subject,
        "attachments":  [p.name for p in attachment_paths],
        "summary":      (
            f"Created email draft to {len(to_list)} recipient(s). "
            f"To send: open {draft_path.name} in a mail client, or "
            f"re-invoke with action='send' once allowlist is configured."
        ),
    }


# ============================================================
# send action
# ============================================================

def _produce_send(
    msg:               EmailMessage,
    to_list:           List[str],
    cc_list:           List[str],
    bcc_list:          List[str],
    subject:           str,
    attachment_paths:  List[Path],
    timeout_seconds:   int,
) -> Dict[str, Any]:
    """Transmit via SMTP. Lazy-imports smtplib so the network capability
    only loads when actually needed (draft path stays network-free)."""
    artifact_id = str(uuid.uuid4())
    smtp_response = _send_smtp(
        msg=msg,
        recipients=to_list + cc_list + bcc_list,
        timeout_seconds=timeout_seconds,
    )

    return {
        "artifact_id":   artifact_id,
        "format":        "smtp_send",
        "provider":      "smtp",
        "sent":          True,
        "to":            to_list,
        "cc":            cc_list,
        "bcc_count":     len(bcc_list),
        "subject":       subject,
        "attachments":   [p.name for p in attachment_paths],
        "message_id":    msg["Message-ID"],
        "smtp_response": smtp_response,
        "sent_at":       datetime.now().isoformat(timespec="seconds"),
        "summary":       f"Sent email to {len(to_list)} recipient(s) ({len(cc_list)} cc, {len(bcc_list)} bcc).",
    }


def _send_smtp(
    msg:              EmailMessage,
    recipients:       Iterable[str],
    timeout_seconds:  int,
) -> Dict[str, Any]:
    """
    Lazy-import smtplib and perform the send. Catches specific SMTP
    exception classes and re-raises with structured error_class values
    so the audit log distinguishes failure modes.
    """
    # Lazy import: keeps network capability out of the module-load surface
    import smtplib

    host = os.environ.get("ADAM_SMTP_HOST", "").strip()
    if not host:
        raise _make_error(
            "smtp_not_configured",
            "ADAM_SMTP_HOST is not set. The send action requires SMTP "
            "configuration via environment variables. Use 'draft' "
            "instead, or configure ADAM_SMTP_HOST, ADAM_SMTP_PORT, "
            "ADAM_SMTP_USERNAME, and ADAM_SMTP_PASSWORD."
        )
    try:
        port = int(os.environ.get("ADAM_SMTP_PORT", "587"))
    except ValueError:
        raise _make_error(
            "smtp_not_configured",
            f"ADAM_SMTP_PORT must be an integer, got "
            f"{os.environ.get('ADAM_SMTP_PORT')!r}",
        )

    username = os.environ.get("ADAM_SMTP_USERNAME", "").strip()
    password = os.environ.get("ADAM_SMTP_PASSWORD", "")
    use_tls = os.environ.get("ADAM_SMTP_USE_TLS", "true").strip().lower() in {"1", "true", "yes", "on"}
    use_ssl = os.environ.get("ADAM_SMTP_USE_SSL", "false").strip().lower() in {"1", "true", "yes", "on"}

    recipients_list = list(recipients)
    if not recipients_list:
        raise ValueError("no recipients provided to SMTP")

    smtp_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP

    try:
        with smtp_cls(host, port, timeout=timeout_seconds) as server:
            # Capture the greeting for audit. server.ehlo() returns a tuple
            # (code, message) we can record.
            try:
                ehlo_resp = server.ehlo()
            except Exception:
                ehlo_resp = (None, b"ehlo failed")

            if use_tls and not use_ssl:
                try:
                    server.starttls()
                    server.ehlo()
                except smtplib.SMTPException as e:
                    raise _make_error(
                        "smtp_tls_failed",
                        f"STARTTLS failed: {type(e).__name__}: {e}"
                    )

            if username:
                try:
                    server.login(username, password)
                except smtplib.SMTPAuthenticationError as e:
                    raise _make_error(
                        "smtp_auth_failed",
                        f"SMTP authentication failed for user {username!r}: "
                        f"server returned code {e.smtp_code}. "
                        f"Verify ADAM_SMTP_USERNAME and ADAM_SMTP_PASSWORD."
                    )
                except smtplib.SMTPException as e:
                    raise _make_error(
                        "smtp_auth_failed",
                        f"SMTP login failed: {type(e).__name__}: {e}"
                    )

            try:
                # send_message returns a dict of {addr: (code, msg)} for
                # any failed-per-recipient deliveries. An empty dict means
                # all recipients accepted.
                refused = server.send_message(msg, to_addrs=recipients_list)
            except smtplib.SMTPRecipientsRefused as e:
                raise _make_error(
                    "smtp_all_recipients_refused",
                    f"server refused all recipients: {e.recipients}"
                )
            except smtplib.SMTPSenderRefused as e:
                raise _make_error(
                    "smtp_sender_refused",
                    f"server refused sender {e.sender!r}: code {e.smtp_code}"
                )
            except smtplib.SMTPDataError as e:
                raise _make_error(
                    "smtp_delivery_failed",
                    f"SMTP DATA error: code {e.smtp_code}, "
                    f"response {e.smtp_error!r}"
                )
            except smtplib.SMTPException as e:
                raise _make_error(
                    "smtp_delivery_failed",
                    f"SMTP error: {type(e).__name__}: {e}"
                )

            # Compose audit-friendly summary
            ehlo_code = ehlo_resp[0] if ehlo_resp else None
            ehlo_msg  = ehlo_resp[1] if ehlo_resp else None
            if isinstance(ehlo_msg, bytes):
                try:
                    ehlo_msg = ehlo_msg.decode("utf-8", errors="replace")[:200]
                except Exception:
                    ehlo_msg = "(undecodable)"
            return {
                "host":         host,
                "port":         port,
                "tls":          use_tls and not use_ssl,
                "ssl":          use_ssl,
                "authenticated": bool(username),
                "ehlo_code":    ehlo_code,
                "ehlo_message": ehlo_msg,
                "partial_failures": {
                    addr: {"code": c, "message": _decode_smtp_msg(m)}
                    for addr, (c, m) in (refused or {}).items()
                },
                "all_accepted": not bool(refused),
            }

    except (ConnectionError, TimeoutError, OSError) as e:
        raise _make_error(
            "smtp_connection_failed",
            f"could not connect to SMTP server {host}:{port}: "
            f"{type(e).__name__}: {e}"
        )


def _decode_smtp_msg(m: Any) -> str:
    """Decode an SMTP response message for audit display."""
    if isinstance(m, bytes):
        try:
            return m.decode("utf-8", errors="replace")[:200]
        except Exception:
            return "(undecodable)"
    return str(m)[:200]


# ============================================================
# Validation helpers
# ============================================================

def _make_error(error_class: str, message: str) -> ValueError:
    """
    Wrap an error message into a ValueError with an attached error_class
    attribute. SkillRuntime's handler_exception path picks up the message;
    the error_class is preserved on the exception so the audit log can
    record it precisely.

    We use a subclass-less attribute pattern so we don't need to define
    a new exception hierarchy. The runtime treats anything raised here
    as 'handler_exception' for the SkillResult's error_class, but the
    embedded ADAM-specific class name is preserved in the message and
    is parseable by audit tools if needed.
    """
    err = ValueError(f"[{error_class}] {message}")
    err.adam_error_class = error_class  # type: ignore[attr-defined]
    return err


def _require_text(args: Dict[str, Any], key: str, limit: int) -> str:
    value = args.get(key)
    if not isinstance(value, str):
        raise ValueError(f"required arg {key!r} must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"required arg {key!r} is empty")
    if len(stripped) > limit:
        raise _make_error(
            f"{key}_too_long",
            f"arg {key!r} exceeds maximum length {limit} (got {len(stripped)})"
        )
    return stripped


def _optional_text(args: Dict[str, Any], key: str, limit: int) -> Optional[str]:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"optional arg {key!r} must be a string when provided")
    stripped = value.strip()
    if not stripped:
        return None
    if len(stripped) > limit:
        raise _make_error(
            f"{key}_too_long",
            f"arg {key!r} exceeds maximum length {limit} (got {len(stripped)})"
        )
    return stripped


def _reject_header_injection(field_name: str, value: str) -> None:
    """
    Refuse CRLF / null in header-bound fields. This is the standard
    email-header-injection defense; an attacker who controls a 'subject'
    or 'from_name' input could otherwise inject additional headers,
    create extra recipients, or smuggle commands past downstream filters.
    """
    if HEADER_INJECTION_RE.search(value):
        raise _make_error(
            "header_injection_attempt",
            f"field {field_name!r} contains CRLF or null characters, "
            f"which is a header-injection pattern. The value has been "
            f"rejected. If you need newlines in {field_name}, you may "
            f"need to redesign the input -- header fields cannot "
            f"contain line breaks."
        )


def _parse_recipients(value: str, field_name: str) -> List[str]:
    """
    Parse a comma-separated recipient string.

    v1 scope reduction: display-name addresses ('First Last <user@x.com>')
    are NOT accepted. Only bare email addresses, one per comma-separated
    entry. This avoids a parsing ambiguity (commas inside quoted names
    versus commas separating addresses) and shrinks the header-injection
    surface area. SKILL.md documents this limitation.
    """
    # Reject embedded CRLF in the raw value before parsing so we don't
    # split on injected newlines and silently split-and-accept.
    _reject_header_injection(field_name, value)

    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        raise ValueError(f"{field_name} must include at least one email address")
    invalid: List[str] = []
    for p in parts:
        if "<" in p or ">" in p:
            invalid.append(p)
            continue
        if not EMAIL_RE.match(p):
            invalid.append(p)
    if invalid:
        raise _make_error(
            "invalid_email_address",
            f"{field_name} contains invalid email address(es): "
            f"{invalid}. v1 of the email skill accepts only bare "
            f"addresses (user@example.com), not display-name forms "
            f"('First Last <user@example.com>'). Re-emit without "
            f"angle brackets and with valid syntax."
        )
    return parts


def _matches_allowlist(recipient: str, allowlist: List[str]) -> bool:
    """
    Check if a recipient matches the allowlist. The allowlist accepts:
      - exact addresses: 'user@example.com'
      - domain wildcards: '*@example.com'  (any user at example.com)
    """
    recipient_lower = recipient.lower()
    for entry in allowlist:
        entry_lower = entry.lower().strip()
        if not entry_lower:
            continue
        if entry_lower.startswith("*@"):
            domain = entry_lower[2:]
            if recipient_lower.endswith("@" + domain):
                return True
        else:
            if recipient_lower == entry_lower:
                return True
    return False


def _resolve_recipient_allowlist() -> Optional[List[str]]:
    """
    Read ADAM_EMAIL_RECIPIENT_ALLOWLIST from env, parse into a list.
    Returns None if not set (which fails the send action with a clear
    error -- 'unconfigured' is intentionally different from 'empty list').
    Returns the parsed list if set, even if it ends up empty after
    parsing (which would block all sends -- safest default).
    """
    raw = os.environ.get("ADAM_EMAIL_RECIPIENT_ALLOWLIST", "").strip()
    if not raw:
        return None
    return [entry.strip() for entry in raw.split(",") if entry.strip()]


def _resolve_from_address() -> str:
    """Read ADAM_EMAIL_FROM, validate, return."""
    email_from = os.environ.get("ADAM_EMAIL_FROM", "").strip()
    if not email_from:
        raise _make_error(
            "from_address_not_configured",
            "ADAM_EMAIL_FROM is not set. The email skill requires the "
            "sender address to be configured as an environment variable, "
            "not passed as an arg, so it cannot be manipulated by "
            "Operator at runtime."
        )
    if not EMAIL_RE.match(email_from):
        raise _make_error(
            "from_address_invalid",
            f"ADAM_EMAIL_FROM is not a valid email address: {email_from!r}"
        )
    # CRLF defense at the env layer too -- belt and suspenders
    _reject_header_injection("ADAM_EMAIL_FROM", email_from)
    return email_from


def _build_from_header(from_address: str, from_name: Optional[str]) -> str:
    if from_name:
        # from_name has already been CRLF-checked in handle()
        return f"{from_name} <{from_address}>"
    return from_address


def _resolve_limits() -> Dict[str, int]:
    """Resolve all numeric limits from env, falling back to defaults."""
    out = dict(DEFAULTS)
    for key in list(DEFAULTS.keys()):
        env_var = "ADAM_EMAIL_" + key.upper()
        if env_var in os.environ:
            try:
                out[key] = int(os.environ[env_var])
            except ValueError:
                # Bad env value -> use default, log via the runtime later
                pass
    return out


# ============================================================
# Attachment handling
# ============================================================

def _collect_attachment_paths(
    args:           Dict[str, Any],
    artifacts_root: Path,
    limits:         Dict[str, int],
) -> List[Path]:
    """
    Collect, validate, dedupe, and size-check attachment paths. All paths
    must resolve (symlinks followed) to a location inside artifacts_root.
    Returns the canonical Path objects.
    """
    raw_paths: List[str] = []

    single = args.get("attachment_path")
    if single is not None:
        if not isinstance(single, str):
            raise ValueError("attachment_path must be a string when provided")
        if single.strip():
            raw_paths.append(single.strip())

    attachments = args.get("attachments")
    if attachments is not None:
        if isinstance(attachments, str):
            for entry in attachments.split(","):
                entry = entry.strip()
                if entry:
                    raw_paths.append(entry)
        elif isinstance(attachments, list):
            for entry in attachments:
                if not isinstance(entry, str):
                    raise ValueError("attachments list entries must be strings")
                entry = entry.strip()
                if entry:
                    raw_paths.append(entry)
        else:
            raise ValueError(
                "attachments must be a list of strings or a comma-separated string"
            )

    if len(raw_paths) > limits["max_attachment_count"]:
        raise _make_error(
            "too_many_attachments",
            f"attachment count {len(raw_paths)} exceeds limit "
            f"{limits['max_attachment_count']}"
        )

    seen: set = set()
    resolved: List[Path] = []
    total_bytes = 0
    for raw in raw_paths:
        path = _safe_attachment_path(raw, artifacts_root, limits)
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        size = path.stat().st_size
        total_bytes += size
        if total_bytes > limits["max_total_attachment_bytes"]:
            raise _make_error(
                "total_attachments_too_large",
                f"total attachment size exceeds limit "
                f"{limits['max_total_attachment_bytes']} bytes "
                f"after adding {path.name}"
            )
        resolved.append(path)
    return resolved


def _safe_attachment_path(
    path_value:     str,
    artifacts_root: Path,
    limits:         Dict[str, int],
) -> Path:
    """
    Resolve a path argument to a canonical Path inside artifacts_root.

    Operators emit attachment paths in three legitimate forms:
      1. Bare filename:        "plan.docx"
      2. Project-relative path: "logs/adam_<stamp>.artifacts/plan.docx"
         (this is how the document skill reports the path in its
         SkillResult, so Operator naturally quotes it verbatim)
      3. Absolute path:        "/full/path/to/logs/adam_X/.../plan.docx"

    Defense pattern: we try each interpretation in order, accept the
    first one that resolves to an existing file inside the canonical
    artifacts_root. If none point inside artifacts_root, the path
    is rejected as outside-artifacts. resolve() follows symlinks
    so symlink-escape attempts fail at the same gate.

    Note: 'project-relative' interpretation is anchored to the
    process working directory. ADAM is launched from the project
    root, so 'logs/adam_X.artifacts/file' resolves correctly there.
    If artifacts_root has been remapped (future cloud backends),
    the bare-filename interpretation still works.
    """
    if not isinstance(path_value, str) or not path_value:
        raise ValueError("attachment_path entry must be a non-empty string")

    # Reject null bytes in path - they truncate C string handling
    if "\x00" in path_value:
        raise _make_error(
            "attachment_path_invalid",
            "attachment path contains null byte"
        )

    root = artifacts_root.resolve()
    raw_path = Path(path_value)

    # Early-reject traversal patterns. We want a precise error_class
    # in the audit log: '../../etc/passwd' is a traversal attempt, not
    # an honest 'file not found'. Even though our bare-filename fallback
    # (stripping the leading path components) would land inside the
    # artifacts dir and the existence check would then return
    # 'attachment_not_found', the input PATTERN tells us this was
    # adversarial input and we should label it as such.
    #
    # The rule: if any segment of the relative path is '..', reject.
    # Absolute paths get checked below via the in-root check.
    if not raw_path.is_absolute():
        if any(part == ".." for part in raw_path.parts):
            raise _make_error(
                "attachment_path_outside_artifacts",
                f"attachment path {path_value!r} contains parent-directory "
                f"references ('..'), which are rejected to prevent path "
                f"traversal attacks. Use the filename as reported by the "
                f"document skill, or a clean relative path from the "
                f"artifacts directory."
            )

    # Generate candidate interpretations in priority order.
    # We pick the FIRST one whose resolved form lives inside `root`
    # AND exists on disk. This handles bare filenames, project-relative
    # paths (which is what document.create's SkillResult returns), and
    # absolute paths uniformly.
    candidates: List[Path] = []
    if raw_path.is_absolute():
        # Absolute path: try as-given
        candidates.append(raw_path)
    else:
        # Relative path: try three interpretations
        # (a) Joined to artifacts_root -- bare filenames ('plan.docx')
        candidates.append(artifacts_root / raw_path)
        # (b) Joined to CWD -- project-relative paths ('logs/adam_X.artifacts/plan.docx')
        candidates.append(Path.cwd() / raw_path)
        # (c) Bare filename of raw_path, joined to artifacts_root.
        #     This catches the case where Operator emits a relative path
        #     containing the artifacts directory's suffix but joined to
        #     artifacts_root would double the suffix (the bug fixed here).
        if raw_path.name and raw_path.name != path_value:
            candidates.append(artifacts_root / raw_path.name)

    # Evaluate candidates: resolve, check inside root, check exists.
    # We collect (resolved, in_root, exists) for diagnostic purposes.
    tried: List[Tuple[Path, bool, bool]] = []
    chosen: Optional[Path] = None
    for cand in candidates:
        try:
            resolved = cand.resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        in_root = (resolved == root) or resolved.is_relative_to(root)
        exists  = resolved.exists()
        tried.append((resolved, in_root, exists))
        if in_root and exists:
            chosen = resolved
            break

    if chosen is None:
        # Distinguish "outside artifacts" from "not found" for clear audit
        any_in_root = any(t[1] for t in tried)
        if not any_in_root:
            # None of our candidate interpretations landed inside the
            # artifacts directory. This is the path-outside-artifacts
            # case (potentially adversarial).
            raise _make_error(
                "attachment_path_outside_artifacts",
                f"attachment path does not resolve inside the run's "
                f"artifacts directory. This is rejected to prevent "
                f"emailing arbitrary files from the host. Path was "
                f"{path_value!r}; tried "
                f"{[str(r) for r, _, _ in tried]}; "
                f"expected within {root}."
            )
        # In-root but not existing -- legitimate filename, file just not there
        raise _make_error(
            "attachment_not_found",
            f"attachment {path_value!r} does not exist inside the "
            f"artifacts directory. The file must be created (e.g., "
            f"via the document skill) before it can be attached. "
            f"Tried: {[str(r) for r, in_root, _ in tried if in_root]}"
        )

    path = chosen

    if not path.is_file():
        raise _make_error(
            "attachment_not_a_file",
            f"attachment path is not a regular file: {path}"
        )

    size = path.stat().st_size
    max_one = limits["max_attachment_bytes"]
    if size > max_one:
        raise _make_error(
            "attachment_too_large",
            f"attachment {path.name} is {size} bytes; per-attachment "
            f"limit is {max_one}. Tune via ADAM_EMAIL_MAX_ATTACHMENT_BYTES "
            f"if your deployment legitimately needs larger attachments."
        )
    return path


def _attach_file(msg: EmailMessage, path: Path) -> None:
    """
    Add a file as an attachment. Streams the bytes for memory efficiency
    on larger files.
    """
    ctype, encoding = mimetypes.guess_type(str(path))
    if ctype is None or encoding is not None:
        ctype = "application/octet-stream"
    maintype, subtype = ctype.split("/", 1)
    with path.open("rb") as f:
        data = f.read()
    msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)


# ============================================================
# Hashing helper
# ============================================================

def _sha256_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()
