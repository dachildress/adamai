"""
Fail-closed spawn governance + /api/health (protected profiles).

Run: python tests/test_spawn_governance_failclosed.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
GUI_ROOT = ROOT / "gui"
sys.path.insert(0, str(GUI_ROOT))
sys.path.insert(0, str(GUI_ROOT / "backend"))

# auth.py imports fcntl at module load; stub on Windows so spawn tests can run.
if sys.platform == "win32" and "fcntl" not in sys.modules:
    import types

    _fcntl = types.ModuleType("fcntl")
    _fcntl.LOCK_EX = 1
    _fcntl.LOCK_SH = 2
    _fcntl.LOCK_UN = 8
    _fcntl.flock = lambda *args, **kwargs: None
    sys.modules["fcntl"] = _fcntl

from fastapi import HTTPException

import auth
import governance
import server

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        msg = f"  FAIL  {name}"
        if detail:
            msg += f"  ({detail})"
        print(msg)


def _skill_dirs(adam_root: Path) -> None:
    for s in ["coder", "document", "email", "slidedeck", "test_echo", "websearch"]:
        (adam_root / "skills" / s).mkdir(parents=True, exist_ok=True)
    (adam_root / "adam_agent_chat.py").write_text("# stub\n", encoding="utf-8")


def _write_governance(gui_root: Path, data: Dict[str, Any]) -> None:
    gui_root.mkdir(parents=True, exist_ok=True)
    (gui_root / "governance.json").write_text(json.dumps(data), encoding="utf-8")
    governance.init_governance(gui_root)


VALID_EDU_CONFIG = {
    "policy_bounds": {
        "standard": {
            "id": "standard",
            "name": "Standard",
            "allowed_skills": ["document", "slidedeck", "coder", "websearch", "test_echo", "email"],
            "blocked_skills": [],
            "external_actions_allowed": True,
            "email_send_allowed": True,
        },
        "education_safe": {
            "id": "education_safe",
            "name": "Education Safe",
            "allowed_skills": ["document", "slidedeck", "websearch"],
            "blocked_skills": ["email", "coder"],
            "external_actions_allowed": False,
            "email_send_allowed": False,
        },
        "strict": {
            "id": "strict",
            "name": "Strict",
            "allowed_skills": ["document", "slidedeck", "websearch"],
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
            "policy_bounds_id": "education_safe",
            "human_review_mode": "conditional",
            "review_required_for": ["public_facing_artifact"],
        },
        "external_action_restricted": {
            "id": "external_action_restricted",
            "name": "External Action Restricted",
            "policy_bounds_id": "strict",
            "human_review_mode": "required",
            "review_required_for": ["external_action"],
        },
    },
    "default_profile_id": "general",
}


class _SpawnTracker:
    def __init__(self) -> None:
        self.popen_calls = 0
        self.captured_cmd: Optional[List[str]] = None


def _patch_spawn(tmp: Path, tracker: _SpawnTracker):
    logs_dir = tmp / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    _skill_dirs(tmp)

    class FakePopen:
        def __init__(self, cmd, **kw):
            tracker.popen_calls += 1
            tracker.captured_cmd = list(cmd)
            self.pid = 4242

        def poll(self):
            return None

    orig = server.subprocess.Popen
    server.subprocess.Popen = FakePopen
    return logs_dir, orig


def _spawn(
    tmp: Path,
    logs_dir: Path,
    profile_id: Optional[str],
    tracker: _SpawnTracker,
) -> Dict[str, Any]:
    return server.spawn_adam_session(
        adam_root=tmp,
        logs_dir=logs_dir,
        user_id="pilotuser",
        display_name="Pilot",
        email="p@example.com",
        seed_text="test seed",
        context_files=[],
        max_turns=5,
        no_verify=True,
        disable_skills=[],
        governance_profile_id=profile_id,
    )


def test_default_general_spawns_with_missing_bounds_fallback() -> None:
    """Default/general keeps permissive fallback when governance.json is absent."""
    tracker = _SpawnTracker()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        gui_root = tmp / "gui"
        gui_root.mkdir()
        governance.init_governance(gui_root)
        logs_dir, orig = _patch_spawn(tmp, tracker)
        try:
            result = _spawn(tmp, logs_dir, None, tracker)
        finally:
            server.subprocess.Popen = orig
        check("default/general spawns without config file", tracker.popen_calls == 1)
        check("session dir created", Path(result["session_dir"]).is_dir())
        cmd = tracker.captured_cmd or []
        check("--policy-bounds passed", "--policy-bounds" in cmd, str(cmd))


def test_education_valid_bounds_spawns() -> None:
    tracker = _SpawnTracker()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        _write_governance(tmp / "gui", VALID_EDU_CONFIG)
        logs_dir, orig = _patch_spawn(tmp, tracker)
        try:
            _spawn(tmp, logs_dir, "education", tracker)
        finally:
            server.subprocess.Popen = orig
        check("education with valid bounds spawns", tracker.popen_calls == 1)
        check("education is protected", governance.is_protected_profile("education"))


def test_education_missing_bounds_refuses_before_spawn() -> None:
    cfg = json.loads(json.dumps(VALID_EDU_CONFIG))
    del cfg["policy_bounds"]["education_safe"]
    tracker = _SpawnTracker()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        _write_governance(tmp / "gui", cfg)
        logs_dir, orig = _patch_spawn(tmp, tracker)
        try:
            try:
                _spawn(tmp, logs_dir, "education", tracker)
                check("education missing bounds raises", False, "no exception")
            except HTTPException as e:
                check("education missing bounds -> HTTP 500", e.status_code == 500, e.detail)
        finally:
            server.subprocess.Popen = orig
        check("education missing bounds: no subprocess", tracker.popen_calls == 0)


def test_external_restricted_missing_bounds_refuses() -> None:
    cfg = json.loads(json.dumps(VALID_EDU_CONFIG))
    del cfg["policy_bounds"]["strict"]
    tracker = _SpawnTracker()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        _write_governance(tmp / "gui", cfg)
        logs_dir, orig = _patch_spawn(tmp, tracker)
        try:
            try:
                _spawn(tmp, logs_dir, "external_action_restricted", tracker)
                check("external_action_restricted missing bounds raises", False)
            except HTTPException as e:
                check("external_action_restricted -> HTTP 500", e.status_code == 500, e.detail)
        finally:
            server.subprocess.Popen = orig
        check("external_action_restricted: no subprocess", tracker.popen_calls == 0)


def test_unknown_profile_http_400() -> None:
    tracker = _SpawnTracker()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        _write_governance(tmp / "gui", VALID_EDU_CONFIG)
        logs_dir, orig = _patch_spawn(tmp, tracker)
        try:
            try:
                _spawn(tmp, logs_dir, "nonexistent_profile", tracker)
                check("unknown profile raises", False)
            except HTTPException as e:
                check("unknown profile -> HTTP 400", e.status_code == 400, e.detail)
        finally:
            server.subprocess.Popen = orig
        check("unknown profile: no subprocess", tracker.popen_calls == 0)


def test_effective_profile_not_requested() -> None:
    """Pilot assigned education who requests general is still protected."""
    pilot = {
        "max_turns_per_session": 12,
        "governance_profile": "education",
        "_role": {},
    }
    effective = auth.effective_governance_profile(pilot, "general")
    check("effective profile ignores pilot's general request", effective == "education")

    cfg = json.loads(json.dumps(VALID_EDU_CONFIG))
    del cfg["policy_bounds"]["education_safe"]
    tracker = _SpawnTracker()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        _write_governance(tmp / "gui", cfg)
        logs_dir, orig = _patch_spawn(tmp, tracker)
        try:
            try:
                _spawn(tmp, logs_dir, effective, tracker)
                check("effective education with bad bounds raises", False)
            except HTTPException as e:
                check("effective education fail-closed -> 500", e.status_code == 500, e.detail)
        finally:
            server.subprocess.Popen = orig
        check("effective education: no subprocess", tracker.popen_calls == 0)


def test_health_endpoint_minimal() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        app = server.build_app(adam_root=tmp, logs_dir=tmp / "logs")
        from fastapi.testclient import TestClient

        client = TestClient(app)
        resp = client.get("/api/health")
        check("health status 200", resp.status_code == 200)
        body = resp.json()
        check("health body exact keys", set(body.keys()) == {"ok", "version"}, str(body))
        check("health ok true", body.get("ok") is True)
        check("health version", body.get("version") == "0.2.0")


def main() -> None:
    print("tests/test_spawn_governance_failclosed.py")
    print("=" * 60)
    for fn in [
        test_default_general_spawns_with_missing_bounds_fallback,
        test_education_valid_bounds_spawns,
        test_education_missing_bounds_refuses_before_spawn,
        test_external_restricted_missing_bounds_refuses,
        test_unknown_profile_http_400,
        test_effective_profile_not_requested,
        test_health_endpoint_minimal,
    ]:
        print(f"\n{fn.__name__}:")
        fn()
    print("\n" + "=" * 60)
    print(f"Results: {PASSED} passed, {FAILED} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
