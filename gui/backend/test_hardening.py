"""
Tests for Pass 1 web hardening: CSRF protection + login rate limiting.

Covers the scenarios enumerated in the hardening task:

  CSRF (signed double-submit, bound to the login session):
    1.  Authenticated mutating POST WITHOUT X-CSRF-Token -> 403
    2.  Authenticated mutating POST WITH matching cookie/header -> succeeds
    3.  POST /api/auth/login works WITHOUT a CSRF token (and SETS adam_csrf)
    4.  GET endpoints do not require CSRF
    5.  FormData session CREATE sends/accepts CSRF (multipart)
    6.  FormData CONTINUE sends/accepts CSRF
    7.  FormData RESUME sends/accepts CSRF
    +   logout requires CSRF; director_message requires CSRF
    +   csrf.validate_token unit tests (signature, binding, fail-closed)

  Login rate limiting (in-memory, fail-OPEN):
    8.  Repeated failed logins for one username -> 429 with Retry-After
    9.  Successful login resets the username failure counter
    10. A simulated limiter exception does NOT block login (fail-open)

  Frontend (api.js source + built bundle):
    11. The mutating api.js helper includes X-CSRF-Token, including
        FormData/multipart calls; login is exempt; the built dist bundle
        carries the header.

The ADAM subprocess is never launched: spawn_adam_session is stubbed.

Run:  python gui/backend/test_hardening.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent            # .../gui/backend
GUI_ROOT = HERE.parent                             # .../gui
PROJ_ROOT = GUI_ROOT.parent                        # project root
sys.path.insert(0, str(GUI_ROOT))                  # so `import backend...` works
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


from backend import server, auth, csrf, ratelimit  # noqa: E402


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _make_app(tmp: Path, *, limiter=None):
    """Build a real app against temp logs/gui dirs with one pilot + one
    admin and live login sessions. Returns (app, logs_dir, tokens)."""
    logs_dir = tmp / "logs"
    gui_root = tmp / "gui"
    logs_dir.mkdir(parents=True, exist_ok=True)
    gui_root.mkdir(parents=True, exist_ok=True)
    auth.init_auth(gui_root)
    auth.add_user(username="pilot", display_name="Pilot", email="p@e.com",
                  role="pilot", password="correct-horse", status="active",
                  sessions_remaining=10, max_turns_per_session=10)
    auth.add_user(username="admin", display_name="Admin", email="a@e.com",
                  role="admin", password="correct-horse", status="active",
                  sessions_remaining=-1, max_turns_per_session=-1)
    pilot_token = auth.create_login_session("pilot")
    app = server.build_app(adam_root=tmp, logs_dir=logs_dir)
    if limiter is not None:
        app.state.login_rate_limiter = limiter
    # csrf.init_csrf ran inside build_app; mint a token bound to pilot_token.
    return app, logs_dir, pilot_token


def _stub_spawn(logs_dir: Path):
    """Replace server.spawn_adam_session with a stub that creates a
    minimal child dir and returns a valid result dict."""
    def fake_spawn(*, adam_root, logs_dir, user_id, display_name, email,
                   seed_text, context_files, max_turns, no_verify,
                   disable_skills=None, parent_session_id=None,
                   governance_profile_id=None, resume_after_review=False,
                   resume_after_information=False):
        child_id = "child-" + str(abs(hash(seed_text)) % 100000)
        cdir = logs_dir / user_id / child_id
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "seed.md").write_text(seed_text, encoding="utf-8")
        return {
            "session_id": child_id, "started_at": "2026-06-26T00:00:00",
            "pid": 1234, "session_dir": str(cdir),
            "seed_path": str(cdir / "seed.md"),
            "context_files": [c["filename"] for c in context_files],
            "status": "starting",
        }
    server.spawn_adam_session = fake_spawn


def _write_completed_parent(logs_dir: Path, user: str, sid: str):
    sdir = logs_dir / user / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "session_state.json").write_text(json.dumps({
        "governance_state": {"seed": "original task"},
        "operator_summary": {"narrative_summary": "prior result"},
    }), encoding="utf-8")
    (sdir / "seed.md").write_text("original task\n", encoding="utf-8")
    return sdir


def _write_paused_session(logs_dir: Path, user: str, sid: str):
    sdir = logs_dir / user / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "pause_state.json").write_text(json.dumps({
        "pause_type": "gate_review",
        "final_synthesis_text": "settled plan",
        "review_reason": "public_facing_artifact",
    }), encoding="utf-8")
    return sdir


# ----------------------------------------------------------------------
# 1. csrf.validate_token unit tests
# ----------------------------------------------------------------------

def test_csrf_unit():
    with tempfile.TemporaryDirectory() as tmp:
        csrf.init_csrf(Path(tmp))
        login = "login-token-abc"
        tok = csrf.issue_token(login)

        check("valid signed token validates",
              csrf.validate_token(tok, tok, login) is True)
        check("missing header rejected",
              csrf.validate_token(tok, None, login) is False)
        check("missing cookie rejected",
              csrf.validate_token(None, tok, login) is False)
        check("cookie/header mismatch rejected",
              csrf.validate_token(tok, tok + "x", login) is False)
        # Plain double-submit (equal but unsigned) must be rejected: this
        # is exactly the cookie-planting case signing defends against.
        check("unsigned cookie==header rejected (signing required)",
              csrf.validate_token("planted.value", "planted.value", login) is False)
        # Binding: a token minted for one login can't be replayed under
        # another login token.
        check("token bound to login session (wrong login rejected)",
              csrf.validate_token(tok, tok, "different-login") is False)


# ----------------------------------------------------------------------
# 2. CSRF endpoint behavior
# ----------------------------------------------------------------------

def test_csrf_endpoints():
    from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        app, logs_dir, pilot_token = _make_app(tmp)
        _stub_spawn(logs_dir)
        client = TestClient(app)

        csrf_token = csrf.issue_token(pilot_token)
        cookies = {"adam_login": pilot_token, "adam_csrf": csrf_token}
        headers = {"X-CSRF-Token": csrf_token}

        # (1) mutating POST without header -> 403
        r = client.post("/api/sessions", data={"seed": "hello"},
                        cookies={"adam_login": pilot_token})
        check("1. mutating POST without CSRF -> 403", r.status_code == 403,
              f"got {r.status_code}")

        # (2) mutating POST with matching cookie+header -> 201
        r = client.post("/api/sessions", data={"seed": "hello"},
                        cookies=cookies, headers=headers)
        check("2. mutating POST with CSRF -> 201", r.status_code == 201,
              f"got {r.status_code}: {r.text[:160]}")

        # bad token -> 403
        r = client.post("/api/sessions", data={"seed": "hi"},
                        cookies=cookies, headers={"X-CSRF-Token": "bogus.sig"})
        check("mismatched CSRF token -> 403", r.status_code == 403,
              f"got {r.status_code}")

        # unauthenticated mutating -> 401 (auth fires before CSRF)
        r = client.post("/api/sessions", data={"seed": "hi"})
        check("unauthenticated mutating -> 401 (not 403)", r.status_code == 401,
              f"got {r.status_code}")

        # (4) GET endpoints do not require CSRF
        r = client.get("/api/sessions", cookies={"adam_login": pilot_token})
        check("4. GET /api/sessions needs no CSRF", r.status_code == 200,
              f"got {r.status_code}")

        # (5) FormData CREATE (multipart with a file) accepts CSRF
        r = client.post(
            "/api/sessions",
            data={"seed": "multipart create"},
            files=[("context_files", ("note.txt", b"hello bytes", "text/plain"))],
            cookies=cookies, headers=headers,
        )
        check("5. FormData CREATE with CSRF -> 201", r.status_code == 201,
              f"got {r.status_code}: {r.text[:160]}")

        # (6) FormData CONTINUE accepts CSRF
        _write_completed_parent(logs_dir, "pilot", "parent-1")
        r = client.post("/api/sessions/parent-1/continue",
                        data={"seed": "follow up"}, cookies=cookies, headers=headers)
        check("6. FormData CONTINUE with CSRF -> 200", r.status_code == 200,
              f"got {r.status_code}: {r.text[:160]}")
        # ...and CONTINUE without CSRF -> 403
        r = client.post("/api/sessions/parent-1/continue",
                        data={"seed": "follow up"}, cookies={"adam_login": pilot_token})
        check("   CONTINUE without CSRF -> 403", r.status_code == 403,
              f"got {r.status_code}")

        # (7) FormData RESUME accepts CSRF
        _write_paused_session(logs_dir, "pilot", "paused-1")
        r = client.post("/api/sessions/paused-1/resume",
                        data={"decision": "approve"}, cookies=cookies, headers=headers)
        check("7. FormData RESUME with CSRF -> 200", r.status_code == 200,
              f"got {r.status_code}: {r.text[:160]}")

        # director_message (the 9th protected endpoint) needs CSRF
        _write_completed_parent(logs_dir, "pilot", "live-1")  # exists, owned
        r = client.post("/api/sessions/live-1/director_message",
                        json={"content": ">>halt"}, cookies={"adam_login": pilot_token})
        check("director_message without CSRF -> 403", r.status_code == 403,
              f"got {r.status_code}")

        # logout requires CSRF: without -> 403, with -> 200
        r = client.post("/api/auth/logout", cookies={"adam_login": pilot_token})
        check("logout without CSRF -> 403", r.status_code == 403, f"got {r.status_code}")
        r = client.post("/api/auth/logout", cookies=cookies, headers=headers)
        check("logout with CSRF -> 200", r.status_code == 200, f"got {r.status_code}")


def test_login_sets_csrf_and_needs_none():
    from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        app, logs_dir, _ = _make_app(tmp)
        client = TestClient(app)

        # (3) login works WITHOUT a CSRF token and SETS adam_csrf
        r = client.post("/api/auth/login",
                        json={"username": "pilot", "password": "correct-horse"})
        check("3. login works without CSRF -> 200", r.status_code == 200,
              f"got {r.status_code}")
        check("3. login sets adam_login cookie", "adam_login" in r.cookies)
        check("3. login sets adam_csrf cookie", "adam_csrf" in r.cookies)
        # the issued cookie validates against the login token
        login_tok = r.cookies.get("adam_login")
        csrf_tok = r.cookies.get("adam_csrf")
        check("3. issued CSRF cookie validates",
              csrf.validate_token(csrf_tok, csrf_tok, login_tok) is True)


# ----------------------------------------------------------------------
# 3. Login rate limiting
# ----------------------------------------------------------------------

def test_rate_limit_429_and_retry_after():
    from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        limiter = ratelimit.LoginRateLimiter(window_seconds=900,
                                             max_per_username=3, max_per_ip=100)
        app, logs_dir, _ = _make_app(tmp, limiter=limiter)
        client = TestClient(app)

        codes = []
        for _ in range(5):
            r = client.post("/api/auth/login",
                            json={"username": "pilot", "password": "WRONG"})
            codes.append(r.status_code)
        # (8) first 3 are 401, then throttled 429
        check("8. failed logins then 429", codes == [401, 401, 401, 429, 429],
              f"got {codes}")
        r = client.post("/api/auth/login", json={"username": "pilot", "password": "WRONG"})
        retry_hdr = {k.lower(): v for k, v in r.headers.items()}.get("retry-after")
        check("8. 429 has Retry-After header", retry_hdr is not None, f"headers {dict(r.headers)}")
        check("8. 429 message is generic (no enumeration)",
              r.json().get("detail") == "too many login attempts; please try again later")
        # throttled-invalid and throttled-(would-be-valid) get identical 429
        r_valid = client.post("/api/auth/login",
                              json={"username": "pilot", "password": "correct-horse"})
        check("8. throttled valid creds also 429 (same response)",
              r_valid.status_code == 429, f"got {r_valid.status_code}")


def test_rate_limit_success_resets():
    from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        limiter = ratelimit.LoginRateLimiter(window_seconds=900,
                                             max_per_username=3, max_per_ip=100)
        app, logs_dir, _ = _make_app(tmp, limiter=limiter)
        client = TestClient(app)

        client.post("/api/auth/login", json={"username": "pilot", "password": "WRONG"})
        client.post("/api/auth/login", json={"username": "pilot", "password": "WRONG"})
        r = client.post("/api/auth/login",
                        json={"username": "pilot", "password": "correct-horse"})
        check("9. successful login after 2 fails -> 200", r.status_code == 200,
              f"got {r.status_code}")
        # (9) counter reset: 3 fresh failures are allowed again (4th would 429)
        codes = [client.post("/api/auth/login",
                             json={"username": "pilot", "password": "WRONG"}).status_code
                 for _ in range(3)]
        check("9. username counter reset after success", codes == [401, 401, 401],
              f"got {codes}")


def test_rate_limit_fail_open():
    from fastapi.testclient import TestClient

    class BrokenLimiter:
        def check(self, *a, **k): raise RuntimeError("boom")
        def record_failure(self, *a, **k): raise RuntimeError("boom")
        def reset_username(self, *a, **k): raise RuntimeError("boom")

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        app, logs_dir, _ = _make_app(tmp, limiter=BrokenLimiter())
        client = TestClient(app)
        # (10) a limiter that throws must NOT block login processing
        r = client.post("/api/auth/login",
                        json={"username": "pilot", "password": "correct-horse"})
        check("10. broken limiter -> login still succeeds (fail-open)",
              r.status_code == 200, f"got {r.status_code}")
        r = client.post("/api/auth/login",
                        json={"username": "pilot", "password": "WRONG"})
        check("10. broken limiter -> bad creds still 401 (not 429/500)",
              r.status_code == 401, f"got {r.status_code}")


# ----------------------------------------------------------------------
# 4. Frontend source + built bundle assertions
# ----------------------------------------------------------------------

def test_frontend_api_js():
    api_js = GUI_ROOT / "frontend" / "src" / "lib" / "api.js"
    src = api_js.read_text(encoding="utf-8")

    check("11. api.js defines readCsrfToken", "export function readCsrfToken" in src)
    check("11. api.js has csrfHeaders helper", "function csrfHeaders" in src)
    check("11. api.js sends X-CSRF-Token", "X-CSRF-Token" in src)

    # Every mutating fetch should reference csrfHeaders. Count call sites
    # (exclude the definition + the comment line by checking >= 9 usages
    # of the form `headers: csrfHeaders`).
    n = src.count("headers: csrfHeaders")
    check("11. all 9 mutating fetches use csrfHeaders", n >= 9, f"found {n}")

    # FormData/multipart calls specifically must include it.
    for fn in ("createNewSession", "continueSession", "resumeSession"):
        idx = src.find(f"function {fn}")
        seg = src[idx: idx + 1200] if idx >= 0 else ""
        check(f"11. FormData call {fn} includes csrfHeaders",
              "headers: csrfHeaders" in seg, "not found in function body")

    # Login must NOT add the CSRF header (it's exempt).
    lidx = src.find("export async function login(")
    lseg = src[lidx: lidx + 500] if lidx >= 0 else ""
    check("11. login() is exempt from CSRF header",
          "csrfHeaders" not in lseg and "X-CSRF-Token" not in lseg)

    # Built bundle must carry the header (stale-bundle guard).
    dist_assets = GUI_ROOT / "frontend" / "dist" / "assets"
    js_files = list(dist_assets.glob("*.js")) if dist_assets.is_dir() else []
    bundle_has = any("X-CSRF-Token" in p.read_text(encoding="utf-8", errors="ignore")
                     for p in js_files)
    check("11. built dist bundle contains X-CSRF-Token", bundle_has,
          f"checked {[p.name for p in js_files]}")


def main():
    print("ADAM Pass 1 hardening tests (CSRF + rate limiting)")
    print("=" * 60)
    for t in [
        test_csrf_unit,
        test_csrf_endpoints,
        test_login_sets_csrf_and_needs_none,
        test_rate_limit_429_and_retry_after,
        test_rate_limit_success_resets,
        test_rate_limit_fail_open,
        test_frontend_api_js,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
