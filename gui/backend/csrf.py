"""
CSRF protection for the ADAM GUI backend (Pass 1 web hardening).

Scheme: SIGNED double-submit cookie, bound to the login session.

Why signed-and-bound rather than plain double-submit:
  A plain double-submit token (cookie value == header value, nothing
  more) is defeated by a cookie-planting attacker -- anyone who can set
  a cookie on the victim's browser (e.g. via a sibling subdomain or a
  network MITM on plain HTTP) can plant BOTH the cookie and a matching
  header and sail through the check. Signing closes that hole: the token
  carries an HMAC computed with a server-held secret, so an attacker
  cannot forge a value the server will accept. Binding the signature to
  the user's adam_login session token additionally ensures a token
  minted for one session can't be replayed in another.

Token shape:
    "<nonce>.<sig>"
  where
    nonce = secrets.token_urlsafe(16)            (URL-safe, no '.')
    sig   = HMAC-SHA256(secret, nonce + "." + login_token), hex

Validation (fail-CLOSED -- any uncertainty rejects):
  1. cookie value and header value must both be present,
  2. they must be equal (constant-time),
  3. the signature must verify against the secret + the request's
     adam_login token.

The secret is read from $ADAM_CSRF_SECRET if set, otherwise a random
secret is generated once and persisted to <gui_root>/.csrf_secret so
tokens survive a backend restart (a restart would otherwise 403 every
already-logged-in user until they re-logged-in). The file is created
0600 where the OS supports it.

This module is stdlib-only (hmac/hashlib/secrets) -- it adds NO new
dependency, keeping the Pass 1 security diff out of the requirements
files (those are Pass 2).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Optional

# Cookie that holds the signed CSRF token. MUST be readable by
# JavaScript (httponly=False) so the frontend can echo it back in the
# X-CSRF-Token header -- that round-trip is the "double submit".
CSRF_COOKIE_NAME = "adam_csrf"

# Header the frontend sends on mutating requests.
CSRF_HEADER_NAME = "X-CSRF-Token"

# Environment override for the signing secret (e.g. to share one secret
# across a future multi-process deployment, or to inject from a vault).
CSRF_SECRET_ENV = "ADAM_CSRF_SECRET"

# Filename used to persist a generated secret under gui_root.
_SECRET_FILENAME = ".csrf_secret"

# Module-global secret, set by init_csrf(). Bytes.
_SECRET: Optional[bytes] = None


def init_csrf(gui_root: Path) -> None:
    """
    Resolve the signing secret once at app startup.

    Precedence:
      1. $ADAM_CSRF_SECRET (if non-empty),
      2. an existing <gui_root>/.csrf_secret file,
      3. a freshly generated secret, persisted to that file.

    Mirrors auth.init_auth(gui_root): called from build_app().
    """
    global _SECRET

    env_secret = os.environ.get(CSRF_SECRET_ENV, "").strip()
    if env_secret:
        _SECRET = env_secret.encode("utf-8")
        return

    secret_path = Path(gui_root) / _SECRET_FILENAME
    try:
        if secret_path.exists():
            data = secret_path.read_text(encoding="utf-8").strip()
            if data:
                _SECRET = data.encode("utf-8")
                return
    except OSError:
        pass

    # Generate and persist. 32 bytes hex == 64 chars of entropy.
    generated = secrets.token_hex(32)
    try:
        gui_root_p = Path(gui_root)
        gui_root_p.mkdir(parents=True, exist_ok=True)
        secret_path.write_text(generated, encoding="utf-8")
        try:
            os.chmod(secret_path, 0o600)
        except OSError:
            # Best effort; not all filesystems support chmod.
            pass
    except OSError:
        # If we can't persist, still run with an in-memory secret. The
        # only downside is tokens won't survive a restart.
        pass
    _SECRET = generated.encode("utf-8")


def _secret() -> bytes:
    """Return the signing secret, generating an ephemeral one if init
    was never called (defensive -- build_app always calls init_csrf)."""
    global _SECRET
    if _SECRET is None:
        _SECRET = secrets.token_hex(32).encode("utf-8")
    return _SECRET


def _sign(nonce: str, login_token: str) -> str:
    msg = (nonce + "." + (login_token or "")).encode("utf-8")
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()


def issue_token(login_token: str) -> str:
    """
    Mint a fresh signed CSRF token bound to the given login session
    token. Set this as the adam_csrf cookie value.
    """
    nonce = secrets.token_urlsafe(16)
    return nonce + "." + _sign(nonce, login_token)


def validate_token(
    cookie_value: Optional[str],
    header_value: Optional[str],
    login_token: Optional[str],
) -> bool:
    """
    Validate a mutating request's CSRF token. Returns True only if the
    cookie and header are present, equal, and carry a signature that
    verifies against the secret + this request's login token.

    Fail-CLOSED: any missing piece, malformed token, or exception
    yields False (the caller turns that into a 403).
    """
    try:
        if not cookie_value or not header_value:
            return False
        # Constant-time equality of the double-submitted values.
        if not hmac.compare_digest(cookie_value, header_value):
            return False
        # Verify the signature carried in the cookie value.
        nonce, _, sig = cookie_value.rpartition(".")
        if not nonce or not sig:
            return False
        expected = _sign(nonce, login_token or "")
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False
