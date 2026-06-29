"""
Data-source connection profiles — encrypted at rest, resolved by handle.

A ratified source carries schema + governance (the immutable RatifiedRecord).
This module holds the OPERATIONAL connection data separately, so a password can
rotate without re-ratifying the schema and the governance record stays pure.

Security model:
  * The password is encrypted at rest with Fernet (cryptography lib) using one
    per-deployment key from $ADAM_DATA_SOURCE_ENCRYPTION_KEY (urlsafe-b64 32B).
    The plaintext exists only in memory at encrypt time (approve) and decrypt
    time (query). Never persisted, logged, returned, or put in fixtures.
  * The store JSON (under the canonical pipeline_data/ dir) holds only a Fernet
    TOKEN for the password, created 0600 and written atomically under a lock.
  * Profiles are keyed by the ratified `version` (== the browser query handle),
    so query resolution by {version} works directly.

Browser-safe fields only ever exposed: source_handle/version, display_name,
database, host, approved_at. Never username/password/encrypted_password/DSN.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# The single per-deployment secret. Loaded into os.environ from .env by
# server.export_provider_keys_from_dotenv? No — that exports only provider keys.
# This name is read directly from os.environ (the .env loader puts provider keys
# in; for this key the operator sets it in .env which the GUI exports... see
# server wiring). Read at use time so tests can set/unset it.
ENCRYPTION_KEY_ENV = "ADAM_DATA_SOURCE_ENCRYPTION_KEY"


class EncryptionKeyError(Exception):
    """Raised when the encryption key is missing/invalid (encrypt or decrypt).
    Callers map it to a clean error — never a stack trace or plaintext."""


class DecryptionError(Exception):
    """Raised when a stored token can't be decrypted (tampered/corrupt/wrong
    key). Callers map it to a clean error — never plaintext."""


def _get_fernet():
    """Build a Fernet from the env key. Raises EncryptionKeyError if the key is
    missing or not a valid Fernet key. Imported lazily so the module imports
    without the key present."""
    from cryptography.fernet import Fernet  # lazy
    raw = os.environ.get(ENCRYPTION_KEY_ENV, "").strip()
    if not raw:
        raise EncryptionKeyError("encryption key not configured")
    try:
        return Fernet(raw.encode("utf-8"))
    except Exception as e:  # malformed key
        raise EncryptionKeyError("encryption key is invalid") from e


def encryption_available() -> bool:
    """True if a usable Fernet key is configured (no plaintext involved)."""
    try:
        _get_fernet()
        return True
    except EncryptionKeyError:
        return False


def encrypt_password(plaintext: str) -> str:
    """Encrypt a password to a Fernet token (str). Raises EncryptionKeyError if
    the key is missing/invalid. Plaintext stays in memory only."""
    f = _get_fernet()
    return f.encrypt((plaintext or "").encode("utf-8")).decode("utf-8")


def decrypt_password(token: str) -> str:
    """Decrypt a Fernet token back to plaintext. Raises EncryptionKeyError (no
    key) or DecryptionError (tampered/corrupt/wrong key)."""
    from cryptography.fernet import InvalidToken  # lazy
    f = _get_fernet()
    try:
        return f.decrypt((token or "").encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise DecryptionError("could not decrypt stored connection password") from e


# ---------------------------------------------------------------------------
# Connection profile + store
# ---------------------------------------------------------------------------

@dataclass
class ConnectionProfile:
    source_handle: str            # == ratified version (the browser query key)
    display_name: str
    host: str
    port: int
    database: str
    username: str
    encrypted_password: str       # Fernet token — NEVER plaintext
    created_at: str
    approved_by: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_handle": self.source_handle,
            "display_name": self.display_name,
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "username": self.username,
            "encrypted_password": self.encrypted_password,
            "created_at": self.created_at,
            "approved_by": self.approved_by,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ConnectionProfile":
        return cls(
            source_handle=d["source_handle"], display_name=d.get("display_name", ""),
            host=d.get("host", ""), port=int(d.get("port", 3306)),
            database=d.get("database", ""), username=d.get("username", ""),
            encrypted_password=d.get("encrypted_password", ""),
            created_at=d.get("created_at", ""), approved_by=d.get("approved_by", ""),
        )

    def safe_view(self) -> Dict[str, Any]:
        """Browser-safe subset — never username/password/token."""
        return {
            "source_handle": self.source_handle,
            "display_name": self.display_name,
            "database": self.database,
            "host": self.host,
            "created_at": self.created_at,
        }


_STORE_PATH: Optional[Path] = None
_STORE_LOCK = threading.Lock()


def init_connection_store(base_dir) -> None:
    """Set the canonical connection-store path (mirrors init_data_sources)."""
    global _STORE_PATH
    _STORE_PATH = Path(base_dir) / "pipeline_data" / "source_connections.json"


def store_lock() -> threading.Lock:
    return _STORE_LOCK


class ConnectionProfileStore:
    """Loads/saves connection profiles keyed by source_handle. The file is
    created 0600 (holds encrypted secrets) and written atomically."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.profiles: Dict[str, ConnectionProfile] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.profiles = {
            h: ConnectionProfile.from_dict(p)
            for h, p in (data.get("profiles") or {}).items()
        }

    def _save(self) -> None:
        payload = {"profiles": {h: p.to_dict() for h, p in self.profiles.items()}}
        blob = json.dumps(payload, indent=2, sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + f".tmp-{uuid.uuid4().hex[:8]}")
        # Restrictive perms BEFORE writing the secret content.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(blob)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def put(self, profile: ConnectionProfile) -> None:
        self.profiles[profile.source_handle] = profile
        self._save()

    def get(self, source_handle: str) -> Optional[ConnectionProfile]:
        return self.profiles.get(source_handle)

    def has(self, source_handle: str) -> bool:
        return source_handle in self.profiles

    def delete(self, source_handle: str) -> bool:
        """Remove the connection profile (the encrypted credential) for a handle
        and persist atomically. Returns True if a profile was removed, False if
        none existed. Touches ONLY the connection store — never the ratified
        record / source model. Caller holds the store lock (mirrors put)."""
        if source_handle not in self.profiles:
            return False
        del self.profiles[source_handle]
        self._save()
        return True

    def list_safe(self) -> List[Dict[str, Any]]:
        return [p.safe_view() for p in self.profiles.values()]


def get_connection_store() -> ConnectionProfileStore:
    if _STORE_PATH is None:
        raise RuntimeError("connection store not initialized (call init_connection_store)")
    return ConnectionProfileStore(_STORE_PATH)


def write_connection_profile(
    *, source_handle: str, display_name: str, host: str, port: Any,
    database: str, username: str, password: str, approved_by: str,
    now_fn=None,
) -> ConnectionProfile:
    """Encrypt the password and persist a connection profile under the lock.
    The plaintext password is used only here (to encrypt) and discarded. Raises
    EncryptionKeyError if the key is missing/invalid."""
    encrypted = encrypt_password(password)   # raises EncryptionKeyError if no key
    now = (now_fn or (lambda: datetime.now().isoformat(timespec="seconds")))()
    profile = ConnectionProfile(
        source_handle=source_handle, display_name=display_name or source_handle,
        host=host, port=int(port or 3306), database=database, username=username,
        encrypted_password=encrypted, created_at=now, approved_by=approved_by,
    )
    with _STORE_LOCK:
        store = get_connection_store()
        store.put(profile)
    return profile


def delete_connection_profile(source_handle: str) -> bool:
    """Remove a connection profile (encrypted credential) under the lock. Returns
    True if one was removed, False if absent. Never touches the ratified record /
    source model — history is preserved."""
    with _STORE_LOCK:
        store = get_connection_store()
        return store.delete(source_handle)
