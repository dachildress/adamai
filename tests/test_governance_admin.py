"""
Tests for Slice 4.2 Phase 1 — governance admin view + validation.

Covers:
  - validate_governance_data(): accepts valid config, rejects bad refs/skills
  - get_admin_view(): plain-language summaries and field metadata
  - GET /api/admin/governance: admin-only read endpoint
  - POST /api/admin/governance/validate: admin-only dry-run validation

Run: python tests/test_governance_admin.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUI_ROOT = ROOT / "gui"
sys.path.insert(0, str(GUI_ROOT))
sys.path.insert(0, str(GUI_ROOT / "backend"))

PASSED = 0
FAILED = 0
SKIPPED = 0


def skip(name, reason):
    global SKIPPED
    SKIPPED += 1
    print(f"  SKIP  {name}  -- {reason}")


def auth_available() -> bool:
    try:
        import fcntl  # noqa: F401
        return True
    except ImportError:
        return False

SKILLS = ["coder", "document", "email", "slidedeck", "test_echo", "websearch"]

VALID_CONFIG = {
    "schema_version": "1.0",
    "policy_bounds": {
        "standard": {
            "id": "standard",
            "name": "Standard",
            "allowed_skills": SKILLS,
            "blocked_skills": [],
            "external_actions_allowed": True,
            "email_send_allowed": True,
            "file_write_allowed": True,
        },
        "strict": {
            "id": "strict",
            "name": "Strict",
            "allowed_skills": ["document", "websearch"],
            "blocked_skills": ["email", "coder"],
            "external_actions_allowed": False,
            "email_send_allowed": False,
            "file_write_allowed": False,
        },
    },
    "governance_profiles": {
        "general": {
            "id": "general",
            "name": "General",
            "policy_bounds_id": "standard",
            "human_review_mode": "none",
            "review_required_for": [],
        },
        "education": {
            "id": "education",
            "name": "Education",
            "policy_bounds_id": "strict",
            "human_review_mode": "conditional",
            "review_required_for": ["public_facing_artifact"],
        },
    },
    "default_profile_id": "general",
}


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}" + (f"  -- {detail}" if detail else ""))


def test_validate_accepts_valid():
    from backend import governance
    result = governance.validate_governance_data(VALID_CONFIG, SKILLS)
    check("valid config passes", result["valid"] is True)
    check("no errors on valid config", len(result["errors"]) == 0)


def test_validate_rejects_bad_ruleset_ref():
    from backend import governance
    bad = json.loads(json.dumps(VALID_CONFIG))
    bad["governance_profiles"]["education"]["policy_bounds_id"] = "missing"
    result = governance.validate_governance_data(bad, SKILLS)
    check("bad ruleset ref fails", result["valid"] is False)
    check("error mentions policy_bounds_id",
          any("policy_bounds_id" in e for e in result["errors"]))


def test_validate_rejects_unknown_skill():
    from backend import governance
    bad = json.loads(json.dumps(VALID_CONFIG))
    bad["policy_bounds"]["standard"]["blocked_skills"] = ["not_a_skill"]
    result = governance.validate_governance_data(bad, SKILLS)
    check("unknown skill fails", result["valid"] is False)
    check("error mentions unknown skill",
          any("not_a_skill" in e for e in result["errors"]))


def test_validate_rejects_overlap():
    from backend import governance
    bad = json.loads(json.dumps(VALID_CONFIG))
    bad["policy_bounds"]["standard"]["allowed_skills"] = ["document"]
    bad["policy_bounds"]["standard"]["blocked_skills"] = ["document"]
    result = governance.validate_governance_data(bad, SKILLS)
    check("allowed+blocked overlap fails", result["valid"] is False)


def test_validate_rejects_bad_review_mode():
    from backend import governance
    bad = json.loads(json.dumps(VALID_CONFIG))
    bad["governance_profiles"]["general"]["human_review_mode"] = "always"
    result = governance.validate_governance_data(bad, SKILLS)
    check("bad review mode fails", result["valid"] is False)


def test_admin_view_summaries():
    from backend import governance
    with tempfile.TemporaryDirectory() as tmp:
        gui_root = Path(tmp)
        (gui_root / "governance.json").write_text(
            json.dumps(VALID_CONFIG), encoding="utf-8"
        )
        governance.init_governance(gui_root)
        view = governance.get_admin_view(SKILLS)
        check("admin view has profiles", len(view["governance_profiles"]) == 2)
        check("admin view has rulesets", len(view["policy_bounds"]) == 2)
        check("profile summary mentions ruleset",
              "strict" in view["governance_profiles"][1]["summary"])
        check("field enforcement metadata present",
              "policy_bounds" in view["field_enforcement"])
        check("education denied skills include coder",
              "coder" in view["governance_profiles"][1]["denied_skills"])
        check("edit_enabled true in phase 2", view["edit_enabled"] is True)
        check("config export present", "config" in view and "policy_bounds" in view["config"])


def test_normalize_strips_empty_allowlist():
    from backend import governance
    cfg = json.loads(json.dumps(VALID_CONFIG))
    cfg["policy_bounds"]["standard"]["allowed_skills"] = []
    normalized = governance.normalize_governance_data(cfg)
    check("empty allowed_skills omitted",
          "allowed_skills" not in normalized["policy_bounds"]["standard"])


def test_save_and_reload():
    from backend import governance
    with tempfile.TemporaryDirectory() as tmp:
        gui_root = Path(tmp)
        path = gui_root / "governance.json"
        path.write_text(json.dumps(VALID_CONFIG), encoding="utf-8")
        governance.init_governance(gui_root)
        modified = json.loads(json.dumps(VALID_CONFIG))
        modified["governance_profiles"]["general"]["human_review_mode"] = "conditional"
        modified["governance_profiles"]["general"]["review_required_for"] = ["email_send"]
        result = governance.save_governance_data(modified, SKILLS)
        check("save returns valid", result["valid"] is True)
        reloaded = json.loads(path.read_text(encoding="utf-8"))
        check("save persisted review mode",
              reloaded["governance_profiles"]["general"]["human_review_mode"] == "conditional")
        check("in-memory cache updated",
              governance.get_profile("general").get("human_review_mode") == "conditional")


def test_save_rejects_invalid():
    from backend import governance
    with tempfile.TemporaryDirectory() as tmp:
        gui_root = Path(tmp)
        (gui_root / "governance.json").write_text(json.dumps(VALID_CONFIG), encoding="utf-8")
        governance.init_governance(gui_root)
        bad = json.loads(json.dumps(VALID_CONFIG))
        bad["default_profile_id"] = "missing"
        try:
            governance.save_governance_data(bad, SKILLS)
            check("invalid save raises", False)
        except ValueError as e:
            check("invalid save raises ValueError", True)
            check("error list non-empty", len(e.args[0]) > 0)


def test_user_governance_assignment():
    if not auth_available():
        skip("user governance assignment (fcntl)", "POSIX only")
        return
    from backend import auth, governance
    with tempfile.TemporaryDirectory() as tmp:
        gui_root = Path(tmp)
        (gui_root / "governance.json").write_text(json.dumps(VALID_CONFIG), encoding="utf-8")
        governance.init_governance(gui_root)
        auth.init_auth(gui_root)
        auth.add_user(
            username="pilot1", display_name="Pilot One",
            email="p1@example.com", role="pilot", password="x" * 10,
            status="active", sessions_remaining=3, max_turns_per_session=10,
        )
        auth.add_user(
            username="admin1", display_name="Admin One",
            email="a1@example.com", role="admin", password="x" * 10,
            status="active", sessions_remaining=-1, max_turns_per_session=-1,
        )
        auth.set_user_governance_profile("pilot1", "education")
        pilot = auth.get_user("pilot1")
        check("pilot assigned profile stored",
              pilot.get("governance_profile") == "education")
        check("assigned_governance_profile returns education",
              auth.assigned_governance_profile(pilot) == "education")
        check("pilot is quota locked", auth.is_quota_locked_user(pilot) is True)
        check("admin is not quota locked",
              auth.is_quota_locked_user(auth.get_user("admin1")) is False)
        summaries = auth.admin_user_summaries()
        check("admin summaries include both users", len(summaries) == 2)
        pilot_row = next(u for u in summaries if u["username"] == "pilot1")
        check("pilot row shows locked", pilot_row["governance_profile_locked"] is True)
        check("pilot row effective profile",
              pilot_row["effective_assigned_profile"] == "education")
        auth.set_user_governance_profile("pilot1", None)
        pilot2 = auth.get_user("pilot1")
        check("cleared assignment removed field",
              "governance_profile" not in pilot2 or pilot2.get("governance_profile") is None)


def _make_app_with_users(tmp: Path):
    if not auth_available():
        return None, None, None
    from backend import auth, server
    logs_dir = tmp / "logs"
    gui_root = tmp / "gui"
    logs_dir.mkdir(parents=True)
    gui_root.mkdir(parents=True)
    auth.init_auth(gui_root)
    auth.add_user(
        username="adminuser", display_name="Admin",
        email="a@example.com", role="admin", password="x" * 10,
        status="active", sessions_remaining=-1, max_turns_per_session=-1,
    )
    auth.add_user(
        username="pilotuser", display_name="Pilot",
        email="p@example.com", role="pilot", password="x" * 10,
        status="active", sessions_remaining=3, max_turns_per_session=10,
    )
    admin_token = auth.create_login_session("adminuser")
    pilot_token = auth.create_login_session("pilotuser")
    (gui_root / "governance.json").write_text(json.dumps(VALID_CONFIG), encoding="utf-8")
    # skills dir for universe discovery
    skills_dir = tmp / "skills"
    for s in SKILLS:
        (skills_dir / s).mkdir(parents=True, exist_ok=True)
    app = server.build_app(adam_root=tmp, logs_dir=logs_dir)
    return app, admin_token, pilot_token


def test_admin_api_endpoints():
    if not auth_available():
        skip("admin API endpoints (fcntl)", "POSIX only")
        return
    try:
        from fastapi.testclient import TestClient
    except Exception as e:
        skip("fastapi TestClient", str(e))
        return

    with tempfile.TemporaryDirectory() as tmp:
        result = _make_app_with_users(Path(tmp))
        if result[0] is None:
            skip("admin API endpoints", "auth unavailable")
            return
        app, admin_token, pilot_token = result
        client = TestClient(app)

        # Pass 1 hardening: mutating admin endpoints now require a signed
        # CSRF token bound to the login session. GETs are exempt.
        from backend import csrf
        admin_csrf = csrf.issue_token(admin_token)
        acookies = {"adam_login": admin_token, "adam_csrf": admin_csrf}
        aheaders = {"X-CSRF-Token": admin_csrf}

        r_anon = client.get("/api/admin/governance")
        check("anon gets 401", r_anon.status_code == 401)

        # CSRF: an authenticated admin PUT with NO token is rejected 403.
        r_nocsrf = client.put(
            "/api/admin/governance",
            cookies={"adam_login": admin_token},
            json=VALID_CONFIG,
        )
        check("admin mutating without CSRF gets 403",
              r_nocsrf.status_code == 403, f"got {r_nocsrf.status_code}")

        r_pilot = client.get(
            "/api/admin/governance",
            cookies={"adam_login": pilot_token},
        )
        check("pilot gets 403", r_pilot.status_code == 403)

        r_admin = client.get(
            "/api/admin/governance",
            cookies={"adam_login": admin_token},
        )
        check("admin gets 200", r_admin.status_code == 200, r_admin.text[:200])
        if r_admin.status_code == 200:
            body = r_admin.json()
            check("admin response has profiles",
                  len(body.get("governance_profiles", [])) == 2)
            check("admin response validation valid",
                  body.get("validation", {}).get("valid") is True)

        r_val = client.post(
            "/api/admin/governance/validate",
            cookies=acookies,
            headers=aheaders,
            json=VALID_CONFIG,
        )
        check("validate endpoint 200", r_val.status_code == 200)
        if r_val.status_code == 200:
            check("validate returns valid true", r_val.json().get("valid") is True)

        bad = json.loads(json.dumps(VALID_CONFIG))
        bad["default_profile_id"] = "nope"
        r_bad = client.post(
            "/api/admin/governance/validate",
            cookies=acookies,
            headers=aheaders,
            json=bad,
        )
        check("validate bad config returns valid false",
              r_bad.status_code == 200 and r_bad.json().get("valid") is False)

        r_put = client.put(
            "/api/admin/governance",
            cookies=acookies,
            headers=aheaders,
            json=VALID_CONFIG,
        )
        check("put valid config 200", r_put.status_code == 200, r_put.text[:200])

        modified = json.loads(json.dumps(VALID_CONFIG))
        modified["default_profile_id"] = "nope"
        r_put_bad = client.put(
            "/api/admin/governance",
            cookies=acookies,
            headers=aheaders,
            json=modified,
        )
        check("put invalid config 400", r_put_bad.status_code == 400)

        r_users = client.get(
            "/api/admin/users",
            cookies={"adam_login": admin_token},
        )
        check("admin users 200", r_users.status_code == 200)
        if r_users.status_code == 200:
            check("users list has pilot",
                  any(u["username"] == "pilotuser" for u in r_users.json().get("users", [])))

        r_patch = client.patch(
            "/api/admin/users/pilotuser/governance-profile",
            cookies=acookies,
            headers=aheaders,
            json={"governance_profile": "education"},
        )
        check("patch pilot profile 200", r_patch.status_code == 200, r_patch.text[:200])
        if r_patch.status_code == 200:
            check("patch returns education",
                  r_patch.json().get("governance_profile") == "education")

        r_patch_bad = client.patch(
            "/api/admin/users/pilotuser/governance-profile",
            cookies=acookies,
            headers=aheaders,
            json={"governance_profile": "not_a_profile"},
        )
        check("patch unknown profile 400", r_patch_bad.status_code == 400)


def test_education_profile_carries_data_intelligence_block():
    # Agent Data Intelligence (Phase 4): the shipped education profile carries a
    # data_intelligence block that resolves to an enabled, aggregate-only scope;
    # general has none (disabled); and the block doesn't break validation.
    sys.path.insert(0, str(ROOT))
    from backend import governance
    from adam.pipeline import DataScope
    governance.init_governance(GUI_ROOT)

    block = governance.get_profile("education").get("data_intelligence")
    check("education profile has a data_intelligence block",
          isinstance(block, dict) and block.get("enabled") is True, str(block))
    ds = DataScope.from_block(block)
    check("block -> enabled DataScope", ds.enabled is True)
    check("allows the sample source", ds.permits_source("adam-test-mysql-v1"))
    check("aggregate-only by default", ds.aggregate_only is True)
    check("denies student PII + guardian wildcard",
          "students.first_name" in ds.denied_fields and "guardians.*" in ds.denied_fields)

    gen = governance.get_profile("general")
    check("general profile has no data_intelligence block", "data_intelligence" not in gen)
    check("general -> disabled DataScope",
          DataScope.from_block(gen.get("data_intelligence")).enabled is False)

    data = json.loads((GUI_ROOT / "governance.json").read_text(encoding="utf-8"))
    # data_intelligence is a real skill in skills/, so it's part of the live
    # skill universe (education_safe now permits it in allowed_skills).
    result = governance.validate_governance_data(data, SKILLS + ["data_intelligence"])
    check("governance.json with data_intelligence still validates",
          result["valid"] is True, str(result.get("errors")))


def main():
    print("test_governance_admin.py")
    print("=" * 60)
    test_validate_accepts_valid()
    test_validate_rejects_bad_ruleset_ref()
    test_validate_rejects_unknown_skill()
    test_validate_rejects_overlap()
    test_validate_rejects_bad_review_mode()
    test_normalize_strips_empty_allowlist()
    test_admin_view_summaries()
    test_save_and_reload()
    test_save_rejects_invalid()
    test_user_governance_assignment()
    test_admin_api_endpoints()
    test_education_profile_carries_data_intelligence_block()
    print("=" * 60)
    print(f"Results: {PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
