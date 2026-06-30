"""
Profile data-scope → pipeline ScopeConfig bridge (agent Data Intelligence).

A governance profile may carry a ``data_intelligence`` block that constrains
which ratified sources an agent may query, what detail level it may retrieve
(aggregate vs. student-level), and which fields are always off-limits. This
module parses that block into a ``DataScope`` and derives the pipeline's
``ScopeConfig`` from it, so the EXISTING Sentinel gate enforces the profile —
no parallel validation path.

Defaults are fail-closed: a profile WITHOUT a ``data_intelligence`` block (or
with ``enabled: false``) yields a disabled scope (no capability, no sources).
Aggregate-only is the default; student-level requires an explicit
``student_level_allowed: true``. ``denied_fields`` are ALWAYS enforced
regardless of detail level.

No GUI coupling: this lives in the pipeline package so the agent skill handler
(core side) can import it without reaching into the web layer.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Set

from .sentinel import ScopeConfig

# Default student-identifying bare field names. Under aggregate-only these may
# not appear in projection/group_by even if a profile didn't list them. This is
# the T3 "student-identifying set"; denied_fields are unioned on top.
DEFAULT_IDENTIFYING_FIELDS: Set[str] = {
    "first_name", "last_name", "name", "full_name", "student_number",
    "student_id", "ssn", "dob", "date_of_birth", "birthdate",
    "street", "address", "address_line1", "address_line2", "city", "zip",
    "zipcode", "postal_code", "phone", "email", "guardian", "guardian_name",
}

# Entities whose UNAGGREGATED rows count as student-level by default.
DEFAULT_STUDENT_ENTITIES: Set[str] = {"students"}

# Budget defaults (per the build prompt §3).
DEFAULT_MAX_QUERIES_PER_SESSION = 5
DEFAULT_MAX_QUERIES_PER_AGENT = 3
DEFAULT_MAX_ROWS_RETURNED = 100


def _as_str_set(value: Any) -> Set[str]:
    if not value:
        return set()
    if isinstance(value, str):
        return {value}
    return {str(v) for v in value}


def _as_int(value: Any, default: int) -> int:
    try:
        n = int(value)
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class DataScope:
    """Parsed ``data_intelligence`` profile block. Fail-closed when disabled."""
    enabled: bool = False
    allowed_sources: Set[str] = field(default_factory=set)
    default_detail_level: str = "aggregate"          # aggregate | student_level
    student_level_allowed: bool = False
    denied_fields: Set[str] = field(default_factory=set)
    allowed_student_fields: Set[str] = field(default_factory=set)
    student_entities: Set[str] = field(default_factory=lambda: set(DEFAULT_STUDENT_ENTITIES))
    max_queries_per_session: int = DEFAULT_MAX_QUERIES_PER_SESSION
    max_queries_per_agent: int = DEFAULT_MAX_QUERIES_PER_AGENT
    max_rows_returned: int = DEFAULT_MAX_ROWS_RETURNED

    # ---- construction -----------------------------------------------------

    @classmethod
    def disabled(cls) -> "DataScope":
        return cls(enabled=False)

    @classmethod
    def from_block(cls, block: Optional[Dict[str, Any]]) -> "DataScope":
        """Build from a profile's ``data_intelligence`` dict. None / non-dict /
        ``enabled: false`` → a disabled scope (no capability, no sources)."""
        if not isinstance(block, dict) or not block.get("enabled", False):
            return cls.disabled()
        budgets = block.get("budgets") if isinstance(block.get("budgets"), dict) else {}
        student_entities = _as_str_set(block.get("student_entities")) or set(DEFAULT_STUDENT_ENTITIES)
        return cls(
            enabled=True,
            allowed_sources=_as_str_set(block.get("allowed_sources")),
            default_detail_level=str(block.get("default_detail_level", "aggregate")),
            student_level_allowed=bool(block.get("student_level_allowed", False)),
            denied_fields=_as_str_set(block.get("denied_fields")),
            allowed_student_fields=_as_str_set(block.get("allowed_student_fields")),
            student_entities=student_entities,
            max_queries_per_session=_as_int(
                budgets.get("max_data_queries_per_session"), DEFAULT_MAX_QUERIES_PER_SESSION),
            max_queries_per_agent=_as_int(
                budgets.get("max_data_queries_per_agent"), DEFAULT_MAX_QUERIES_PER_AGENT),
            max_rows_returned=_as_int(
                budgets.get("max_rows_returned"), DEFAULT_MAX_ROWS_RETURNED),
        )

    # ---- predicates -------------------------------------------------------

    @property
    def aggregate_only(self) -> bool:
        """Aggregate-only unless the profile explicitly allows student-level.
        denied_fields are enforced independently in either case."""
        return not self.student_level_allowed

    def permits_source(self, version: str) -> bool:
        return self.enabled and version in self.allowed_sources

    def permits_output_field(self, entity: str, field: str) -> bool:
        """Row-level OUTPUT gate for a single (entity, field). Used when shaping
        governed student-level rows for the DATA_RESULT:
          - denied_fields ALWAYS win (explicit 'entity.field', bare 'field', or
            an 'entity.*' wildcard) → never emitted;
          - an IDENTIFYING field (names, student_number, dob, address, guardian/
            contact, …) is emitted ONLY if the profile explicitly permits it via
            allowed_student_fields (T3 — student-level does not auto-grant every
            identifier);
          - any other non-denied field is emitted.
        This is independent of student_level_allowed (the caller gates rows on
        that first); here we decide per-COLUMN which permitted fields survive."""
        qualified = f"{entity}.{field}"
        for d in self.denied_fields:
            if d == qualified or d == field or (d.endswith(".*") and d[:-2] == entity):
                return False
        if field in DEFAULT_IDENTIFYING_FIELDS:
            return qualified in self.allowed_student_fields or field in self.allowed_student_fields
        return True

    # ---- derivation -------------------------------------------------------

    def build_scope_config(self, source_model) -> ScopeConfig:
        """Derive the pipeline ScopeConfig the governed query runs under.

        - allowed_entities: the ratified source model is its own allowlist.
        - denied_entities: any ``entity.*`` wildcard in denied_fields blocks the
          whole entity (Sentinel matches fields, not wildcards).
        - denied_fields: the profile's explicit field denylist (qualified/bare).
        - aggregate_only + identifying/student-entity sets feed the detail-level
          gate added in Phase 1.
        """
        denied_entities: Set[str] = set()
        denied_fields: Set[str] = set()
        for entry in self.denied_fields:
            if entry.endswith(".*"):
                denied_entities.add(entry[:-2])
            else:
                denied_fields.add(entry)
        # Identifying fields are kept PRECISE: the profile's explicit denials,
        # plus the default identifying names QUALIFIED to student entities only
        # (so a benign dimension like schools.name is never mistaken for PII).
        identifying: Set[str] = set(denied_fields)
        for entity in self.student_entities:
            for fieldname in source_model.entities.get(entity, ()):  # type: ignore[attr-defined]
                if fieldname in DEFAULT_IDENTIFYING_FIELDS:
                    identifying.add(f"{entity}.{fieldname}")
        return ScopeConfig(
            allowed_entities=set(source_model.entities.keys()),
            denied_entities=denied_entities,
            denied_fields=denied_fields,
            aggregate_only=self.aggregate_only,
            identifying_fields=identifying,
            student_entities=set(self.student_entities),
        )


# ---------------------------------------------------------------------------
# Per-session scope file — the single injection point between the GUI spawn
# (which resolves a profile's data_intelligence block) and the agent skill
# handler (which reads it). One canonical path so the two sides never drift.
# ---------------------------------------------------------------------------

# Relative to the deliberation session dir (== handler artifacts_root.parent).
SESSION_SCOPE_RELPATH = ("data_intelligence", "scope.json")


def session_scope_path(session_dir) -> Path:
    return Path(session_dir).joinpath(*SESSION_SCOPE_RELPATH)


def write_session_scope(session_dir, block: Optional[Dict[str, Any]]) -> Path:
    """Persist a profile's resolved data_intelligence block for a session. An
    empty/None block writes ``{}`` → a disabled scope (capability off)."""
    path = session_scope_path(session_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(block or {}, indent=2, sort_keys=True), encoding="utf-8")
    return path


def load_session_scope(session_dir) -> "DataScope":
    """Read a session's data_intelligence block → DataScope. Missing/unreadable
    → disabled (fail-closed)."""
    path = session_scope_path(session_dir)
    if not path.exists():
        return DataScope.disabled()
    try:
        block = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DataScope.disabled()
    return DataScope.from_block(block)
