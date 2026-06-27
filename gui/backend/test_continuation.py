"""
Tests for the session-continuation feature (server.py).

Covers:
  - _compose_continuation_seed: the child seed is built from the
    parent's original prompt + narrative_summary + open questions/risks
    + the follow-up, in that order.
  - _load_parent_for_continuation: reads a real session_state.json
    (including the artifact pointer) and falls back to seed.md.
  - POST /api/sessions/{parent_id}/continue (end-to-end via TestClient):
      * requires auth (401 without a valid session cookie)
      * 404 for a parent that isn't the user's
      * a pilot continuation creates a child session, records
        parent_session_id in .process_info.json, and DECREMENTS the
        pilot's sessions_remaining by exactly one
      * the child's composed seed.md contains the parent's prompt, the
        prior result, and the follow-up
      * the per-role turn clamp is applied (pilot's submitted max_turns
        is ignored in favor of users.json max_turns_per_session)

The ADAM subprocess is never actually run: spawn_adam_session is
monkeypatched to a stub that performs the on-disk session-dir setup we
assert against, without launching a real deliberation.

Run:  python3 gui/backend/test_continuation.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent           # .../gui/backend
GUI_ROOT = HERE.parent                            # .../gui
PROJ_ROOT = GUI_ROOT.parent                       # project root
sys.path.insert(0, str(GUI_ROOT))                 # so `import backend...` / server works
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


# Import the server module normally (as the `backend.server` package
# member) so pydantic response models resolve their forward references
# (e.g. List[...]) against the real module globals. Importing via
# spec_from_file_location under a synthetic name breaks that resolution.
from backend import server  # noqa: E402


# ----------------------------------------------------------------------
# Fixtures: a realistic parent session_state.json
# ----------------------------------------------------------------------

PARENT_STATE = {
    "schema_version": "1.0",
    "governance_state": {
        "seed": "create a plan for implementing AI into K-5 instruction. the 5 c's should be included. create a word document with the plan",
        "ratified_decisions": [],
    },
    "deliberation_state": {
        "final_synthesis_text": "The session produced a complete plan...",
        "open_questions": ["Which 5 C's variant does the district prefer?"],
        "notable_risks": ["COPPA compliance pending for some tools"],
    },
    "operator_summary": {
        "narrative_summary": "This session developed a comprehensive K-5 AI plan around the 5 C's, defaulting to Computational Thinking as the fifth C with a substitution note.",
        "open_questions": [],
        "next_session_recommendation": "Confirm the 5 C's variant.",
        "notable_risks": [],
        "summary_quality": "good",
        "source": "operator_wrap_up",
    },
    "skill_state": {
        "skills_enabled": True,
        "invocations": [
            {
                "status": "success",
                "filename": "K5_AI_Integration_Plan.docx",
                "artifact_id": "1f01ecae-6940-49c9-874c-86323d72ec45",
                "format": "docx",
                "turn": 10,
                "caller": "Operator",
            }
        ],
        "summary": {"total": 1, "successes": 1, "failures": 0},
    },
}


def write_parent_session(user_dir: Path, session_id: str) -> Path:
    sdir = user_dir / session_id
    (sdir / "artifacts").mkdir(parents=True, exist_ok=True)
    (sdir / "session_state.json").write_text(json.dumps(PARENT_STATE), encoding="utf-8")
    (sdir / "seed.md").write_text(PARENT_STATE["governance_state"]["seed"] + "\n", encoding="utf-8")
    # A tiny stand-in docx (content irrelevant for these tests).
    (sdir / "artifacts" / "K5_AI_Integration_Plan.docx").write_bytes(b"PK\x03\x04 fake docx bytes")
    return sdir


# ----------------------------------------------------------------------
# 1. Pure helper tests
# ----------------------------------------------------------------------

def test_compose_seed_structure():
    parent = {
        "original_prompt": "ORIGINAL TASK TEXT",
        "prior_result":    "PRIOR RESULT TEXT",
        "open_questions":  ["Q1", "Q2"],
        "notable_risks":   ["R1"],
        "artifact":        {"filename": "plan.docx", "artifact_id": "abc", "session_relative": "artifacts/plan.docx"},
    }
    seed = server._compose_continuation_seed(parent, "DO THE FOLLOW-UP")

    check("seed includes original prompt", "ORIGINAL TASK TEXT" in seed)
    check("seed includes prior result", "PRIOR RESULT TEXT" in seed)
    check("seed includes open questions", "Q1" in seed and "Q2" in seed)
    check("seed includes notable risks", "R1" in seed)
    check("seed mentions the prior artifact", "plan.docx" in seed)
    check("seed includes the follow-up", "DO THE FOLLOW-UP" in seed)

    # Ordering: original task appears before prior result before follow-up.
    i_orig = seed.index("ORIGINAL TASK TEXT")
    i_prior = seed.index("PRIOR RESULT TEXT")
    i_follow = seed.index("DO THE FOLLOW-UP")
    check("seed order: original < prior < follow-up",
          i_orig < i_prior < i_follow,
          f"orig={i_orig} prior={i_prior} follow={i_follow}")


def test_load_parent_from_state():
    with tempfile.TemporaryDirectory() as tmp:
        user_dir = Path(tmp) / "pilotuser"
        sdir = write_parent_session(user_dir, "parent-123")
        parent = server._load_parent_for_continuation(sdir)

        check("loaded original prompt from governance_state.seed",
              "K-5 instruction" in parent["original_prompt"])
        check("loaded prior_result from operator_summary.narrative_summary",
              "Computational Thinking" in parent["prior_result"])
        check("loaded open_questions",
              parent["open_questions"] == ["Which 5 C's variant does the district prefer?"])
        check("loaded notable_risks",
              parent["notable_risks"] == ["COPPA compliance pending for some tools"])
        check("loaded artifact pointer (docx)",
              parent["artifact"] and parent["artifact"]["filename"] == "K5_AI_Integration_Plan.docx")


def test_load_parent_seed_fallback():
    # No session_state.json -> original prompt comes from seed.md.
    with tempfile.TemporaryDirectory() as tmp:
        sdir = Path(tmp) / "pilotuser" / "parent-nofinal"
        sdir.mkdir(parents=True)
        (sdir / "seed.md").write_text("RAW SEED PROMPT ONLY\n", encoding="utf-8")
        parent = server._load_parent_for_continuation(sdir)
        check("seed.md fallback supplies original_prompt",
              parent["original_prompt"] == "RAW SEED PROMPT ONLY")
        check("no artifact when session never completed",
              parent["artifact"] is None)


# ----------------------------------------------------------------------
# 2. End-to-end endpoint test
# ----------------------------------------------------------------------

def _make_app_with_pilot(tmp: Path):
    """Build a real app against a temp logs/gui dir, with a real pilot
    user + a real login session so the auth cookie validates."""
    logs_dir = tmp / "logs"
    gui_root = tmp / "gui"
    logs_dir.mkdir(parents=True, exist_ok=True)
    gui_root.mkdir(parents=True, exist_ok=True)

    # build_app calls auth.init_auth(gui_root). We pre-init against the
    # SAME gui_root and add a pilot so the user store is shared.
    from backend import auth
    auth.init_auth(gui_root)
    auth.add_user(
        username="pilotuser", display_name="Pilot User",
        email="p@example.com", role="pilot", password="x" * 10,
        status="active", sessions_remaining=3, max_turns_per_session=10,
    )
    token = auth.create_login_session("pilotuser")

    # adam_root is where build_app expects gui/ to live. We point it at
    # tmp and ensure tmp/gui exists (done above).
    app = server.build_app(adam_root=tmp, logs_dir=logs_dir)
    return app, logs_dir, token, auth


def test_continue_endpoint_e2e():
    try:
        from fastapi.testclient import TestClient
    except Exception as e:
        check("fastapi TestClient available", False, str(e))
        return

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        app, logs_dir, token, auth = _make_app_with_pilot(tmp)

        # Seed a completed parent session under the pilot's logs dir.
        user_dir = logs_dir / "pilotuser"
        write_parent_session(user_dir, "parent-123")

        # Stub spawn so no real ADAM subprocess launches. The stub does
        # the part we assert on: create the child dir, write seed.md and
        # .process_info.json (with parent_session_id), return a result.
        spawned = {}

        def fake_spawn(*, adam_root, logs_dir, user_id, display_name, email,
                       seed_text, context_files, max_turns, no_verify,
                       disable_skills=None, parent_session_id=None,
                       governance_profile_id=None):
            child_id = "child-456"
            cdir = logs_dir / user_id / child_id
            (cdir / "input_context").mkdir(parents=True, exist_ok=True)
            (cdir / "seed.md").write_text(seed_text, encoding="utf-8")
            proc_info = {
                "pid": 99999, "status": "starting",
                "parent_session_id": parent_session_id,
            }
            (cdir / ".process_info.json").write_text(json.dumps(proc_info), encoding="utf-8")
            spawned.update(
                seed_text=seed_text, max_turns=max_turns,
                parent_session_id=parent_session_id,
                context_files=[c["filename"] for c in context_files],
            )
            return {
                "session_id": child_id, "started_at": "2026-06-17T00:00:00",
                "pid": 99999, "session_dir": str(cdir),
                "seed_path": str(cdir / "seed.md"),
                "context_files": [c["filename"] for c in context_files],
                "status": "starting",
            }

        _orig_spawn_1 = server.spawn_adam_session
        server.spawn_adam_session = fake_spawn  # monkeypatch

        client = TestClient(app)
        # Pass 1 hardening: authenticated mutating requests now require a
        # signed CSRF token (cookie + matching X-CSRF-Token header). Mint
        # one bound to this login token; build_app already called
        # csrf.init_csrf so the secret is set.
        csrf_token = server.csrf.issue_token(token)
        cookies = {"adam_login": token, "adam_csrf": csrf_token}
        csrf_headers = {"X-CSRF-Token": csrf_token}

        # --- 401 without auth (no cookie at all) ---
        r_noauth = client.post(
            "/api/sessions/parent-123/continue",
            data={"seed": "add a board version"},
        )
        check("401 without auth cookie", r_noauth.status_code == 401,
              f"got {r_noauth.status_code}")

        # --- 404 for a parent that isn't theirs ---
        r_404 = client.post(
            "/api/sessions/does-not-exist/continue",
            data={"seed": "add a board version"},
            cookies=cookies,
            headers=csrf_headers,
        )
        check("404 for unknown parent", r_404.status_code == 404,
              f"got {r_404.status_code}")

        # --- happy path ---
        before = auth.get_user("pilotuser")["sessions_remaining"]
        r = client.post(
            "/api/sessions/parent-123/continue",
            data={"seed": "redo using Citizenship as the 5th C", "max_turns": "200"},
            cookies=cookies,
            headers=csrf_headers,
        )
        check("continue returns 200", r.status_code == 200,
              f"got {r.status_code}: {r.text[:200]}")

        if r.status_code == 200:
            body = r.json()
            check("child session_id returned", body.get("session_id") == "child-456")

            # Composed seed carries parent prompt + prior result + follow-up.
            check("composed seed has original prompt",
                  "K-5 instruction" in spawned["seed_text"])
            check("composed seed has prior result",
                  "Computational Thinking" in spawned["seed_text"])
            check("composed seed has follow-up",
                  "Citizenship as the 5th C" in spawned["seed_text"])

            # Parent .docx carried into context for revision.
            check("parent docx copied into child context",
                  "K5_AI_Integration_Plan.docx" in spawned["context_files"])

            # Lineage recorded.
            check("parent_session_id passed to spawn",
                  spawned["parent_session_id"] == "parent-123")

            # Turn clamp: pilot submitted 200, must be clamped to 10.
            check("pilot turn clamp applied (200 -> 10)",
                  spawned["max_turns"] == 10, f"got {spawned['max_turns']}")

            # Quota decremented by exactly one.
            after = auth.get_user("pilotuser")["sessions_remaining"]
            check("pilot sessions_remaining decremented by 1",
                  after == before - 1, f"{before} -> {after}")

            # The summary surfaces lineage + full prompt.
            child_dir = logs_dir / "pilotuser" / "child-456"
            summary = server._summarize_session(child_dir)
            check("summary surfaces parent_session_id",
                  summary.get("parent_session_id") == "parent-123")

            parent_dir = logs_dir / "pilotuser" / "parent-123"
            psum = server._summarize_session(parent_dir)
            check("parent summary exposes full prompt",
                  psum.get("prompt_full") and "K-5 instruction" in psum["prompt_full"])

        server.spawn_adam_session = _orig_spawn_1


def test_governance_loader():
    from backend import governance
    with tempfile.TemporaryDirectory() as tmp:
        gui_root = Path(tmp) / "gui"
        gui_root.mkdir(parents=True)
        # Write a minimal governance.json
        (gui_root / "governance.json").write_text(json.dumps({
            "policy_bounds": {
                "standard": {"id": "standard", "name": "Standard", "blocked_skills": ["email"]},
                "strict":   {"id": "strict", "name": "Strict", "blocked_skills": ["email", "coder"]},
            },
            "governance_profiles": {
                "general": {"id": "general", "name": "General", "policy_bounds_id": "standard", "human_review_mode": "none"},
                "locked":  {"id": "locked", "name": "Locked", "policy_bounds_id": "strict", "human_review_mode": "required"},
            },
            "default_profile_id": "general",
        }), encoding="utf-8")
        governance.init_governance(gui_root)

        check("default_profile_id reads from config",
              governance.default_profile_id() == "general")
        check("resolve_profile_id passes through a known id",
              governance.resolve_profile_id("locked") == "locked")
        check("resolve_profile_id falls back to default for unknown",
              governance.resolve_profile_id("nope") == "general")
        check("resolve_profile_id falls back to default for None",
              governance.resolve_profile_id(None) == "general")
        check("get_policy_bounds follows the profile's reference",
              governance.get_policy_bounds("locked").get("id") == "strict")
        check("get_policy_bounds for general -> standard",
              governance.get_policy_bounds("general").get("id") == "standard")


def test_governance_missing_file_safe():
    from backend import governance
    with tempfile.TemporaryDirectory() as tmp:
        gui_root = Path(tmp) / "gui"
        gui_root.mkdir(parents=True)
        # No governance.json at all.
        governance.init_governance(gui_root)
        check("missing config -> fallback default id is 'general'",
              governance.resolve_profile_id(None) == "general")
        check("missing config -> get_profile returns a usable fallback",
              governance.get_profile(None).get("id") == "general")
        check("missing config -> get_policy_bounds returns standard fallback",
              governance.get_policy_bounds(None).get("id") == "standard")


def test_governance_attached_and_inherited():
    """A fresh session records the default profile; a continuation
    inherits the parent's profile unless overridden."""
    try:
        from fastapi.testclient import TestClient
    except Exception as e:
        check("fastapi TestClient available", False, str(e))
        return

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        app, logs_dir, token, auth = _make_app_with_pilot(tmp)

        # Write a governance.json into the app's gui_root so the server's
        # init_governance picked it up. _make_app_with_pilot built the app
        # already, so re-init governance against that same gui_root.
        from backend import governance
        gui_root = tmp / "gui"
        (gui_root / "governance.json").write_text(json.dumps({
            "policy_bounds": {
                "standard": {"id": "standard", "name": "Standard"},
                "strict":   {"id": "strict", "name": "Strict"},
            },
            "governance_profiles": {
                "general": {"id": "general", "name": "General", "policy_bounds_id": "standard"},
                "locked":  {"id": "locked", "name": "Locked", "policy_bounds_id": "strict"},
            },
            "default_profile_id": "general",
        }), encoding="utf-8")
        governance.init_governance(gui_root)

        user_dir = logs_dir / "pilotuser"
        write_parent_session(user_dir, "parent-123")

        spawned = {}

        def fake_spawn(*, adam_root, logs_dir, user_id, display_name, email,
                       seed_text, context_files, max_turns, no_verify,
                       disable_skills=None, parent_session_id=None,
                       governance_profile_id=None):
            child_id = f"child-{len(spawned)}"
            cdir = logs_dir / user_id / child_id
            (cdir / "input_context").mkdir(parents=True, exist_ok=True)
            (cdir / "seed.md").write_text(seed_text, encoding="utf-8")
            resolved = governance.resolve_profile_id(governance_profile_id)
            (cdir / ".process_info.json").write_text(json.dumps({
                "pid": 1, "status": "starting",
                "parent_session_id": parent_session_id,
                "governance_profile_id": resolved,
            }), encoding="utf-8")
            spawned[child_id] = {"requested_profile": governance_profile_id, "resolved": resolved}
            return {
                "session_id": child_id, "started_at": "2026-06-17T00:00:00",
                "pid": 1, "session_dir": str(cdir),
                "seed_path": str(cdir / "seed.md"), "context_files": [],
                "status": "starting",
            }

        _orig_spawn_2 = server.spawn_adam_session
        server.spawn_adam_session = fake_spawn
        client = TestClient(app)
        # Pass 1 hardening: CSRF token bound to this login session.
        csrf_token = server.csrf.issue_token(token)
        cookies = {"adam_login": token, "adam_csrf": csrf_token}
        csrf_headers = {"X-CSRF-Token": csrf_token}

        # Fresh session, no profile specified -> default "general".
        r1 = client.post("/api/sessions", data={"seed": "do a thing"},
                         cookies=cookies, headers=csrf_headers)
        check("fresh session spawn 201", r1.status_code == 201, r1.text[:200])
        last = sorted(spawned.keys())[-1]
        check("fresh session gets default profile (general)",
              spawned[last]["resolved"] == "general",
              f"got {spawned[last]['resolved']}")

        # Make the parent carry a non-default profile, then continue it.
        parent_dir = user_dir / "parent-123"
        (parent_dir / ".process_info.json").write_text(json.dumps({
            "pid": 1, "status": "complete",
            "governance_profile_id": "locked",
        }), encoding="utf-8")

        r2 = client.post("/api/sessions/parent-123/continue",
                         data={"seed": "follow up"}, cookies=cookies,
                         headers=csrf_headers)
        check("continuation spawn 200", r2.status_code == 200, r2.text[:200])
        last = sorted(spawned.keys())[-1]
        check("continuation INHERITS parent's profile (locked)",
              spawned[last]["resolved"] == "locked",
              f"got {spawned[last]['resolved']}")

        # Continue again, this time OVERRIDING the profile.
        r3 = client.post("/api/sessions/parent-123/continue",
                         data={"seed": "follow up 2", "governance_profile_id": "general"},
                         cookies=cookies, headers=csrf_headers)
        check("continuation override spawn 200", r3.status_code == 200, r3.text[:200])
        last = sorted(spawned.keys())[-1]
        check("continuation OVERRIDE wins over inheritance (general)",
              spawned[last]["resolved"] == "general",
              f"got {spawned[last]['resolved']}")

        server.spawn_adam_session = _orig_spawn_2


def test_policy_denied_skills_resolver():
    from backend import governance
    with tempfile.TemporaryDirectory() as tmp:
        gui_root = Path(tmp) / "gui"
        gui_root.mkdir(parents=True)
        (gui_root / "governance.json").write_text(json.dumps({
            "policy_bounds": {
                "permissive": {"id": "permissive", "name": "Permissive"},  # no lists
                "blocklist":  {"id": "blocklist", "name": "Blocklist", "blocked_skills": ["email"]},
                "allowlist":  {"id": "allowlist", "name": "Allowlist",
                               "allowed_skills": ["document", "websearch"]},
            },
            "governance_profiles": {
                "p_permissive": {"id": "p_permissive", "name": "P", "policy_bounds_id": "permissive"},
                "p_block":      {"id": "p_block", "name": "B", "policy_bounds_id": "blocklist"},
                "p_allow":      {"id": "p_allow", "name": "A", "policy_bounds_id": "allowlist"},
            },
            "default_profile_id": "p_permissive",
        }), encoding="utf-8")
        governance.init_governance(gui_root)
        universe = ["coder", "document", "email", "slidedeck", "websearch"]

        check("permissive ruleset (no lists) denies nothing",
              governance.policy_denied_skills("p_permissive", universe) == [])
        check("blocklist denies exactly the blocked skill",
              governance.policy_denied_skills("p_block", universe) == ["email"])
        # allow-list => default-deny everything not allowed
        denied = governance.policy_denied_skills("p_allow", universe)
        check("allowlist denies everything not on the allow-list",
              set(denied) == {"coder", "email", "slidedeck"},
              f"got {denied}")
        check("allowlist keeps allowed skills available",
              "document" not in denied and "websearch" not in denied)


def test_policy_union_with_role_at_spawn():
    """The spawn path disables the UNION of role denials (disable_skills)
    and policy denials (from the profile's bounds). Tested by calling
    spawn_adam_session directly with subprocess.Popen patched, capturing
    the --disable-skill flags it builds."""
    from backend import governance

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        gui_root = tmp / "gui"
        gui_root.mkdir(parents=True)
        (gui_root / "governance.json").write_text(json.dumps({
            "policy_bounds": {
                "edu": {"id": "edu", "name": "Edu",
                        "allowed_skills": ["document", "slidedeck", "websearch"]},
            },
            "governance_profiles": {
                "education": {"id": "education", "name": "Education", "policy_bounds_id": "edu"},
            },
            "default_profile_id": "education",
        }), encoding="utf-8")
        governance.init_governance(gui_root)

        # Real skills/ dir so _discover_skill_universe finds the universe.
        for s in ["coder", "document", "email", "slidedeck", "test_echo", "websearch"]:
            (tmp / "skills" / s).mkdir(parents=True, exist_ok=True)

        logs_dir = tmp / "logs"
        logs_dir.mkdir()

        captured = {}
        class FakePopen:
            def __init__(self, cmd, **kw):
                captured["cmd"] = cmd
                self.pid = 4242
            def poll(self): return None
        orig = server.subprocess.Popen
        server.subprocess.Popen = FakePopen
        try:
            server.spawn_adam_session(
                adam_root=tmp, logs_dir=logs_dir,
                user_id="pilotuser", display_name="Pilot", email="p@x.com",
                seed_text="x", context_files=[], max_turns=10, no_verify=True,
                disable_skills=["email"],            # role denial (pilot)
                governance_profile_id="education",   # policy denials: coder/email/test_echo
            )
        finally:
            server.subprocess.Popen = orig

        cmd = captured.get("cmd", [])
        disabled = [cmd[i+1] for i, tok in enumerate(cmd) if tok == "--disable-skill"]
        check("union disables coder (policy only)", "coder" in disabled, f"got {disabled}")
        check("union disables email (role+policy)", "email" in disabled, f"got {disabled}")
        check("union disables test_echo (policy only)", "test_echo" in disabled, f"got {disabled}")
        check("allowed skills NOT disabled",
              not any(s in disabled for s in ["document", "slidedeck", "websearch"]),
              f"got {disabled}")


def main():
    print("ADAM session-continuation tests")
    print("=" * 60)
    for t in [
        test_compose_seed_structure,
        test_load_parent_from_state,
        test_load_parent_seed_fallback,
        test_continue_endpoint_e2e,
        test_governance_loader,
        test_governance_missing_file_safe,
        test_governance_attached_and_inherited,
        test_policy_denied_skills_resolver,
        test_policy_union_with_role_at_spawn,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
