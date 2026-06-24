"""
Standalone SMTP sender for the ADAM register app.

Why this exists separately from skills/email/handler.py
=======================================================
The email *skill* deliberately enforces ADAM_EMAIL_RECIPIENT_ALLOWLIST
on every send. That guardrail exists to constrain the autonomous agent
-- so a misjudging Operator can't email arbitrary strangers. The
register app is the opposite situation: it is admin tooling that you
operate, and it MUST be able to reach two recipients who are by
definition not on any allowlist:

  1. the director (to notify of a new request), and
  2. a brand-new applicant (to tell them their account is active).

So this mailer reuses the same SMTP credentials from .env but does NOT
apply the skill's allowlist. It is intentionally small: connect,
optional STARTTLS, optional login, send, done. It reads exactly the
same ADAM_SMTP_* / ADAM_EMAIL_FROM variables the skill reads, so there
is one place to configure SMTP for the whole system.

It still keeps the defenses that matter for correctness and safety:
  - FROM comes from env only, never from caller input.
  - Every header value is checked for CRLF/null injection.
  - Recipients are syntax-validated.
  - The SMTP password is read from env and never logged or returned.
"""
from __future__ import annotations

import os
import re
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Dict, Optional

# Same conservative address syntax the skill uses. Catches obvious
# mistakes and injection patterns at our layer; the SMTP server does
# the authoritative validation.
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
HEADER_INJECTION_RE = re.compile(r"[\r\n\x00]")

DEFAULT_TIMEOUT_SECONDS = 30


# ============================================================
# .env loading -- byte-for-byte the same parser the GUI uses
# ============================================================

def load_dotenv(path: Path) -> Dict[str, str]:
    """
    Read .env if present and return a dict of values. Does not mutate
    os.environ. This matches gui/backend/server.py's load_dotenv
    exactly, including its tolerance of spaces around '=' (the ADAM
    .env writes 'ADAM_EMAIL_FROM =value'), because partition('=')
    splits on the first '=' and both halves are stripped.
    """
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class Mailer:
    """
    Holds resolved SMTP settings and sends messages. Construct once at
    app startup from the .env next to adam, then call send() per email.

    Settings precedence matches the GUI: a value present in .env wins,
    then os.environ, then the built-in default. This lets a deployment
    override via real environment variables without editing .env.
    """

    def __init__(self, env_path: Path):
        env = load_dotenv(env_path)

        def get(key: str, default: str = "") -> str:
            return env.get(key, os.environ.get(key, default)).strip()

        self.from_address = get("ADAM_EMAIL_FROM")
        self.host = get("ADAM_SMTP_HOST")
        self.port = self._int(get("ADAM_SMTP_PORT", "587"), 587)
        self.username = get("ADAM_SMTP_USERNAME")
        self.password = env.get("ADAM_SMTP_PASSWORD",
                                os.environ.get("ADAM_SMTP_PASSWORD", ""))
        self.use_tls = self._flag(get("ADAM_SMTP_USE_TLS", "true"))
        self.use_ssl = self._flag(get("ADAM_SMTP_USE_SSL", "false"))
        self.timeout = DEFAULT_TIMEOUT_SECONDS

    @staticmethod
    def _int(value: str, fallback: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _flag(value: str) -> bool:
        return value.strip().lower() in {"1", "true", "yes", "on"}

    def is_configured(self) -> bool:
        """True if we have at least a from-address and a host to send through."""
        return bool(self.from_address and self.host)

    def config_problem(self) -> Optional[str]:
        """Human-readable reason we can't send, or None if we can."""
        if not self.from_address:
            return "ADAM_EMAIL_FROM is not set in .env"
        if not EMAIL_RE.match(self.from_address):
            return f"ADAM_EMAIL_FROM is not a valid address: {self.from_address!r}"
        if not self.host:
            return "ADAM_SMTP_HOST is not set in .env"
        return None

    def send(self, *, to: str, subject: str, body: str,
             from_name: Optional[str] = None,
             attachments: Optional[list] = None) -> Dict[str, object]:
        """
        Send a plain-text email, optionally with file attachments. Raises
        ValueError on bad input or configuration, RuntimeError on SMTP
        failure. Returns a small audit dict on success (never includes
        the password).

        attachments: optional list of (filename, bytes, maintype, subtype)
        tuples. Each is attached to the message. Example:
            ("ADAM_Pilot_Guide.pdf", data, "application", "pdf")
        """
        problem = self.config_problem()
        if problem:
            raise ValueError(f"mailer not configured: {problem}")

        to = (to or "").strip()
        if not EMAIL_RE.match(to):
            raise ValueError(f"recipient is not a valid email address: {to!r}")
        if HEADER_INJECTION_RE.search(subject):
            raise ValueError("subject contains CRLF/null (header injection)")
        if from_name and HEADER_INJECTION_RE.search(from_name):
            raise ValueError("from_name contains CRLF/null (header injection)")

        msg = EmailMessage()
        msg["From"] = f"{from_name} <{self.from_address}>" if from_name else self.from_address
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()
        msg.set_content(body)

        for att in (attachments or []):
            try:
                filename, data, maintype, subtype = att
            except (ValueError, TypeError) as e:
                raise ValueError(
                    "each attachment must be a (filename, bytes, maintype, "
                    "subtype) tuple"
                ) from e
            if HEADER_INJECTION_RE.search(filename):
                raise ValueError("attachment filename contains CRLF/null")
            msg.add_attachment(data, maintype=maintype, subtype=subtype,
                               filename=filename)

        smtp_cls = smtplib.SMTP_SSL if self.use_ssl else smtplib.SMTP
        try:
            with smtp_cls(self.host, self.port, timeout=self.timeout) as server:
                server.ehlo()
                if self.use_tls and not self.use_ssl:
                    server.starttls()
                    server.ehlo()
                if self.username:
                    server.login(self.username, self.password)
                refused = server.send_message(msg, to_addrs=[to])
        except smtplib.SMTPAuthenticationError as e:
            raise RuntimeError(
                f"SMTP authentication failed (code {e.smtp_code}). "
                f"Check ADAM_SMTP_USERNAME / ADAM_SMTP_PASSWORD."
            ) from e
        except smtplib.SMTPException as e:
            raise RuntimeError(f"SMTP error: {type(e).__name__}: {e}") from e
        except (ConnectionError, TimeoutError, OSError) as e:
            raise RuntimeError(
                f"could not connect to SMTP {self.host}:{self.port}: "
                f"{type(e).__name__}: {e}"
            ) from e

        return {
            "to": to,
            "subject": subject,
            "message_id": msg["Message-ID"],
            "all_accepted": not bool(refused),
        }
