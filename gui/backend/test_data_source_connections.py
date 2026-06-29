"""
Tests for the encrypted-at-rest connection-profile store.

Offline, no live DB. Verifies: passwords are Fernet-encrypted on disk (never
plaintext), decrypt round-trips, missing/invalid key fails safely, tampered
tokens fail cleanly, file is 0600, and safe views never leak secrets.

Run:  python gui/backend/test_data_source_connections.py
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUI_ROOT = HERE.parent
PROJ_ROOT = GUI_ROOT.parent
sys.path.insert(0, str(PROJ_ROOT)); sys.path.insert(0, str(GUI_ROOT)); sys.path.insert(0, str(HERE))

from backend import data_source_connections as dsc  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402

PASSED = 0
FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}" + (f"  -- {detail}" if detail else ""))


import contextlib  # noqa: E402


@contextlib.contextmanager
def env(**overrides):
    saved = {k: os.environ.get(k) for k in overrides}
    try:
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


def fresh_store(tmp):
    dsc.init_connection_store(tmp)
    return dsc.get_connection_store()


PW = "s3cr3t-db-pass!"


def test_password_encrypted_at_rest():
    key = Fernet.generate_key().decode()
    with tempfile.TemporaryDirectory() as raw, env(ADAM_DATA_SOURCE_ENCRYPTION_KEY=key):
        tmp = Path(raw)
        dsc.init_connection_store(tmp)
        dsc.write_connection_profile(
            source_handle="inventory-v1", display_name="Inventory", host="db.example",
            port=3306, database="inv", username="ro", password=PW, approved_by="admin")
        path = tmp / "pipeline_data" / "source_connections.json"
        on_disk = path.read_text(encoding="utf-8")
        check("plaintext password NOT on disk", PW not in on_disk)
        data = json.loads(on_disk)
        token = data["profiles"]["inventory-v1"]["encrypted_password"]
        check("encrypted_password is a Fernet token", token and token != PW)
        check("token decrypts back to the password", dsc.decrypt_password(token) == PW)
        check("username present (not secret) but password field absent",
              data["profiles"]["inventory-v1"]["username"] == "ro" and "password" not in data["profiles"]["inventory-v1"])


def test_store_file_is_0600():
    key = Fernet.generate_key().decode()
    with tempfile.TemporaryDirectory() as raw, env(ADAM_DATA_SOURCE_ENCRYPTION_KEY=key):
        tmp = Path(raw)
        dsc.init_connection_store(tmp)
        dsc.write_connection_profile(source_handle="v1", display_name="d", host="h",
            port=3306, database="db", username="u", password=PW, approved_by="a")
        path = tmp / "pipeline_data" / "source_connections.json"
        mode = stat.S_IMODE(os.stat(path).st_mode)
        check("connection store file is 0600", mode == 0o600, oct(mode))


def test_safe_view_and_get():
    key = Fernet.generate_key().decode()
    with tempfile.TemporaryDirectory() as raw, env(ADAM_DATA_SOURCE_ENCRYPTION_KEY=key):
        tmp = Path(raw)
        dsc.init_connection_store(tmp)
        dsc.write_connection_profile(source_handle="v1", display_name="Disp", host="h",
            port=3306, database="db", username="u", password=PW, approved_by="a")
        store = dsc.get_connection_store()
        prof = store.get("v1")
        check("get returns the profile", prof is not None and prof.source_handle == "v1")
        safe = prof.safe_view()
        check("safe_view has no username", "username" not in safe)
        check("safe_view has no password/token",
              "password" not in safe and "encrypted_password" not in safe)
        check("safe_view shows display_name + database", safe["display_name"] == "Disp" and safe["database"] == "db")


def test_missing_key_fails_safe():
    with tempfile.TemporaryDirectory() as raw, env(ADAM_DATA_SOURCE_ENCRYPTION_KEY=None):
        tmp = Path(raw)
        dsc.init_connection_store(tmp)
        check("encryption_available() False when key unset", dsc.encryption_available() is False)
        try:
            dsc.encrypt_password(PW)
            check("encrypt without key raises", False)
        except dsc.EncryptionKeyError:
            check("encrypt without key -> EncryptionKeyError", True)


def test_invalid_key_fails_safe():
    with env(ADAM_DATA_SOURCE_ENCRYPTION_KEY="not-a-valid-fernet-key"):
        try:
            dsc.encrypt_password(PW)
            check("invalid key raises", False)
        except dsc.EncryptionKeyError:
            check("invalid key -> EncryptionKeyError", True)


def test_tampered_token_fails_clean():
    key = Fernet.generate_key().decode()
    with env(ADAM_DATA_SOURCE_ENCRYPTION_KEY=key):
        token = dsc.encrypt_password(PW)
        tampered = token[:-4] + "AAAA"
        try:
            dsc.decrypt_password(tampered)
            check("tampered token raises", False)
        except dsc.DecryptionError:
            check("tampered token -> DecryptionError (no plaintext)", True)
        # wrong key
        with env(ADAM_DATA_SOURCE_ENCRYPTION_KEY=Fernet.generate_key().decode()):
            try:
                dsc.decrypt_password(token)
                check("wrong key raises", False)
            except dsc.DecryptionError:
                check("wrong key -> DecryptionError", True)


def test_delete_removes_only_the_profile():
    key = Fernet.generate_key().decode()
    with tempfile.TemporaryDirectory() as raw, env(ADAM_DATA_SOURCE_ENCRYPTION_KEY=key):
        tmp = Path(raw)
        dsc.init_connection_store(tmp)
        dsc.write_connection_profile(source_handle="a-v1", display_name="A", host="h",
            port=3306, database="da", username="u", password=PW, approved_by="adm")
        dsc.write_connection_profile(source_handle="b-v1", display_name="B", host="h",
            port=3306, database="db", username="u", password=PW, approved_by="adm")
        path = tmp / "pipeline_data" / "source_connections.json"

        removed = dsc.delete_connection_profile("a-v1")
        check("delete returns True for an existing profile", removed is True)

        store = dsc.get_connection_store()
        check("deleted handle is gone", store.has("a-v1") is False)
        check("other handle untouched", store.has("b-v1") is True)

        data = json.loads(path.read_text(encoding="utf-8"))
        check("deleted handle absent from store file", "a-v1" not in data["profiles"])
        check("surviving handle still in store file", "b-v1" in data["profiles"])

        again = dsc.delete_connection_profile("a-v1")
        check("deleting an absent profile returns False (no error)", again is False)

        missing = dsc.delete_connection_profile("never-existed")
        check("deleting an unknown handle returns False", missing is False)


def main():
    print("Data-source connection store tests")
    print("=" * 60)
    for t in [
        test_password_encrypted_at_rest,
        test_store_file_is_0600,
        test_safe_view_and_get,
        test_missing_key_fails_safe,
        test_invalid_key_fails_safe,
        test_tampered_token_fails_clean,
        test_delete_removes_only_the_profile,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
