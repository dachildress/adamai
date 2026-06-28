"""
Adapter contract — the source-agnostic interface every adapter implements.

Slice 3 turns the concrete `SQLiteAdapter` into an implementation of a real
contract, so that future adapters (a second SQL dialect, or a non-SQL one
like CSV/API) implement the SAME interface with no changes here, and the
model-driven skill (Slice 4) plans against a stable boundary.

Source-agnosticism is the load-bearing property: the contract's method
signatures mention nothing SQL-specific (no `translate`, `cursor`,
`statement`). The checkable test — "if a method would change when you swap
PostgreSQL for BigQuery or CSV, it is physical planning and does not belong
here" — is satisfied: `translate()` and SQL generation stay private to
SQL-family adapters.

The contract:
    capabilities() -> AdapterCapabilities   what the adapter can express
    health()       -> AdapterHealth         current operational state
    estimate_cost(plan) -> AdapterCostEstimate | None   adapter-supplied cost
    execute(plan)  -> QueryResult           run the plan, return a result

`AdapterCostEstimate` is REUSED from `sentinel` (one cost type, not two);
`QueryResult` stays defined in the SQL adapter and is referenced here only
as a type (under TYPE_CHECKING) so the contract module imports nothing
SQL-specific at runtime.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from .adapter_capabilities import AdapterCapabilities
from .sentinel import AdapterCostEstimate

if TYPE_CHECKING:  # avoid runtime coupling to SQL-specific modules
    from .execution_plan import ExecutionPlan
    from .query_result import QueryResult


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

# Status values (architecture doc "Adapter Health").
READY = "READY"
DEGRADED = "DEGRADED"
REINDEXING = "REINDEXING"
OFFLINE = "OFFLINE"
AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"

# Runtime-response split:
#   READY                  -> proceed unconditionally
#   DEGRADED / REINDEXING  -> transient; proceed WITH a recorded warning
#   OFFLINE / AUTH_FAILED  -> terminal until fixed; do NOT proceed
_TRANSIENT = {DEGRADED, REINDEXING}
_TERMINAL = {OFFLINE, AUTHENTICATION_FAILED}

# Pipeline outcome when the adapter is not in a usable state (terminal
# health). Mirrors interface §9's pre-planning ADAPTER_UNAVAILABLE category;
# the runner returns it before validation/Sentinel/execute.
ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass(frozen=True)
class AdapterHealth:
    status: str
    checked_at: str
    detail: Optional[str] = None

    @property
    def is_ready(self) -> bool:
        return self.status == READY

    @property
    def is_transient(self) -> bool:
        return self.status in _TRANSIENT

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL

    @property
    def may_proceed(self) -> bool:
        """True for READY and transient states (proceed, possibly with a
        warning); False for terminal states."""
        return not self.is_terminal


def health(status: str = READY, detail: Optional[str] = None) -> AdapterHealth:
    """Convenience constructor stamping checked_at = now."""
    return AdapterHealth(status=status, checked_at=_now(), detail=detail)


# ---------------------------------------------------------------------------
# Adapter interface
# ---------------------------------------------------------------------------

class Adapter(ABC):
    """The contract every adapter implements. Deliberately source-agnostic:
    a CSV or API adapter implements this without any SQL concept."""

    @abstractmethod
    def capabilities(self) -> AdapterCapabilities:
        """What this adapter can express (joins, grouping, aggregation,
        ordering). Used by validation to reject unexpressible plans."""
        raise NotImplementedError

    @abstractmethod
    def health(self) -> AdapterHealth:
        """Current operational state. The runtime checks this first and
        short-circuits on terminal states before any planning/execution."""
        raise NotImplementedError

    @abstractmethod
    def estimate_cost(self, plan: "ExecutionPlan") -> Optional[AdapterCostEstimate]:
        """Adapter-supplied cost estimate for a plan, or None when the
        adapter cannot estimate (Sentinel decides what absence means)."""
        raise NotImplementedError

    @abstractmethod
    def execute(self, plan: "ExecutionPlan") -> "QueryResult":
        """Execute the (validated, approved) plan and return a structured
        result."""
        raise NotImplementedError
