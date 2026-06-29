"""
ExecutionPlan — the canonical, source-agnostic plan object (interface v1).

This is the spine of the governed execution pipeline: the immutable,
declarative statement a capability emits ("here is the operation I believe
satisfies the objective"). Sentinel evaluates it, the adapter translates
it, audit records it.

Scope of THIS slice (see mainprompt.md): only `intent_type="query"` with
`body.operation="select"`. The envelope and the structured-query body are
modeled here; `mutation` and `raw_statement` are intentionally NOT
implemented and are rejected by validation as out of scope.

Immutability
------------
`ExecutionPlan`, `QueryBody`, and the small body elements (`Filter`,
`Join`, `Aggregation`, `Order`) are frozen dataclasses, and all
collections are stored as tuples. An adapter or any downstream consumer
cannot mutate a plan. Runtime/adapter metadata lives OUTSIDE the plan, on
`ExecutionRequest.runtime_context`, so the plan that gets hashed and
audited never changes during execution.

plan_id
-------
`plan_id` = SHA-256 over the canonical, key-sorted JSON of the plan
envelope + body. Because the runtime context is not part of the plan, and
the adapter result is not part of the plan, neither affects the id. JSON
key ordering does not affect it (keys are sorted); a changed logical plan
does.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

# Schema version of this ExecutionPlan object (envelope field), distinct
# from a source_model_version.
PLAN_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Structured-query body elements (frozen; tuples everywhere)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Filter:
    field: str
    op: str
    value: Any

    def to_dict(self) -> Dict[str, Any]:
        return {"field": self.field, "op": self.op, "value": self.value}


@dataclass(frozen=True)
class Join:
    left: str
    right: str
    type: str = "inner"

    def to_dict(self) -> Dict[str, Any]:
        return {"left": self.left, "right": self.right, "type": self.type}


@dataclass(frozen=True)
class Aggregation:
    fn: str
    field: str
    as_: str

    def to_dict(self) -> Dict[str, Any]:
        # Serialized key is "as" (a Python keyword, so the attr is as_).
        return {"fn": self.fn, "field": self.field, "as": self.as_}


@dataclass(frozen=True)
class Order:
    field: str
    direction: str = "asc"

    def to_dict(self) -> Dict[str, Any]:
        return {"field": self.field, "direction": self.direction}


@dataclass(frozen=True)
class QueryBody:
    """Canonical structured body for intent_type=query (closed model)."""
    operation: str
    entities: Tuple[str, ...]
    projection: Tuple[str, ...] = ()
    filters: Tuple[Filter, ...] = ()
    joins: Tuple[Join, ...] = ()
    group_by: Tuple[str, ...] = ()
    aggregations: Tuple[Aggregation, ...] = ()
    order_by: Tuple[Order, ...] = ()
    limit: Optional[int] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "QueryBody":
        return cls(
            operation=d.get("operation"),
            entities=tuple(d.get("entities", []) or []),
            projection=tuple(d.get("projection", []) or []),
            filters=tuple(Filter(**f) for f in (d.get("filters") or [])),
            joins=tuple(Join(**j) for j in (d.get("joins") or [])),
            group_by=tuple(d.get("group_by", []) or []),
            aggregations=tuple(
                # Normalize the aggregation fn to lowercase at the parse boundary
                # so the canonical/audited plan always carries a lowercase fn,
                # regardless of how the model cased it (COUNT/Count/count). This
                # does NOT widen the allowed set — validation still gates the name.
                Aggregation(fn=str(a["fn"]).lower(), field=a["field"], as_=a.get("as", a.get("as_")))
                for a in (d.get("aggregations") or [])
            ),
            order_by=tuple(Order(**o) for o in (d.get("order_by") or [])),
            limit=d.get("limit"),
        )

    def to_dict(self) -> Dict[str, Any]:
        # Always emit every key (empty collections included) so two
        # logically identical bodies — one built with filters omitted, one
        # with filters=[] — normalize to the same canonical form.
        return {
            "operation": self.operation,
            "entities": list(self.entities),
            "projection": list(self.projection),
            "filters": [f.to_dict() for f in self.filters],
            "joins": [j.to_dict() for j in self.joins],
            "group_by": list(self.group_by),
            "aggregations": [a.to_dict() for a in self.aggregations],
            "order_by": [o.to_dict() for o in self.order_by],
            "limit": self.limit,
        }


# ---------------------------------------------------------------------------
# ExecutionPlan envelope (frozen / immutable)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecutionPlan:
    intent_type: str
    connection_handle: str
    source_type: str
    source_model_version: str
    purpose: str
    estimated_row_scope: str
    body: QueryBody
    plan_version: str = PLAN_VERSION

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExecutionPlan":
        """Build a plan from a JSON-shaped dict (as in the interface spec).

        Tolerant of a missing `body` (left as None) so validation — not
        construction — is what rejects malformed envelopes.
        """
        raw_body = d.get("body")
        body = QueryBody.from_dict(raw_body) if isinstance(raw_body, dict) else raw_body
        return cls(
            plan_version=d.get("plan_version", PLAN_VERSION),
            intent_type=d.get("intent_type"),
            connection_handle=d.get("connection_handle"),
            source_type=d.get("source_type"),
            source_model_version=d.get("source_model_version"),
            purpose=d.get("purpose"),
            estimated_row_scope=d.get("estimated_row_scope"),
            body=body,
        )

    def to_canonical_dict(self) -> Dict[str, Any]:
        """The plan as a plain dict for hashing/audit. Excludes any runtime
        or adapter metadata by construction (those are not on the plan)."""
        return {
            "plan_version": self.plan_version,
            "intent_type": self.intent_type,
            "connection_handle": self.connection_handle,
            "source_type": self.source_type,
            "source_model_version": self.source_model_version,
            "purpose": self.purpose,
            "estimated_row_scope": self.estimated_row_scope,
            "body": self.body.to_dict() if isinstance(self.body, QueryBody) else self.body,
        }

    def canonical_json(self) -> str:
        # sort_keys makes dict-key ordering irrelevant to the hash; compact
        # separators keep the digest input stable. default=str is a
        # defensive fallback for any unexpected value type.
        return json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    @property
    def plan_id(self) -> str:
        """Deterministic SHA-256 of the canonical plan JSON."""
        return compute_plan_id(self)


def compute_plan_id(plan: ExecutionPlan) -> str:
    """SHA-256 over the canonical normalized ExecutionPlan JSON.

    Identical logical plans -> identical hash; key ordering does not
    matter; runtime context and adapter results are not inputs.
    """
    return hashlib.sha256(plan.canonical_json().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Runtime wrapper — keeps mutable runtime/adapter metadata off the plan
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecutionRequest:
    """Wraps an immutable plan with mutable runtime context.

    `runtime_context` may hold adapter health, cost estimate, timestamps,
    execution state, retry count, etc. It is a plain mutable dict on
    purpose — the runtime updates it during execution — but it lives here,
    not on the ExecutionPlan, so it never affects plan_id and adapters
    cannot mutate the plan through it.
    """
    execution_id: str
    plan: ExecutionPlan
    runtime_context: Dict[str, Any] = field(default_factory=dict)
