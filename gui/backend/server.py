"""
ADAM GUI backend.

A FastAPI service that:
  - Walks the ADAM logs/ directory to enumerate sessions
  - Serves session_state.json and the per-subsystem log files
  - Streams events.jsonl as Server-Sent Events for live consumption
  - Hosts the static React build at /

Design constraints (per architecture decision):
  - Separate process from ADAM. No shared imports, no shared lifecycle.
  - Contract with ADAM is the on-disk file layout in logs/<user_id>/<session_id>/.
  - Director identity comes from the same .env that ADAM uses, so the
    GUI and ADAM agree on whose sessions to list without coordination.
  - Single-tenant: one director per deployment. Multi-user auth
    layered on later via the same LDAPS pattern Paperclip uses.

Run with:
    python -m backend.server [--logs-dir PATH] [--host HOST] [--port PORT]

Or via the bundled launcher:
    python adam_gui.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, Body, UploadFile, File, Form, Response, Cookie, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

# v5 multi-user: auth module owns users.json, login sessions, bcrypt.
# Import path is resolved relative to this file so the module can be
# located whether the server is run as `python -m backend.server` or
# `python gui/backend/server.py`.
try:
    from . import auth  # when imported as backend.server
    from . import csrf
    from . import data_sources
    from . import data_source_connections
    from . import governance
    from . import ratelimit
    from . import verification
except ImportError:
    import auth          # when run directly
    import csrf
    import data_sources
    import data_source_connections
    import governance
    import ratelimit
    import verification


# ============================================================
# Configuration
# ============================================================

DEFAULT_LOGS_DIR = Path("logs")
DEFAULT_HOST     = "127.0.0.1"
DEFAULT_PORT     = 8765   # ADAM speaking on 8 = octal for "deliberation"; 765 is just a free port

# v5 multi-user: cookie name for the login session token. The cookie
# is httponly + samesite=lax + secure-when-deployed. The name is
# chosen to not collide with anything else; "adam_session" would be
# confusing because we also have ADAM sessions (the deliberation kind).
LOGIN_COOKIE_NAME = "adam_login"

# Cookie lifetime should match the login session TTL in auth.py.
# 7 days of inactivity-based expiry handled server-side; the cookie
# itself we set with the same max-age so the browser also forgets it.
LOGIN_COOKIE_MAX_AGE = auth.LOGIN_SESSION_TTL_SECONDS

# How often the SSE tail polls events.jsonl for new bytes. Low enough
# to feel live, high enough to be cheap. The watchdog library would be
# slightly more efficient but adds a dependency and the poll cost here
# is negligible compared to a deliberation turn.
SSE_POLL_INTERVAL_SECONDS = 0.5

# Backoff for SSE clients that fall behind. The server still ships
# every event in order; this just throttles how fast we re-check the
# file when there's no new data.
SSE_IDLE_POLL_SECONDS = 1.5

# Part 8: maximum size of a single director message. Matches the cap
# enforced ADAM-side in consume_director_inbox. The cap protects
# against an unbounded request body and against accidental paste of
# very large content (e.g. a whole document) into the GUI prompt bar.
DIRECTOR_MESSAGE_MAX_CHARS = 8000

# Part 9: bounds on new-session inputs.
#
# - SEED_MAX_CHARS bounds the size of the seed.md the GUI writes. Real
#   seeds are usually 1-5 sentences; even very long ones rarely exceed
#   a few KB. 50KB is generous and catches accidental whole-document
#   pastes before they reach disk.
# - MAX_CONTEXT_FILES caps how many files a single new-session request
#   can upload. The context loader handles large file counts fine, but
#   we want a sane backstop against accidental directory uploads.
# - MAX_CONTEXT_FILE_BYTES caps an individual file. Files larger than
#   this are likely the wrong asset; the context loader would refuse
#   them anyway after summarization.
SEED_MAX_CHARS         = 50_000
MAX_CONTEXT_FILES      = 20
MAX_CONTEXT_FILE_BYTES = 10 * 1024 * 1024   # 10 MB

# Part 9: status taxonomy for sessions. The status field in the
# sidebar feed is derived from filesystem state, not stored anywhere.
#
#   starting  : .process_info.json exists, PID alive, no events yet
#   active    : events.jsonl exists with content, no session_state.json
#   complete  : session_state.json exists
#   errored   : .process_info.json exists, PID dead, no session_state.json
#   unknown   : no signals available (very rare; pre-Part-9 sessions
#               or transitional states)
#
# The terms match what the frontend already binds to in CSS (session-item__dot--active etc.),
# with one new value added: "starting".


# ============================================================
# Part 8: request models for live GUI director input
# ============================================================

class DirectorMessageRequest(BaseModel):
    """
    Payload accepted by POST /api/sessions/{id}/director_message.

    The server's only job is to validate the message and append it to
    the session's director_inbox.jsonl with a server-generated
    message_id. ADAM consumes the inbox at the top of each loop
    iteration. The server does not interpret the content -- raw
    director-syntax strings (>>halt, >>Logician: text, plain text)
    pass through verbatim.
    """
    content: str = Field(
        ...,
        min_length=1,
        max_length=DIRECTOR_MESSAGE_MAX_CHARS,
        description="Raw director input, e.g. '>>Logician: reconsider this' or '>>halt'.",
    )


class DirectorMessageResponse(BaseModel):
    """Response after successfully queueing a director message."""
    message_id: str
    queued_at:  str
    inbox_path: str


# Part 9: response model for POST /api/sessions (new session creation).
# The request itself is multipart/form-data because it may include
# uploaded context files; multipart can't be modeled with a single
# Pydantic class. Instead, the endpoint declares its form fields
# directly (via Form(...) and File(...)) and we model only the response.

class NewSessionResponse(BaseModel):
    """
    Returned by POST /api/sessions after a session directory has been
    prepared and ADAM has been spawned. The session_id can be used
    immediately to open an SSE event stream; the GUI will see events
    once ADAM completes its startup (~5-15 seconds).
    """
    session_id:    str
    started_at:    str
    pid:           int
    session_dir:   str
    seed_path:     str
    context_files: List[str]    # filenames placed in input_context/
    status:        str          # "starting"


# v5 multi-user auth models
# =========================
# LoginRequest: POST body for /api/auth/login. Username/password
# combination; the server validates against users.json and sets a
# cookie on success.
# WhoamiResponse: GET response for /api/auth/whoami. The frontend
# calls this on every page load to determine whether to show the
# login screen or the dashboard.

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=200)


class WhoamiResponse(BaseModel):
    username:               str
    display_name:           str
    email:                  str
    role:                   str
    sessions_remaining:     int            # -1 means unlimited
    max_turns_per_session:  int            # -1 means unlimited
    skills_denied:          List[str]      # for the frontend to grey-out controls
    governance_profile:     Optional[str] = None   # assigned profile (pilots)
    governance_profile_locked: bool = False        # True when server enforces assignment
    must_change_password:   bool = False           # force change-password screen on login


# Request body for assigning/clearing a user's governance profile.
# MUST be module-level: a Pydantic request-body model defined inside a
# function (nested/local scope) is not recognized by FastAPI as a body
# model — FastAPI falls back to treating the parameter as a QUERY param,
# producing 422 errors with loc ["query","body"]. Keep all request-body
# models at module scope.
class UserGovernanceProfileUpdate(BaseModel):
    governance_profile: Optional[str] = None   # None/"" => reset to role default


# Request body for an admin override of a Truthseeker verdict. Module-level
# for the same reason as above (nested body models break FastAPI parsing).
class VerificationOverrideBody(BaseModel):
    claim_id: str
    status:   str
    reason:   str
    feedback: Optional[str] = None


# Minimum length enforced for any user-chosen password (the change-password
# flow). Temp passwords generated by auth.generate_temp_password() are well
# above this.
MIN_PASSWORD_LENGTH = 8


# Request body for the authenticated user-driven change-password flow.
# Module-level (see the note on UserGovernanceProfileUpdate): a body model
# defined inside build_app would be mis-parsed by FastAPI as query params.
class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=200)
    new_password:     str = Field(..., min_length=1, max_length=200)


# Admin create-user request. Module-level (see note above). The temp
# password is server-generated, so it is NOT a field here. sessions_remaining
# and max_turns_per_session are optional with the same pilot defaults as
# auth.add_user.
class CreateUserRequest(BaseModel):
    username:              str = Field(..., min_length=1, max_length=64)
    display_name:         str = Field(..., min_length=1, max_length=200)
    email:                str = Field(..., min_length=1, max_length=320)
    role:                 str = Field(..., min_length=1, max_length=32)
    sessions_remaining:   int = 3
    max_turns_per_session: int = 10


# Admin edit-user request. Profile fields only -- NO password, NO status
# (those have dedicated endpoints). All optional so a partial update only
# touches the fields provided.
class EditUserRequest(BaseModel):
    display_name:          Optional[str] = Field(None, max_length=200)
    email:                 Optional[str] = Field(None, max_length=320)
    role:                  Optional[str] = Field(None, max_length=32)
    sessions_remaining:    Optional[int] = None
    max_turns_per_session: Optional[int] = None


# Data Sources (governed query pipeline) request bodies. Module-level.
# `password` may be received for admin test/introspect; it is used to build a
# connect_fn and NEVER echoed back, persisted, or logged.
class MySQLTestRequest(BaseModel):
    host:     str = Field(..., min_length=1, max_length=255)
    port:     int = 3306
    user:     str = Field(..., min_length=1, max_length=128)
    password: str = Field("", max_length=512)
    database: str = Field(..., min_length=1, max_length=128)


class MySQLIntrospectRequest(MySQLTestRequest):
    source_name: str = Field(..., min_length=1, max_length=128)


# Admin approve body — OPTIONAL connection details. When present (admin+CSRF
# only), the password is encrypted at rest into a connection profile at ratify
# time and then discarded. Approve still works with no body (ratify only). The
# password is accepted here ONLY on this admin-protected route — never at query.
class ApproveCandidateRequest(BaseModel):
    host:         Optional[str] = Field(None, max_length=255)
    port:         int = 3306
    user:         Optional[str] = Field(None, max_length=128)
    password:     Optional[str] = Field(None, max_length=512)
    database:     Optional[str] = Field(None, max_length=128)
    display_name: Optional[str] = Field(None, max_length=255)


# The USER query body — by firm requirement carries NO connection credentials
# (no host/user/password/database/dsn). A source is identified by its ratified
# version only; the read-only connection resolves server-side from the handle.
class DataIntelligenceQueryRequest(BaseModel):
    version:   str = Field(..., min_length=1, max_length=128)
    objective: str = Field(..., min_length=1, max_length=4000)


# ============================================================
# .env loader (minimal, no python-dotenv dependency)
# ============================================================

def load_dotenv(path: Path) -> Dict[str, str]:
    """Read .env if present and return a dict of values. Doesn't mutate os.environ."""
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def resolve_director(adam_root: Path) -> Dict[str, str]:
    """
    Resolve director identity from ADAM's .env, then os.environ as
    fallback. Mirrors the ADAM runtime's own logic so the GUI shows
    the same director ADAM would log under.
    """
    env_path = adam_root / ".env"
    env = load_dotenv(env_path)
    def get(key: str) -> str:
        return env.get(key, os.environ.get(key, "")).strip()
    user_id      = get("ADAM_DEFAULT_DIRECTOR")
    email        = get("ADAM_DEFAULT_DIRECTOR_EMAIL")
    display_name = get("ADAM_DEFAULT_DIRECTOR_DISPLAY_NAME") or user_id
    return {
        "user_id":      user_id,
        "email":        email,
        "display_name": display_name,
    }


def export_provider_keys_from_dotenv(adam_root: Path, extra_names=()) -> None:
    """Export ONLY the provider api_key_env names declared in providers.json
    (plus any explicit `extra_names`, e.g. the data-source encryption key) from
    .env into os.environ, so .env is the single source of truth for model keys
    and they need not be duplicated in the systemd unit.

    Existing env wins: a non-empty os.environ value is never overwritten (same
    precedence as adam/core/session.py:load_dotenv). Limited to a specific set
    of names (never a bulk .env dump, to avoid leaking unrelated entries). Never
    logs a key value — only the name and whether it was set-from-dotenv vs
    already-present. Reuses the existing minimal load_dotenv (no new dependency).
    """
    try:
        env_values = load_dotenv(adam_root / ".env")
    except Exception:
        return
    if not env_values:
        return
    provider_names = set()
    try:
        with open(adam_root / "config" / "providers.json", encoding="utf-8") as f:
            providers = json.load(f)
        provider_names = {
            p["api_key_env"] for p in providers.values()
            if isinstance(p, dict) and p.get("api_key_env")
        }
    except (OSError, ValueError):
        provider_names = set()

    key_names = sorted(provider_names | {n for n in extra_names if n})
    for name in key_names:
        if os.environ.get(name, "").strip():
            print(f"provider key {name}: already present in environment (not overridden)",
                  file=sys.stderr)
            continue
        value = (env_values.get(name) or "").strip()
        if value:
            os.environ[name] = value
            print(f"provider key {name}: set from .env", file=sys.stderr)


# ============================================================
# Session discovery
# ============================================================

def _is_session_dir(path: Path) -> bool:
    """A session directory must contain at least one of the expected files."""
    if not path.is_dir():
        return False
    for marker in (
        "session.log", "audit.jsonl", "session_state.json",
        "events.jsonl",
        # Part 9: a session may exist as soon as the GUI has created
        # its directory and dropped .process_info.json, even before
        # ADAM has written any logs.
        ".process_info.json", "seed.md",
    ):
        if (path / marker).exists():
            return True
    return False


def _discover_skill_universe(adam_root: Path) -> List[str]:
    """Return the full set of skill names that exist, by listing the
    skills/ directory under adam_root. This is the universe against
    which a policy-bounds allow-list is interpreted (default-deny of
    anything not allowed). Falls back to the known built-in skills if
    the directory can't be read, so policy enforcement degrades safely
    rather than silently allowing everything."""
    fallback = ["coder", "document", "email", "slidedeck", "test_echo", "websearch"]
    try:
        skills_dir = Path(adam_root) / "skills"
        names = [
            p.name for p in skills_dir.iterdir()
            if p.is_dir() and not p.name.startswith((".", "_"))
        ]
        return sorted(names) if names else fallback
    except Exception:
        return fallback


def _read_process_info(session_dir: Path) -> Optional[Dict[str, Any]]:
    """Read .process_info.json if present. Returns None on any error."""
    path = session_dir / ".process_info.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _check_process_alive(pid: int) -> bool:
    """
    Check whether a PID is still alive.

    On Unix: os.kill(pid, 0) raises ProcessLookupError if the process is
    gone, PermissionError if it exists but we don't own it. Either way,
    a missing pid raises; an existing one does not. We treat
    PermissionError as alive because the only realistic case where this
    happens for us is the GUI running as a different user than ADAM,
    which is intentional in some deployments.

    On Windows the same os.kill(pid, 0) check is supported in Python 3.x
    and behaves equivalently.
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _safe_upload_filename(raw: str) -> str:
    """
    Make a user-supplied filename safe for filesystem use without
    silently mangling it beyond recognition. Drops path separators,
    leading dots, and control characters; preserves spaces and
    non-ASCII printable characters (real-world documents use them).

    Returns "upload" if nothing usable remains.
    """
    if not raw:
        return "upload"
    name = os.path.basename(raw)
    # Strip path-traversal lead and control chars
    name = name.lstrip(".").replace("\x00", "")
    name = "".join(c for c in name if c >= " " or c == "\t")
    # Reject anything still containing a path separator after basename
    if "/" in name or "\\" in name:
        return "upload"
    return name.strip() or "upload"


def spawn_adam_session(
    *,
    adam_root:    Path,
    logs_dir:     Path,
    user_id:      str,
    display_name: str,
    email:        str,
    seed_text:    str,
    context_files: List[Dict[str, Any]],
    max_turns:    Optional[int],
    no_verify:    bool,
    disable_skills: Optional[List[str]] = None,
    parent_session_id: Optional[str] = None,
    governance_profile_id: Optional[str] = None,
    resume_after_review: bool = False,
    resume_after_information: bool = False,
) -> Dict[str, Any]:
    """
    Part 9 / v5 multi-user: prepare a session directory and spawn ADAM.

    Parameters changed in v5:
      - `director` dict replaced by direct `user_id` / `display_name` /
        `email` strings, derived from the authenticated user's record
        in users.json. These three flow through to ADAM as
        --director-user-id / --director-name / --director-email CLI
        flags, which override anything in .env. Without this, ADAM
        would write its logs to logs/<env-director>/<session>/ instead
        of logs/<authenticated-user>/<session>/, and the GUI's tail
        of events.jsonl would see nothing because it's looking in
        the wrong directory.
      - `disable_skills` added: list of skill names denied to this
        user's role. Passed to ADAM as repeated --disable-skill flags
        so the agents never see those skills in their catalog.

    The work is split into a series of small, recoverable steps:

      1. Generate a session_id.
      2. Create the session directory under logs/<user_id>/<session_id>/.
      3. Write seed.md.
      4. If context_files were uploaded, write them to input_context/.
      5. Build the ADAM command line (now with --disable-skill flags).
      6. Open process_stdout.log and process_stderr.log for capture.
      7. subprocess.Popen with start_new_session=True so ADAM survives
         backend restarts.
      8. Write .process_info.json so the GUI can detect liveness later.

    Returns a dict with session_id, pid, started_at, session_dir, and
    the list of context filenames actually written.

    Raises HTTPException(500) if any step fails after the session_dir
    has been created. The session_dir is intentionally LEFT IN PLACE
    on error -- it serves as the audit record of the failed start, and
    the user can see the partial files plus the stderr log.
    """
    # Step 1: session_id. Use a UUID so it's collision-free across
    # concurrent submissions and clearly distinct from user input.
    session_id = str(uuid.uuid4())
    started_at = datetime.now().isoformat(timespec='seconds')

    # Step 2: session directory. logs/<user_id>/<session_id>/. The
    # user_id is the authenticated user's username (NOT .env), so
    # different users' sessions live in separate, isolated directories.
    if not user_id:
        raise HTTPException(
            status_code=500,
            detail="user_id is empty; cannot create session directory",
        )
    session_dir = logs_dir / user_id / session_id
    session_dir.mkdir(parents=True, exist_ok=False)

    # Step 3: write seed.md. The trailing newline is conventional for
    # markdown files and keeps tools like cat / less from complaining.
    seed_path = session_dir / "seed.md"
    seed_path.write_text(
        seed_text.rstrip() + "\n",
        encoding="utf-8",
    )

    # Step 4: write context files to input_context/. Each file gets
    # its sanitized name; collisions get a numeric suffix.
    input_context_dir = session_dir / "input_context"
    input_context_dir.mkdir(parents=True, exist_ok=True)
    written_names: List[str] = []
    for entry in context_files:
        raw_name = entry["filename"]
        data: bytes = entry["bytes"]
        safe_name = _safe_upload_filename(raw_name)
        # Collision handling: if a file with this name already exists in
        # the directory (rare but possible if the user uploaded two
        # files with the same name), append " (n)" before the extension.
        target = input_context_dir / safe_name
        if target.exists():
            stem = target.stem
            suffix = target.suffix
            n = 2
            while target.exists():
                target = input_context_dir / f"{stem} ({n}){suffix}"
                n += 1
        with open(target, "wb") as f:
            f.write(data)
        written_names.append(target.name)

    # Step 5: build the ADAM command. Use the current Python
    # interpreter (same venv as the backend) so dependencies match.
    # --session-id pins ADAM to the directory the GUI prepared, and
    # --seed-file points at the seed.md we just wrote. --context-dir
    # is only passed when there are files in input_context/.
    cmd: List[str] = [
        sys.executable, "-u",
        str(adam_root / "adam_agent_chat.py"),
        "--session-id",  session_id,
        "--seed-file",   str(seed_path),
        # v5 multi-user: tell ADAM who the director is, so it writes
        # logs under logs/<authenticated-user>/<session>/ instead of
        # logs/<env-director>/<session>/. Without these three flags,
        # ADAM falls back to .env and writes to the wrong directory.
        "--director-user-id", user_id,
        "--director-name",    display_name or user_id,
        "--director-email",   email or "",
    ]
    if max_turns is not None:
        cmd += ["--max-turns", str(max_turns)]
    if no_verify:
        cmd += ["--no-verify"]
    if written_names:
        cmd += [
            "--context-dir", str(input_context_dir),
            # The director already consented by clicking Start; skip
            # ADAM's interactive consent prompt. The GUI is the consent
            # surface, not the terminal.
            "--yes-context-risk",
        ]
    # v5 multi-user: per-user skill access. Each name in disable_skills
    # becomes a repeated --disable-skill flag. ADAM's cli.py collects
    # these into a list, and adam_agent_chat merges them into the
    # disabled_skills list before discover_skills() runs. The denied
    # skills are removed from the catalog the agents see, so Operator
    # can't propose calling them and the runtime rejects any attempt.
    #
    # Slice 2: the effective denial set is the UNION of (a) the user's
    # ROLE denials, passed in as disable_skills, and (b) the skills the
    # session's governance profile's policy-bounds ruleset forbids.
    # Union-of-denials == intersection-of-allowances == "most restrictive
    # wins": a skill is available only if BOTH the role and the policy
    # permit it. This is computed HERE, in one place, so both the fresh
    # and continuation spawn paths get identical enforcement -- there is
    # no second, separate policy filter elsewhere.
    resolved_profile_id = governance.resolve_profile_id(governance_profile_id)
    skill_universe = _discover_skill_universe(adam_root)
    policy_denied = governance.policy_denied_skills(resolved_profile_id, skill_universe)
    effective_denied = sorted(set(disable_skills or []) | set(policy_denied))

    for skill_name in effective_denied:
        cmd += ["--disable-skill", skill_name]

    # Slice 3: the deliberation subprocess needs its policy bounds at
    # RUNTIME (not just at spawn) so the post-synthesis policy gate can
    # check the planned Operator action before it runs. We resolve the
    # bounds here (one place) and pass them to the subprocess as a compact
    # JSON arg, so governance.json parsing stays entirely in the backend.
    try:
        _bounds = dict(governance.get_policy_bounds(resolved_profile_id))
        # Slice 4a: fold the profile's human-review settings into the same
        # object so the subprocess's review gate sees them alongside the
        # policy bounds. These come from the PROFILE (Slice 1 data model),
        # not the bounds ruleset, so merge them in here.
        try:
            _profile = governance.get_profile(resolved_profile_id)
            _bounds["human_review_mode"] = _profile.get("human_review_mode", "none")
            _bounds["review_required_for"] = _profile.get("review_required_for", [])
        except Exception:
            _bounds.setdefault("human_review_mode", "none")
            _bounds.setdefault("review_required_for", [])
        cmd += [
            "--governance-profile-id", resolved_profile_id,
            "--policy-bounds", json.dumps(_bounds, separators=(",", ":")),
        ]
    except Exception:
        # If bounds can't be resolved, the subprocess defaults to a
        # permissive gate (governance off) -- never block spawning.
        pass

    # Slice 4a: resumed-after-review sessions skip deliberation and route
    # straight to Operator (the synthesis is already settled and composed
    # into the seed).
    if resume_after_review:
        cmd += ["--resume-after-review"]
    if resume_after_information:
        cmd += ["--resume-after-information"]
    if parent_session_id:
        cmd += ["--parent-session-id", parent_session_id]

    # Step 6: stdout/stderr capture. Files are opened in append mode
    # (write-binary), so subprocess.Popen can redirect to them without
    # interleaving. They live in the session directory so the GUI can
    # serve them via the same path-bounded artifact endpoint pattern.
    stdout_path = session_dir / "process_stdout.log"
    stderr_path = session_dir / "process_stderr.log"

    # Step 7: spawn. start_new_session=True puts ADAM in its own
    # process group so killing the backend doesn't kill ADAM. This is
    # the load-bearing reliability property: the user can close the
    # browser and restart the GUI without losing in-progress sessions.
    try:
        stdout_f = open(stdout_path, "wb")
        stderr_f = open(stderr_path, "wb")
        proc = subprocess.Popen(
            cmd,
            cwd=str(adam_root),
            stdout=stdout_f,
            stderr=stderr_f,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        # Note: we deliberately keep stdout_f and stderr_f open in the
        # backend process. Subprocess.Popen dup'd the fds to the child,
        # which now owns them for writing. Closing here is fine, but
        # leaving them open is also fine and matches typical "let the
        # OS reap when the parent exits" practice. Close to keep our
        # open-fd count predictable.
        stdout_f.close()
        stderr_f.close()
    except Exception as e:
        # The session dir survives so the failure is visible.
        raise HTTPException(
            status_code=500,
            detail=f"failed to spawn ADAM: {type(e).__name__}: {e}",
        )

    # Step 8: sidecar. .process_info.json is the GUI's hook for
    # liveness detection. The dot-prefix marks it as metadata so a
    # casual viewer doesn't mistake it for session output.
    process_info = {
        "pid":          proc.pid,
        "command":      cmd,
        "cwd":          str(adam_root),
        "started_at":   started_at,
        "status":       "starting",
        "stdout_path":  str(stdout_path.name),
        "stderr_path":  str(stderr_path.name),
        "spawned_by":   "gui",
        "spawned_by_version": "part9",
        # Lineage: set when this session was created via "Continue from
        # this session". None for normal fresh sessions. The sidebar
        # reads this back (via _summarize_session) to render the
        # continued-from / continued-as relationship. Stored here rather
        # than in session_state.json because process_info is written at
        # spawn time, so the link is visible immediately -- session_state
        # only lands when the session ends.
        "parent_session_id": parent_session_id,
        # Governance profile governing this session (Slice 1: recorded
        # only; Slice 2: now also drives the skill denial union above).
        # Reuse the already-resolved id so the recorded profile exactly
        # matches the one whose policy bounds were enforced.
        "governance_profile_id": resolved_profile_id,
    }
    with open(session_dir / ".process_info.json", "w", encoding="utf-8") as f:
        json.dump(process_info, f, indent=2)

    return {
        "session_id":    session_id,
        "started_at":    started_at,
        "pid":           proc.pid,
        "session_dir":   str(session_dir),
        "seed_path":     str(seed_path),
        "context_files": written_names,
        "status":        "starting",
    }


# ============================================================
# Session continuation ("Continue from this session")
# ============================================================

def _load_parent_for_continuation(session_dir: Path) -> Dict[str, Any]:
    """
    Read a completed parent session's continuity artifacts so a child
    session can be seeded from its RESULT (not its full transcript).

    Returns a dict with:
      original_prompt   -- the parent's seed (governance_state.seed)
      prior_result      -- operator_summary.narrative_summary, the dense
                           prose conclusion of the parent run
      open_questions    -- carried-forward unresolved items
      notable_risks     -- risks flagged during the parent run
      artifact          -- {filename, path, artifact_id} of the last
                           produced artifact, or None

    Reads session_state.json (the canonical continuity artifact). Falls
    back to seed.md for the original prompt if session_state is absent
    (e.g. the parent never completed -- continuation is still allowed,
    we just have less to seed from).
    """
    state_path = session_dir / "session_state.json"
    seed_path  = session_dir / "seed.md"

    out: Dict[str, Any] = {
        "original_prompt": "",
        "prior_result":    "",
        "open_questions":  [],
        "notable_risks":   [],
        "artifact":        None,
    }

    state: Dict[str, Any] = {}
    if state_path.exists():
        try:
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = {}

    gov = state.get("governance_state") or {}
    out["original_prompt"] = (gov.get("seed") or "").strip()

    # Fallback for the original prompt: the raw seed file.
    if not out["original_prompt"] and seed_path.exists():
        try:
            out["original_prompt"] = seed_path.read_text(encoding="utf-8").strip()
        except Exception:
            pass

    op = state.get("operator_summary") or {}
    out["prior_result"]   = (op.get("narrative_summary") or "").strip()

    delib = state.get("deliberation_state") or {}
    # Prefer deliberation_state's lists; fall back to operator_summary's.
    out["open_questions"] = (
        delib.get("open_questions")
        or op.get("open_questions")
        or []
    )
    out["notable_risks"] = (
        delib.get("notable_risks")
        or op.get("notable_risks")
        or []
    )

    # Last produced artifact, if any. The .docx (a text_document) can be
    # copied into the child's context so ADAM can revise it; a .json
    # would NOT be extracted into the deliberation, so we only surface
    # extractable artifact types here.
    skill_state = state.get("skill_state") or {}
    invocations = skill_state.get("invocations") or []
    for inv in reversed(invocations):
        if inv.get("status") == "success" and inv.get("filename"):
            fmt = (inv.get("format") or "").lower()
            if fmt in ("docx", "md", "txt", "pdf", "html"):
                out["artifact"] = {
                    "filename":    inv["filename"],
                    "artifact_id": inv.get("artifact_id"),
                    # path is recorded relative to the ADAM root in the
                    # invocation record; we also know the on-disk location
                    # under the parent session dir's artifacts/ folder.
                    "session_relative": f"artifacts/{inv['filename']}",
                }
                break

    return out


def _compose_continuation_seed(parent: Dict[str, Any], follow_up: str) -> str:
    """
    Build the child session's seed.md text from the parent's result and
    the user's new follow-up prompt.

    The composed seed seeds on the parent's SYNTHESIS, not its transcript:
    cheaper in context and it tells the agents what was concluded without
    inviting them to re-litigate settled ground. The follow-up is the
    live task.
    """
    lines: List[str] = []

    if parent.get("original_prompt"):
        lines.append("## Original task (from the prior session)")
        lines.append("")
        lines.append(parent["original_prompt"])
        lines.append("")

    if parent.get("prior_result"):
        lines.append("## What the prior session concluded")
        lines.append("")
        lines.append(parent["prior_result"])
        lines.append("")

    oq = parent.get("open_questions") or []
    if oq:
        lines.append("## Open questions carried forward")
        lines.append("")
        for q in oq:
            lines.append(f"- {q}")
        lines.append("")

    nr = parent.get("notable_risks") or []
    if nr:
        lines.append("## Notable risks from the prior session")
        lines.append("")
        for r in nr:
            lines.append(f"- {r}")
        lines.append("")

    if parent.get("artifact"):
        lines.append(
            f"## Prior artifact\n\nThe prior session produced "
            f"`{parent['artifact']['filename']}`. If your task involves "
            f"revising it, the document's content has been provided as "
            f"context for this session."
        )
        lines.append("")

    lines.append("## Your task now")
    lines.append("")
    lines.append(follow_up.strip())

    return "\n".join(lines).strip() + "\n"


def _load_pause_state(session_dir: Path) -> Optional[Dict[str, Any]]:
    """Slice 4a: read a paused session's pause_state.json, or None."""
    p = session_dir / "pause_state.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _compose_resume_seed(pause: Dict[str, Any], guidance: str,
                         decision: str) -> str:
    """
    Slice 4a: build the resumed session's seed from the paused synthesis
    plus the director's review response. The deliberation is already
    settled (it paused at the terminal gate), so the resumed run does not
    re-deliberate -- it routes straight to Operator. This seed tells
    Operator what was concluded, that a human reviewed it, the director's
    decision (approve / redirect), and any guidance to apply.

    Uses the same composition pattern as the continuation builder: seed on
    the settled synthesis, not the transcript.
    """
    lines: List[str] = []
    lines.append("## Reviewed and settled plan (from the paused session)")
    lines.append("")
    lines.append(pause.get("final_synthesis_text", "").strip()
                 or "[no synthesis text captured at pause]")
    lines.append("")
    lines.append("## Human review")
    lines.append("")
    lines.append(f"This plan was paused for human review because: "
                 f"{pause.get('review_reason', 'review required')}")
    lines.append("")
    decision_label = {
        "approve":  "The director APPROVED the planned action.",
        "redirect": "The director REDIRECTED the planned action (see guidance).",
        "reject":   "The director REJECTED the planned action.",
    }.get(decision, "The director responded.")
    lines.append(decision_label)
    lines.append("")
    if guidance.strip():
        lines.append("## Director guidance")
        lines.append("")
        lines.append(guidance.strip())
        lines.append("")
    lines.append("## Your task now")
    lines.append("")
    if decision == "reject":
        lines.append(
            "The director rejected the planned action. Do NOT produce the "
            "originally planned artifact. Instead, produce a brief record "
            "noting that the action was reviewed and declined, incorporating "
            "any director guidance above."
        )
    else:
        lines.append(
            "Produce the deliverable as concluded above, applying the "
            "director's guidance. The plan has been reviewed and authorized; "
            "proceed to execution."
        )
    return "\n".join(lines).strip() + "\n"


def _compose_information_resume_seed(
    pause: Dict[str, Any],
    guidance: str,
    uploaded_names: List[str],
) -> str:
    """Slice 4b: seed for resuming mid-deliberation after an information pause."""
    lines: List[str] = []
    lines.append("## Resumed deliberation (information pause)")
    lines.append("")
    lines.append("The prior session paused mid-deliberation because:")
    lines.append(pause.get("information_reason", "additional information was required"))
    lines.append("")
    if pause.get("agent_text"):
        lines.append("## Synthesizer at pause")
        lines.append("")
        lines.append(str(pause.get("agent_text", "")).strip())
        lines.append("")
    if guidance.strip():
        lines.append("## Director guidance")
        lines.append("")
        lines.append(guidance.strip())
        lines.append("")
    if uploaded_names:
        lines.append("## New context documents")
        lines.append("")
        for name in uploaded_names:
            lines.append(f"- {name}")
        lines.append("")
    lines.append("## Continue")
    lines.append("")
    lines.append(
        "Resume deliberation with the information above. Do not repeat the "
        "pause unless a genuinely new material gap remains."
    )
    return "\n".join(lines).strip() + "\n"


def list_sessions(logs_dir: Path, user_id: str) -> List[Dict[str, Any]]:
    """
    Return a list of session summaries for the given director, newest
    first. Each summary carries just enough for the sidebar to render
    without loading the full session_state.

    The list reads from disk on every call. That's fine for the
    expected scale (low hundreds of sessions per director). If it ever
    becomes hot, a watchdog-based index cache fits cleanly here.
    """
    user_dir = logs_dir / user_id
    if not user_dir.is_dir():
        return []

    sessions: List[Dict[str, Any]] = []
    for entry in user_dir.iterdir():
        if not _is_session_dir(entry):
            continue
        sessions.append(_summarize_session(entry))

    # Sort by started_at descending; sessions without a started_at fall
    # to the end. Newest first matches the mockup's sidebar.
    sessions.sort(
        key=lambda s: s.get("started_at") or "",
        reverse=True,
    )
    return sessions


def _summarize_session(session_dir: Path) -> Dict[str, Any]:
    """
    Build the lightweight summary used by the sidebar. Reads session_state
    if present (the canonical source for completed sessions) and falls
    back to events.jsonl for sessions that are still active or never
    completed.
    """
    session_id = session_dir.name
    summary: Dict[str, Any] = {
        "session_id":  session_id,
        "title":       None,
        "prompt_full": None,           # full original prompt (untruncated)
        "started_at":  None,
        "ended_at":    None,
        "end_reason":  None,
        "status":      "unknown",      # unknown | starting | active | complete | errored
        "max_turns":   None,
        "turn_count":  None,
        "skills_used": 0,
        "has_events":  (session_dir / "events.jsonl").exists(),
        "process":     None,           # populated if .process_info.json exists
        "parent_session_id": None,     # set if this session continues another
        "governance_profile_id": None, # the profile governing this session
        "policy_blocked": False,       # Slice 3: gate prevented Operator
        "policy_block_reason": None,
        "awaiting_human_review": False, # Slice 4a: paused for director review
        "review_reason": None,
        "awaiting_information": False, # Slice 4b: paused for missing input
        "information_reason": None,
        "pause_type": None,
        "governance_boundary_blocked": False,
        "governance_boundary_reason": None,
        "refusal_terminated": False,
        "refusal_reason": None,
    }

    state_path  = session_dir / "session_state.json"
    events_path = session_dir / "events.jsonl"
    seed_path   = session_dir / "seed.md"
    proc_info   = _read_process_info(session_dir)

    # Part 9: surface basic process info for GUI-launched sessions.
    # Keep it small; the GUI doesn't need the full sidecar, just enough
    # to render a status badge and pull diagnostic logs on demand.
    if proc_info:
        summary["process"] = {
            "pid":          proc_info.get("pid"),
            "started_at":   proc_info.get("started_at"),
            "command":      proc_info.get("command"),
            "status":       proc_info.get("status"),
            "alive":        (
                _check_process_alive(proc_info["pid"])
                if isinstance(proc_info.get("pid"), int)
                else None
            ),
        }
        # Use the process info's started_at if we don't have anything
        # better yet (in particular, before events.jsonl exists).
        if summary["started_at"] is None and proc_info.get("started_at"):
            summary["started_at"] = proc_info["started_at"]

    if state_path.exists():
        # Completed session. session_state is authoritative.
        try:
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
            runtime = state.get("runtime_state", {}) or {}
            summary["started_at"] = runtime.get("started_at")
            summary["ended_at"]   = runtime.get("ended_at")
            summary["end_reason"] = runtime.get("end_reason")
            summary["max_turns"]  = runtime.get("max_turns")
            tcs = runtime.get("turn_counts", {}) or {}
            summary["turn_count"] = sum(
                v for k, v in tcs.items() if k != "Truthseeker"
            ) or None
            skill_state = state.get("skill_state", {}) or {}
            summary["skills_used"] = (skill_state.get("summary") or {}).get("total", 0)
            # Slice 3: governance / policy-blocked status. Surfaced as a
            # first-class status so a blocked session is visible in the
            # GUI, not buried in logs.
            gov = runtime.get("governance", {}) or {}
            summary["governance_profile_id"] = gov.get("profile_id") or summary.get("governance_profile_id")
            summary["policy_blocked"] = bool(gov.get("policy_blocked"))
            summary["policy_block_reason"] = gov.get("policy_block_reason")
            summary["awaiting_human_review"] = bool(gov.get("awaiting_human_review"))
            summary["review_reason"] = gov.get("review_reason")
            summary["awaiting_information"] = bool(gov.get("awaiting_information"))
            summary["information_reason"] = gov.get("information_reason")
            summary["governance_boundary_blocked"] = bool(gov.get("governance_boundary_blocked"))
            summary["governance_boundary_reason"] = gov.get("governance_boundary_reason")
            summary["refusal_terminated"] = bool(gov.get("refusal_terminated"))
            summary["refusal_reason"] = gov.get("refusal_reason")
            # Status from end_reason
            er = (summary["end_reason"] or "").lower()
            if gov.get("awaiting_human_review") or "awaiting_human_review" in er:
                summary["status"] = "awaiting_human_review"
            elif gov.get("awaiting_information") or "awaiting_information" in er:
                summary["status"] = "awaiting_information"
            elif gov.get("governance_boundary_blocked") or "governance_boundary_blocked" in er:
                summary["status"] = "governance_boundary_blocked"
            elif gov.get("refusal_terminated") or "refusal_terminated" in er:
                summary["status"] = "refusal_terminated"
            elif gov.get("policy_blocked") or "policy_blocked" in er:
                summary["status"] = "policy_blocked"
            elif "complete" in er:
                summary["status"] = "complete"
            elif "error" in er or "hard stop" in er:
                summary["status"] = "errored"
            else:
                summary["status"] = "complete"
        except Exception:
            pass

    if events_path.exists() and not state_path.exists():
        # Either still active or never reached session_state write.
        # Use events.jsonl to derive status and started_at. This block
        # runs regardless of whether started_at was already filled
        # from .process_info.json, because event-derived status is the
        # only way to know if a session is 'active' vs 'starting'.
        #
        # Bug fixed in Part 9.1: previously this was guarded by
        # `started_at is None`, which meant that GUI-launched sessions
        # (where .process_info.json's started_at was used first) never
        # reached the active-detection code path. The status stayed
        # 'unknown' and was then overwritten to 'starting' / 'errored'
        # by the proc_info fallback, even when events.jsonl had been
        # writing for minutes. Visible symptom: a running session
        # stuck at "STARTING ADAM..." in the GUI.
        try:
            # If we don't have a started_at yet, use the first event's ts.
            # If we do (from .process_info.json), prefer events.jsonl's
            # value since it reflects the actual session start, not the
            # process spawn time. They're usually within a second of
            # each other but events.jsonl is canonical.
            with open(events_path, encoding="utf-8") as f:
                first_line = f.readline()
                if first_line:
                    first = json.loads(first_line)
                    if first.get("ts"):
                        summary["started_at"] = first.get("ts")
            # Re-open to walk for session_ended
            last_event: Optional[Dict[str, Any]] = None
            for_count = 0
            with open(events_path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        try:
                            last_event = json.loads(line)
                            for_count += 1
                        except Exception:
                            continue
            if last_event:
                if last_event.get("event_type") == "session_ended":
                    summary["status"]     = "complete"
                    summary["end_reason"] = last_event.get("payload", {}).get("end_reason")
                    summary["ended_at"]   = last_event.get("payload", {}).get("ended_at")
                else:
                    # Last event isn't session_ended -> still active
                    summary["status"] = "active"
        except Exception:
            pass

    # Title: use the first ~80 chars of the seed if we can find it.
    # session_state stores it; fall back to peeking session.log.
    # prompt_full carries the untruncated prompt so the UI can reveal
    # the whole thing on demand (the title is only a sidebar preview).
    if state_path.exists():
        try:
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
            seed = (state.get("governance_state") or {}).get("seed")
            if seed:
                summary["title"] = seed.strip().splitlines()[0][:80]
                summary["prompt_full"] = seed.strip()
        except Exception:
            pass
    if not summary["title"] and events_path.exists():
        try:
            with open(events_path, encoding="utf-8") as f:
                for line in f:
                    e = json.loads(line)
                    if e.get("event_type") == "session_started":
                        seed = e.get("payload", {}).get("seed") or ""
                        summary["title"] = seed.strip().splitlines()[0][:80]
                        summary["prompt_full"] = seed.strip()
                        break
        except Exception:
            pass
    # Part 9: for GUI-launched sessions still in 'starting' state,
    # session_state.json doesn't exist yet and events.jsonl may be
    # empty. The seed file is the earliest source of truth for what
    # this session is about. Use the first non-blank line as the title.
    if not summary["title"] and seed_path.exists():
        try:
            text = seed_path.read_text(encoding="utf-8")
            stripped = text.strip()
            if stripped and not summary["prompt_full"]:
                summary["prompt_full"] = stripped
            for line in text.splitlines():
                line = line.strip()
                if line:
                    summary["title"] = line[:80]
                    break
        except Exception:
            pass

    # Lineage: surface parent_session_id from .process_info.json so the
    # sidebar can render the continued-from / continued-as relationship.
    if proc_info and proc_info.get("parent_session_id"):
        summary["parent_session_id"] = proc_info["parent_session_id"]
    if proc_info and proc_info.get("governance_profile_id"):
        summary["governance_profile_id"] = proc_info["governance_profile_id"]

    pause = _load_pause_state(session_dir)
    if pause:
        summary["pause_type"] = pause.get("pause_type")
        if pause.get("pause_type") == "information":
            summary["information_reason"] = (
                summary.get("information_reason") or pause.get("information_reason")
            )

    # Part 9: final status determination. Precedence:
    #   session_state.json present       -> complete or errored (per end_reason)
    #   events.jsonl with content        -> active (set earlier above)
    #   .process_info.json present       -> starting (PID alive) or errored (PID dead)
    #   nothing                          -> unknown
    if summary["status"] == "unknown" and proc_info:
        pid = proc_info.get("pid")
        alive = (
            _check_process_alive(pid)
            if isinstance(pid, int) else False
        )
        if alive:
            # Process running; events.jsonl not yet produced. The
            # GUI shows this as "starting" until the first event lands.
            summary["status"] = "starting"
        else:
            # Process is gone and no session_state.json was written.
            # ADAM crashed during startup or was killed externally.
            summary["status"] = "errored"

    return summary


# ============================================================
# Events streaming (Server-Sent Events)
# ============================================================

async def _tail_events_file(
    path: Path,
    request: Request,
) -> AsyncIterator[Dict[str, str]]:
    """
    Tail an events.jsonl file, yielding SSE-shaped events as new lines
    appear. Yields existing lines on first connection (catch-up) then
    polls for new bytes.

    Stops yielding when:
      - the client disconnects
      - a session_ended event is observed AND there are no further
        writes for SSE_IDLE_POLL_SECONDS (so the GUI sees the final
        event and then the stream closes cleanly)

    The "catch up first, then live tail" pattern is the simplest
    correct behavior: a GUI loading mid-session needs the back-events
    to derive current state, and then live updates from there.

    Part 9.1: if events.jsonl doesn't exist YET (GUI-launched session
    that just spawned ADAM and is in startup), wait for it to appear
    rather than bailing. Previously the SSE would close immediately
    in that case, leaving the GUI stranded at the StartingPanel even
    after ADAM came up and started emitting events.
    """
    # Tail position. Start at byte 0 so the GUI gets everything from
    # session_started forward.
    pos = 0
    saw_session_ended = False
    idle_since: Optional[float] = None
    waited_for_file = False    # True once we've started polling for the file to appear

    while True:
        if await request.is_disconnected():
            return

        if not path.exists():
            # Part 9.1: keep polling for the file to appear. ADAM
            # creates events.jsonl on its first emit_event call,
            # typically 5-15 seconds after spawn. We send a one-time
            # waiting event so the GUI can show a useful indicator.
            if not waited_for_file:
                yield {
                    "event": "awaiting_events",
                    "data":  json.dumps({"reason": "events.jsonl not yet created; waiting"}),
                }
                waited_for_file = True
            await asyncio.sleep(SSE_POLL_INTERVAL_SECONDS)
            continue

        try:
            stat = path.stat()
        except FileNotFoundError:
            await asyncio.sleep(SSE_POLL_INTERVAL_SECONDS)
            continue

        if stat.st_size > pos:
            # New bytes. Read from `pos` to end, yield each complete
            # line. If a line is partial (file mid-write), leave the
            # partial bytes for the next poll.
            with open(path, "rb") as f:
                f.seek(pos)
                chunk = f.read(stat.st_size - pos)
            # Find the last newline so we don't ship a partial line
            last_nl = chunk.rfind(b"\n")
            if last_nl == -1:
                # No complete line yet; defer until next poll
                await asyncio.sleep(SSE_POLL_INTERVAL_SECONDS)
                continue
            complete = chunk[: last_nl + 1].decode("utf-8", errors="replace")
            pos += last_nl + 1
            for line in complete.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    # Malformed line; skip but tell the GUI we did
                    yield {
                        "event": "malformed_event",
                        "data":  json.dumps({"raw": line[:500]}),
                    }
                    continue
                if obj.get("event_type") == "session_ended":
                    saw_session_ended = True
                yield {
                    "event": "adam_event",
                    "data":  json.dumps(obj),
                }
            idle_since = None
        else:
            # No new bytes
            if saw_session_ended:
                if idle_since is None:
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since > SSE_IDLE_POLL_SECONDS:
                    # Session is done and quiet. Close cleanly.
                    yield {"event": "stream_closed", "data": "{}"}
                    return
            await asyncio.sleep(SSE_POLL_INTERVAL_SECONDS)


# ============================================================
# FastAPI app
# ============================================================

def build_app(adam_root: Path, logs_dir: Path) -> FastAPI:
    """
    Construct the FastAPI app rooted at the given ADAM directory.
    adam_root is the directory containing .env and (typically) the
    logs/ subdirectory; logs_dir lets the operator point at a non-
    default location if the deployment has separated them.

    v5 multi-user: initializes the auth module against
    <adam_root>/gui/, which is where users.json and login_sessions.json
    live. The auth module is shared with manage_users.py CLI, so
    both processes see the same database.
    """
    app = FastAPI(
        title="ADAM GUI",
        description="Multi-user observer and director for ADAM deliberation sessions.",
        version="0.2.0",
    )

    # v5: locate gui/ relative to adam_root. users.json lives there.
    gui_root = adam_root / "gui"

    # Make .env the single source of truth for provider model keys: export the
    # api_key_env names from providers.json into os.environ (existing env wins)
    # so the in-process model seam (client_dispatch.call_model) can resolve them
    # without the key being duplicated in the systemd unit. Done early, before
    # any model-seam provider runs. Also exports the data-source connection
    # encryption key, so .env is the single per-deployment secret source.
    export_provider_keys_from_dotenv(
        adam_root, extra_names=(data_source_connections.ENCRYPTION_KEY_ENV,))

    auth.init_auth(gui_root)
    governance.init_governance(gui_root)   # Slice 1: data model only
    csrf.init_csrf(gui_root)               # Pass 1 hardening: CSRF signing secret

    # Pass 1 hardening: per-process login rate limiter. Stored on
    # app.state so it has app lifetime (one set of counters per process)
    # and is easy to swap in tests. In-memory, single-process, resets on
    # restart -- see ratelimit.py for the documented limitations.
    app.state.login_rate_limiter = ratelimit.LoginRateLimiter()

    # Data Sources: the ONE canonical ingestion store path lives under
    # adam_root; every data-source route resolves the store via
    # data_sources.get_pipeline_ingestion_store(). The model-fns and
    # connection-resolution SEAMS default to "not configured" (live query ->
    # MODEL_NOT_CONFIGURED / CONNECTION_NOT_CONFIGURED); tests override them on
    # app.state with fakes to exercise the full governed flow.
    data_sources.init_data_sources(adam_root)
    data_source_connections.init_connection_store(adam_root)
    app.state.pipeline_model_fns_provider = data_sources.default_model_fns_provider
    app.state.resolve_connection = data_sources.default_resolve_connection
    # connect_factory(**kwargs) -> connect_fn for admin test/introspect. Real
    # PyMySQL by default; tests inject a fake so no live server is needed.
    app.state.mysql_connect_factory = data_sources.make_pymysql_connect_fn

    # CORS: now restrictive because we use cookies. allow_credentials=True
    # is required for the browser to send the login cookie on cross-
    # origin requests, but allow_credentials=True is incompatible with
    # allow_origins=["*"]. For a single-origin deployment (which this
    # is in practice -- frontend and backend served by the same uvicorn)
    # CORS rarely matters at all. For dev where the frontend dev server
    # runs on a different port, set explicit origins below.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH"],
        allow_headers=["*"],
    )

    # v5: the .env director is no longer the canonical identity source --
    # users.json is. We don't read .env at all here. The legacy
    # resolve_director() helper remains in the module for any callers
    # that still need it (none in this file), but build_app() no longer
    # relies on it.

    # ============================================================
    # Authentication helpers
    # ============================================================
    #
    # Two helpers: require_user (401 if not logged in), and
    # require_session_access (additionally 403 if the requested
    # ADAM session is owned by someone else and the user isn't an
    # admin). Both are FastAPI dependencies so they slot cleanly
    # into the endpoint signatures.
    #
    # The cookie value is the login session token; auth.validate_login_session
    # turns it into a user record (with role expanded) or None.

    def current_user_optional(request: Request) -> Optional[Dict[str, Any]]:
        """Return the current user if logged in, else None. No raises."""
        token = request.cookies.get(LOGIN_COOKIE_NAME)
        return auth.validate_login_session(token)

    def require_user(request: Request) -> Dict[str, Any]:
        """Dependency: 401 if not logged in. Returns user record."""
        user = current_user_optional(request)
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        return user

    def require_admin(user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        """Dependency: 403 unless the user has the admin role."""
        if not is_admin(user):
            raise HTTPException(status_code=403, detail="admin access required")
        return user

    def require_csrf(
        request: Request,
        user: Dict[str, Any] = Depends(require_user),
    ) -> Dict[str, Any]:
        """
        Pass 1 hardening: enforce the signed double-submit CSRF token on
        authenticated MUTATING requests.

        Depends on require_user so the ordering is: unauthenticated ->
        401 (auth fires first), authenticated-but-bad-token -> 403. This
        preserves the existing 401-without-cookie contract while adding
        the 403-without-CSRF-token one.

        The token is validated against the request's adam_login token, so
        a token minted for one session can't be replayed in another. Only
        applied to the mutating endpoints (never to login or GETs).
        """
        login_token = request.cookies.get(LOGIN_COOKIE_NAME) or ""
        cookie_value = request.cookies.get(csrf.CSRF_COOKIE_NAME)
        header_value = request.headers.get(csrf.CSRF_HEADER_NAME)
        if not csrf.validate_token(cookie_value, header_value, login_token):
            raise HTTPException(
                status_code=403,
                detail="CSRF token missing or invalid",
            )
        return user

    def is_admin(user: Dict[str, Any]) -> bool:
        return user.get("role") == "admin"

    def user_owns_session(user: Dict[str, Any], session_id: str) -> bool:
        """
        Check whether `session_id` belongs to `user`. A session
        belongs to a user iff it lives under logs/<username>/<session_id>/.
        Admins are treated as owning all sessions (so they can debug
        any user's session). The directory must exist for non-admins.
        """
        if is_admin(user):
            return True
        return (logs_dir / user["username"] / session_id).is_dir()

    def resolve_session_dir(user: Dict[str, Any], session_id: str) -> Path:
        """
        Locate the on-disk session directory for `session_id`, given the
        authenticated user. For non-admins, the session must be under
        their own user directory or 404. For admins, search across all
        user directories (admin can inspect any user's session).

        Raises HTTPException(404) if not found, HTTPException(403) if
        the user is not allowed to see it.
        """
        # Non-admin: only their own directory.
        if not is_admin(user):
            sdir = logs_dir / user["username"] / session_id
            if not sdir.is_dir():
                # Could be either "doesn't exist" or "exists but you don't own it".
                # To avoid leaking the existence of other users' sessions, return
                # 404 in both cases.
                raise HTTPException(status_code=404, detail="session not found")
            return sdir

        # Admin: search all user dirs. logs_dir might not exist yet on a
        # fresh install; treat that as "no sessions".
        if not logs_dir.is_dir():
            raise HTTPException(status_code=404, detail="session not found")
        for user_dir in logs_dir.iterdir():
            if not user_dir.is_dir():
                continue
            candidate = user_dir / session_id
            if candidate.is_dir():
                return candidate
        raise HTTPException(status_code=404, detail="session not found")

    # ---- API routes ----

    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        # Public endpoint (no auth required). Reports basic server
        # state. Does NOT include director identity from .env -- in
        # multi-user mode the director concept is per-user, not global.
        return {
            "ok":              True,
            "adam_root":       str(adam_root.resolve()),
            "logs_dir":        str(logs_dir.resolve()),
            "logs_dir_exists": logs_dir.exists(),
            "auth_mode":       "users.json",
            "version":         "0.2.0",
        }

    @app.post("/api/auth/login")
    def login(payload: LoginRequest, response: Response,
              request: Request) -> Dict[str, Any]:
        """
        Authenticate a user via username + password. On success, set
        a login cookie and return the user's profile. On failure,
        return 401 with a generic error message (don't leak whether
        username exists vs password was wrong).

        Status semantics:
          - 200 + cookie set: logged in
          - 401: bad credentials
          - 403: user exists but suspended
        """
        # Source IP for rate-limit attribution and the audit record below.
        # We read the raw client IP (not X-Forwarded-For) -- behind a
        # reverse proxy this is the proxy, which is acceptable for the pilot.
        ip_address = request.client.host if request.client else ""

        # Pass 1 hardening: login rate limiting. Checked BEFORE touching
        # the user store so a throttled valid user and a throttled invalid
        # user get the identical 429 (no username enumeration).
        #
        # FAIL OPEN: the limiter is a speed bump, not an auth gate. If it
        # raises for any reason, log and let the login proceed -- a limiter
        # bug must never lock everyone out. (Deliberate asymmetry with
        # governance, which fails CLOSED.)
        limiter = getattr(request.app.state, "login_rate_limiter", None)
        try:
            retry_after = limiter.check(payload.username, ip_address) if limiter else None
        except Exception:
            import traceback
            print("WARNING: login rate limiter check failed; allowing login (fail-open)",
                  file=sys.stderr)
            traceback.print_exc()
            retry_after = None
        if retry_after is not None:
            # Generic message + Retry-After. Same response whether or not
            # the username exists.
            raise HTTPException(
                status_code=429,
                detail="too many login attempts; please try again later",
                headers={"Retry-After": str(retry_after)},
            )

        user = auth.get_user(payload.username)
        if user is None or not auth.verify_password(
            payload.password, user.get("password_hash", "")
        ):
            # Failed attempt: count it against username + IP (fail-open).
            try:
                if limiter:
                    limiter.record_failure(payload.username, ip_address)
            except Exception:
                import traceback
                print("WARNING: login rate limiter record_failure failed (fail-open)",
                      file=sys.stderr)
                traceback.print_exc()
            # Generic message so attackers can't enumerate usernames
            raise HTTPException(status_code=401, detail="invalid credentials")
        if user.get("status") != "active":
            raise HTTPException(
                status_code=403,
                detail="account is not active",
            )

        # Successful credential check: clear the username's failure counter
        # so earlier typos don't penalize a now-correct login (fail-open).
        try:
            if limiter:
                limiter.reset_username(payload.username)
        except Exception:
            import traceback
            print("WARNING: login rate limiter reset_username failed (fail-open)",
                  file=sys.stderr)
            traceback.print_exc()

        # Create login session. We record user-agent and IP for audit;
        # the IP from request.client is good enough behind a reverse
        # proxy (Caddy/nginx) because the proxy populates X-Forwarded-For
        # which FastAPI doesn't auto-trust -- we read the raw client
        # IP. For a small beta this is fine; richer attribution is a
        # later concern.
        user_agent = request.headers.get("user-agent", "")
        # ip_address was resolved above for rate-limit attribution.
        token = auth.create_login_session(
            payload.username,
            user_agent=user_agent,
            ip_address=ip_address,
        )

        # secure=True requires HTTPS. We set it conditionally based on
        # whether the request came over TLS (which the reverse proxy
        # will indicate via X-Forwarded-Proto on prod). On a dev box
        # without TLS we want the cookie to still work, so we accept
        # secure=False there. samesite=lax balances CSRF defense with
        # normal user navigation; httponly prevents JS access to the
        # cookie value.
        is_secure = request.headers.get("x-forwarded-proto", "").lower() == "https" \
                    or request.url.scheme == "https"
        response.set_cookie(
            key=LOGIN_COOKIE_NAME,
            value=token,
            max_age=LOGIN_COOKIE_MAX_AGE,
            httponly=True,
            secure=is_secure,
            samesite="lax",
            path="/",
        )

        # Pass 1 hardening: issue a signed CSRF token bound to this login
        # session. Unlike the login cookie, adam_csrf is httponly=False so
        # the frontend can read it and echo it back in X-CSRF-Token (the
        # "double submit"). samesite=lax / secure-when-deployed match the
        # login cookie. The value is signed, so an attacker can't forge it.
        response.set_cookie(
            key=csrf.CSRF_COOKIE_NAME,
            value=csrf.issue_token(token),
            max_age=LOGIN_COOKIE_MAX_AGE,
            httponly=False,
            secure=is_secure,
            samesite="lax",
            path="/",
        )

        return _whoami_payload(user)

    @app.post("/api/auth/logout")
    def logout(
        request: Request,
        response: Response,
        _csrf: Dict[str, Any] = Depends(require_csrf),
    ) -> Dict[str, str]:
        """
        Invalidate the current login session and clear the cookie.

        Pass 1 hardening: this mutating endpoint now requires a valid
        CSRF token (and therefore a valid login session). A request with
        no session yields 401 (from require_user); a session without a
        valid CSRF token yields 403. Both the login and CSRF cookies are
        cleared on success.
        """
        token = request.cookies.get(LOGIN_COOKIE_NAME)
        auth.delete_login_session(token)
        response.delete_cookie(LOGIN_COOKIE_NAME, path="/")
        response.delete_cookie(csrf.CSRF_COOKIE_NAME, path="/")
        return {"status": "logged_out"}

    @app.get("/api/auth/whoami", response_model=WhoamiResponse)
    def whoami(request: Request, response: Response) -> WhoamiResponse:
        """
        Return the currently logged-in user's profile, or 401 if not
        logged in. Frontend calls this on every page load to decide
        whether to render the login screen or the dashboard.

        Pass 1 hardening: whoami is a GET (no CSRF required to call it),
        but it doubles as the recovery path for the CSRF cookie. If a
        valid login session exists without an adam_csrf cookie -- e.g. a
        user logged in before this hardening shipped, or the cookie
        expired/was cleared -- we mint and set one here so their next
        mutating request isn't rejected with a 403. This runs on every
        page load, so active sessions self-heal.
        """
        user = current_user_optional(request)
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")

        if not request.cookies.get(csrf.CSRF_COOKIE_NAME):
            login_token = request.cookies.get(LOGIN_COOKIE_NAME) or ""
            if login_token:
                is_secure = (
                    request.headers.get("x-forwarded-proto", "").lower() == "https"
                    or request.url.scheme == "https"
                )
                response.set_cookie(
                    key=csrf.CSRF_COOKIE_NAME,
                    value=csrf.issue_token(login_token),
                    max_age=LOGIN_COOKIE_MAX_AGE,
                    httponly=False,
                    secure=is_secure,
                    samesite="lax",
                    path="/",
                )

        return WhoamiResponse(**_whoami_payload(user))

    def _whoami_payload(user: Dict[str, Any]) -> Dict[str, Any]:
        """Build the dict returned by /login and /whoami. Pure helper."""
        locked = auth.is_quota_locked_user(user)
        return {
            "username":              user["username"],
            "display_name":          user.get("display_name", user["username"]),
            "email":                 user.get("email", ""),
            "role":                  user.get("role", ""),
            "sessions_remaining":    user.get("sessions_remaining", 0),
            "max_turns_per_session": user.get("max_turns_per_session", 0),
            "skills_denied":         auth.skills_denied_for_user(user),
            "governance_profile":    auth.assigned_governance_profile(user) if locked else None,
            "governance_profile_locked": locked,
            # Forced password change: missing field on legacy records reads
            # as False. Surfaced on both /login and /whoami so the frontend
            # can route to the change-password screen and keep enforcing it
            # across page reloads until the user changes it.
            "must_change_password": bool(user.get("must_change_password", False)),
        }

    @app.post("/api/auth/change-password")
    def change_password(
        payload: ChangePasswordRequest,
        user: Dict[str, Any] = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """
        Authenticated user-driven password change. Used both for the
        normal "change my password" action and for the forced first-login
        change when must_change_password is set.

        require_csrf depends on require_user, so this is 401 when not
        logged in and 403 on a missing/invalid CSRF token; it returns the
        authenticated user record. We verify the current password, enforce
        a minimum new-password length, then set the new password and clear
        must_change_password via auth.set_user_password(must_change=False).
        """
        username = user["username"]
        if not auth.verify_password(
            payload.current_password, user.get("password_hash", "")
        ):
            raise HTTPException(status_code=400, detail="current password is incorrect")

        new_pw = payload.new_password
        if len(new_pw) < MIN_PASSWORD_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=f"new password must be at least {MIN_PASSWORD_LENGTH} characters",
            )
        if new_pw == payload.current_password:
            raise HTTPException(
                status_code=400,
                detail="new password must differ from the current password",
            )

        try:
            auth.set_user_password(username, new_pw, must_change=False)
        except KeyError:
            # Authenticated but the record vanished (deleted mid-session).
            raise HTTPException(status_code=404, detail="user not found") from None
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        return {"status": "password_changed", "must_change_password": False}

    @app.get("/api/director")
    def get_director(request: Request) -> Dict[str, Any]:
        """
        v5 deprecation note: /api/director is kept for backward
        compatibility with frontends written against the v0.1
        single-user API. New code should call /api/auth/whoami
        which is auth-aware. This endpoint now returns the
        currently authenticated user's identity in the same shape
        the old endpoint returned for the .env-configured director.
        Returns 401 if not logged in.
        """
        user = current_user_optional(request)
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        return {
            "user_id":      user["username"],
            "email":        user.get("email", ""),
            "display_name": user.get("display_name", user["username"]),
            "source":       "users.json",
        }

    @app.get("/api/sessions")
    def get_sessions(request: Request) -> Dict[str, Any]:
        """
        List sessions visible to the current user.

        - Regular user (pilot): only sessions under logs/<their_username>/
        - Admin: all sessions across all users. Each session is tagged
          with an `owner` field so the GUI can render which user it
          belongs to.
        """
        user = current_user_optional(request)
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")

        if is_admin(user):
            # Walk all user dirs and aggregate. We tag each session
            # with its owner so the admin UI can group/filter.
            all_sessions: List[Dict[str, Any]] = []
            if logs_dir.is_dir():
                for user_dir in sorted(logs_dir.iterdir()):
                    if not user_dir.is_dir():
                        continue
                    owner = user_dir.name
                    user_sessions = list_sessions(logs_dir, owner)
                    for s in user_sessions:
                        s["owner"] = owner
                    all_sessions.extend(user_sessions)
            # Sort by started_at descending (newest first), nulls last
            all_sessions.sort(
                key=lambda s: (s.get("started_at") is None, s.get("started_at") or ""),
                reverse=True,
            )
            return {"sessions": all_sessions}

        # Non-admin: just their own dir.
        sessions = list_sessions(logs_dir, user["username"])
        for s in sessions:
            s["owner"] = user["username"]
        return {"sessions": sessions}

    @app.post(
        "/api/sessions",
        response_model=NewSessionResponse,
        status_code=201,
    )
    async def post_new_session(
        request:       Request,
        seed:          str          = Form(...),
        max_turns:     Optional[int] = Form(None),
        no_verify:     bool         = Form(False),
        context_files: List[UploadFile] = File(default=[]),
        governance_profile_id: Optional[str] = Form(None),
        _csrf:         Dict[str, Any] = Depends(require_csrf),
    ) -> NewSessionResponse:
        """
        Create a new ADAM session and spawn the deliberation in the
        background.

        v5 multi-user changes:
          - Requires authentication (cookie). 401 if not logged in.
          - Quota enforced: 403 if user has 0 sessions remaining.
          - Suspended users: 403 with "inactive" reason.
          - max_turns: pilots' submitted value is IGNORED in favor of
            users.json's max_turns_per_session (server-side enforcement
            of the role limit; the disabled UI field is only UX honesty).
          - Skill denial: --disable-skill flags built from the user's
            role's skills_denied list.
          - Decrement on successful spawn. Failure to spawn does NOT
            charge the user a session.

        Multipart form fields (unchanged):
          - seed         (required, str)   : deliberation seed text
          - max_turns    (optional, int)   : honored only for admin
          - no_verify    (optional, bool)  : disable Truthseeker
          - context_files (optional, list of files) : input_context/
        """
        user = current_user_optional(request)
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")

        # Quota check. can_start_session checks status + remaining.
        # Suspended -> "inactive" message. 0 remaining -> "fully used"
        # message. Both are 403 because the user is authenticated but
        # not authorized to perform this action right now.
        allowed, reason = auth.can_start_session(user)
        if not allowed:
            raise HTTPException(status_code=403, detail=reason)

        # Validate seed
        seed_clean = (seed or "").strip()
        if not seed_clean:
            raise HTTPException(status_code=422, detail="seed is required")
        if len(seed_clean) > SEED_MAX_CHARS:
            raise HTTPException(
                status_code=422,
                detail=f"seed exceeds maximum length ({SEED_MAX_CHARS} chars)",
            )

        # max_turns: resolve effective value. For admin (max_turns_per_session
        # == -1), the user's submitted value is honored if 1..200. For
        # pilots, the user's submitted value is IGNORED and the quota
        # value is used. This is the server-side enforcement that backs
        # the disabled UI field; a hand-crafted curl request can't bypass it.
        effective_turns = auth.effective_max_turns(user, max_turns)
        if effective_turns is not None:
            if effective_turns < 1 or effective_turns > 200:
                raise HTTPException(
                    status_code=422,
                    detail="max_turns must be between 1 and 200",
                )

        # Validate context_files. UploadFile from FastAPI is async; we
        # read each file's bytes here. The reads happen sequentially
        # rather than in parallel because the file count is small.
        if len(context_files) > MAX_CONTEXT_FILES:
            raise HTTPException(
                status_code=422,
                detail=f"too many context files (max {MAX_CONTEXT_FILES})",
            )
        files_data: List[Dict[str, Any]] = []
        for upload in context_files:
            if not upload or not upload.filename:
                continue
            data = await upload.read()
            if len(data) > MAX_CONTEXT_FILE_BYTES:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"context file '{upload.filename}' exceeds size limit "
                        f"({MAX_CONTEXT_FILE_BYTES // (1024*1024)} MB)"
                    ),
                )
            files_data.append({
                "filename": upload.filename,
                "bytes":    data,
            })

        # Compute per-user skill denials. For admin this is [], for
        # pilot it's ["email"] (configurable in users.json/roles).
        denied = auth.skills_denied_for_user(user)

        # Server-side governance-profile enforcement (mirrors the turn
        # clamp above): admins get the profile they chose; pilots are
        # forced to their assigned profile regardless of what the form
        # sent. The disabled picker in the UI is only a hint for this.
        effective_profile = auth.effective_governance_profile(user, governance_profile_id)

        # All validation passed; create the session under this user's
        # logs dir. We pass the authenticated user's username (NOT the
        # .env director) so the session lives under their own dir and
        # is owned by them for all future endpoint access checks.
        try:
            result = spawn_adam_session(
                adam_root      = adam_root,
                logs_dir       = logs_dir,
                user_id        = user["username"],
                display_name   = user.get("display_name", user["username"]),
                email          = user.get("email", ""),
                seed_text      = seed_clean,
                context_files  = files_data,
                max_turns      = effective_turns,
                no_verify      = no_verify,
                disable_skills = denied,
                governance_profile_id = effective_profile,
            )
        except HTTPException:
            # Spawn failed; the session_dir may have been partially
            # created and is left in place for forensics. We do NOT
            # decrement the user's session count -- they shouldn't pay
            # for an infrastructure failure.
            raise

        # Decrement on successful spawn. -1 (unlimited) is a no-op in
        # auth.decrement_sessions_remaining. The call is atomic;
        # concurrent spawns can't accidentally double-decrement or
        # miss a decrement.
        try:
            auth.decrement_sessions_remaining(user["username"])
        except Exception:
            # If decrement fails for some reason (disk full, perms),
            # the spawn already succeeded. We log but don't fail the
            # response -- the user will see their session running.
            # The admin can fix the counter manually via manage_users.py.
            import traceback
            print(
                f"WARNING: failed to decrement sessions_remaining for "
                f"{user['username']!r}; session {result['session_id']} "
                f"spawned successfully but counter not updated",
                file=sys.stderr,
            )
            traceback.print_exc()

        return NewSessionResponse(
            session_id    = result["session_id"],
            started_at    = result["started_at"],
            pid           = result["pid"],
            session_dir   = result["session_dir"],
            seed_path     = result["seed_path"],
            context_files = result["context_files"],
            status        = result["status"],
        )

    @app.post("/api/sessions/{parent_id}/continue")
    async def post_continue_session(
        parent_id:     str,
        request:       Request,
        seed:          str          = Form(...),
        max_turns:     Optional[int] = Form(None),
        no_verify:     bool         = Form(False),
        context_files: List[UploadFile] = File(default=[]),
        governance_profile_id: Optional[str] = Form(None),
        _csrf:         Dict[str, Any] = Depends(require_csrf),
    ) -> NewSessionResponse:
        """
        Continue from a completed session: create a NEW (child) session
        seeded from the parent's RESULT plus a follow-up prompt.

        This mirrors POST /api/sessions exactly -- same auth, same quota
        enforcement, same per-role turn clamp, same skill denials, same
        decrement-on-success, same response -- with three differences:

          1. The parent session is resolved and authorized first (404 if
             it doesn't exist or isn't the user's).
          2. The `seed` form field is the FOLLOW-UP prompt. The actual
             seed written to the child is composed by _compose_continuation_seed
             from the parent's original prompt + narrative_summary +
             open questions/risks + this follow-up.
          3. If the parent produced an extractable artifact (e.g. a
             .docx), it is copied into the child's context so ADAM can
             revise it, and the child's .process_info.json records
             parent_session_id for lineage.

        A continuation is a full session: for pilots it costs one
        sessions_remaining and its turn count is clamped to the role's
        max_turns_per_session, identical to a fresh session.
        """
        user = current_user_optional(request)
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")

        # Resolve + authorize the parent (raises 404/403 itself).
        parent_dir = resolve_session_dir(user, parent_id)

        # Quota check -- identical to a fresh session.
        allowed, reason = auth.can_start_session(user)
        if not allowed:
            raise HTTPException(status_code=403, detail=reason)

        # The follow-up prompt is what the user typed.
        follow_up = (seed or "").strip()
        if not follow_up:
            raise HTTPException(status_code=422, detail="a follow-up prompt is required")

        # Build the composed child seed from the parent's continuity
        # artifacts. This reads session_state.json (with a seed.md
        # fallback for the original prompt).
        parent = _load_parent_for_continuation(parent_dir)
        composed_seed = _compose_continuation_seed(parent, follow_up)
        if len(composed_seed) > SEED_MAX_CHARS:
            # Extremely unlikely (narrative_summary is one paragraph),
            # but guard the same cap the fresh path enforces. Trim the
            # composed seed's prior-result section rather than fail hard.
            composed_seed = composed_seed[:SEED_MAX_CHARS]

        # Turn clamp -- identical server-side enforcement to fresh path.
        effective_turns = auth.effective_max_turns(user, max_turns)
        if effective_turns is not None:
            if effective_turns < 1 or effective_turns > 200:
                raise HTTPException(
                    status_code=422,
                    detail="max_turns must be between 1 and 200",
                )

        # Read any user-uploaded context files (same validation as fresh).
        if len(context_files) > MAX_CONTEXT_FILES:
            raise HTTPException(
                status_code=422,
                detail=f"too many context files (max {MAX_CONTEXT_FILES})",
            )
        files_data: List[Dict[str, Any]] = []
        for upload in context_files:
            if not upload or not upload.filename:
                continue
            data = await upload.read()
            if len(data) > MAX_CONTEXT_FILE_BYTES:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"context file '{upload.filename}' exceeds size limit "
                        f"({MAX_CONTEXT_FILE_BYTES // (1024*1024)} MB)"
                    ),
                )
            files_data.append({"filename": upload.filename, "bytes": data})

        # If the parent produced an extractable artifact, carry it into
        # the child's context so ADAM can revise it. Read it from the
        # parent's artifacts/ dir. Failure to read is non-fatal -- the
        # continuation still proceeds with the composed text seed.
        if parent.get("artifact"):
            art = parent["artifact"]
            art_path = parent_dir / art["session_relative"]
            try:
                if art_path.is_file():
                    art_bytes = art_path.read_bytes()
                    if len(art_bytes) <= MAX_CONTEXT_FILE_BYTES:
                        files_data.append({
                            "filename": art["filename"],
                            "bytes":    art_bytes,
                        })
            except Exception:
                pass

        denied = auth.skills_denied_for_user(user)

        # Governance profile: a continuation INHERITS the parent's profile
        # by default. An explicit governance_profile_id on the request
        # overrides it (e.g. continue under stricter bounds). If the
        # parent has none recorded, resolve_profile_id falls back to the
        # default profile.
        if governance_profile_id:
            child_profile_id = governance_profile_id
        else:
            parent_proc = _read_process_info(parent_dir) or {}
            child_profile_id = parent_proc.get("governance_profile_id")

        try:
            result = spawn_adam_session(
                adam_root         = adam_root,
                logs_dir          = logs_dir,
                user_id           = user["username"],
                display_name      = user.get("display_name", user["username"]),
                email             = user.get("email", ""),
                seed_text         = composed_seed,
                context_files     = files_data,
                max_turns         = effective_turns,
                no_verify         = no_verify,
                disable_skills    = denied,
                parent_session_id = parent_id,
                governance_profile_id = child_profile_id,
            )
        except HTTPException:
            raise

        # Decrement on success -- identical to fresh path.
        try:
            auth.decrement_sessions_remaining(user["username"])
        except Exception:
            import traceback
            print(
                f"WARNING: failed to decrement sessions_remaining for "
                f"{user['username']!r}; continuation session "
                f"{result['session_id']} spawned but counter not updated",
                file=sys.stderr,
            )
            traceback.print_exc()

        return NewSessionResponse(
            session_id    = result["session_id"],
            started_at    = result["started_at"],
            pid           = result["pid"],
            session_dir   = result["session_dir"],
            seed_path     = result["seed_path"],
            context_files = result["context_files"],
            status        = result["status"],
        )

    @app.post("/api/sessions/{paused_id}/resume")
    async def post_resume_session(
        paused_id:     str,
        request:       Request,
        guidance:      str          = Form(""),
        decision:      str          = Form("approve"),
        max_turns:     Optional[int] = Form(None),
        no_verify:     bool         = Form(False),
        context_files: List[UploadFile] = File(default=[]),
        _csrf:         Dict[str, Any] = Depends(require_csrf),
    ) -> NewSessionResponse:
        """
        Slice 4a/4b: resume a paused session.

        pause_type gate_review (4a): routes straight to Operator.
        pause_type information (4b): restores deliberation history and continues.
        """
        user = current_user_optional(request)
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")

        paused_dir = resolve_session_dir(user, paused_id)

        pause = _load_pause_state(paused_dir)
        if pause is None:
            raise HTTPException(
                status_code=409,
                detail="this session is not paused "
                       "(no pause_state.json found)",
            )

        pause_type = pause.get("pause_type") or "gate_review"

        decision = (decision or "approve").strip().lower()
        if pause_type == "gate_review" and decision not in ("approve", "redirect", "reject"):
            raise HTTPException(
                status_code=422,
                detail="decision must be approve, redirect, or reject",
            )
        if pause_type == "information" and not guidance.strip() and not context_files:
            raise HTTPException(
                status_code=422,
                detail="provide guidance text or at least one context document",
            )

        allowed, reason = auth.can_start_session(user)
        if not allowed:
            raise HTTPException(status_code=403, detail=reason)

        effective_turns = auth.effective_max_turns(user, max_turns)
        if effective_turns is not None and (effective_turns < 1 or effective_turns > 200):
            raise HTTPException(status_code=422, detail="max_turns must be between 1 and 200")

        if len(context_files) > MAX_CONTEXT_FILES:
            raise HTTPException(
                status_code=422,
                detail=f"too many context files (max {MAX_CONTEXT_FILES})",
            )
        files_data: List[Dict[str, Any]] = []
        for upload in context_files:
            if not upload or not upload.filename:
                continue
            data = await upload.read()
            if len(data) > MAX_CONTEXT_FILE_BYTES:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"context file '{upload.filename}' exceeds size limit "
                        f"({MAX_CONTEXT_FILE_BYTES // (1024*1024)} MB)"
                    ),
                )
            files_data.append({"filename": upload.filename, "bytes": data})

        denied = auth.skills_denied_for_user(user)

        paused_proc = _read_process_info(paused_dir) or {}
        resume_profile_id = paused_proc.get("governance_profile_id") \
            or pause.get("governance_profile_id")

        uploaded_names = [f["filename"] for f in files_data]

        if pause_type == "information":
            composed_seed = _compose_information_resume_seed(
                pause, guidance, uploaded_names,
            )
            resume_after_review = False
            resume_after_information = True
            audit_event = "information_pause_resolved"
        else:
            composed_seed = _compose_resume_seed(pause, guidance, decision)
            resume_after_review = True
            resume_after_information = False
            audit_event = "human_review_resolved"

        if len(composed_seed) > SEED_MAX_CHARS:
            composed_seed = composed_seed[:SEED_MAX_CHARS]

        result = spawn_adam_session(
            adam_root         = adam_root,
            logs_dir          = logs_dir,
            user_id           = user["username"],
            display_name      = user.get("display_name", user["username"]),
            email             = user.get("email", ""),
            seed_text         = composed_seed,
            context_files     = files_data,
            max_turns         = effective_turns,
            no_verify         = no_verify,
            disable_skills    = denied,
            parent_session_id = paused_id,
            governance_profile_id = resume_profile_id,
            resume_after_review = resume_after_review,
            resume_after_information = resume_after_information,
        )

        try:
            audit_path = paused_dir / "audit.jsonl"
            with audit_path.open("a", encoding="utf-8") as af:
                audit_row: Dict[str, Any] = {
                    "event":         audit_event,
                    "guidance":      guidance.strip(),
                    "uploaded_docs": uploaded_names,
                    "resumed_as":    result["session_id"],
                    "resolved_by":   user["username"],
                    "pause_type":    pause_type,
                    "ts":            datetime.now().isoformat(timespec="seconds"),
                }
                if pause_type == "gate_review":
                    audit_row["decision"] = decision
                af.write(json.dumps(audit_row) + "\n")
        except Exception:
            pass

        try:
            auth.decrement_sessions_remaining(user["username"])
        except Exception:
            pass

        return NewSessionResponse(
            session_id    = result["session_id"],
            started_at    = result["started_at"],
            pid           = result["pid"],
            session_dir   = result["session_dir"],
            seed_path     = result["seed_path"],
            context_files = result["context_files"],
            status        = result["status"],
        )

    @app.get("/api/governance/profiles")
    def get_governance_profiles(request: Request) -> Dict[str, Any]:
        """List available governance profiles (Slice 1: informational).
        The GUI can use this to populate a profile picker. Auth required
        so we don't leak config to anonymous callers."""
        user = current_user_optional(request)
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        return {
            "default_profile_id": governance.default_profile_id(),
            "profiles": governance.list_profiles(),
        }

    @app.get("/api/admin/governance")
    def get_admin_governance(user: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        """Slice 4.2 Phase 1: read-only governance config for admins.
        Includes rulesets, profiles, plain-language summaries, which
        fields are enforced at runtime, and validation of the live file."""
        skill_universe = _discover_skill_universe(adam_root)
        return governance.get_admin_view(skill_universe)

    @app.post("/api/admin/governance/validate")
    def validate_admin_governance(
        payload: Dict[str, Any] = Body(...),
        user: Dict[str, Any] = Depends(require_admin),
        _csrf: Dict[str, Any] = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """Slice 4.2: validate a proposed governance config without saving."""
        skill_universe = _discover_skill_universe(adam_root)
        normalized = governance.normalize_governance_data(payload)
        return governance.validate_governance_data(normalized, skill_universe)

    @app.put("/api/admin/governance")
    def put_admin_governance(
        payload: Dict[str, Any] = Body(...),
        user: Dict[str, Any] = Depends(require_admin),
        _csrf: Dict[str, Any] = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """Slice 4.2 Phase 2: validate and save governance.json, then hot-reload."""
        skill_universe = _discover_skill_universe(adam_root)
        try:
            governance.save_governance_data(payload, skill_universe)
        except ValueError as e:
            errors = e.args[0] if e.args else ["validation failed"]
            if not isinstance(errors, list):
                errors = [str(errors)]
            raise HTTPException(
                status_code=400,
                detail={"errors": errors, "message": "governance validation failed"},
            ) from e
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        return governance.get_admin_view(skill_universe)

    @app.get("/api/admin/users")
    def get_admin_users(user: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        """Slice 4.2 Phase 3: list users for governance profile assignment."""
        profiles = governance.list_profiles()
        return {
            "users": auth.admin_user_summaries(),
            "default_profile_id": governance.default_profile_id(),
            "profiles": profiles,
        }

    @app.patch("/api/admin/users/{username}/governance-profile")
    def patch_user_governance_profile(
        username: str,
        body: UserGovernanceProfileUpdate,
        admin: Dict[str, Any] = Depends(require_admin),
        _csrf: Dict[str, Any] = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """Assign or clear a user's governance profile (stored on users.json)."""
        target = auth.get_user(username)
        if target is None:
            raise HTTPException(status_code=404, detail="user not found")

        profile_id = body.governance_profile
        if profile_id is not None and profile_id.strip() == "":
            profile_id = None
        if profile_id is not None:
            valid_ids = {p.get("id") for p in governance.list_profiles()}
            if profile_id not in valid_ids:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown governance profile: {profile_id!r}",
                )

        try:
            auth.set_user_governance_profile(username, profile_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="user not found") from None

        updated = auth.get_user(username)
        return {
            "username": username,
            "governance_profile": updated.get("governance_profile"),
            "effective_assigned_profile": auth.assigned_governance_profile(updated),
        }

    # ---- Admin user CRUD (usercrud pass) ----
    #
    # All endpoints below are admin-only (require_admin) and CSRF-protected
    # (require_csrf), and reuse the existing auth.py user layer -- they do
    # not reimplement storage, hashing, or locking. "Delete" is deliberately
    # NOT exposed: the UI's delete action maps to suspend, so user history
    # stays intact and attributable. auth.delete_user() is never called here.

    def _user_summary(username: str) -> Dict[str, Any]:
        """The sanitized admin summary for one user (or a minimal stub)."""
        for s in auth.admin_user_summaries():
            if s.get("username") == username:
                return s
        return {"username": username}

    def _valid_roles() -> set:
        """Defined roles (admin/pilot), with a safe fallback."""
        try:
            roles = set(auth.list_roles().keys())
        except Exception:
            roles = set()
        return roles or {"admin", "pilot"}

    def _email_looks_valid(email: str) -> bool:
        # Same rule auth.add_user enforces, applied here for edits.
        return "@" in email and "." in email.split("@")[-1]

    def _count_active_admins() -> int:
        return sum(
            1 for s in auth.admin_user_summaries()
            if s.get("role") == "admin" and s.get("status") == "active"
        )

    @app.post("/api/admin/users")
    def create_user(
        body: CreateUserRequest,
        admin: Dict[str, Any] = Depends(require_admin),
        _csrf: Dict[str, Any] = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """
        Create a user with a server-generated temporary password, shown to
        the admin exactly once. The new user must change it on first login
        (must_change_password=True). Returns the user summary + temp password.
        """
        username = body.username.strip()
        if body.role not in _valid_roles():
            raise HTTPException(
                status_code=400,
                detail=f"role must be one of {sorted(_valid_roles())}",
            )

        temp_password = auth.generate_temp_password()
        try:
            auth.add_user(
                username,
                display_name=body.display_name,
                email=body.email,
                role=body.role,
                password=temp_password,
                status="active",
                sessions_remaining=body.sessions_remaining,
                max_turns_per_session=body.max_turns_per_session,
            )
        except KeyError as e:
            # add_user raises KeyError when the username already exists.
            raise HTTPException(
                status_code=409,
                detail="a user with that username already exists",
            ) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        # Force a password change on first login. add_user doesn't set this
        # flag (it's shared with the CLI/register paths), so we set it here.
        try:
            auth.set_user_password(username, temp_password, must_change=True)
        except Exception:
            # The account exists and is usable; the flag is best-effort.
            import traceback
            traceback.print_exc()

        return {
            "user": _user_summary(username),
            "temporary_password": temp_password,
        }

    @app.patch("/api/admin/users/{username}")
    def edit_user(
        username: str,
        body: EditUserRequest,
        admin: Dict[str, Any] = Depends(require_admin),
        _csrf: Dict[str, Any] = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """
        Edit profile fields only (display_name, email, role, quotas). Does
        NOT change password or status (those have dedicated endpoints). An
        admin cannot change their own role.
        """
        target = auth.get_user(username)
        if target is None:
            raise HTTPException(status_code=404, detail="user not found")

        if body.display_name is not None and not body.display_name.strip():
            raise HTTPException(status_code=400, detail="display_name cannot be empty")
        if body.email is not None and not _email_looks_valid(body.email):
            raise HTTPException(status_code=400, detail="email does not look valid")
        if body.role is not None and body.role not in _valid_roles():
            raise HTTPException(
                status_code=400,
                detail=f"role must be one of {sorted(_valid_roles())}",
            )
        if (body.role is not None
                and username == admin["username"]
                and body.role != target.get("role")):
            raise HTTPException(status_code=400, detail="you cannot change your own role")

        def _mod(user: Dict[str, Any]) -> None:
            if body.display_name is not None:
                user["display_name"] = body.display_name.strip()
            if body.email is not None:
                user["email"] = body.email
            if body.role is not None:
                user["role"] = body.role
            if body.sessions_remaining is not None:
                user["sessions_remaining"] = body.sessions_remaining
            if body.max_turns_per_session is not None:
                user["max_turns_per_session"] = body.max_turns_per_session

        try:
            auth.update_user(username, _mod)
        except KeyError:
            raise HTTPException(status_code=404, detail="user not found") from None

        return {"user": _user_summary(username)}

    @app.post("/api/admin/users/{username}/suspend")
    def suspend_user(
        username: str,
        admin: Dict[str, Any] = Depends(require_admin),
        _csrf: Dict[str, Any] = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """
        Suspend a user (the UI 'delete' action). Sets status=suspended;
        never hard-deletes, so the user's sessions/history are preserved.
        Guardrails: can't suspend yourself, can't suspend the last active
        admin.
        """
        target = auth.get_user(username)
        if target is None:
            raise HTTPException(status_code=404, detail="user not found")
        # Last-active-admin guard FIRST: if the target is the sole active
        # admin, block regardless of who's asking (including self). This is
        # what keeps the system from ever having zero active admins. The
        # self-suspend guard below then handles the "you, but there are
        # other admins" case with a clearer message.
        if (target.get("role") == "admin"
                and target.get("status") == "active"
                and _count_active_admins() <= 1):
            raise HTTPException(
                status_code=400,
                detail="cannot suspend the last active admin",
            )
        if username == admin["username"]:
            raise HTTPException(status_code=400, detail="you cannot suspend your own account")

        try:
            auth.update_user(username, lambda u: u.__setitem__("status", "suspended"))
        except KeyError:
            raise HTTPException(status_code=404, detail="user not found") from None
        return {"user": _user_summary(username)}

    @app.post("/api/admin/users/{username}/reactivate")
    def reactivate_user(
        username: str,
        admin: Dict[str, Any] = Depends(require_admin),
        _csrf: Dict[str, Any] = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """
        Reactivate a suspended user (status=active). Does not touch the
        password or must_change_password.
        """
        target = auth.get_user(username)
        if target is None:
            raise HTTPException(status_code=404, detail="user not found")
        try:
            auth.update_user(username, lambda u: u.__setitem__("status", "active"))
        except KeyError:
            raise HTTPException(status_code=404, detail="user not found") from None
        return {"user": _user_summary(username)}

    @app.post("/api/admin/users/{username}/reset-password")
    def reset_user_password(
        username: str,
        admin: Dict[str, Any] = Depends(require_admin),
        _csrf: Dict[str, Any] = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """
        Reset a user's password to a new server-generated temp password
        (shown once), forcing a change on next login. Invalidates the
        user's existing login sessions so the reset forces re-login. An
        admin cannot reset their own password here (use change-password).
        """
        target = auth.get_user(username)
        if target is None:
            raise HTTPException(status_code=404, detail="user not found")
        if username == admin["username"]:
            raise HTTPException(
                status_code=400,
                detail="use the change-password screen to change your own password",
            )

        temp_password = auth.generate_temp_password()
        try:
            auth.set_user_password(username, temp_password, must_change=True)
        except KeyError:
            raise HTTPException(status_code=404, detail="user not found") from None
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        # Force re-login: drop the user's active login sessions.
        try:
            auth.invalidate_user_sessions(username)
        except Exception:
            import traceback
            traceback.print_exc()

        return {
            "user": _user_summary(username),
            "temporary_password": temp_password,
        }

    # ============================================================
    # Data Sources — governed query pipeline (web integration)
    #
    # Admin: configure connection -> test -> introspect -> review -> approve.
    # User: pick a ratified source by version, ask one objective.
    # All ingestion + query route through data_sources, which uses the ONE
    # canonical IngestionStore and the REAL pipeline (validation -> Sentinel ->
    # adapter -> SkillResult). Passwords are never echoed/persisted/logged; the
    # user query body carries NO credentials.
    # ============================================================

    @app.post("/api/admin/data-sources/mysql/test")
    def data_source_mysql_test(
        body: MySQLTestRequest,
        request: Request,
        admin: Dict[str, Any] = Depends(require_admin),
        _csrf: Dict[str, Any] = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """Test a MySQL connection; return a COARSE status + base-table count.
        The password is used to connect and never echoed/persisted/logged."""
        factory = getattr(request.app.state, "mysql_connect_factory",
                          data_sources.make_pymysql_connect_fn)
        return data_sources.test_mysql_connection(
            host=body.host, port=body.port, user=body.user,
            password=body.password, database=body.database, connect_factory=factory,
        )

    @app.post("/api/admin/data-sources/mysql/introspect")
    def data_source_mysql_introspect(
        body: MySQLIntrospectRequest,
        request: Request,
        admin: Dict[str, Any] = Depends(require_admin),
        _csrf: Dict[str, Any] = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """Introspect a real MySQL schema into a PENDING candidate (no ratify).
        Uses the real MySQLIntrospector explicitly (never the synthetic default).
        Returns the candidate incl. full schema_detail; never the password."""
        factory = getattr(request.app.state, "mysql_connect_factory",
                          data_sources.make_pymysql_connect_fn)
        try:
            candidate = data_sources.introspect_mysql_source(
                host=body.host, port=body.port, user=body.user,
                password=body.password, database=body.database,
                source_name=body.source_name, connect_factory=factory,
            )
        except Exception as e:
            # Client gets a coarse message (a driver error can contain the DSN).
            # But the operator MUST be able to diagnose: log the real exception
            # type + message + traceback server-side, scrubbed of the password.
            import logging, traceback
            _pw = body.password or ""
            _detail = traceback.format_exc()
            if _pw:
                _detail = _detail.replace(_pw, "***")
            logging.getLogger("adam.data_sources").error(
                "introspect failed for source=%r db=%r host=%r: %s: %s\n%s",
                body.source_name, body.database, body.host,
                type(e).__name__,
                (str(e).replace(_pw, "***") if _pw else str(e)),
                _detail,
            )
            raise HTTPException(status_code=400, detail="introspection failed: could not read the source schema")
        return candidate.to_dict()

    @app.get("/api/admin/source-model-candidates")
    def list_source_model_candidates(
        admin: Dict[str, Any] = Depends(require_admin),
    ) -> Dict[str, Any]:
        store = data_sources.get_pipeline_ingestion_store()
        return {"candidates": [c.to_dict() for c in store.list_candidates()]}

    @app.post("/api/admin/source-model-candidates/{candidate_id}/approve")
    def approve_source_model_candidate(
        candidate_id: str,
        admin: Dict[str, Any] = Depends(require_admin),
        _csrf: Dict[str, Any] = Depends(require_csrf),
        body: Optional[ApproveCandidateRequest] = None,
    ) -> Dict[str, Any]:
        """Approve -> ratify (mint immutable version), recording the admin's
        username. If connection details are supplied (admin+CSRF only), also
        write an encrypted connection profile so the source is queryable; the
        plaintext password is used once to encrypt and never persisted. Terminal:
        re-approving returns a clean 409 (never a 500)."""
        # Connection profile is written only when all required fields are present.
        has_conn = bool(body and body.host and body.database and body.user and body.password)
        if has_conn and not data_source_connections.encryption_available():
            # Fail BEFORE ratifying so we never mint an unqueryable version due
            # to a missing key. Clean message; never the password.
            raise HTTPException(status_code=400, detail="encryption key not configured")

        with data_sources.store_lock():
            store = data_sources.get_pipeline_ingestion_store()
            cand = store.get_candidate(candidate_id)
            if cand is None:
                raise HTTPException(status_code=404, detail="candidate not found")
            if cand.status == "approved" and cand.version:
                existing = store.ratified.get(cand.version)
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "candidate already approved",
                        "ratified": existing.to_dict() if existing else None,
                    },
                )
            try:
                record = store.approve(candidate_id, approved_by=admin["username"])
            except Exception as e:
                raise HTTPException(status_code=409, detail=f"cannot approve: {e}") from e

        if has_conn:
            try:
                # Keyed by the ratified VERSION (== the browser query handle).
                data_source_connections.write_connection_profile(
                    source_handle=record.version,
                    display_name=body.display_name or record.source_name,
                    host=body.host, port=body.port, database=body.database,
                    username=body.user, password=body.password,
                    approved_by=admin["username"],
                )
            except data_source_connections.EncryptionKeyError:
                raise HTTPException(status_code=400, detail="encryption key not configured")
        # Response carries only the ratified record — never the password/token.
        return record.to_dict()

    @app.post("/api/admin/source-model-candidates/{candidate_id}/reject")
    def reject_source_model_candidate(
        candidate_id: str,
        admin: Dict[str, Any] = Depends(require_admin),
        _csrf: Dict[str, Any] = Depends(require_csrf),
    ) -> Dict[str, Any]:
        with data_sources.store_lock():
            store = data_sources.get_pipeline_ingestion_store()
            cand = store.get_candidate(candidate_id)
            if cand is None:
                raise HTTPException(status_code=404, detail="candidate not found")
            try:
                updated = store.reject(candidate_id)
            except Exception as e:
                raise HTTPException(status_code=409, detail=f"cannot reject: {e}") from e
        return updated.to_dict()

    @app.get("/api/admin/source-models")
    def list_source_models(
        admin: Dict[str, Any] = Depends(require_admin),
    ) -> Dict[str, Any]:
        store = data_sources.get_pipeline_ingestion_store()
        return {"source_models": [
            {
                "version": r.version,
                "source_name": r.source_name,
                "entity_count": len(r.entities),
                "approved_by": r.approved_by,
                "approved_at": r.approved_at,
            }
            for r in store.list_ratified()
        ]}

    @app.post("/api/data-intelligence/query")
    def data_intelligence_query(
        body: DataIntelligenceQueryRequest,
        request: Request,
        user: Dict[str, Any] = Depends(require_user),
        _csrf: Dict[str, Any] = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """Run a governed query against a ratified source. Body is {version,
        objective} ONLY — no credentials. Returns the typed SkillResult (facts
        vs. judgment) or a config/blocked outcome (never a 500)."""
        provider = getattr(request.app.state, "pipeline_model_fns_provider",
                           data_sources.default_model_fns_provider)
        resolver = getattr(request.app.state, "resolve_connection",
                           data_sources.default_resolve_connection)
        try:
            model_fns = provider()
        except Exception:
            model_fns = None
        out = data_sources.run_governed_query(
            version=body.version, objective=body.objective, user=user,
            model_fns=model_fns, resolve_connection=resolver,
        )
        if out.get("error") == "UNKNOWN_VERSION":
            raise HTTPException(status_code=404, detail="unknown source model version")
        return out

    @app.get("/api/data-intelligence/source-models")
    def data_intelligence_source_models(
        user: Dict[str, Any] = Depends(require_user),
    ) -> Dict[str, Any]:
        """User-readable list of ratified sources for the query picker. Returns
        only non-secret metadata (version / source_name / approved_at) — no
        connection details. Distinct from the admin list (same data, user auth)
        so any authenticated user can choose a source to query."""
        store = data_sources.get_pipeline_ingestion_store()
        conn_store = data_source_connections.get_connection_store()
        return {"source_models": [
            {
                "version": r.version,
                "source_name": r.source_name,
                "entity_count": len(r.entities),
                "approved_at": r.approved_at,
                # Safe boolean only — whether a (read-only) connection is
                # configured. No host/user/password ever exposed to the browser.
                "has_connection": conn_store.has(r.version),
            }
            for r in store.list_ratified()
        ]}

    @app.get("/api/sessions/{session_id}/process_logs")
    def get_process_logs(session_id: str, request: Request) -> Dict[str, Any]:
        """
        Part 9: return the tail of process_stdout.log and process_stderr.log
        for a GUI-launched session. Used only for diagnosing startup
        failures -- when a session shows as 'errored' before any events
        were written, this is where the actual cause lives.

        Returns the last ~50 KB of each file (the most recent output).
        Larger logs are tail-truncated rather than middle-truncated because
        the relevant info is almost always at the end.
        """
        user = current_user_optional(request)
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        sdir = resolve_session_dir(user, session_id)
        TAIL_BYTES = 50_000
        out: Dict[str, Any] = {}
        for label, fname in [("stdout", "process_stdout.log"),
                             ("stderr", "process_stderr.log")]:
            path = sdir / fname
            if not path.exists():
                out[label] = None
                continue
            try:
                size = path.stat().st_size
                with open(path, "rb") as f:
                    if size > TAIL_BYTES:
                        f.seek(size - TAIL_BYTES)
                    data = f.read()
                out[label] = {
                    "size":      size,
                    "tail_bytes": len(data),
                    "truncated": size > TAIL_BYTES,
                    "text":      data.decode("utf-8", errors="replace"),
                }
            except OSError as e:
                out[label] = {"error": f"{type(e).__name__}: {e}"}
        # Also include the .process_info.json content so callers don't
        # need to chase down a separate endpoint.
        proc_info = _read_process_info(sdir)
        out["process_info"] = proc_info
        if proc_info and isinstance(proc_info.get("pid"), int):
            out["pid_alive"] = _check_process_alive(proc_info["pid"])
        return out

    @app.get("/api/sessions/{session_id}/state")
    def get_session_state(session_id: str, request: Request) -> JSONResponse:
        user = current_user_optional(request)
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        sdir = resolve_session_dir(user, session_id)
        path = sdir / "session_state.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="session_state.json not found")
        with open(path, encoding="utf-8") as f:
            return JSONResponse(json.load(f))

    @app.get("/api/sessions/{session_id}/events")
    def get_session_events(session_id: str, request: Request) -> Dict[str, Any]:
        """Return the full events.jsonl as an ordered list."""
        user = current_user_optional(request)
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        sdir = resolve_session_dir(user, session_id)
        path = sdir / "events.jsonl"
        if not path.exists():
            return {"events": []}
        events: List[Dict[str, Any]] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
        return {"events": events}

    @app.get("/api/sessions/{session_id}/stream")
    async def stream_session_events(session_id: str, request: Request) -> EventSourceResponse:
        """SSE endpoint: catch-up + live tail of events.jsonl."""
        user = current_user_optional(request)
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        sdir = resolve_session_dir(user, session_id)
        path = sdir / "events.jsonl"
        return EventSourceResponse(_tail_events_file(path, request))

    @app.get("/api/sessions/{session_id}/verifications")
    def get_verifications(session_id: str, request: Request) -> Dict[str, Any]:
        user = current_user_optional(request)
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        sdir = resolve_session_dir(user, session_id)
        claims = verification.load_claims(sdir)
        return {
            "claims":       claims,
            "verifications": claims,
            "summary":      verification.summarize_claims(claims),
        }

    @app.post("/api/sessions/{session_id}/verifications/override")
    def post_verification_override(
        session_id: str,
        body: VerificationOverrideBody,
        admin: Dict[str, Any] = Depends(require_admin),
        _csrf: Dict[str, Any] = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """Admin override of a Truthseeker verdict with audit trail."""
        sdir = resolve_session_dir(admin, session_id)
        feedback_dir = gui_root / "data"
        try:
            override = verification.save_override(
                session_dir=sdir,
                session_id=session_id,
                feedback_dir=feedback_dir,
                claim_id=body.claim_id.strip(),
                admin_username=admin.get("username", "admin"),
                status=body.status,
                reason=body.reason,
                feedback=body.feedback,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        claims = verification.load_claims(sdir)
        return {
            "ok":       True,
            "override": override,
            "claims":   claims,
            "summary":  verification.summarize_claims(claims),
        }

    @app.get("/api/sessions/{session_id}/skills")
    def get_skill_invocations(session_id: str, request: Request) -> Dict[str, Any]:
        user = current_user_optional(request)
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        sdir = resolve_session_dir(user, session_id)
        path = sdir / "skills.jsonl"
        if not path.exists():
            return {"invocations": []}
        records: List[Dict[str, Any]] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
        return {"invocations": records}

    @app.get("/api/sessions/{session_id}/artifacts/{artifact_path:path}")
    def get_artifact(session_id: str, artifact_path: str, request: Request) -> FileResponse:
        """
        Part 9.2: serve an artifact from the session's artifacts/ tree,
        including files inside per-skill workspace subdirectories like
        coder/<slug>_<artifact_id>/<file>.

        v5 multi-user: also enforces that the requesting user owns the
        session (or is admin). Without this check, any logged-in user
        could construct another user's session id and download their
        artifacts. The check happens by way of resolve_session_dir,
        which raises 404 for non-admins requesting other users'
        sessions.

        The path parameter uses FastAPI's :path converter so slashes
        pass through unchanged. That means we MUST enforce path-traversal
        defense ourselves: the resolved file must be a descendant of the
        session's artifacts/ directory, with no .. components, no
        absolute leading slash, no leading dot path components, and no
        symlinks pointing outside the tree.

        Pre-Part-9.2 behavior was a flat-only endpoint (one path
        component). The flat case still works -- a single-segment
        artifact_path like "foo.docx" resolves to artifacts/foo.docx,
        same as before.
        """
        # v5 multi-user: authenticate first, then resolve which
        # user's session this is. Non-admin requesting another user's
        # session id gets 404 (not 403, to avoid leaking the existence
        # of other sessions).
        user = current_user_optional(request)
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")

        # Reject obvious bad shapes before doing any filesystem work.
        # The :path converter strips the leading slash, but does not
        # protect against ".." segments or hidden-file prefixes.
        if not artifact_path or artifact_path.startswith("/"):
            raise HTTPException(status_code=400, detail="invalid artifact path")
        # Normalize backslashes (Windows clients) so the segment-by-
        # segment check is consistent.
        normalized = artifact_path.replace("\\", "/")
        segments = [s for s in normalized.split("/") if s != ""]
        if not segments:
            raise HTTPException(status_code=400, detail="invalid artifact path")
        for seg in segments:
            if seg in ("", ".", ".."):
                raise HTTPException(
                    status_code=400,
                    detail="path traversal not permitted",
                )
            if seg.startswith("."):
                # Hidden files (e.g. .process_info.json) live alongside
                # artifacts but are not artifacts themselves. Reject to
                # avoid exposing metadata files through the artifact URL.
                raise HTTPException(
                    status_code=400,
                    detail="hidden files cannot be served as artifacts",
                )

        # resolve_session_dir does the ownership + 404 logic.
        sdir = resolve_session_dir(user, session_id)
        artifact_dir = sdir / "artifacts"
        # Build the candidate path inside the artifacts directory. We
        # resolve() both sides and then check is_relative_to so symlink
        # tricks can't escape. If the file doesn't exist yet, resolve()
        # still works on the path; the existence check happens after.
        candidate = (artifact_dir / Path(*segments)).resolve()
        try:
            artifact_dir_resolved = artifact_dir.resolve()
        except OSError:
            raise HTTPException(status_code=404, detail="artifacts directory not found")

        # is_relative_to is Python 3.9+, which we already require.
        if not candidate.is_relative_to(artifact_dir_resolved):
            raise HTTPException(
                status_code=400,
                detail="resolved path escapes artifacts directory",
            )

        if not candidate.exists() or not candidate.is_file():
            raise HTTPException(status_code=404, detail="artifact not found")
        return FileResponse(candidate)

    @app.post(
        "/api/sessions/{session_id}/director_message",
        response_model=DirectorMessageResponse,
    )
    def post_director_message(
        session_id: str,
        body: DirectorMessageRequest,
        request: Request,
        _csrf: Dict[str, Any] = Depends(require_csrf),
    ) -> DirectorMessageResponse:
        """
        Part 8: queue a Director message for an active session.

        Appends one JSON line to <session_dir>/director_inbox.jsonl.
        ADAM polls the inbox at the top of each loop iteration and
        consumes new lines via consume_director_inbox(). When the
        message is consumed, ADAM emits a director_message event
        (or a director_message_error event if the line is malformed
        in some way the server didn't catch).

        The server validates and rejects:
          - missing/invalid session_id (404 from resolve_session_dir)
          - non-owner trying to message someone else's session (404)
          - empty/oversized content (handled by the Pydantic schema)
          - sessions that have already ended (409 Conflict)

        On success returns the message_id so the GUI can match the
        eventual director_message event back to the request.

        v5: only the session's owner (or an admin) can send director
        messages to it. Pilots get 404 if they try to message another
        user's session.
        """
        user = current_user_optional(request)
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        sdir = resolve_session_dir(user, session_id)

        # Session-ended check. Reject with 409 if session_state.json
        # shows the session has completed. This is belt-and-suspenders
        # with the frontend's disabled-when-ended prompt bar; a stale
        # browser tab could otherwise submit to a finished session.
        if _session_has_ended(sdir):
            raise HTTPException(
                status_code=409,
                detail="session has already ended; new director messages cannot be queued",
            )

        # Server-generated message_id. Format: dir_<isoformat_compact>_<short_uuid>
        # The isoformat prefix gives lexicographic ordering by submission
        # time (useful when grepping the inbox file); the uuid suffix
        # disambiguates concurrent submissions in the same second.
        now = datetime.now()
        message_id = (
            f"dir_{now.strftime('%Y%m%dT%H%M%S')}"
            f"_{uuid.uuid4().hex[:8]}"
        )
        ts_iso = now.isoformat(timespec='seconds')

        # Append one JSON line to the inbox. Open in append-binary mode
        # and write a single encoded line so the file's last-newline
        # invariant is preserved (consume_director_inbox relies on it
        # to detect partial writes).
        inbox_path = sdir / "director_inbox.jsonl"
        line = json.dumps({
            "message_id": message_id,
            "ts":         ts_iso,
            "source":     "gui",
            "content":    body.content,
        }, ensure_ascii=False) + "\n"
        try:
            with open(inbox_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            raise HTTPException(
                status_code=500,
                detail=f"failed to write inbox: {type(e).__name__}",
            )

        return DirectorMessageResponse(
            message_id = message_id,
            queued_at  = ts_iso,
            inbox_path = str(inbox_path.relative_to(logs_dir.parent))
                         if inbox_path.is_relative_to(logs_dir.parent)
                         else str(inbox_path),
        )

    # ---- Static frontend ----

    # The frontend is built into ./frontend/dist by Vite. We mount it
    # last so /api/* routes take precedence. If the build directory
    # doesn't exist, fall back to a stub page that explains how to
    # build the frontend.
    frontend_dist = (Path(__file__).parent.parent / "frontend" / "dist").resolve()
    if frontend_dist.exists() and (frontend_dist / "index.html").exists():
        # SPA: serve index.html for all unmatched routes so client-side
        # routing works. StaticFiles with html=True handles this.
        app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")
    else:
        @app.get("/")
        def frontend_stub() -> JSONResponse:
            return JSONResponse({
                "warning": "Frontend build not found at " + str(frontend_dist),
                "hint":    "Run `cd frontend && npm install && npm run build`.",
                "api_root": "/api",
            })

    return app


def _session_dir(logs_dir: Path, user_id: str, session_id: str) -> Path:
    """Resolve and validate a session directory path."""
    if not user_id:
        raise HTTPException(status_code=400, detail="director not configured")
    # Defensive: session_id should be a UUID-like string. Reject any
    # path traversal attempts.
    if "/" in session_id or ".." in session_id:
        raise HTTPException(status_code=400, detail="invalid session_id")
    path = logs_dir / user_id / session_id
    if not path.exists():
        raise HTTPException(status_code=404, detail="session not found")
    return path


def _session_has_ended(session_dir: Path) -> bool:
    """
    Quick check whether a session has completed.

    A session is considered ended if either:
      - session_state.json exists (ADAM writes it only after the loop
        completes), OR
      - events.jsonl exists and its last event is session_ended.

    Used by the director_message endpoint to reject late submissions
    with 409 Conflict. False-negatives (treating an ended session as
    active) would let a message accumulate in the inbox that ADAM
    will never read; false-positives (treating an active session as
    ended) would block legitimate submissions. We err toward
    false-negatives -- the inbox line is at worst orphaned, which
    is cheap; blocking a legitimate message is expensive.
    """
    state_path  = session_dir / "session_state.json"
    if state_path.exists():
        return True

    events_path = session_dir / "events.jsonl"
    if not events_path.exists():
        return False

    # Cheap last-event check: read the last ~1KB of the file and look
    # for a session_ended marker. Avoids parsing the whole file.
    try:
        size = events_path.stat().st_size
        if size == 0:
            return False
        with open(events_path, "rb") as f:
            tail_size = min(2048, size)
            f.seek(size - tail_size)
            tail = f.read().decode("utf-8", errors="replace")
        # Find the last complete line
        lines = [ln for ln in tail.splitlines() if ln.strip()]
        if not lines:
            return False
        try:
            last = json.loads(lines[-1])
            return last.get("event_type") == "session_ended"
        except Exception:
            return False
    except OSError:
        return False


# ============================================================
# Entry point
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="ADAM GUI backend")
    parser.add_argument(
        "--adam-root", type=Path, default=Path("."),
        help="Path to the ADAM project root (where .env lives). Default: cwd.",
    )
    parser.add_argument(
        "--logs-dir", type=Path, default=None,
        help="Path to ADAM's logs/ directory. Default: <adam-root>/logs.",
    )
    parser.add_argument(
        "--host", type=str, default=DEFAULT_HOST,
        help=f"Host to bind. Default: {DEFAULT_HOST} (localhost only).",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Port to bind. Default: {DEFAULT_PORT}.",
    )
    parser.add_argument(
        "--open-browser", action="store_true",
        help="Open the GUI in the default browser after startup.",
    )
    args = parser.parse_args()

    adam_root = args.adam_root.resolve()
    logs_dir = (args.logs_dir or (adam_root / "logs")).resolve()

    if not adam_root.exists():
        sys.exit(f"ADAM root not found: {adam_root}")
    if not logs_dir.exists():
        print(f"WARNING: logs directory not found at {logs_dir}", file=sys.stderr)
        print("The GUI will run but will show no sessions until ADAM creates one.", file=sys.stderr)

    app = build_app(adam_root, logs_dir)

    if args.open_browser:
        import threading
        import webbrowser
        url = f"http://{args.host}:{args.port}/"
        def _open():
            time.sleep(0.5)  # give uvicorn a moment to bind
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
