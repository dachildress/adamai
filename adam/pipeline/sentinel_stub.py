"""
Sentinel stub.

The real Sentinel is a deterministic policy engine evaluating governance
predicates (read-only, scope, cost) against the structured plan. That is
explicitly OUT OF SCOPE for this slice.

This stub stands in at the choke point: it receives an already-VALIDATED
plan and returns a structured allow/deny. For this pass it allows valid
`query` plans (the only in-scope intent). It does not parse statements, it
does not implement policy — it is a placeholder so the lifecycle has a
gating step between validation and execution.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .execution_plan import ExecutionPlan


@dataclass(frozen=True)
class SentinelDecision:
    allow: bool
    reason: Optional[str] = None


def sentinel_check(plan: ExecutionPlan) -> SentinelDecision:
    """Allow valid query plans; deny anything else. Stub only — the real
    policy engine (read-only / scope / cost predicates) comes later."""
    if plan.intent_type == "query":
        return SentinelDecision(allow=True)
    return SentinelDecision(
        allow=False,
        reason=f"stub Sentinel permits only 'query'; got {plan.intent_type!r}",
    )
