"""
governance.py — Governance profiles and policy bounds.

Slice 1 (this file): DATA MODEL ONLY. Loads gui/governance.json, exposes
profiles and their referenced policy-bounds rulesets, and resolves a
profile id (with a safe default) for a session. NOTHING here is enforced
yet — no skill filtering, no gate. Enforcement arrives in later slices:

  Slice 2 — policy bounds drives the resolved skill set at spawn.
  Slice 3 — a post-synthesis, pre-Operator gate consults policy bounds.
  Slice 4 — human_review_mode pauses the loop at that same gate.

Design rule (load-bearing): a GovernanceProfile SELECTS a policy_bounds
ruleset by id; the ruleset DEFINES what is allowed. Allow/deny rules live
in policy_bounds only, never duplicated onto the profile. One source of
truth.

Mirrors auth.py's init_*(gui_root) pattern so the GUI wires it the same
way. A missing or malformed governance.json must NOT break session
spawning: every accessor falls back to a built-in "general" profile with
permissive-but-sane standard bounds.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

# Module-level cache, populated by init_governance().
_GOVERNANCE_PATH: Optional[Path] = None
_DATA: Dict[str, Any] = {}

# Built-in fallback used when governance.json is absent or unreadable, so
# Slice 1 can never break spawning. Mirrors the "general" / "standard"
# entries in the shipped governance.json.
_FALLBACK_BOUNDS = {
    "id": "standard",
    "name": "Standard (fallback)",
    "allowed_skills": ["document", "slidedeck", "coder", "websearch", "test_echo"],
    "blocked_skills": ["email"],
    "allowed_artifact_types": ["document", "slidedeck", "code"],
    "external_actions_allowed": False,
    "email_send_allowed": False,
    "file_write_allowed": True,
    "public_output_rules": "none",
    "education_data_rules": "none",
}
_FALLBACK_PROFILE = {
    "id": "general",
    "name": "General (fallback)",
    "policy_bounds_id": "standard",
    "human_review_mode": "none",
    "review_required_for": [],
}
_FALLBACK_DEFAULT_ID = "general"


def init_governance(gui_root: Path) -> None:
    """Load gui/governance.json into the module cache. Safe to call again
    to reload. Never raises on a missing/bad file — logs nothing, falls
    back to built-ins."""
    global _GOVERNANCE_PATH, _DATA
    gui_root = Path(gui_root).resolve()
    _GOVERNANCE_PATH = gui_root / "governance.json"
    _DATA = {}
    try:
        if _GOVERNANCE_PATH.exists():
            raw = _GOVERNANCE_PATH.read_text(encoding="utf-8")
            parsed = json.loads(raw) if raw.strip() else {}
            if isinstance(parsed, dict):
                _DATA = parsed
    except Exception:
        # Malformed file -> behave as if absent. Fallbacks cover us.
        _DATA = {}


def _profiles() -> Dict[str, Any]:
    p = _DATA.get("governance_profiles")
    return p if isinstance(p, dict) else {}


def _bounds() -> Dict[str, Any]:
    b = _DATA.get("policy_bounds")
    return b if isinstance(b, dict) else {}


def default_profile_id() -> str:
    """The id new sessions get when none is specified."""
    pid = _DATA.get("default_profile_id")
    if isinstance(pid, str) and pid in _profiles():
        return pid
    return _FALLBACK_DEFAULT_ID


def resolve_profile_id(requested: Optional[str]) -> str:
    """Resolve a requested profile id to a real one. Unknown/None ->
    the configured default. Guarantees the returned id is usable by
    get_profile()."""
    profiles = _profiles()
    if requested and requested in profiles:
        return requested
    d = default_profile_id()
    if d in profiles:
        return d
    return _FALLBACK_PROFILE["id"]


def get_profile(profile_id: Optional[str]) -> Dict[str, Any]:
    """Return the profile dict for an id, or the fallback profile if the
    id is unknown or governance.json is missing."""
    profiles = _profiles()
    if profile_id and profile_id in profiles:
        return profiles[profile_id]
    # Try the default, then the hard fallback.
    d = default_profile_id()
    if d in profiles:
        return profiles[d]
    return dict(_FALLBACK_PROFILE)


def get_policy_bounds(profile_id: Optional[str]) -> Dict[str, Any]:
    """Return the policy-bounds ruleset that the given profile references.
    Falls back to standard bounds if anything is missing. This is what
    Slice 2/3 will consult — exposed now so the data path is exercised."""
    profile = get_profile(profile_id)
    bounds_id = profile.get("policy_bounds_id")
    bounds = _bounds()
    if bounds_id and bounds_id in bounds:
        return bounds[bounds_id]
    return dict(_FALLBACK_BOUNDS)


def list_profiles() -> List[Dict[str, Any]]:
    """All profiles, for the GUI to populate a picker. Each entry is the
    profile dict augmented with a resolved `policy_bounds` summary."""
    out: List[Dict[str, Any]] = []
    for pid, prof in _profiles().items():
        entry = dict(prof)
        entry["policy_bounds"] = get_policy_bounds(pid)
        out.append(entry)
    return out


def policy_denied_skills(profile_id: Optional[str],
                         skill_universe: List[str]) -> List[str]:
    """
    Slice 2 enforcement primitive. Given a profile and the full set of
    skills that exist, return the skills its policy-bounds ruleset
    FORBIDS.

    A skill is denied by policy if either:
      - it appears in the ruleset's `blocked_skills`, OR
      - the ruleset declares an `allowed_skills` allow-list and the skill
        is not on it (default-deny once an allow-list is present).

    If the ruleset declares no `allowed_skills` at all, only
    `blocked_skills` applies (default-allow). This lets a permissive
    ruleset omit the allow-list entirely.

    The caller unions this with the user's ROLE denials to get the final
    --disable-skill set, so enforcement is "most restrictive wins":
    role-denied OR policy-denied => disabled. There is exactly one
    resolved set, computed at spawn; this function never reaches into the
    skill runtime directly.
    """
    bounds = get_policy_bounds(profile_id)
    universe = set(skill_universe or [])

    blocked = set(bounds.get("blocked_skills") or [])
    denied = set(s for s in blocked if s in universe)

    allowed = bounds.get("allowed_skills")
    if isinstance(allowed, list):
        allow_set = set(allowed)
        # default-deny: anything in the universe not explicitly allowed
        denied |= {s for s in universe if s not in allow_set}

    return sorted(denied)
