"""
Tests for the usercrud pass: admin user management + password change.

Covers the scenarios enumerated in the task (1-22):

  Create / duplicate / validation / edit / suspend / reactivate / reset,
  forced password change, the self-action and last-admin guardrails, the
  legacy-record default, and that request body models are module-level
  (so FastAPI does not mis-parse them as query params).

Scenario 23 ("existing auth/session/governance/CSRF tests still pass") is
checked by running the other suites, not here.

All admin endpoints are CSRF-protected, so each mutating call carries a
token bound to the acting user's login session.

Run:  python gui/backend/test_usercrud.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent            # .../gui/backend
GUI_ROOT = HERE.parent                             # .../gui
sys.path.insert(0, str(GUI_ROOT))
sys.path.insert(0, str(HERE))

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


from backend import server, auth, csrf  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _make_app(tmp: Path):
    logs = tmp / "logs"; gui = tmp / "gui"
    logs.mkdir(parents=True, exist_ok=True); gui.mkdir(parents=True, exist_ok=True)
    auth.init_auth(gui)
    auth.add_user(username="admin", display_name="Admin", email="admin@e.com",
                  role="admin", password="adminpass12", status="active",
                  sessions_remaining=-1, max_turns_per_session=-1)
    app = server.build_app(adam_root=tmp, logs_dir=logs)
    return app, logs


def _ctx(username):
    tok = auth.create_login_session(username)
    ct = csrf.issue_token(tok)
    return {"adam_login": tok, "adam_csrf": ct}, {"X-CSRF-Token": ct}


def test_create_and_validation():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw); app, _ = _make_app(tmp); c = TestClient(app)
        cook, hdr = _ctx("admin")

        # 1. create returns 200, temp pw once, user exists, must_change True
        r = c.post("/api/admin/users", cookies=cook, headers=hdr, json={
            "username": "jdoe", "display_name": "John Doe",
            "email": "jdoe@example.com", "role": "pilot",
        })
        check("1. create -> 200", r.status_code == 200, f"{r.status_code}: {r.text[:160]}")
        body = r.json()
        check("1. temp password returned once", bool(body.get("temporary_password")))
        check("1. user exists", auth.get_user("jdoe") is not None)
        check("1. must_change_password True", auth.get_user("jdoe").get("must_change_password") is True)

        # 2. duplicate -> 409
        r = c.post("/api/admin/users", cookies=cook, headers=hdr, json={
            "username": "jdoe", "display_name": "x", "email": "x@y.com", "role": "pilot"})
        check("2. duplicate -> 409", r.status_code == 409, f"{r.status_code}")

        # 3. invalid email -> 400 readable
        r = c.post("/api/admin/users", cookies=cook, headers=hdr, json={
            "username": "bad", "display_name": "x", "email": "notanemail", "role": "pilot"})
        check("3. invalid email -> 400", r.status_code == 400, f"{r.status_code}")
        check("3. readable detail", isinstance(r.json().get("detail"), str) and r.json()["detail"])

        # 4. invalid role -> 400 readable
        r = c.post("/api/admin/users", cookies=cook, headers=hdr, json={
            "username": "bad2", "display_name": "x", "email": "x@y.com", "role": "superuser"})
        check("4. invalid role -> 400", r.status_code == 400, f"{r.status_code}")
        check("4. readable detail mentions role",
              "role" in str(r.json().get("detail", "")).lower())


def test_edit():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw); app, _ = _make_app(tmp); c = TestClient(app)
        cook, hdr = _ctx("admin")
        c.post("/api/admin/users", cookies=cook, headers=hdr, json={
            "username": "jdoe", "display_name": "John", "email": "jdoe@example.com", "role": "pilot"})

        # 5. edit profile fields -> 200 + persists
        r = c.patch("/api/admin/users/jdoe", cookies=cook, headers=hdr, json={
            "display_name": "Johnny", "email": "johnny@example.com", "sessions_remaining": 7})
        check("5. edit -> 200", r.status_code == 200, f"{r.status_code}: {r.text[:160]}")
        u = auth.get_user("jdoe")
        check("5. display_name persisted", u["display_name"] == "Johnny")
        check("5. email persisted", u["email"] == "johnny@example.com")
        check("5. sessions_remaining persisted", u["sessions_remaining"] == 7)

        # 6. edit invalid role -> 400
        r = c.patch("/api/admin/users/jdoe", cookies=cook, headers=hdr, json={"role": "root"})
        check("6. edit invalid role -> 400", r.status_code == 400, f"{r.status_code}")


def test_suspend_reactivate():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw); app, _ = _make_app(tmp); c = TestClient(app)
        cook, hdr = _ctx("admin")
        r = c.post("/api/admin/users", cookies=cook, headers=hdr, json={
            "username": "jdoe", "display_name": "John", "email": "jdoe@example.com", "role": "pilot"})
        temp_pw = r.json()["temporary_password"]

        # 7. suspend sets status suspended
        r = c.post("/api/admin/users/jdoe/suspend", cookies=cook, headers=hdr)
        check("7. suspend -> 200", r.status_code == 200, f"{r.status_code}")
        check("7. status suspended", auth.get_user("jdoe")["status"] == "suspended")

        # 8. suspended user cannot log in
        r = c.post("/api/auth/login", json={"username": "jdoe", "password": temp_pw})
        check("8. suspended login -> 403", r.status_code == 403, f"{r.status_code}")

        # 9. reactivate sets status active
        r = c.post("/api/admin/users/jdoe/reactivate", cookies=cook, headers=hdr)
        check("9. reactivate -> 200", r.status_code == 200, f"{r.status_code}")
        check("9. status active", auth.get_user("jdoe")["status"] == "active")

        # 10. reactivated user can log in again
        r = c.post("/api/auth/login", json={"username": "jdoe", "password": temp_pw})
        check("10. reactivated login -> 200", r.status_code == 200, f"{r.status_code}")


def test_reset_password():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw); app, _ = _make_app(tmp); c = TestClient(app)
        cook, hdr = _ctx("admin")
        r = c.post("/api/admin/users", cookies=cook, headers=hdr, json={
            "username": "jdoe", "display_name": "John", "email": "jdoe@example.com", "role": "pilot"})
        old_pw = r.json()["temporary_password"]
        # User changes password to a known value first, so we can prove the
        # reset invalidates it.
        cuser, chdr = _ctx("jdoe")
        c.post("/api/auth/change-password", cookies=cuser, headers=chdr,
               json={"current_password": old_pw, "new_password": "knownpass123"})

        # 11. reset returns new temp pw once
        r = c.post("/api/admin/users/jdoe/reset-password", cookies=cook, headers=hdr)
        check("11. reset -> 200", r.status_code == 200, f"{r.status_code}")
        new_pw = r.json().get("temporary_password")
        check("11. new temp pw returned", bool(new_pw) and new_pw != old_pw)

        # 12. reset sets must_change True
        check("12. must_change_password True after reset",
              auth.get_user("jdoe").get("must_change_password") is True)

        # 13. old password no longer works
        r = c.post("/api/auth/login", json={"username": "jdoe", "password": "knownpass123"})
        check("13. old password rejected -> 401", r.status_code == 401, f"{r.status_code}")
        r = c.post("/api/auth/login", json={"username": "jdoe", "password": new_pw})
        check("13. new temp password works -> 200", r.status_code == 200, f"{r.status_code}")


def test_change_password_flow():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw); app, _ = _make_app(tmp); c = TestClient(app)
        cook, hdr = _ctx("admin")
        r = c.post("/api/admin/users", cookies=cook, headers=hdr, json={
            "username": "jdoe", "display_name": "John", "email": "jdoe@example.com", "role": "pilot"})
        temp_pw = r.json()["temporary_password"]

        # 14. login with must_change True succeeds and returns must_change True
        r = c.post("/api/auth/login", json={"username": "jdoe", "password": temp_pw})
        check("14. forced-change login -> 200", r.status_code == 200, f"{r.status_code}")
        check("14. must_change_password true in response",
              r.json().get("must_change_password") is True)

        cuser, chdr = _ctx("jdoe")
        # 16. wrong current password rejected
        r = c.post("/api/auth/change-password", cookies=cuser, headers=chdr,
                   json={"current_password": "WRONG", "new_password": "freshpass123"})
        check("16. wrong current -> 400", r.status_code == 400, f"{r.status_code}")

        # 15. correct current clears must_change
        r = c.post("/api/auth/change-password", cookies=cuser, headers=chdr,
                   json={"current_password": temp_pw, "new_password": "freshpass123"})
        check("15. change -> 200", r.status_code == 200, f"{r.status_code}: {r.text[:160]}")
        check("15. must_change_password cleared",
              auth.get_user("jdoe").get("must_change_password") is False)


def test_guardrails():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw); app, _ = _make_app(tmp); c = TestClient(app)
        cook, hdr = _ctx("admin")

        # 19. admin cannot change their own role
        r = c.patch("/api/admin/users/admin", cookies=cook, headers=hdr, json={"role": "pilot"})
        check("19. self role-change -> 400", r.status_code == 400, f"{r.status_code}")

        # 20. cannot suspend the last active admin (sole admin = self here)
        r = c.post("/api/admin/users/admin/suspend", cookies=cook, headers=hdr)
        check("20. suspend last admin -> 400", r.status_code == 400, f"{r.status_code}")
        check("20. message mentions last admin",
              "last active admin" in str(r.json().get("detail", "")).lower())

        # 18. admin cannot reset their own account via admin reset
        r = c.post("/api/admin/users/admin/reset-password", cookies=cook, headers=hdr)
        check("18. self reset -> 400", r.status_code == 400, f"{r.status_code}")

        # 17. admin cannot suspend their own account (needs a 2nd admin so the
        # last-admin guard doesn't fire first).
        c.post("/api/admin/users", cookies=cook, headers=hdr, json={
            "username": "admin2", "display_name": "Admin2", "email": "a2@e.com",
            "role": "admin", "sessions_remaining": -1, "max_turns_per_session": -1})
        r = c.post("/api/admin/users/admin/suspend", cookies=cook, headers=hdr)
        check("17. self-suspend (2 admins) -> 400", r.status_code == 400, f"{r.status_code}")
        check("17. message mentions own account",
              "own account" in str(r.json().get("detail", "")).lower())


def test_legacy_record_default():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw); app, _ = _make_app(tmp); c = TestClient(app)
        # 21. a record created without must_change_password reads as False
        auth.add_user(username="legacy", display_name="L", email="l@e.com",
                      role="pilot", password="legacypass12")
        rec = auth.get_user("legacy")
        check("21. legacy record has no stored flag",
              "must_change_password" not in {k for k, v in rec.items()} or
              rec.get("must_change_password") in (None, False))
        r = c.post("/api/auth/login", json={"username": "legacy", "password": "legacypass12"})
        check("21. legacy login must_change_password False",
              r.json().get("must_change_password") is False)


def test_models_module_level():
    # 22. request body models are module-level (not nested in build_app),
    # which is what prevents FastAPI's loc:["query","body"] mis-parse.
    for name in ("CreateUserRequest", "EditUserRequest", "ChangePasswordRequest"):
        check(f"22. {name} is module-level", hasattr(server, name))
    # And a valid JSON body actually parses as a body (200, not a 422 with
    # loc ["query","body"]).
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw); app, _ = _make_app(tmp); c = TestClient(app)
        cook, hdr = _ctx("admin")
        r = c.post("/api/admin/users", cookies=cook, headers=hdr, json={
            "username": "parsecheck", "display_name": "P", "email": "p@e.com", "role": "pilot"})
        check("22. body parsed (not query) -> 200", r.status_code == 200, f"{r.status_code}: {r.text[:200]}")
        # If it were mis-parsed, we'd get 422 with loc containing 'query'.
        check("22. no loc:[query,body] mis-parse", r.status_code != 422)


def main():
    print("ADAM usercrud tests (admin user management + password change)")
    print("=" * 60)
    for t in [
        test_create_and_validation,
        test_edit,
        test_suspend_reactivate,
        test_reset_password,
        test_change_password_flow,
        test_guardrails,
        test_legacy_record_default,
        test_models_module_level,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
