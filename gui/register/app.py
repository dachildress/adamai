"""
ADAM self-service registration app (register.adamai.us).

Purpose
=======
Lets prospective pilots request an account without you SSHing in to
run manage_users.py by hand. The flow:

  1. Applicant fills the form here and chooses their OWN password
     (typed over HTTPS -- you never see it, it's never emailed).
  2. A *suspended* user record is created in gui/users.json via
     backend.auth.add_user. Suspended accounts cannot log in to the
     main GUI, so nothing is live yet.
  3. The director (ADAM_DEFAULT_DIRECTOR_EMAIL in .env) gets an email
     with an Approve link and a Deny link.
  4. Approve -> the account flips to active and the applicant is
     emailed their login URL. Deny -> the suspended record is deleted
     and nothing is emailed (silent).

This app runs on its own port (default 8800) and is meant to sit
behind nginx as register.adamai.us. It shares gui/users.json with the
main GUI through the same backend.auth module and file locks, so the
two never corrupt each other.

Design notes
============
- It imports backend.auth -- one user store, one schema, no drift.
- It imports register.mailer -- one SMTP config, allowlist bypassed
  because this is admin tooling that must reach new people.
- Approve/Deny authority = possession of a single-use, 7-day token
  (register.pending). No director login needed; the token is the
  credential, and it's consumed atomically so a link works once.
- Abuse defenses: a hidden honeypot field and an in-memory per-IP
  rate limit. Bots that blindly POST get silently dropped; nothing is
  created. Every real request still waits behind your Approve click.
- Pages are self-contained HTML strings -- no Vite build, no static
  bundle to deploy. Trivial to run and proxy.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import sys
import time
import urllib.parse
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Deque, Dict, Optional, Tuple

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse

# Make 'backend.auth' and 'register.*' importable when launched from
# the gui/ directory (same convention manage_users.py uses).
HERE = Path(__file__).resolve().parent          # .../gui/register
GUI_ROOT = HERE.parent                           # .../gui
sys.path.insert(0, str(GUI_ROOT))

from backend import auth            # noqa: E402  (shared user store)
from register import pending        # noqa: E402  (token store)
from register.mailer import Mailer  # noqa: E402  (allowlist-bypassing sender)


# ============================================================
# Configuration (resolved in build_app / main)
# ============================================================

# Defaults for new registrants. Matches what we agreed: pilots get the
# pilot role (which denies the email skill) and modest quotas.
NEW_USER_ROLE = "pilot"
NEW_USER_SESSIONS = 3
NEW_USER_MAX_TURNS = 10

# Rate limit: at most REGISTER_RATE_MAX submissions per IP within
# REGISTER_RATE_WINDOW seconds. Keeps a bot (or a stuck refresh) from
# flooding the director's inbox and littering users.json with
# suspended junk. In-memory only -- resets on restart, which is fine
# for a small beta.
REGISTER_RATE_MAX = 5
REGISTER_RATE_WINDOW = 3600  # 1 hour


# ============================================================
# Validation (reuses the same rules as manage_users.py / auth)
# ============================================================

_USERNAME_RE = re.compile(r"[a-z0-9_.-]+")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _validate_username(value: str) -> Optional[str]:
    if not value:
        return "Username is required."
    if not _USERNAME_RE.fullmatch(value):
        return "Username may only contain lowercase letters, digits, underscore, dot, or hyphen."
    if len(value) > 64:
        return "Username must be 64 characters or fewer."
    return None


def _validate_email(value: str) -> Optional[str]:
    if not value:
        return "Email is required."
    if not _EMAIL_RE.match(value):
        return "That doesn't look like a valid email address."
    return None


def _validate_password(pw: str, pw2: str) -> Optional[str]:
    if not pw:
        return "Password is required."
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if pw != pw2:
        return "The two passwords do not match."
    return None


# ============================================================
# In-memory per-IP rate limiter
# ============================================================

class RateLimiter:
    """Sliding-window per-IP counter. Thread-safe, memory-only."""

    def __init__(self, max_events: int, window_seconds: int):
        self.max = max_events
        self.window = window_seconds
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            dq = self._hits.setdefault(key, deque())
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self.max:
                return False
            dq.append(now)
            # Opportunistic cleanup so the dict doesn't grow unbounded.
            if len(self._hits) > 4096:
                for k in [k for k, v in self._hits.items() if not v]:
                    self._hits.pop(k, None)
            return True


def _client_ip(request: Request) -> str:
    """
    Best-effort client IP. Behind nginx, X-Forwarded-For carries the
    real client; we take the first hop. Falls back to the socket peer.
    Used only for rate-limit bucketing, not for any security decision.
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ============================================================
# HTML pages (self-contained, no external assets)
# ============================================================

_PAGE_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  margin: 0; min-height: 100vh; display: flex; align-items: center;
  justify-content: center; background: #0f1419; color: #e6e6e6; padding: 24px;
}
.card {
  width: 100%; max-width: 460px; background: #1a212b; border: 1px solid #2a3340;
  border-radius: 14px; padding: 32px; box-shadow: 0 12px 40px rgba(0,0,0,.4);
}
h1 { font-size: 1.4rem; margin: 0 0 4px; }
.sub { color: #9aa7b4; font-size: .9rem; margin: 0 0 24px; }
label { display: block; font-size: .85rem; color: #c4cdd6; margin: 14px 0 6px; }
input {
  width: 100%; padding: 11px 12px; border-radius: 8px; border: 1px solid #33404e;
  background: #11161d; color: #e6e6e6; font-size: .95rem;
}
input:focus { outline: none; border-color: #4f8cff; }
button {
  width: 100%; margin-top: 22px; padding: 12px; border: none; border-radius: 8px;
  background: #4f8cff; color: #fff; font-size: 1rem; font-weight: 600; cursor: pointer;
}
button:hover { background: #3d78e8; }
.err { background: #2a1518; border: 1px solid #5a2630; color: #ffb3bd;
  padding: 10px 12px; border-radius: 8px; font-size: .88rem; margin-bottom: 18px; }
.ok { color: #8fe3a8; }
.note { color: #9aa7b4; font-size: .82rem; margin-top: 18px; line-height: 1.5; }
.hp { position: absolute; left: -9999px; top: -9999px; width: 1px; height: 1px;
  opacity: 0; }
.center { text-align: center; }
.big { font-size: 2.2rem; margin: 0 0 10px; }
"""


def _shell(title: str, inner: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{html.escape(title)}</title>
<style>{_PAGE_CSS}</style>
</head><body><div class="card">{inner}</div></body></html>"""


def _form_page(*, error: str = "", values: Optional[Dict[str, str]] = None) -> str:
    v = values or {}

    def val(key: str) -> str:
        return html.escape(v.get(key, ""))

    err_block = f'<div class="err">{html.escape(error)}</div>' if error else ""
    inner = f"""
<h1>Request an ADAM account</h1>
<p class="sub">Fill this out and you'll get access once it's approved.</p>
{err_block}
<form method="post" action="/api/register" autocomplete="off">
  <label for="display_name">Your name</label>
  <input id="display_name" name="display_name" value="{val('display_name')}" required>

  <label for="username">Choose a username</label>
  <input id="username" name="username" value="{val('username')}"
         placeholder="lowercase letters, digits, . _ -" required>

  <label for="email">Email</label>
  <input id="email" name="email" type="email" value="{val('email')}" required>

  <label for="password">Choose a password</label>
  <input id="password" name="password" type="password"
         placeholder="at least 8 characters" required>

  <label for="password2">Confirm password</label>
  <input id="password2" name="password2" type="password" required>

  <!-- Honeypot: hidden from humans, irresistible to dumb bots. If this
       arrives filled in, we silently drop the request. -->
  <div class="hp" aria-hidden="true">
    <label for="website">Leave this empty</label>
    <input id="website" name="website" tabindex="-1" autocomplete="off">
  </div>

  <button type="submit">Request account</button>
</form>
<p class="note">You set your own password here over a secure connection &mdash;
it is never shown to or stored in plain text by anyone.</p>
"""
    return _shell("Request an ADAM account", inner)


def _message_page(title: str, heading: str, body_html: str, *, emoji: str = "") -> str:
    big = f'<div class="big">{emoji}</div>' if emoji else ""
    inner = f"""
<div class="center">
  {big}
  <h1>{html.escape(heading)}</h1>
  <p class="note">{body_html}</p>
</div>
"""
    return _shell(title, inner)


# ============================================================
# Email composition
# ============================================================

def _director_email(*, applicant_name: str, username: str, applicant_email: str,
                    approve_url: str, deny_url: str) -> Tuple[str, str]:
    subject = f"ADAM: new account request from {applicant_name} ({username})"
    body = (
        f"A new ADAM account has been requested.\n\n"
        f"  Name:     {applicant_name}\n"
        f"  Username: {username}\n"
        f"  Email:    {applicant_email}\n\n"
        f"The account has been created but is suspended and cannot log in "
        f"until you approve it.\n\n"
        f"Approve (activate the account and email the user):\n"
        f"  {approve_url}\n\n"
        f"Deny (delete the request; the user is not notified):\n"
        f"  {deny_url}\n\n"
        f"These links work once and expire in 7 days.\n"
    )
    return subject, body


def _applicant_email(*, applicant_name: str, login_url: str,
                     guide_attached: bool = True) -> Tuple[str, str]:
    subject = "Your ADAM account is active"
    guide_line = (
        "I've attached the ADAM Pilot Guide -- a ten-minute read that walks "
        "you through your first session. Worth a look before you dive in.\n\n"
        if guide_attached else ""
    )
    body = (
        f"Hi {applicant_name},\n\n"
        f"Good news -- your ADAM account has been approved and is now active.\n\n"
        f"You can sign in here:\n"
        f"  {login_url}\n\n"
        f"Use the username and password you chose when you registered.\n\n"
        f"{guide_line}"
        f"Welcome aboard.\n"
    )
    return subject, body


# ============================================================
# App factory
# ============================================================

def build_app(*, adam_root: Path, public_url: str, login_url: str,
              director_email: str, mailer: Mailer,
              pilot_guide_path: Optional[Path] = None) -> FastAPI:
    """
    Construct the register FastAPI app.

      adam_root      -- dir containing .env and gui/ (users.json lives
                        in gui/). auth + pending are initialized here.
      public_url     -- the externally reachable base for THIS app,
                        e.g. https://register.adamai.us. Approve/Deny
                        links are built from it so they point at the
                        public host, not the internal port.
      login_url      -- where approved users sign in (the main GUI),
                        e.g. https://adamai.us.
      director_email -- where new-request notifications go.
      mailer         -- configured Mailer (allowlist-bypassing).
      pilot_guide_path -- optional path to the Pilot Guide file attached
                        to the approval email. Defaults to
                        adam_root/docs/ADAM_Pilot_Guide.pdf. If the file
                        is absent at approval time, the email still sends
                        without the attachment.
    """
    gui_root = adam_root / "gui"
    auth.init_auth(gui_root)
    pending.init_pending(gui_root)

    if pilot_guide_path is None:
        pilot_guide_path = adam_root / "docs" / "ADAM_Pilot_Guide.pdf"

    public_url = public_url.rstrip("/")
    login_url = login_url.rstrip("/")

    limiter = RateLimiter(REGISTER_RATE_MAX, REGISTER_RATE_WINDOW)

    app = FastAPI(
        title="ADAM Registration",
        description="Self-service account requests for ADAM, gated by director approval.",
        version="1.0.0",
        docs_url=None, redoc_url=None, openapi_url=None,  # no API explorer on a public form
    )

    @app.get("/", response_class=HTMLResponse)
    def form() -> HTMLResponse:
        return HTMLResponse(_form_page())

    @app.get("/healthz")
    def healthz() -> Dict[str, object]:
        return {
            "ok": True,
            "mailer_configured": mailer.is_configured(),
            "director_email_set": bool(director_email),
        }

    @app.post("/api/register", response_class=HTMLResponse)
    def register(
        request: Request,
        display_name: str = Form(""),
        username: str = Form(""),
        email: str = Form(""),
        password: str = Form(""),
        password2: str = Form(""),
        website: str = Form(""),   # honeypot
    ) -> HTMLResponse:
        # --- Honeypot: a filled hidden field means a bot. Return the
        # same success page a human would see, but do nothing. We don't
        # reveal that it was rejected. ---
        if website.strip():
            return HTMLResponse(_message_page(
                "Request received", "Request received",
                "Thanks &mdash; if everything checks out you'll hear back by email.",
                emoji="\u2709\ufe0f",
            ))

        # --- Rate limit per IP. Over the limit -> friendly slow-down. ---
        if not limiter.allow(_client_ip(request)):
            return HTMLResponse(_message_page(
                "Too many requests", "Slow down a moment",
                "You've sent several requests recently. Please wait a little "
                "while before trying again.",
            ), status_code=429)

        display_name = display_name.strip()
        username = username.strip().lower()
        email = email.strip()
        values = {"display_name": display_name, "username": username, "email": email}

        # --- Field validation ---
        if not display_name:
            return HTMLResponse(_form_page(error="Your name is required.", values=values))
        for err in (_validate_username(username),
                    _validate_email(email),
                    _validate_password(password, password2)):
            if err:
                return HTMLResponse(_form_page(error=err, values=values))

        # --- Username uniqueness. A suspended (pending) user already
        # occupies the name, so this also blocks duplicate requests. ---
        if auth.get_user(username) is not None:
            return HTMLResponse(_form_page(
                error="That username is already taken. Please choose another.",
                values=values,
            ))

        # --- Create the suspended account. Password is hashed inside
        # add_user; plaintext never persists. ---
        try:
            auth.add_user(
                username=username,
                display_name=display_name,
                email=email,
                role=NEW_USER_ROLE,
                password=password,
                status="suspended",
                sessions_remaining=NEW_USER_SESSIONS,
                max_turns_per_session=NEW_USER_MAX_TURNS,
            )
        except KeyError:
            # Race: someone took the name between the check and now.
            return HTMLResponse(_form_page(
                error="That username was just taken. Please choose another.",
                values=values,
            ))
        except ValueError as e:
            return HTMLResponse(_form_page(error=str(e), values=values))

        # --- Mint the approval token and notify the director. ---
        token = pending.create_token(
            username=username, email=email, display_name=display_name,
        )
        q = urllib.parse.urlencode({"token": token})
        approve_url = f"{public_url}/approve?{q}"
        deny_url = f"{public_url}/deny?{q}"

        subject, body = _director_email(
            applicant_name=display_name, username=username,
            applicant_email=email, approve_url=approve_url, deny_url=deny_url,
        )
        try:
            mailer.send(to=director_email, subject=subject, body=body,
                        from_name="ADAM Registration")
        except (ValueError, RuntimeError) as e:
            # The account exists and is safely suspended; only the
            # notification failed. Tell the applicant it's pending (true)
            # and log the failure for the operator.
            print(f"[register] WARNING: director notification failed: {e}",
                  file=sys.stderr)

        return HTMLResponse(_message_page(
            "Request received", "Request received",
            "Thanks! Your request has been submitted. You'll get an email at "
            f"<b>{html.escape(email)}</b> once it's approved.",
            emoji="\u2709\ufe0f",
        ))

    @app.get("/approve", response_class=HTMLResponse)
    def approve(token: str = "") -> HTMLResponse:
        entry = pending.consume_token(token, outcome="approved")
        if entry is None:
            return HTMLResponse(_message_page(
                "Link not valid", "This link is no longer valid",
                "It may have already been used, expired, or been superseded. "
                "No action was taken.",
            ), status_code=410)

        username = entry["username"]
        user = auth.get_user(username)
        if user is None:
            # The suspended record was removed out-of-band (e.g. denied
            # by another link, or manage_users.py). Nothing to activate.
            return HTMLResponse(_message_page(
                "Nothing to approve", "Nothing to approve",
                "That account no longer exists, so there was nothing to "
                "activate.",
            ), status_code=410)

        def _activate(u: Dict[str, object]) -> None:
            u["status"] = "active"

        auth.update_user(username, _activate)

        # Notify the applicant their account is live, attaching the Pilot
        # Guide if it's available. A missing guide must NOT block the
        # approval email -- the account is already active.
        mail_note = ""

        attachments = []
        guide_attached = False
        try:
            if pilot_guide_path.is_file():
                data = pilot_guide_path.read_bytes()
                fname = pilot_guide_path.name
                ext = pilot_guide_path.suffix.lower()
                if ext == ".pdf":
                    maintype, subtype = "application", "pdf"
                elif ext == ".docx":
                    maintype, subtype = (
                        "application",
                        "vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                else:
                    maintype, subtype = "application", "octet-stream"
                attachments.append((fname, data, maintype, subtype))
                guide_attached = True
            else:
                print(f"[register] WARNING: pilot guide not found at "
                      f"{pilot_guide_path}; sending approval email without it",
                      file=sys.stderr)
        except OSError as e:
            print(f"[register] WARNING: could not read pilot guide "
                  f"{pilot_guide_path}: {e}; sending without attachment",
                  file=sys.stderr)

        subject, body = _applicant_email(
            applicant_name=entry.get("display_name") or username,
            login_url=login_url,
            guide_attached=guide_attached,
        )

        try:
            mailer.send(to=entry["email"], subject=subject, body=body,
                        from_name="ADAM", attachments=attachments)
        except (ValueError, RuntimeError) as e:
            # If the failure may be attachment-related, retry once without it
            # so the applicant still gets their activation notice.
            if attachments:
                print(f"[register] WARNING: activation email with attachment "
                      f"failed ({e}); retrying without attachment",
                      file=sys.stderr)
                guide_attached = False
                subject, body = _applicant_email(
                    applicant_name=entry.get("display_name") or username,
                    login_url=login_url, guide_attached=False,
                )
                try:
                    mailer.send(to=entry["email"], subject=subject, body=body,
                                from_name="ADAM")
                except (ValueError, RuntimeError) as e2:
                    print(f"[register] WARNING: applicant activation email "
                          f"failed: {e2}", file=sys.stderr)
                    mail_note = ("<br><br>(Note: the account is active, but the "
                                 "notification email could not be sent. Let them "
                                 "know directly.)")
            else:
                print(f"[register] WARNING: applicant activation email failed: {e}",
                      file=sys.stderr)
                mail_note = ("<br><br>(Note: the account is active, but the "
                             "notification email could not be sent. Let them know "
                             "directly.)")

        guide_note = ""
        if guide_attached:
            guide_note = " The Pilot Guide was attached to their email."
        elif pilot_guide_path.is_file():
            guide_note = " (The Pilot Guide could not be attached this time.)"

        return HTMLResponse(_message_page(
            "Approved", "Account approved",
            f"<b>{html.escape(username)}</b> is now active and has been "
            f"emailed their sign-in link.{guide_note}{mail_note}",
            emoji="\u2705",
        ))

    @app.get("/deny", response_class=HTMLResponse)
    def deny(token: str = "") -> HTMLResponse:
        entry = pending.consume_token(token, outcome="denied")
        if entry is None:
            return HTMLResponse(_message_page(
                "Link not valid", "This link is no longer valid",
                "It may have already been used, expired, or been superseded. "
                "No action was taken.",
            ), status_code=410)

        username = entry["username"]
        # delete_user only removes a still-suspended account, under the
        # file lock, so a "deny" can never delete an account that was
        # activated in the meantime. Silent: the applicant is NOT
        # emailed on denial.
        auth.delete_user(username, require_status="suspended")

        return HTMLResponse(_message_page(
            "Denied", "Request denied",
            f"The request for <b>{html.escape(username)}</b> has been removed. "
            f"The applicant was not notified.",
            emoji="\U0001f5d1\ufe0f",
        ))

    return app


# ============================================================
# Entry point
# ============================================================

def _resolve_director_email(adam_root: Path) -> str:
    from register.mailer import load_dotenv
    env = load_dotenv(adam_root / ".env")
    return env.get("ADAM_DEFAULT_DIRECTOR_EMAIL",
                   os.environ.get("ADAM_DEFAULT_DIRECTOR_EMAIL", "")).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="ADAM self-service registration app")
    parser.add_argument(
        "--adam-root", type=Path, default=GUI_ROOT.parent,
        help="Project root containing .env and gui/. Defaults to the parent "
             "of the gui/ directory this script lives in.",
    )
    parser.add_argument("--host", type=str, default="127.0.0.1",
                        help="Bind host. Keep 127.0.0.1 behind nginx.")
    parser.add_argument("--port", type=int, default=8800, help="Bind port.")
    parser.add_argument(
        "--public-url", type=str, default="https://register.adamai.us",
        help="Public base URL for THIS app; approve/deny links are built "
             "from it so they point at the public host, not the bind port.",
    )
    parser.add_argument(
        "--login-url", type=str, default="https://adamai.us",
        help="Where approved users sign in (the main ADAM GUI).",
    )
    parser.add_argument(
        "--director-email", type=str, default="",
        help="Override the notification recipient. Defaults to "
             "ADAM_DEFAULT_DIRECTOR_EMAIL from .env.",
    )
    parser.add_argument(
        "--pilot-guide", type=Path, default=None,
        help="Path to the Pilot Guide file attached to approval emails. "
             "Defaults to <adam_root>/docs/ADAM_Pilot_Guide.pdf. If the "
             "file is absent, approval emails still send without it.",
    )
    args = parser.parse_args()

    adam_root = args.adam_root.resolve()
    director_email = args.director_email.strip() or _resolve_director_email(adam_root)
    if not director_email:
        print("ERROR: no director email. Set ADAM_DEFAULT_DIRECTOR_EMAIL in .env "
              "or pass --director-email.", file=sys.stderr)
        return 1

    mailer = Mailer(adam_root / ".env")
    problem = mailer.config_problem()
    if problem:
        print(f"WARNING: mailer not fully configured: {problem}. "
              f"Registration will still create accounts, but emails won't send.",
              file=sys.stderr)

    app = build_app(
        adam_root=adam_root,
        public_url=args.public_url,
        login_url=args.login_url,
        director_email=director_email,
        mailer=mailer,
        pilot_guide_path=(args.pilot_guide.resolve() if args.pilot_guide else None),
    )

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
