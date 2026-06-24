"""
ADAM GUI authentication and authorization (v5 multi-user).

This module is the single source of truth for:
  - Reading and writing gui/users.json (the user database)
  - Reading and writing gui/login_sessions.json (active login sessions)
  - Password hashing and verification (bcrypt)
  - Login session token generation and validation
  - Role -> denied skills resolution

Concurrency discipline
======================
Both files are small JSON databases that get updated on every login,
every session start (decrements sessions_remaining), and every admin
action (manage_users.py). Two requests racing on the same file would
silently lose writes -- one reads sessions_remaining=3, the other
reads 3 too, both decrement to 2, the second write overwrites the
first.

Every read-modify-write goes through _atomic_modify(), which:
  1. Acquires an exclusive flock on the target file
  2. Reads the current state
  3. Calls the caller's mutator function
  4. Writes to a temp file in the same directory
  5. fsync + atomic rename
  6. Releases the lock

This is overkill for a 10-user beta but it's also ~30 lines and we
get the property for free. Stops being overkill the moment two
pilots happen to submit a new session in the same second.

Storage location
================
gui/users.json          -- the user database
gui/login_sessions.json -- active login session tokens

Both live under gui/ so they're easy to back up together. The path
is configurable at module-init time via init_auth(gui_root).

Schema versioning
=================
Both files carry a `schema_version` field. The loader checks it and
refuses to operate on a future version. Current version: "1.0".

Login session lifetime
======================
Login sessions expire after LOGIN_SESSION_TTL_SECONDS (default 7
days) of inactivity. Every authenticated request bumps the session's
last_seen timestamp; sessions older than the TTL on read are
treated as expired and deleted.

Pruning of expired sessions happens lazily on every read -- there's
no background job. The login_sessions.json file stays small because
expired entries are dropped during normal use.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Optional

try:
    import bcrypt
except ImportError as e:
    raise ImportError(
        "bcrypt is required for ADAM GUI multi-user mode. "
        "Install with: pip install bcrypt"
    ) from e


# ============================================================
# Constants
# ============================================================

SCHEMA_VERSION = "1.0"

# Login sessions expire after 7 days of inactivity. This is short
# enough that a forgotten laptop becomes safe quickly, long enough
# that active users aren't forced to re-authenticate constantly.
LOGIN_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60

# bcrypt work factor. 12 is the modern default -- ~250ms per hash
# on commodity hardware, which is slow enough to make brute-forcing
# expensive but fast enough that login isn't perceptibly slow.
BCRYPT_ROUNDS = 12

# Sentinel for "no limit" on quota fields. Matches what we use in
# users.json. Keeps the meaning of -1 explicit in code.
UNLIMITED = -1


# ============================================================
# Module state (initialized via init_auth)
# ============================================================

_USERS_DB_PATH: Optional[Path] = None
_LOGIN_SESSIONS_PATH: Optional[Path] = None


def init_auth(gui_root: Path) -> None:
    """
    Configure the auth module to read/write under the given gui/ dir.
    Must be called before any other function in this module.

    Creates the directory and seeds empty files if they don't exist,
    so a fresh install works without manual setup.
    """
    global _USERS_DB_PATH, _LOGIN_SESSIONS_PATH
    gui_root = Path(gui_root).resolve()
    gui_root.mkdir(parents=True, exist_ok=True)
    _USERS_DB_PATH = gui_root / "users.json"
    _LOGIN_SESSIONS_PATH = gui_root / "login_sessions.json"

    # Seed empty databases if they don't exist. We create them with
    # restrictive permissions (0600) since they contain password
    # hashes and session tokens.
    if not _USERS_DB_PATH.exists():
        _write_atomic(_USERS_DB_PATH, {
            "schema_version": SCHEMA_VERSION,
            "users": {},
            "roles": _default_roles(),
        })
        os.chmod(_USERS_DB_PATH, 0o600)
    if not _LOGIN_SESSIONS_PATH.exists():
        _write_atomic(_LOGIN_SESSIONS_PATH, {
            "schema_version": SCHEMA_VERSION,
            "sessions": {},
        })
        os.chmod(_LOGIN_SESSIONS_PATH, 0o600)


def _default_roles() -> Dict[str, Dict[str, Any]]:
    """
    The default roles block, written into a fresh users.json so
    `manage_users.py add` has something to validate against
    immediately. Admins can edit the file to add roles later.
    """
    return {
        "admin": {
            "skills_denied": [],
            "description": "Full ADAM access. Used by project owner.",
        },
        "pilot": {
            "skills_denied": ["email"],
            "description": "Beta pilot. Cannot send email.",
        },
    }


def _ensure_initialized() -> None:
    if _USERS_DB_PATH is None or _LOGIN_SESSIONS_PATH is None:
        raise RuntimeError(
            "auth module not initialized -- call init_auth(gui_root) first"
        )


# ============================================================
# Atomic file IO with locking
# ============================================================

def _write_atomic(path: Path, data: Dict[str, Any]) -> None:
    """
    Write `data` as JSON to `path` atomically: write to a temp file
    in the same directory, fsync, then rename. The rename is atomic
    on POSIX, so readers either see the old file or the new file --
    never a half-written one.

    Does NOT acquire a lock. Callers that need to read-modify-write
    must use _atomic_modify() instead.
    """
    parent = path.parent
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        # Match existing file's permissions if it exists; default to 0600
        if path.exists():
            os.chmod(tmp_path, path.stat().st_mode & 0o777)
        else:
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        # Clean up the temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _atomic_modify(path: Path, mutator: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
    """
    Read JSON from `path`, pass it to `mutator`, write the result
    back atomically. Holds an exclusive flock on a sidecar lockfile
    (`<path>.lock`) for the entire read-modify-write sequence so
    concurrent calls serialize cleanly.

    Returns the mutated dict.

    Mutator contract: receives the parsed dict (or {} if the file
    is empty/missing), returns the dict to write back. May mutate
    in place and return the same dict, or return a new one.

    Why a sidecar lockfile instead of locking `path` itself: the
    atomic-write pattern uses os.replace() to swap a temp file into
    `path`. That replace operation makes our locked fd point at an
    orphaned (unlinked) inode, while concurrent callers open a
    fresh fd against the new inode and get a separate, uncontended
    lock. The result is silent lost updates -- two writers both
    "have the lock" against different inodes and each clobber the
    other's write. The sidecar lockfile is never renamed, so all
    callers contend on the same inode and serialization actually
    works.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lockfile = path.with_suffix(path.suffix + ".lock")

    # Open the lockfile (create if absent). We never write to it;
    # it just exists to be flock()'d.
    lock_fd = os.open(lockfile, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        # Now under the lock, read the data file
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            data = json.loads(raw) if raw else {}
        else:
            data = {}

        mutated = mutator(data)
        if mutated is None:
            mutated = data
        _write_atomic(path, mutated)
        return mutated
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _read_locked(path: Path) -> Dict[str, Any]:
    """
    Read JSON from `path` under a shared lock on the sidecar
    lockfile. Used for consistency on reads that don't need to
    write back. Shared locks allow multiple concurrent reads but
    block during a writer's exclusive lock.
    """
    if not path.exists():
        return {}
    lockfile = path.with_suffix(path.suffix + ".lock")
    lock_fd = os.open(lockfile, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            if not raw:
                return {}
            return json.loads(raw)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


# ============================================================
# Password hashing
# ============================================================

def hash_password(plaintext: str) -> str:
    """
    Hash a plaintext password with bcrypt. Returns a string suitable
    for storage in users.json's password_hash field.

    Bcrypt's output is URL-safe ASCII (no escaping needed in JSON).
    """
    if not isinstance(plaintext, str) or not plaintext:
        raise ValueError("password must be a non-empty string")
    hashed = bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt(BCRYPT_ROUNDS))
    return hashed.decode("utf-8")


def verify_password(plaintext: str, hashed: str) -> bool:
    """
    Constant-time check of a plaintext password against a stored
    bcrypt hash. Returns False on any error (bad hash format,
    missing data, etc.) rather than raising -- a malformed hash in
    the DB should not crash the login flow, it should just fail
    authentication.
    """
    if not isinstance(plaintext, str) or not isinstance(hashed, str):
        return False
    if not plaintext or not hashed:
        return False
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        # ValueError fires on malformed hash, TypeError on bad types
        return False


# ============================================================
# User database
# ============================================================

def load_users_db() -> Dict[str, Any]:
    """
    Read the full users.json under a shared lock. Returns the full
    dict including roles. Callers should not mutate the returned
    dict and expect persistence -- use update_user() etc. for that.
    """
    _ensure_initialized()
    data = _read_locked(_USERS_DB_PATH)
    if not data:
        return {"schema_version": SCHEMA_VERSION, "users": {}, "roles": _default_roles()}
    if data.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(
            f"users.json schema_version is {data.get('schema_version')!r}; "
            f"this code expects {SCHEMA_VERSION!r}"
        )
    return data


def get_user(username: str) -> Optional[Dict[str, Any]]:
    """
    Look up a user by username. Returns the user record (with role
    expanded into the record as `_role`) or None if not found.

    The role expansion is a convenience for callers -- they get one
    dict with everything they need to make a decision, instead of
    having to do two lookups.
    """
    if not username:
        return None
    db = load_users_db()
    record = db["users"].get(username)
    if record is None:
        return None
    # Attach the role definition so callers don't need a separate
    # lookup for skills_denied etc. The underscore prefix marks it
    # as a derived field, not stored on disk.
    role_name = record.get("role")
    record = dict(record)  # copy to avoid mutating db
    record["username"] = username
    record["_role"] = db["roles"].get(role_name, {})
    return record


def update_user(username: str, mutator: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    """
    Atomically modify a user record. The mutator receives the user's
    dict (NOT the full db) and may modify it in place. Raises
    KeyError if the user doesn't exist.

    Returns the updated user record (without the _role expansion).
    """
    _ensure_initialized()
    def _mod(db: Dict[str, Any]) -> Dict[str, Any]:
        if "users" not in db or username not in db["users"]:
            raise KeyError(f"user not found: {username}")
        user = db["users"][username]
        mutator(user)
        return db
    db = _atomic_modify(_USERS_DB_PATH, _mod)
    return db["users"][username]


def add_user(
    username: str,
    *,
    display_name: str,
    email: str,
    role: str,
    password: str,
    status: str = "active",
    sessions_remaining: int = 3,
    max_turns_per_session: int = 10,
) -> Dict[str, Any]:
    """
    Add a new user. Raises ValueError on validation failure, KeyError
    if the username already exists. Used by manage_users.py.

    The defaults (sessions_remaining=3, max_turns_per_session=10)
    match what's reasonable for a pilot. Admins should override to
    -1 / -1 via manage_users.py prompting.
    """
    _ensure_initialized()

    # Validate
    if not username or not isinstance(username, str):
        raise ValueError("username must be a non-empty string")
    if " " in username or not username.isascii() or username != username.lower():
        raise ValueError("username must be lowercase ASCII with no spaces")
    if not display_name:
        raise ValueError("display_name required")
    if "@" not in email or "." not in email.split("@")[-1]:
        raise ValueError("email does not look valid")
    if not password:
        raise ValueError("password required")
    if status not in ("active", "suspended"):
        raise ValueError(f"status must be 'active' or 'suspended', got {status!r}")

    password_hash = hash_password(password)
    created_at = datetime.now().isoformat(timespec="seconds")

    def _mod(db: Dict[str, Any]) -> Dict[str, Any]:
        if "users" not in db:
            db["users"] = {}
        if "roles" not in db:
            db["roles"] = _default_roles()
        if username in db["users"]:
            raise KeyError(f"user already exists: {username}")
        if role not in db["roles"]:
            raise ValueError(
                f"role {role!r} not defined; "
                f"available roles: {sorted(db['roles'].keys())}"
            )
        db["users"][username] = {
            "password_hash":         password_hash,
            "role":                  role,
            "display_name":          display_name,
            "email":                 email,
            "sessions_remaining":    sessions_remaining,
            "max_turns_per_session": max_turns_per_session,
            "created_at":            created_at,
            "last_login_at":         None,
            "status":                status,
        }
        return db

    db = _atomic_modify(_USERS_DB_PATH, _mod)
    return db["users"][username]


def delete_user(username: str, *, require_status: Optional[str] = None) -> bool:
    """
    Remove a user from users.json entirely. Returns True if a user was
    deleted, False if no such user existed.

    If `require_status` is given, the deletion only proceeds when the
    user's current status matches it; otherwise the user is left
    untouched and False is returned. The register app uses
    require_status="suspended" so a "deny" link can only ever remove a
    still-pending account, never an account that has since been
    activated.

    The whole check-and-delete happens under the file lock, so it's
    race-safe against a concurrent approve.
    """
    _ensure_initialized()
    deleted = {"ok": False}

    def _mod(db: Dict[str, Any]) -> Dict[str, Any]:
        users = db.get("users", {})
        user = users.get(username)
        if user is None:
            return db
        if require_status is not None and user.get("status") != require_status:
            return db
        del users[username]
        deleted["ok"] = True
        return db

    _atomic_modify(_USERS_DB_PATH, _mod)
    return deleted["ok"]


def list_users() -> Dict[str, Dict[str, Any]]:
    """Return all users keyed by username. Used by manage_users.py list."""
    db = load_users_db()
    return db.get("users", {})


def list_roles() -> Dict[str, Dict[str, Any]]:
    """Return all roles. Used by manage_users.py for validation prompts."""
    db = load_users_db()
    return db.get("roles", {})


# ============================================================
# Quota and policy helpers
# ============================================================

def effective_max_turns(user: Dict[str, Any], user_supplied: Optional[int]) -> Optional[int]:
    """
    Resolve the effective max_turns for a session, given the user's
    role and what they submitted in the new-session form.

    Rules:
      - Admin (max_turns_per_session == -1): honor user_supplied; if
        None, return None so ADAM uses its default.
      - Pilot (max_turns_per_session > 0): IGNORE user_supplied,
        return the user's quota value. This is the server-side
        enforcement that the disabled UI field is just a UX hint
        for; even a hand-crafted request can't bypass it.

    This is called server-side in the new-session POST handler.
    """
    quota = user.get("max_turns_per_session", UNLIMITED)
    if quota == UNLIMITED:
        return user_supplied
    return int(quota)


def can_start_session(user: Dict[str, Any]) -> tuple[bool, Optional[str]]:
    """
    Check whether a user is allowed to start a new session right now.
    Returns (allowed, reason_if_denied).

    Reasons returned are user-facing strings; the API caller can
    surface them verbatim or wrap them.
    """
    if user.get("status") != "active":
        return False, (
            "Your account is currently inactive. "
            "Please contact David if this is unexpected."
        )
    remaining = user.get("sessions_remaining", 0)
    if remaining == UNLIMITED:
        return True, None
    if remaining <= 0:
        return False, (
            "Your pilot allocation is fully used. "
            "Email David to request more time with ADAM."
        )
    return True, None


def decrement_sessions_remaining(username: str) -> int:
    """
    Atomically decrement sessions_remaining for a user. Returns the
    new value. No-op if the user has -1 (unlimited).

    Called after a successful ADAM spawn in the new-session handler.
    Does NOT check that the value is > 0 first; that's done by
    can_start_session before the spawn attempt. If the value goes
    negative, that's a bug somewhere -- log it but don't crash.
    """
    new_value = [0]  # closure capture
    def _mod(user: Dict[str, Any]) -> None:
        remaining = user.get("sessions_remaining", 0)
        if remaining == UNLIMITED:
            new_value[0] = UNLIMITED
            return
        user["sessions_remaining"] = remaining - 1
        new_value[0] = user["sessions_remaining"]
    update_user(username, _mod)
    return new_value[0]


def skills_denied_for_user(user: Dict[str, Any]) -> list[str]:
    """
    Return the list of skill names denied to this user, based on
    their role. Used to build --disable-skill CLI flags at spawn.
    """
    role = user.get("_role", {})
    denied = role.get("skills_denied", [])
    if not isinstance(denied, list):
        return []
    return [str(s) for s in denied]


def effective_governance_profile(user: Dict[str, Any],
                                 user_supplied: Optional[str]) -> Optional[str]:
    """
    Resolve the effective governance profile for a session, given the
    user's role and what they submitted in the new-session form.

    Mirrors effective_max_turns -- the server-side enforcement that the
    disabled profile picker in the UI is only a UX hint for. Even a
    hand-crafted request cannot bypass a pilot's assigned profile.

    Rules:
      - Admin (max_turns_per_session == UNLIMITED): honor user_supplied;
        if None, return None so the backend resolves its default profile.
      - Pilot (a fixed allocation): IGNORE user_supplied and return the
        pilot's assigned profile. The assigned profile is read from the
        user record's `governance_profile` field, falling back to the
        role's `governance_profile`, falling back to None (which the
        backend resolves to the default profile). Set a pilot's profile
        by adding "governance_profile": "education" to their users.json
        record (or to the pilot role).

    Returning None means "let the backend resolve the default" -- so an
    admin who picks nothing, or a pilot with no assigned profile, both
    get the configured default. A pilot WITH an assigned profile is
    locked to it regardless of what the form sent.
    """
    quota = user.get("max_turns_per_session", UNLIMITED)
    if quota == UNLIMITED:
        # Admin: honor their explicit choice (or None -> default).
        return user_supplied
    # Pilot: force the assigned profile, ignoring user_supplied.
    assigned = user.get("governance_profile")
    if not assigned:
        role = user.get("_role", {})
        assigned = role.get("governance_profile")
    return assigned or None


# ============================================================
# Login sessions (the cookie-tracked kind, NOT ADAM sessions)
# ============================================================
#
# Note on naming: "login session" vs "ADAM session" -- we use the
# qualifier consistently in code and in JSON keys because the
# overload would otherwise be unbearable. Login sessions are
# browser auth state. ADAM sessions are deliberation runs.

def create_login_session(username: str, *, user_agent: Optional[str] = None,
                          ip_address: Optional[str] = None) -> str:
    """
    Create a new login session for the given user. Returns the
    session token, which the caller sets as a cookie.

    Tokens are 32 bytes of cryptographic randomness, URL-safe
    encoded. Length is ~43 chars; collision probability is
    negligible (2^256 space).

    Also updates the user's last_login_at to now.
    """
    _ensure_initialized()
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    record = {
        "username":   username,
        "created_at": now.isoformat(timespec="seconds"),
        "last_seen":  now.isoformat(timespec="seconds"),
        "user_agent": (user_agent or "")[:200],   # cap to avoid abuse
        "ip_address": (ip_address or "")[:64],
    }
    def _mod(db: Dict[str, Any]) -> Dict[str, Any]:
        if "sessions" not in db:
            db["sessions"] = {}
        if "schema_version" not in db:
            db["schema_version"] = SCHEMA_VERSION
        # Prune expired while we have the lock
        _prune_expired_in_place(db)
        db["sessions"][token] = record
        return db
    _atomic_modify(_LOGIN_SESSIONS_PATH, _mod)

    # Bump last_login_at on the user record. Separate write because
    # different file, different lock.
    def _bump(user: Dict[str, Any]) -> None:
        user["last_login_at"] = now.isoformat(timespec="seconds")
    try:
        update_user(username, _bump)
    except KeyError:
        # User deleted between auth check and now; the session token
        # is now orphaned. Validate-login will reject it next time.
        pass

    return token


def validate_login_session(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Look up a login session token and return the associated user
    record (with role expanded), or None if the token is invalid,
    expired, or the user no longer exists / is suspended.

    Also bumps last_seen on a successful validation, so active
    sessions don't time out.
    """
    if not token:
        return None
    _ensure_initialized()

    # Read-only first to avoid taking a write lock on every request
    db = _read_locked(_LOGIN_SESSIONS_PATH)
    sessions = db.get("sessions", {})
    record = sessions.get(token)
    if record is None:
        return None

    # Check expiry
    try:
        last_seen = datetime.fromisoformat(record["last_seen"])
    except (KeyError, ValueError):
        return None
    if (datetime.now() - last_seen).total_seconds() > LOGIN_SESSION_TTL_SECONDS:
        # Expired -- delete and refuse
        _delete_login_session(token)
        return None

    username = record.get("username")
    user = get_user(username) if username else None
    if user is None or user.get("status") != "active":
        # User vanished or got suspended; treat as not-authenticated.
        return None

    # Bump last_seen. This is a write, so it grabs the exclusive lock.
    # We accept the cost on every request because (a) it's a tiny file,
    # (b) it gives us idle-timeout behavior, and (c) users are few.
    def _bump(d: Dict[str, Any]) -> Dict[str, Any]:
        if token in d.get("sessions", {}):
            d["sessions"][token]["last_seen"] = datetime.now().isoformat(timespec="seconds")
        return d
    try:
        _atomic_modify(_LOGIN_SESSIONS_PATH, _bump)
    except Exception:
        # If the bump fails (disk full, perms, whatever), still return
        # the user -- authentication succeeded, we just couldn't
        # update the timestamp.
        pass

    return user


def delete_login_session(token: Optional[str]) -> None:
    """Public: log out a session. Idempotent -- no error if missing."""
    if not token:
        return
    _delete_login_session(token)


def _delete_login_session(token: str) -> None:
    _ensure_initialized()
    def _mod(db: Dict[str, Any]) -> Dict[str, Any]:
        if "sessions" in db and token in db["sessions"]:
            del db["sessions"][token]
        return db
    _atomic_modify(_LOGIN_SESSIONS_PATH, _mod)


def _prune_expired_in_place(db: Dict[str, Any]) -> None:
    """
    Remove expired sessions from the db dict (mutates in place).
    Called from create_login_session under the lock so we don't
    accumulate stale entries forever.
    """
    sessions = db.get("sessions", {})
    cutoff = datetime.now() - timedelta(seconds=LOGIN_SESSION_TTL_SECONDS)
    expired = []
    for token, record in sessions.items():
        try:
            last_seen = datetime.fromisoformat(record["last_seen"])
            if last_seen < cutoff:
                expired.append(token)
        except (KeyError, ValueError):
            # Malformed record -- drop it
            expired.append(token)
    for token in expired:
        del sessions[token]
