"""
Pipeline runner — the end-to-end lifecycle for the slice.

    ExecutionPlan -> validate -> Sentinel policy evaluation -> SQLite adapter -> result

Slice 2 replaces the Sentinel stub with real deterministic predicate
evaluation (``adam.pipeline.sentinel``). Flow control is strict: a
validation rejection never reaches Sentinel, and a Sentinel outcome that is
not ALLOWED (i.e. POLICY_DENIED or APPROVAL_REQUIRED) stops the pipeline
BEFORE the adapter — no SQL is built or executed.

Still standalone (not wired into the live ADAM loop), still synthetic data,
still no model call.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .adapter_capabilities import AdapterCapabilities, SQLITE_CAPABILITIES
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

# Permissive defaults so an in-scope read-only query still runs end to end
# without the caller having to spell out a config. Tests that exercise
# denial pass stricter configs explicitly.
DEFAULT_GOVERNANCE = GovernanceConfig(read_only=True)


def _default_scope(source_model: SourceModel) -> ScopeConfig:
    """Allow every entity the model declares; deny nothing. The starting
    point a real instance would then tighten."""
    return ScopeConfig(
        allowed_entities=set(source_model.entities.keys()),
        denied_entities=set(),
        denied_fields=set(),
    )


@dataclass
class PipelineResult:
    ok: bool
    stage: str                                   # validation | sentinel | execution
    validation: ValidationOutcome
    sentinel: Optional[SentinelOutcome] = None
    result: Optional[QueryResult] = None
    detail: Optional[str] = None


def run_plan(
    plan: ExecutionPlan,
    connection,
    source_model: SourceModel = SYNTHETIC_SCHOOL_V1,
    capabilities: AdapterCapabilities = SQLITE_CAPABILITIES,
    config: ValidationConfig = ValidationConfig(),
    governance: Optional[GovernanceConfig] = None,
    scope: Optional[ScopeConfig] = None,
    cost_estimate: Optional[AdapterCostEstimate] = None,
) -> PipelineResult:
    governance = governance or DEFAULT_GOVERNANCE
    scope = scope if scope is not None else _default_scope(source_model)

    # Stage 1: validation (structural; before governance).
    outcome = validate(plan, capabilities, config)
    if not outcome.ok:
        return PipelineResult(False, "validation", outcome, detail=outcome.detail)

    # Stage 2: Sentinel governance predicates (structured plan only).
    decision = sentinel_evaluate(plan, governance, scope, cost_estimate)
    if not decision.allow:
        # POLICY_DENIED or APPROVAL_REQUIRED both stop here — the adapter is
        # never constructed and no SQL is built or run.
        return PipelineResult(False, "sentinel", outcome, sentinel=decision, detail=decision.detail)

    # Stage 3: adapter execution (only reached for ALLOWED plans).
    adapter = SQLiteAdapter(connection, source_model, capabilities)
    result = adapter.execute(plan)
    return PipelineResult(True, "execution", outcome, sentinel=decision, result=result)
