"""
adam.pipeline — governed execution pipeline (first vertical slice).

ExecutionPlan -> validation -> stub Sentinel -> SQLite adapter -> result.

Isolated package: importable and testable with no dependency on the live
ADAM deliberation loop. Scope is intent_type="query" / operation="select"
against synthetic SQLite only (see codeprompt/mainprompt.md). mutation and
raw_statement are out of scope and rejected by validation.
"""
from __future__ import annotations

from .execution_plan import (
    PLAN_VERSION,
    Aggregation,
    ExecutionPlan,
    ExecutionRequest,
    Filter,
    Join,
    Order,
    QueryBody,
    compute_plan_id,
)
from .adapter_capabilities import AdapterCapabilities, SQLITE_CAPABILITIES
from .source_model import (
    RATIFIED_MODELS,
    SYNTHETIC_SCHOOL_V1,
    SourceModel,
    get_source_model,
)
from .validation import (
    CAPABILITY_ERROR,
    SOURCE_MODEL_ERROR,
    VALIDATION_ERROR,
    ValidationConfig,
    ValidationOutcome,
    scan_for_credentials,
    validate,
)
from .sentinel_stub import SentinelDecision, sentinel_check
from .sentinel import (
    ALLOWED,
    APPROVAL_REQUIRED,
    POLICY_DENIED,
    AdapterCostEstimate,
    GovernanceConfig,
    ScopeConfig,
    SentinelOutcome,
    evaluate as sentinel_evaluate,
    is_write,
)
from .sqlite_adapter import (
    AdapterError,
    QueryResult,
    SQLiteAdapter,
    create_synthetic_db,
)
from .runner import PipelineResult, run_plan

__all__ = [
    "PLAN_VERSION",
    "ExecutionPlan",
    "ExecutionRequest",
    "QueryBody",
    "Filter",
    "Join",
    "Aggregation",
    "Order",
    "compute_plan_id",
    "AdapterCapabilities",
    "SQLITE_CAPABILITIES",
    "SourceModel",
    "SYNTHETIC_SCHOOL_V1",
    "RATIFIED_MODELS",
    "get_source_model",
    "validate",
    "ValidationOutcome",
    "ValidationConfig",
    "scan_for_credentials",
    "VALIDATION_ERROR",
    "SOURCE_MODEL_ERROR",
    "CAPABILITY_ERROR",
    "sentinel_check",
    "SentinelDecision",
    "SQLiteAdapter",
    "QueryResult",
    "AdapterError",
    "create_synthetic_db",
    "run_plan",
    "PipelineResult",
    "sentinel_evaluate",
    "is_write",
    "SentinelOutcome",
    "GovernanceConfig",
    "ScopeConfig",
    "AdapterCostEstimate",
    "ALLOWED",
    "POLICY_DENIED",
    "APPROVAL_REQUIRED",
]
