"""
Review-resume state consistency (handoff fix2m).

Run: python tests/test_review_resume.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
GUI_ROOT = ROOT / "gui"
sys.path.insert(0, str(GUI_ROOT))
sys.path.insert(0, str(GUI_ROOT / "backend"))

if sys.platform == "win32" and "fcntl" not in sys.modules:
    import types

    _fcntl = types.ModuleType("fcntl")
    _fcntl.LOCK_EX = 1
    _fcntl.LOCK_SH = 2
    _fcntl.LOCK_UN = 8
    _win_lock = threading.Lock()

    def _win_flock(fd, op):
        if op == _fcntl.LOCK_EX:
            _win_lock.acquire()
        elif op == _fcntl.LOCK_UN:
            _win_lock.release()

    _fcntl.flock = _win_flock
    sys.modules["fcntl"] = _fcntl

from fastapi import HTTPException

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


def _write_paused_session(
    logs_dir: Path,
    user_id: str,
    session_id: str,
    *,
    old_format: bool = True,
) -> Path:
    sdir = logs_dir / user_id / session_id
    sdir.mkdir(parents=True, exist_ok=True)
    pause: Dict[str, Any] = {
        "pause_type": "gate_review",
        "review_reason": "public-facing output requires review",
        "final_synthesis_text": "Operator will email parents.",
        "status": "awaiting_human_review",
    }
    if not old_format:
        pause["resolved"] = False
    (sdir / "pause_state.json").write_text(json.dumps(pause), encoding="utf-8")
    state = {
        "schema_version": "1.0",
        "runtime_state": {
            "session_id": session_id,
            "started_at": "2026-06-01T10:00:00",
            "ended_at": "2026-06-01T10:30:00",
            "end_reason": "awaiting_human_review",
            "governance": {
                "awaiting_human_review": True,
                "review_reason": pause["review_reason"],
            },
        },
        "governance_state": {"seed": "Draft parent newsletter"},
    }
    (sdir / "session_state.json").write_text(json.dumps(state), encoding="utf-8")
    (sdir / "seed.md").write_text("Draft parent newsletter\n", encoding="utf-8")
    (sdir / ".process_info.json").write_text(
        json.dumps({"pid": 100, "started_at": "2026-06-01T10:00:00",
                    "governance_profile_id": "general"}),
        encoding="utf-8",
    )
    return sdir


class _SpawnTracker:
    def __init__(self) -> None:
        self.count = 0
        self.last_cmd: Optional[List[str]] = None


def _patch_spawn(tracker: _SpawnTracker):
    class FakePopen:
        def __init__(self, cmd, **kw):
            tracker.count += 1
            tracker.last_cmd = list(cmd)
            self.pid = 9000 + tracker.count

        def poll(self):
            return None

    orig = server.subprocess.Popen
    server.subprocess.Popen = FakePopen
    return orig


def _approve_resume(
    tmp: Path,
    logs_dir: Path,
    sdir: Path,
    tracker: _SpawnTracker,
    *,
    old_format: bool = True,
) -> str:
    pause = server._load_pause_state(sdir)
    assert pause is not None
    resolved_at = "2026-06-01T11:00:00"
    stored = server.normalize_review_decision("approve")
    with server._claim_pause_resolution(sdir, "pilotuser") as claimed:
        result = server.spawn_adam_session(
            adam_root=tmp,
            logs_dir=logs_dir,
            user_id="pilotuser",
            display_name="Pilot",
            email="p@example.com",
            seed_text=server._compose_resume_seed(pause, "", "approve"),
            context_files=[],
            max_turns=5,
            no_verify=True,
            disable_skills=[],
            parent_session_id=sdir.name,
            governance_profile_id="general",
            resume_after_review=True,
            resumed_from_review=True,
            review_decision=stored,
            review_resolved_at=resolved_at,
        )
        server._finalize_review_resolution(
            sdir,
            pause=claimed,
            decision=stored,
            guidance="",
            resumed_as=result["session_id"],
            resolved_by="pilotuser",
            resolved_at=resolved_at,
            pause_type="gate_review",
        )
    return result["session_id"]


def test_old_format_pause_still_open() -> None:
    pause = {"pause_type": "gate_review", "status": "awaiting_human_review"}
    check("old pause without resolved is open", server.pause_is_open(pause))


def test_approve_updates_parent_and_summary() -> None:
    tracker = _SpawnTracker()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        logs_dir = tmp / "logs"
        gui_root = tmp / "gui"
        gui_root.mkdir()
        governance.init_governance(gui_root)
        _skill_dirs(tmp)
        parent_id = str(uuid.uuid4())
        sdir = _write_paused_session(logs_dir, "pilotuser", parent_id)
        orig = _patch_spawn(tracker)
        try:
            child_id = _approve_resume(tmp, logs_dir, sdir, tracker)
        finally:
            server.subprocess.Popen = orig

        state = json.loads((sdir / "session_state.json").read_text(encoding="utf-8"))
        rt = state["runtime_state"]
        check("parent end_reason review_resolved", rt["end_reason"] == "review_resolved")
        check("parent resumed_as child", rt["governance"]["resumed_as"] == child_id)

        summary = server._summarize_session(sdir)
        check("parent not awaiting_human_review", summary["status"] != "awaiting_human_review")
        check("parent status review_resolved", summary["status"] == "review_resolved")

        child_dir = logs_dir / "pilotuser" / child_id
        proc = json.loads((child_dir / ".process_info.json").read_text(encoding="utf-8"))
        check("child parent_session_id", proc.get("parent_session_id") == parent_id)
        check("child resumed_from_review", proc.get("resumed_from_review") is True)
        check("child review_decision", proc.get("review_decision") == "approve")

        child_summary = server._summarize_session(child_dir)
        check("child summary breadcrumb", child_summary.get("resumed_from_review") is True)


def test_second_resume_returns_409() -> None:
    tracker = _SpawnTracker()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        logs_dir = tmp / "logs"
        governance.init_governance(tmp / "gui")
        _skill_dirs(tmp)
        parent_id = str(uuid.uuid4())
        sdir = _write_paused_session(logs_dir, "pilotuser", parent_id)
        orig = _patch_spawn(tracker)
        try:
            _approve_resume(tmp, logs_dir, sdir, tracker)
            try:
                with server._claim_pause_resolution(sdir, "pilotuser"):
                    check("second claim should not run", False)
            except HTTPException as e:
                check("second resume HTTP 409", e.status_code == 409)
        finally:
            server.subprocess.Popen = orig
        check("only one spawn", tracker.count == 1)


def test_decline_no_child_spawn() -> None:
    tracker = _SpawnTracker()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        logs_dir = tmp / "logs"
        parent_id = str(uuid.uuid4())
        sdir = _write_paused_session(logs_dir, "pilotuser", parent_id)
        pause = server._load_pause_state(sdir)
        resolved_at = "2026-06-01T11:00:00"
        orig = _patch_spawn(tracker)
        try:
            with server._claim_pause_resolution(sdir, "pilotuser") as claimed:
                server._finalize_review_resolution(
                    sdir,
                    pause=claimed,
                    decision="declined",
                    guidance="Do not send",
                    resumed_as=None,
                    resolved_by="pilotuser",
                    resolved_at=resolved_at,
                    pause_type="gate_review",
                )
        finally:
            server.subprocess.Popen = orig

        check("decline spawns nothing", tracker.count == 0)
        state = json.loads((sdir / "session_state.json").read_text(encoding="utf-8"))
        check("decline decision stored", state["runtime_state"]["governance"]["review_decision"] == "declined")
        check("decline resumed_as null", state["runtime_state"]["governance"]["resumed_as"] is None)
        summary = server._summarize_session(sdir)
        check("decline summary review_resolved", summary["status"] == "review_resolved")


def test_concurrent_claims_single_winner() -> None:
    with tempfile.TemporaryDirectory() as raw:
        logs_dir = Path(raw) / "logs"
        parent_id = str(uuid.uuid4())
        sdir = _write_paused_session(logs_dir, "pilotuser", parent_id)
        results: List[str] = []

        def attempt():
            try:
                with server._claim_pause_resolution(sdir, "pilotuser"):
                    results.append("won")
                    import time
                    time.sleep(0.05)
            except HTTPException as e:
                if e.status_code == 409:
                    results.append("blocked")

        t1 = threading.Thread(target=attempt)
        t2 = threading.Thread(target=attempt)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        check("exactly one winner", results.count("won") == 1)
        check("one blocked", results.count("blocked") == 1)


def test_old_format_still_resumable() -> None:
    tracker = _SpawnTracker()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        logs_dir = tmp / "logs"
        governance.init_governance(tmp / "gui")
        _skill_dirs(tmp)
        parent_id = str(uuid.uuid4())
        sdir = _write_paused_session(logs_dir, "pilotuser", parent_id, old_format=True)
        pause = server._load_pause_state(sdir)
        check("legacy pause is open", server.pause_is_open(pause))
        orig = _patch_spawn(tracker)
        try:
            child_id = _approve_resume(tmp, logs_dir, sdir, tracker, old_format=True)
            check("legacy pause resumes", bool(child_id))
        finally:
            server.subprocess.Popen = orig


def main() -> None:
    print("tests/test_review_resume.py")
    print("=" * 60)
    for fn in [
        test_old_format_pause_still_open,
        test_approve_updates_parent_and_summary,
        test_second_resume_returns_409,
        test_decline_no_child_spawn,
        test_concurrent_claims_single_winner,
        test_old_format_still_resumable,
    ]:
        print(f"\n{fn.__name__}:")
        fn()
    print("\n" + "=" * 60)
    print(f"Results: {PASSED} passed, {FAILED} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
