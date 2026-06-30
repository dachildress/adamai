"""
Data Intelligence skill handler — governed, READ-ONLY data retrieval for agents.

Entry point ``handle(action, args, context)`` called by SkillRuntime, which has
already enforced (from SKILL.md):
  - caller is in allowed_callers (Seeker / Truthseeker),
  - action exists (query / verify),
  - required_args are present (query: source+objective; verify: +claim),
  - arg values are within the runtime's content-size limit.

This handler enforces governance that the runtime does NOT:
  1. capability + source scope from the session's governance profile,
  2. per-session / per-agent budgets,
  3. the shared governed pipeline (validation → Sentinel → adapter) via
     run_governed_query, scoped by the profile (denied fields, aggregate-only),
  4. mapping the outcome to an immutable, citable DATA_RESULT, persisted to the
     session-local evidence store.

It NEVER writes to any database, sends, or acts on results — read-only retrieval
and structured evidence only. On any clean pipeline error it returns a
DATA_RESULT-shaped body with a governance_status, never a stack trace.
"""
from __future__ import annotations

from typing import Any, Dict

# Relative imports work because the runtime loads this as a package
# (skills/data_intelligence has __init__.py → imported as data_intelligence.handler).
from .store import (
    BudgetStore,
    EvidenceStore,
    build_data_result,
    load_session_data_scope,
    run_query,
    session_dir_from_context,
    BUDGET_EXHAUSTED,
    DENIED_SCOPE,
)


def _require_nonempty_str(args: Dict[str, Any], key: str) -> str:
    val = args.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return val.strip()


def _denied_body(*, source, objective, caller, action, claim, context, reason) -> Dict[str, Any]:
    """A DATA_RESULT-shaped body for a pre-pipeline denial/exhaustion — no DB
    access happened. Mirrors the real evidence object so the transcript is
    uniform and citable."""
    body = build_data_result(
        {"error": reason},
        source=source, objective=objective, caller=caller,
        action=action, claim=claim, context=context,
    )
    body["governance_status"] = reason if reason in (BUDGET_EXHAUSTED, DENIED_SCOPE) else body["governance_status"]
    return body


def _wrap(data_result: Dict[str, Any]) -> Dict[str, Any]:
    """Return body: the runtime adds invocation_id/status/etc. on top. Agents
    cite the result by data_result_id."""
    return {
        "data_result_id": data_result["id"],
        "data_result": data_result,
        "governance_status": data_result["governance_status"],
        "summary": _summarize(data_result),
    }


def _summarize(dr: Dict[str, Any]) -> str:
    gs = dr.get("governance_status")
    if gs == BUDGET_EXHAUSTED:
        return ("Data query budget exhausted for this session/agent — reason with "
                "the evidence already gathered; no new query was run.")
    if gs == DENIED_SCOPE:
        return f"Data query denied by governance scope ({dr.get('note') or 'not permitted'}); no data returned."
    if gs == "blocked":
        return f"Data query blocked ({dr.get('note') or 'unavailable'}); no data returned."
    n = dr.get("row_count")
    base = f"Retrieved evidence {dr['id']} from {dr['source']}"
    return base + (f" ({n} rows)." if isinstance(n, int) else ".")


def handle(action: str, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    if action not in ("query", "verify"):
        raise ValueError(f"unsupported action: {action!r}")

    caller = context.get("caller") or "unknown"
    source = _require_nonempty_str(args, "source")
    objective = _require_nonempty_str(args, "objective")
    claim = None
    if action == "verify":
        claim = _require_nonempty_str(args, "claim")

    session_dir = session_dir_from_context(context)
    scope = load_session_data_scope(session_dir)

    common = dict(source=source, objective=objective, caller=caller,
                  action=action, claim=claim, context=context)

    # 1. capability + source scope — BEFORE any data access.
    if not scope.enabled:
        dr = _denied_body(reason=DENIED_SCOPE, **common)
        dr["note"] = "data_intelligence is not enabled for this session's governance profile"
        EvidenceStore(session_dir).append(dr)
        return _wrap(dr)
    if not scope.permits_source(source):
        dr = _denied_body(reason=DENIED_SCOPE, **common)
        dr["note"] = f"source {source!r} is not permitted for this profile"
        EvidenceStore(session_dir).append(dr)
        return _wrap(dr)

    # 2. budgets — BEFORE running (no DB hit when exhausted).
    budgets = BudgetStore(session_dir)
    if budgets.would_exceed(caller, scope):
        dr = _denied_body(reason=BUDGET_EXHAUSTED, **common)
        EvidenceStore(session_dir).append(dr)
        return _wrap(dr)

    # 3. run the shared governed core (read-only), profile-scoped. For verify,
    #    bind the objective to the specific claim so it stays a verification, not
    #    open-ended mining.
    effective_objective = objective
    if action == "verify":
        effective_objective = (
            f"Verify this claim against the data and report only what the data shows: "
            f"{claim}\n\nContext objective: {objective}"
        )
    outcome = run_query(source=source, objective=effective_objective, caller=caller, scope=scope)

    # Count the query against budgets (it reached the pipeline). Done after the
    # call so a pre-pipeline denial/exhaustion never consumes budget.
    budgets.increment(caller)

    # 4. map to an immutable DATA_RESULT and persist to the evidence store.
    dr = build_data_result(outcome, source=source, objective=objective, caller=caller,
                           action=action, claim=claim, context=context)
    EvidenceStore(session_dir).append(dr)
    return _wrap(dr)
