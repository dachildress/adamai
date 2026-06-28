"""
Pipeline runner — the end-to-end lifecycle.

Slice 3 adds two real steps that talk to the adapter THROUGH the interface:

    adapter health check
       OFFLINE / AUTHENTICATION_FAILED -> stop; ADAPTER_UNAVAILABLE
                                          (do NOT validate, plan, or execute)
       DEGRADED / REINDEXING           -> proceed; record a warning
       READY                           -> proceed
    validation                         (existing)
    adapter cost estimate              -> fed into Sentinel (existing consumer)
    Sentinel                           ALLOWED proceeds; POLICY_DENIED /
                                       APPROVAL_REQUIRED stop
    execute                            (existing)

Health and cost are obtained FROM the adapter (no hand-passing required),
so the runner is source-agnostic: it knows only the `Adapter` contract. A
caller may still inject a forced cost estimate or a forced-health adapter to
exercise specific paths. Still standalone (no live-loop), synthetic data,
no model call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from .adapter import ADAPTER_UNAVAILABLE, Adapter, AdapterHealth
from .execution_plan import ExecutionPlan
from .sentinel import (
    AdapterCostEstimate,
    GovernanceConfig,
    ScopeConfig,
    SentinelOutcome,
    evaluate as sentinel_evaluate,
)
from .source_model import SYNTHETIC_SCHOOL_V1, SourceModel
from .sqlite_adapter import QueryResult, SQLiteAdapter
from .validation import ValidationConfig, ValidationOutcome, validate

DEFAULT_GOVERNANCE = GovernanceConfig(read_only=True)


def _default_scope(source_model: SourceModel) -> ScopeConfig:
    return ScopeConfig(
        allowed_entities=set(source_model.entities.keys()),
        denied_entities=set(),
        denied_fields=set(),
    )


@dataclass
class PipelineResult:
    ok: bool
    stage: str                                   # adapter_health | validation | sentinel | execution
    validation: Optional[ValidationOutcome] = None
    sentinel: Optional[SentinelOutcome] = None
    health: Optional[AdapterHealth] = None
    result: Optional[QueryResult] = None
    detail: Optional[str] = None
    warnings: List[str] = field(default_factory=list)


def run_plan(
    plan: ExecutionPlan,
    connection: Any = None,
    *,
    adapter: Optional[Adapter] = None,
    source_model: SourceModel = SYNTHETIC_SCHOOL_V1,
    config: ValidationConfig = ValidationConfig(),
    governance: Optional[GovernanceConfig] = None,
    scope: Optional[ScopeConfig] = None,
    cost_estimate: Optional[AdapterCostEstimate] = None,
) -> PipelineResult:
    governance = governance or DEFAULT_GOVERNANCE
    scope = scope if scope is not None else _default_scope(source_model)
    if adapter is None:
        adapter = SQLiteAdapter(connection, source_model)

    warnings: List[str] = []

    # Stage 0: adapter health — checked FIRST so a dead adapter short-circuits
    # before any planning. Terminal states stop; transient states warn.
    health = adapter.health()
    if health.is_terminal:
        detail = f"{ADAPTER_UNAVAILABLE}: adapter health is {health.status}"
        if health.detail:
            detail += f" ({health.detail})"
        return PipelineResult(False, "adapter_health", health=health, detail=detail)
    if health.is_transient:
        msg = f"adapter health is {health.status}"
        if health.detail:
            msg += f": {health.detail}"
        warnings.append(msg)

    # Stage 1: validation (structural; capabilities come from the adapter).
    outcome = validate(plan, adapter.capabilities(), config)
    if not outcome.ok:
        return PipelineResult(False, "validation", validation=outcome,
                              health=health, detail=outcome.detail, warnings=warnings)

    # Stage 2: cost estimate — adapter-supplied (unless a caller forces one).
    cost = cost_estimate if cost_estimate is not None else adapter.estimate_cost(plan)

    # Stage 3: Sentinel governance predicates (consumes the cost estimate).
    decision = sentinel_evaluate(plan, governance, scope, cost)
    if not decision.allow:
        return PipelineResult(False, "sentinel", validation=outcome, sentinel=decision,
                              health=health, detail=decision.detail, warnings=warnings)

    # Stage 4: execute (only for ALLOWED plans on a usable adapter).
    result = adapter.execute(plan)
    return PipelineResult(True, "execution", validation=outcome, sentinel=decision,
                          health=health, result=result, warnings=warnings)
