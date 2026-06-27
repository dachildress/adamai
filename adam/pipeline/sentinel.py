"""
Sentinel — deterministic governance predicates over the structured plan.

This replaces the Slice-1 stub. Sentinel runs AFTER validation (which means
the plan is already well-formed) and decides whether a well-formed plan is
ALLOWED, POLICY_DENIED, or APPROVAL_REQUIRED.

Hard rules:
  * Sentinel evaluates ONLY structured `ExecutionPlan` fields. It never
    parses SQL and never sees adapter-generated SQL — that is the whole
    point of the structured plan.
  * Sentinel outcomes are distinct from validation outcomes. Validation =
    "malformed". Sentinel = "well-formed but allowed / denied / approval".
  * A denied or approval-required outcome stops the pipeline before the
    adapter runs (enforced by the runner).

Predicates (this slice): read-only, entity scope, field denylist, cost.
Order is fixed for stable, audit-legible dispositions: read-only → entity
scope → field denylist → cost.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Set

from .execution_plan import ExecutionPlan, QueryBody

# Dispositions (interface §9 subset relevant to this slice).
ALLOWED = "ALLOWED"
POLICY_DENIED = "POLICY_DENIED"
APPROVAL_REQUIRED = "APPROVAL_REQUIRED"

# Categories mirror the disposition for denied/approval outcomes so an
# auditor can filter on either; ALLOWED has no category.
_CATEGORY = {
    POLICY_DENIED: "POLICY_DENIED",
    APPROVAL_REQUIRED: "APPROVAL_REQUIRED",
}

# Read vs write classification. `query`/`select` is read; everything that
# writes (mutations, raw statements) is write. is_write() below is the
# single source of truth so a future write intent flows through the
# read-only predicate unchanged.
_READ_OPERATIONS = {"select"}
_WRITE_OPERATIONS = {"insert", "update", "delete", "upsert"}
_WRITE_INTENT_TYPES = {"mutation"}  # raw_statement is treated write-ish below

# Explicit complexity ordering — never string comparison.
_COMPLEXITY_RANK = {"low": 1, "medium": 2, "high": 3}


@dataclass(frozen=True)
class GovernanceConfig:
    read_only: bool = True
    approval_required_for_cost_absence: bool = False
    max_estimated_rows: Optional[int] = None
    max_cost_complexity: Optional[str] = None   # "low" | "medium" | "high"


@dataclass(frozen=True)
class ScopeConfig:
    allowed_entities: Set[str] = field(default_factory=set)
    denied_entities: Set[str] = field(default_factory=set)
    denied_fields: Set[str] = field(default_factory=set)


@dataclass(frozen=True)
class AdapterCostEstimate:
    rows: Optional[int] = None
    bytes_scanned: Optional[int] = None
    complexity: Optional[str] = None   # "low" | "medium" | "high"


@dataclass(frozen=True)
class SentinelOutcome:
    ok: bool
    disposition: str
    category: Optional[str] = None
    detail: Optional[str] = None

    @property
    def allow(self) -> bool:
        """Convenience alias: True only when ALLOWED. (Approval-required is
        not an allow — execution must stop.)"""
        return self.disposition == ALLOWED


def _allowed() -> SentinelOutcome:
    return SentinelOutcome(ok=True, disposition=ALLOWED)


def _denied(detail: str) -> SentinelOutcome:
    return SentinelOutcome(ok=False, disposition=POLICY_DENIED,
                           category=_CATEGORY[POLICY_DENIED], detail=detail)


def _approval(detail: str) -> SentinelOutcome:
    return SentinelOutcome(ok=False, disposition=APPROVAL_REQUIRED,
                           category=_CATEGORY[APPROVAL_REQUIRED], detail=detail)


# ---------------------------------------------------------------------------
# Write classification (generic — not hardcoded to "query is always read")
# ---------------------------------------------------------------------------

def is_write(plan: ExecutionPlan) -> bool:
    """Classify a plan as a write. Structured on intent_type + operation so
    future write intents (mutation, raw_statement) are denied by the
    read-only predicate with NO change to predicate logic.

    A non-select structured operation is a write. A `mutation` intent is a
    write. A `raw_statement` cannot be reasoned about structurally, so it is
    treated as a potential write (conservative)."""
    if plan.intent_type in _WRITE_INTENT_TYPES:
        return True
    if plan.intent_type == "raw_statement":
        return True
    body = plan.body
    if isinstance(body, QueryBody):
        op = body.operation
        if op in _READ_OPERATIONS:
            return False
        if op in _WRITE_OPERATIONS:
            return True
        # Unknown operation reaching Sentinel: treat conservatively as write.
        return True
    # No structured body we recognize → conservative.
    return True


# ---------------------------------------------------------------------------
# Field-reference collection (structured only)
# ---------------------------------------------------------------------------

def _referenced_fields(body: QueryBody):
    """Yield every field reference the plan touches, across all field-bearing
    places. Used by the field denylist predicate."""
    for p in body.projection:
        yield p
    for f in body.filters:
        yield f.field
    for j in body.joins:
        yield j.left
        yield j.right
    for g in body.group_by:
        yield g
    for a in body.aggregations:
        yield a.field
    for o in body.order_by:
        yield o.field


def _entity_of(ref: str) -> Optional[str]:
    """The entity part of a qualified 'entity.field' reference, else None."""
    return ref.split(".")[0] if "." in ref else None


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------

def _predicate_read_only(plan: ExecutionPlan, gov: GovernanceConfig) -> Optional[SentinelOutcome]:
    if gov.read_only and is_write(plan):
        return _denied(
            f"Write-class plan (intent_type={plan.intent_type!r}) denied on a "
            f"read-only connection"
        )
    return None


def _predicate_entity_scope(body: QueryBody, scope: ScopeConfig) -> Optional[SentinelOutcome]:
    for entity in body.entities:
        if entity in scope.denied_entities:
            return _denied(f"Entity {entity} is explicitly denied by scope")
        if scope.allowed_entities and entity not in scope.allowed_entities:
            return _denied(f"Entity {entity} is outside allowed scope")
    return None


def _predicate_field_denylist(body: QueryBody, scope: ScopeConfig) -> Optional[SentinelOutcome]:
    if not scope.denied_fields:
        return None
    for ref in _referenced_fields(body):
        # A reference matches the denylist either as the full qualified name
        # (e.g. "students.ssn") or as a bare field name where the denylist
        # lists the field unqualified (e.g. "ssn").
        bare = ref.split(".")[-1]
        if ref in scope.denied_fields or bare in scope.denied_fields:
            return _denied(f"Field {ref} is denied")
    return None


def _predicate_cost(
    gov: GovernanceConfig,
    cost: Optional[AdapterCostEstimate],
) -> Optional[SentinelOutcome]:
    if cost is None:
        if gov.approval_required_for_cost_absence:
            return _approval("Cost estimate absent and approval is required by governance profile")
        return None  # absence allowed by config → continue

    # Rows threshold.
    if (gov.max_estimated_rows is not None
            and cost.rows is not None
            and cost.rows > gov.max_estimated_rows):
        return _denied(
            f"Cost estimate rows={cost.rows} exceeds max_estimated_rows={gov.max_estimated_rows}"
        )

    # Complexity threshold (numeric ranking, never string comparison).
    if gov.max_cost_complexity is not None and cost.complexity is not None:
        max_rank = _COMPLEXITY_RANK.get(gov.max_cost_complexity)
        cost_rank = _COMPLEXITY_RANK.get(cost.complexity)
        if max_rank is not None and cost_rank is not None and cost_rank > max_rank:
            return _denied(
                f"Cost estimate complexity={cost.complexity} exceeds "
                f"max_cost_complexity={gov.max_cost_complexity}"
            )
    return None


# ---------------------------------------------------------------------------
# Evaluate
# ---------------------------------------------------------------------------

def evaluate(
    plan: ExecutionPlan,
    governance: GovernanceConfig,
    scope: ScopeConfig,
    cost_estimate: Optional[AdapterCostEstimate] = None,
) -> SentinelOutcome:
    """Evaluate governance predicates against the structured plan. Returns a
    typed SentinelOutcome; never raises for a denied plan. Assumes the plan
    already passed validation (well-formed)."""
    # 1. read-only
    out = _predicate_read_only(plan, governance)
    if out is not None:
        return out

    body = plan.body
    # Defensive: if a non-structured body reached here, deny rather than
    # silently allow (validation should have stopped it).
    if not isinstance(body, QueryBody):
        return _denied("Sentinel received a plan without a structured body")

    # 2. entity scope
    out = _predicate_entity_scope(body, scope)
    if out is not None:
        return out

    # 3. field denylist
    out = _predicate_field_denylist(body, scope)
    if out is not None:
        return out

    # 4. cost
    out = _predicate_cost(governance, cost_estimate)
    if out is not None:
        return out

    return _allowed()
