"""
Pending-registration token store for the ADAM register app.

When someone registers at register.adamai.us, two things happen:
  1. A *suspended* user record is written into gui/users.json (via
     backend.auth.add_user), so the account exists but cannot log in.
  2. A single-use approval token is written here, into
     gui/pending_registrations.json, and emailed to the director as
     part of the approve / deny links.

This module is the single source of truth for those tokens. It owns
gui/pending_registrations.json and nothing else touches it.

Why a JSON-backed token instead of a stateless signed token
===========================================================
A signed (itsdangerous-style) token would be stateless and need no
storage, but "single use" is then hard to enforce -- once a director
clicks approve, the same link still verifies and could be replayed.
A stored token lets us mark it `used` the moment it's consumed, so a
second click is a clean "already handled" instead of a second state
change. The file stays tiny (one entry per outstanding request) and
is human-inspectable, which matters for a small beta you operate by
hand.

Concurrency discipline
======================
Mirrors backend.auth exactly: every read-modify-write goes through
_atomic_modify(), which flocks the file, reads, mutates, writes to a
temp file in the same directory, fsyncs, and atomically renames. This
is the same pattern auth.py uses for users.json and login_sessions.json,
so the register app and the GUI never corrupt each other's view of
gui/ even if they run at the same time.

Token lifetime
==============
Tokens expire after TOKEN_TTL_SECONDS (default 7 days). Expired and
used tokens are pruned lazily on every read -- no background job. The
file self-cleans during normal use.

Storage location
================
gui/pending_registrations.json -- lives alongside users.json and
login_sessions.json so the whole gui/ state backs up together.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ============================================================
# Constants
# ============================================================

SCHEMA_VERSION = "1.0"

# Approval links are good for 7 days. Long enough that a director who
# only checks email occasionally won't miss the window; short enough
# that a stale request doesn't linger forever as an actionable link.
TOKEN_TTL_SECONDS = 7 * 24 * 60 * 60

# Token entropy. token_urlsafe(32) yields ~43 url-safe characters /
# 256 bits, which is overkill-secure for an approval link and safe to
# drop straight into a query string without escaping.
TOKEN_NBYTES = 32


# ============================================================
# Module state (initialized via init_pending)
# ============================================================

_PENDING_DB_PATH: Optional[Path] = None


def init_pending(gui_root: Path) -> None:
    """
    Configure the store to read/write under the given gui/ dir. Must
    be called before any other function here. Seeds an empty database
    with restrictive permissions if it doesn't exist, so a fresh
    install works without manual setup.
    """
    global _PENDING_DB_PATH
    gui_root = Path(gui_root).resolve()
    gui_root.mkdir(parents=True, exist_ok=True)
    _PENDING_DB_PATH = gui_root / "pending_registrations.json"
    if not _PENDING_DB_PATH.exists():
        _write_atomic(_PENDING_DB_PATH, {
            "schema_version": SCHEMA_VERSION,
            "pending": {},
        })
        os.chmod(_PENDING_DB_PATH, 0o600)


def _ensure_initialized() -> None:
    if _PENDING_DB_PATH is None:
        raise RuntimeError(
            "pending store not initialized -- call init_pending(gui_root) first"
        )


# ============================================================
# Atomic file IO with locking (mirrors backend.auth)
# ============================================================

def _write_atomic(path: Path, data: Dict[str, Any]) -> None:
    """
    Write `data` as JSON to `path` atomically: temp file in the same
    directory, fsync, then rename. Readers see old-or-new, never a
    half-written file. Does NOT lock -- callers needing read-modify-
    write use _atomic_modify().
    """
    parent = path.parent
    fd, tmp_path = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            os.chmod(tmp_path, path.stat().st_mode & 0o777)
        else:
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _atomic_modify(path: Path, mutator: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
    """
    Flock the file, read it, run the mutator, write it back atomically,
    release the lock. The mutator receives the parsed dict and returns
    the dict to persist. Same shape as auth._atomic_modify.
    """
    # Open with O_CREAT so the lock target exists even on first call.
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with os.fdopen(os.dup(fd), "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            raw = ""
        if raw.strip():
            data = json.loads(raw)
        else:
            data = {"schema_version": SCHEMA_VERSION, "pending": {}}
        new_data = mutator(data)
        _write_atomic(path, new_data)
        return new_data
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_locked(path: Path) -> Dict[str, Any]:
    """Shared-lock read. Returns the parsed dict (or an empty seed)."""
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "pending": {}}
    fd = os.open(str(path), os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as f:
            raw = f.read()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    if not raw.strip():
        return {"schema_version": SCHEMA_VERSION, "pending": {}}
    return json.loads(raw)


# ============================================================
# Pruning
# ============================================================

def _now() -> datetime:
    return datetime.now()


def _is_expired(entry: Dict[str, Any]) -> bool:
    exp = entry.get("expires_at")
    if not exp:
        return True
    try:
        return datetime.fromisoformat(exp) < _now()
    except ValueError:
        return True


def _prune_in_place(db: Dict[str, Any]) -> None:
    """Drop used and expired tokens. Mutates db['pending']."""
    pending = db.get("pending", {})
    dead = [
        tok for tok, e in pending.items()
        if e.get("used") or _is_expired(e)
    ]
    for tok in dead:
        del pending[tok]


# ============================================================
# Public API
# ============================================================

def create_token(*, username: str, email: str, display_name: str) -> str:
    """
    Mint a single-use approval token for a newly-registered (suspended)
    user and persist it. Returns the token string to embed in the
    approve / deny links.

    The token records which username it governs so the approve / deny
    handlers know which user record to act on without trusting anything
    from the URL except the opaque token itself.
    """
    _ensure_initialized()
    token = secrets.token_urlsafe(TOKEN_NBYTES)
    created = _now()
    expires = created + timedelta(seconds=TOKEN_TTL_SECONDS)

    def _mod(db: Dict[str, Any]) -> Dict[str, Any]:
        if "pending" not in db:
            db["pending"] = {}
        _prune_in_place(db)
        db["pending"][token] = {
            "username":     username,
            "email":        email,
            "display_name": display_name,
            "created_at":   created.isoformat(timespec="seconds"),
            "expires_at":   expires.isoformat(timespec="seconds"),
            "used":         False,
            "used_at":      None,
            "outcome":      None,   # "approved" | "denied" once consumed
        }
        return db

    _atomic_modify(_PENDING_DB_PATH, _mod)
    return token


def peek_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Return the token entry if it exists, is unused, and is unexpired;
    else None. Read-only -- does NOT consume the token. Used to render
    a confirmation page before acting, if desired.
    """
    _ensure_initialized()
    if not token:
        return None
    db = _read_locked(_PENDING_DB_PATH)
    entry = db.get("pending", {}).get(token)
    if entry is None:
        return None
    if entry.get("used") or _is_expired(entry):
        return None
    return dict(entry)


def consume_token(token: Optional[str], outcome: str) -> Optional[Dict[str, Any]]:
    """
    Atomically validate and consume a token in one locked operation.
    Returns the entry (a snapshot taken BEFORE marking used) on success,
    or None if the token is missing, already used, or expired.

    `outcome` is recorded for audit: "approved" or "denied".

    Doing validate + mark-used under a single lock is what makes the
    token genuinely single-use: two concurrent clicks on the same link
    can't both win, because the second sees `used` already set.
    """
    _ensure_initialized()
    if not token:
        return None
    if outcome not in ("approved", "denied"):
        raise ValueError(f"outcome must be 'approved' or 'denied', got {outcome!r}")

    captured: Dict[str, Optional[Dict[str, Any]]] = {"entry": None}

    def _mod(db: Dict[str, Any]) -> Dict[str, Any]:
        pending = db.setdefault("pending", {})
        entry = pending.get(token)
        if entry is None or entry.get("used") or _is_expired(entry):
            # Leave the snapshot None -> caller treats as invalid.
            _prune_in_place(db)
            return db
        # Snapshot before mutation so the caller gets the original fields.
        captured["entry"] = dict(entry)
        entry["used"] = True
        entry["used_at"] = _now().isoformat(timespec="seconds")
        entry["outcome"] = outcome
        return db

    _atomic_modify(_PENDING_DB_PATH, _mod)
    return captured["entry"]


def list_pending() -> List[Dict[str, Any]]:
    """Return all live (unused, unexpired) pending entries. For ops/debug."""
    _ensure_initialized()
    db = _read_locked(_PENDING_DB_PATH)
    out = []
    for tok, e in db.get("pending", {}).items():
        if e.get("used") or _is_expired(e):
            continue
        item = dict(e)
        item["token_preview"] = tok[:8] + "..."
        out.append(item)
    return out
