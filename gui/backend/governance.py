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
import os
import tempfile
from copy import deepcopy
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


# ============================================================
# Admin API (Slice 4.2 Phase 1): validation + read-only view
# ============================================================

GOVERNANCE_SCHEMA_VERSION = "1.0"

# Conditions recognized by evaluate_review_gate() in adam/core/loop.py.
_VALID_REVIEW_CONDITIONS = frozenset({
    "public_facing_artifact",
    "student_data_output",
    "external_action",
    "email_send",
    "file_write",
})

_VALID_REVIEW_MODES = frozenset({"none", "conditional", "required"})

# Which fields are enforced at runtime vs recorded for future use.
# Surfaced in the admin UI so edits are honest about what matters today.
FIELD_ENFORCEMENT: Dict[str, Any] = {
    "policy_bounds": {
        "allowed_skills": {
            "enforced": True,
            "slice": "2",
            "label": "Allowed skills",
            "description": "Slice 2: skills not on this list are disabled at spawn when an allow-list is present.",
        },
        "blocked_skills": {
            "enforced": True,
            "slice": "2",
            "label": "Blocked skills",
            "description": "Slice 2: always disabled at spawn regardless of allow-list.",
        },
        "external_actions_allowed": {
            "enforced": True,
            "slice": "3",
            "label": "External actions",
            "description": "Slice 3: policy gate blocks Operator when false and synthesis plans an external action.",
        },
        "email_send_allowed": {
            "enforced": True,
            "slice": "3",
            "label": "Email send",
            "description": "Slice 3: policy gate blocks Operator when false and synthesis plans email.",
        },
        "allowed_artifact_types": {
            "enforced": False,
            "label": "Allowed artifact types",
            "description": "Recorded only — not enforced by the runtime yet.",
        },
        "file_write_allowed": {
            "enforced": False,
            "label": "File write",
            "description": "Recorded only — not enforced by the policy gate yet (may trigger review when listed in review_required_for).",
        },
        "public_output_rules": {
            "enforced": False,
            "label": "Public output rules",
            "description": "Recorded only — review detection uses profile review_required_for, not this field.",
        },
        "education_data_rules": {
            "enforced": False,
            "label": "Education data rules",
            "description": "Recorded only — FERPA/COPPA awareness is declarative until wired.",
        },
    },
    "governance_profiles": {
        "policy_bounds_id": {
            "enforced": True,
            "slice": "2/3/4a",
            "label": "Ruleset reference",
            "description": "Selects which policy_bounds ruleset applies to sessions using this profile.",
        },
        "human_review_mode": {
            "enforced": True,
            "slice": "4a",
            "label": "Human review mode",
            "description": "Slice 4a: none | conditional | required — controls whether Operator pauses for approval.",
        },
        "review_required_for": {
            "enforced": True,
            "slice": "4a",
            "label": "Review conditions",
            "description": "Slice 4a: which action types trigger a human-review pause.",
        },
    },
}

_REVIEW_CONDITION_LABELS = {
    "public_facing_artifact": "public- or parent-facing output",
    "student_data_output": "student data / PII",
    "external_action": "external action",
    "email_send": "email send",
    "file_write": "file write",
}


def governance_file_path() -> Optional[Path]:
    """Absolute path to governance.json, or None if init_governance() not called."""
    return _GOVERNANCE_PATH


def get_raw_data() -> Dict[str, Any]:
    """Return the cached governance dict (may be empty if file missing)."""
    return dict(_DATA)


def reload_governance() -> None:
    """Re-read governance.json from disk. Alias for init_governance with the
    same gui_root path."""
    if _GOVERNANCE_PATH is None:
        return
    init_governance(_GOVERNANCE_PATH.parent)


def _source_info() -> Dict[str, Any]:
    path = _GOVERNANCE_PATH
    exists = bool(path and path.exists())
    loaded_from_file = bool(_DATA)
    return {
        "path": str(path) if path else None,
        "file_exists": exists,
        "loaded_from_file": loaded_from_file,
        "using_builtin_fallback": not loaded_from_file,
        "reload_note": (
            "Saved changes apply to new sessions immediately (in-memory reload). "
            "A manual adam-gui restart is only needed if the file is edited "
            "outside this UI."
        ),
    }


def _format_review_mode(mode: str) -> str:
    m = (mode or "none").lower()
    if m == "none":
        return "No human review"
    if m == "conditional":
        return "Human review when listed conditions match"
    if m == "required":
        return "Human review required when any listed condition matches"
    return f"Unknown mode ({mode!r})"


def _format_review_conditions(conditions: List[str]) -> str:
    if not conditions:
        return "none"
    labels = [_REVIEW_CONDITION_LABELS.get(c, c) for c in conditions]
    return ", ".join(labels)


def _describe_ruleset(bounds: Dict[str, Any], skill_universe: List[str]) -> str:
    allowed = bounds.get("allowed_skills")
    blocked = bounds.get("blocked_skills") or []
    parts = []
    if isinstance(allowed, list) and allowed:
        parts.append(f"skills allowed: {', '.join(allowed)}")
    elif not allowed:
        parts.append("skills: default-allow except blocked list")
    if blocked:
        parts.append(f"blocked: {', '.join(blocked)}")
    denied = policy_denied_skills_from_bounds(bounds, skill_universe)
    if denied:
        parts.append(f"effectively disabled at spawn: {', '.join(denied)}")
    flags = []
    if bounds.get("external_actions_allowed") is False:
        flags.append("no external actions")
    if bounds.get("email_send_allowed") is False:
        flags.append("no email send")
    if bounds.get("file_write_allowed") is False:
        flags.append("no file write (declarative)")
    if flags:
        parts.append("; ".join(flags))
    return ". ".join(parts) + "." if parts else "No restrictions configured."


def policy_denied_skills_from_bounds(bounds: Dict[str, Any],
                                     skill_universe: List[str]) -> List[str]:
    """Like policy_denied_skills() but takes a ruleset dict directly."""
    universe = set(skill_universe or [])
    blocked = set(bounds.get("blocked_skills") or [])
    denied = {s for s in blocked if s in universe}
    allowed = bounds.get("allowed_skills")
    if isinstance(allowed, list):
        allow_set = set(allowed)
        denied |= {s for s in universe if s not in allow_set}
    return sorted(denied)


def _describe_profile(profile: Dict[str, Any],
                      bounds: Dict[str, Any],
                      skill_universe: List[str]) -> str:
    ruleset_id = profile.get("policy_bounds_id", "?")
    review = _format_review_mode(profile.get("human_review_mode", "none"))
    conditions = _format_review_conditions(profile.get("review_required_for") or [])
    rules = _describe_ruleset(bounds, skill_universe)
    return (
        f"Uses ruleset '{ruleset_id}'. {rules} "
        f"Review: {review}"
        + (f" ({conditions})." if conditions != "none" else ".")
    )


def validate_governance_data(data: Dict[str, Any],
                             skill_universe: List[str]) -> Dict[str, Any]:
    """
    Strict validation of a governance config dict. Returns:
      { "valid": bool, "errors": [...], "warnings": [...] }

    Unlike init_governance(), this does NOT fall back — callers use it
    before writes (Phase 2) or to surface mistakes in the live file.
    """
    errors: List[str] = []
    warnings: List[str] = []

    if not isinstance(data, dict):
        return {"valid": False, "errors": ["config must be a JSON object"], "warnings": []}

    sv = data.get("schema_version")
    if sv is not None and sv != GOVERNANCE_SCHEMA_VERSION:
        warnings.append(
            f"schema_version is {sv!r}; this code expects {GOVERNANCE_SCHEMA_VERSION!r}"
        )

    bounds_map = data.get("policy_bounds")
    profiles_map = data.get("governance_profiles")

    if not isinstance(bounds_map, dict) or not bounds_map:
        errors.append("policy_bounds must be a non-empty object")
        bounds_map = {}
    if not isinstance(profiles_map, dict) or not profiles_map:
        errors.append("governance_profiles must be a non-empty object")
        profiles_map = {}

    universe = set(skill_universe or [])
    if not universe:
        warnings.append("skill universe is empty — skill name checks skipped")

    # --- Validate rulesets ---
    for rid, bounds in bounds_map.items():
        prefix = f"policy_bounds.{rid}"
        if not isinstance(bounds, dict):
            errors.append(f"{prefix}: must be an object")
            continue
        bid = bounds.get("id")
        if bid != rid:
            errors.append(f"{prefix}: id must match key ({rid!r} != {bid!r})")
        for field in ("allowed_skills", "blocked_skills"):
            val = bounds.get(field)
            if val is None:
                if field == "blocked_skills":
                    val = []
                else:
                    continue
            if not isinstance(val, list):
                errors.append(f"{prefix}.{field}: must be a list")
                continue
            for skill in val:
                if not isinstance(skill, str) or not skill:
                    errors.append(f"{prefix}.{field}: invalid skill name {skill!r}")
                elif universe and skill not in universe:
                    errors.append(
                        f"{prefix}.{field}: unknown skill {skill!r} "
                        f"(available: {', '.join(sorted(universe))})"
                    )
        allowed = set(bounds.get("allowed_skills") or [])
        blocked = set(bounds.get("blocked_skills") or [])
        overlap = allowed & blocked
        if overlap:
            errors.append(
                f"{prefix}: skill(s) appear in both allowed and blocked: "
                f"{', '.join(sorted(overlap))}"
            )
        for flag in ("external_actions_allowed", "email_send_allowed", "file_write_allowed"):
            val = bounds.get(flag)
            if val is not None and not isinstance(val, bool):
                errors.append(f"{prefix}.{flag}: must be a boolean")

    # --- Validate profiles ---
    for pid, profile in profiles_map.items():
        prefix = f"governance_profiles.{pid}"
        if not isinstance(profile, dict):
            errors.append(f"{prefix}: must be an object")
            continue
        if profile.get("id") != pid:
            errors.append(f"{prefix}: id must match key")
        bounds_id = profile.get("policy_bounds_id")
        if not bounds_id or bounds_id not in bounds_map:
            errors.append(
                f"{prefix}: policy_bounds_id {bounds_id!r} does not reference "
                "an existing ruleset"
            )
        mode = (profile.get("human_review_mode") or "none").lower()
        if mode not in _VALID_REVIEW_MODES:
            errors.append(
                f"{prefix}: human_review_mode must be one of "
                f"{sorted(_VALID_REVIEW_MODES)}, got {mode!r}"
            )
        conditions = profile.get("review_required_for")
        if conditions is None:
            continue
        if not isinstance(conditions, list):
            errors.append(f"{prefix}: review_required_for must be a list")
            continue
        for cond in conditions:
            if cond not in _VALID_REVIEW_CONDITIONS:
                errors.append(
                    f"{prefix}: unknown review condition {cond!r} "
                    f"(valid: {', '.join(sorted(_VALID_REVIEW_CONDITIONS))})"
                )
        if mode == "none" and conditions:
            warnings.append(
                f"{prefix}: review_required_for is set but human_review_mode is 'none'"
            )

    default_id = data.get("default_profile_id")
    if not default_id:
        errors.append("default_profile_id is required")
    elif default_id not in profiles_map:
        errors.append(
            f"default_profile_id {default_id!r} does not reference an existing profile"
        )

    return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}


def _write_governance_atomic(path: Path, data: Dict[str, Any]) -> None:
    """Write governance JSON atomically (temp file + replace)."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            os.chmod(tmp_path, path.stat().st_mode & 0o777)
        else:
            os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def normalize_governance_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a config dict for persistence: ensure ids match keys,
    schema_version is set, and list fields have sane defaults."""
    bounds_in = data.get("policy_bounds") or {}
    profiles_in = data.get("governance_profiles") or {}
    out: Dict[str, Any] = {
        "schema_version": data.get("schema_version") or GOVERNANCE_SCHEMA_VERSION,
        "default_profile_id": data.get("default_profile_id"),
        "policy_bounds": {},
        "governance_profiles": {},
    }
    if isinstance(data.get("_comment"), str):
        out["_comment"] = data["_comment"]

    for rid, bounds in bounds_in.items():
        if not isinstance(bounds, dict):
            continue
        b = deepcopy(bounds)
        b["id"] = rid
        if b.get("blocked_skills") is None:
            b["blocked_skills"] = []
        if not isinstance(b.get("blocked_skills"), list):
            b["blocked_skills"] = []
        # Omit empty allowed_skills — means default-allow mode.
        allowed = b.get("allowed_skills")
        if allowed is None or (isinstance(allowed, list) and len(allowed) == 0):
            b.pop("allowed_skills", None)
        out["policy_bounds"][rid] = b

    for pid, profile in profiles_in.items():
        if not isinstance(profile, dict):
            continue
        p = deepcopy(profile)
        p["id"] = pid
        if p.get("review_required_for") is None:
            p["review_required_for"] = []
        if not isinstance(p.get("review_required_for"), list):
            p["review_required_for"] = []
        if not p.get("human_review_mode"):
            p["human_review_mode"] = "none"
        out["governance_profiles"][pid] = p

    return out


def export_config_for_editor() -> Dict[str, Any]:
    """Raw governance.json shape for the admin editor."""
    data = get_raw_data()
    if not data:
        return {
            "schema_version": GOVERNANCE_SCHEMA_VERSION,
            "default_profile_id": _FALLBACK_DEFAULT_ID,
            "policy_bounds": {k: deepcopy(v) for k, v in _bounds().items()},
            "governance_profiles": {k: deepcopy(v) for k, v in _profiles().items()},
        }
    return {
        "schema_version": data.get("schema_version", GOVERNANCE_SCHEMA_VERSION),
        "default_profile_id": data.get("default_profile_id", default_profile_id()),
        "policy_bounds": deepcopy(_bounds()),
        "governance_profiles": deepcopy(_profiles()),
        **({"_comment": data["_comment"]} if isinstance(data.get("_comment"), str) else {}),
    }


def save_governance_data(data: Dict[str, Any],
                         skill_universe: List[str]) -> Dict[str, Any]:
    """
    Validate, normalize, write governance.json, and reload the cache.
    Returns the validation result on success.

    Raises ValueError with the errors list if validation fails.
    Raises RuntimeError if governance was never initialized.
    """
    if _GOVERNANCE_PATH is None:
        raise RuntimeError("governance not initialized — call init_governance() first")
    normalized = normalize_governance_data(data)
    validation = validate_governance_data(normalized, skill_universe)
    if not validation["valid"]:
        raise ValueError(validation["errors"])
    _write_governance_atomic(_GOVERNANCE_PATH, normalized)
    reload_governance()
    return validation


def get_admin_view(skill_universe: List[str]) -> Dict[str, Any]:
    """
    Full read-only governance snapshot for the admin UI. Includes plain-
    language summaries, field enforcement metadata, and validation of the
    currently loaded config.
    """
    data = get_raw_data()
    validation = validate_governance_data(data, skill_universe) if data else {
        "valid": False,
        "errors": ["governance.json missing or unreadable — using built-in fallbacks"],
        "warnings": [],
    }

    bounds_map = _bounds()
    profiles_map = _profiles()

    rulesets_out = []
    for rid, bounds in bounds_map.items():
        entry = dict(bounds)
        entry["summary"] = _describe_ruleset(bounds, skill_universe)
        entry["denied_skills"] = policy_denied_skills_from_bounds(bounds, skill_universe)
        rulesets_out.append(entry)

    profiles_out = []
    for pid, profile in profiles_map.items():
        bounds = bounds_map.get(profile.get("policy_bounds_id"), {})
        entry = dict(profile)
        entry["policy_bounds"] = dict(bounds) if bounds else {}
        entry["summary"] = _describe_profile(profile, bounds, skill_universe)
        entry["denied_skills"] = policy_denied_skills_from_bounds(bounds, skill_universe)
        profiles_out.append(entry)

    return {
        "schema_version": data.get("schema_version", GOVERNANCE_SCHEMA_VERSION),
        "source": _source_info(),
        "validation": validation,
        "default_profile_id": default_profile_id(),
        "skill_universe": list(skill_universe or []),
        "field_enforcement": FIELD_ENFORCEMENT,
        "policy_bounds": rulesets_out,
        "governance_profiles": profiles_out,
        "config": export_config_for_editor(),
        "edit_enabled": True,
        "review_modes": sorted(_VALID_REVIEW_MODES),
        "review_conditions": sorted(_VALID_REVIEW_CONDITIONS),
    }
