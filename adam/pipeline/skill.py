"""
Data Intelligence skill — turns a natural-language objective into a governed
ExecutionPlan by asking a MODEL for a structured plan BODY.

This is the first slice that involves a model. The central principle is that
**the model's output is UNTRUSTED**: the skill does SEMANTIC planning only
(what data, in domain terms) and hands a constructed plan to the EXISTING
pipeline (validation → Sentinel → adapter), which governs it. A model that
hallucinates an entity or over-broadens scope is fine — governance catches
it. The model can be wrong and the architecture still holds.

Trust boundary (enforced, not assumed):
  * The model proposes ONLY the query body. It must NOT supply any envelope
    field (connection_handle, source_type, source_model_version,
    plan_version, intent_type, purpose). Those are skill/runtime-owned —
    connection_handle resolves to real credentials and a real source, so a
    model that could set it could steer which database is queried.
  * If the model returns an envelope field, the skill REJECTS it as
    PLAN_PARSE_ERROR (fail loud — do not silently ignore).
  * The envelope is built from skill-owned values; the parsed model JSON is
    NEVER spread/merged into the plan (no ExecutionPlan(**model_json)).

Isolation: this is the only pipeline module that touches the model seam, and
it does so LAZILY (inside the production wrapper) so `import adam.pipeline`
stays free of `adam.core`. The pipeline core stays deterministic and
model-free.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .execution_plan import PLAN_VERSION, ExecutionPlan, QueryBody
from .runner import PipelineResult, run_plan
from .source_model import SourceModel

# The injectable model seam: (system_prompt, user_objective) -> raw model text.
# Tests inject a fake; production wires it to call_model via make_call_model_fn.
PlanModelFn = Callable[[str, str], str]

# Skill-level outcome category for a model response that can't become a plan.
# Deliberately SEPARATE from ValidationOutcome / SentinelOutcome — a parse /
# ownership failure happens BEFORE any plan reaches the pipeline.
PLAN_PARSE_ERROR = "PLAN_PARSE_ERROR"

# Envelope fields the model may never supply — runtime/skill-owned.
_ENVELOPE_FIELDS = frozenset({
    "connection_handle", "source_type", "source_model_version",
    "plan_version", "intent_type", "purpose",
})

# Default registry model id. Resolved from config at call time (see
# make_call_model_fn); kept as a default param, never a hardcoded provider,
# so the in-house server later is a config change, not a skill change.
DEFAULT_MODEL_ID = "claude-sonnet-4-6"


class PlanParseError(Exception):
    """Raised when an untrusted model response cannot be turned into a plan
    body (malformed, multiple objects, SQL, or envelope-bearing). Carries the
    PLAN_PARSE_ERROR category."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.category = PLAN_PARSE_ERROR
        self.detail = detail


@dataclass
class DataIntelligenceResult:
    """What the skill returns. Either a parse/ownership failure (parse_error
    set, no plan reached the pipeline) OR a constructed plan plus the
    pipeline's outcome. (No bespoke SkillResult type this slice — Slice 5.)"""
    ok: bool
    parse_error: Optional[str] = None      # PLAN_PARSE_ERROR detail, if any
    plan: Optional[ExecutionPlan] = None
    pipeline: Optional[PipelineResult] = None


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_system_prompt(source_model: SourceModel) -> str:
    """Build the system prompt: the available entities/fields, the closed
    query-body schema, and strict body-only / no-SQL / no-envelope rules."""
    lines: List[str] = []
    lines.append(
        "You are a data-retrieval planner. Given an objective, you propose a "
        "STRUCTURED QUERY BODY (not SQL) describing what data answers it."
    )
    lines.append("")
    lines.append("Available entities and their fields (use ONLY these):")
    for entity in sorted(source_model.entities.keys()):
        fields = ", ".join(source_model.entities[entity])
        lines.append(f"  - {entity}: {fields}")
    lines.append("")
    lines.append("Return ONLY a single JSON object matching this query-body schema:")
    lines.append(
        '  {"operation": "select", "entities": [..], "projection": [..], '
        '"filters": [{"field","op","value"}], "joins": [{"left","right","type"}], '
        '"group_by": [..], "aggregations": [{"fn","field","as"}], '
        '"order_by": [{"field","direction"}], "limit": <int>}'
    )
    lines.append("")
    lines.append("Rules:")
    lines.append("  - operation MUST be 'select'.")
    lines.append("  - Reference ONLY the entities and fields listed above; do not invent any.")
    lines.append("  - 'limit' is REQUIRED (a positive integer). Never project ['*'].")
    lines.append("  - Output ONLY the JSON body. No prose, no markdown, no SQL.")
    lines.append(
        "  - Do NOT include any envelope fields. Specifically, do NOT emit "
        "connection_handle, source_type, source_model_version, plan_version, "
        "intent_type, or purpose. Those are set by the runtime, not by you."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Untrusted-output parsing
# ---------------------------------------------------------------------------

def _extract_json_objects(text: str) -> List[Dict[str, Any]]:
    """Extract all TOP-LEVEL balanced {...} objects that parse as JSON dicts,
    ignoring surrounding prose / markdown fences. Brace matching respects
    string literals so braces inside string values don't confuse it. Nested
    objects are part of their parent, not counted separately."""
    objs: List[Dict[str, Any]] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth, in_str, esc, j = 0, False, False, i
        closed = False
        while j < n:
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        closed = True
                        break
            j += 1
        if closed:
            try:
                parsed = json.loads(text[i:j + 1])
                if isinstance(parsed, dict):
                    objs.append(parsed)
            except Exception:
                pass
            i = j + 1
        else:
            break  # unbalanced from here on
    return objs


def parse_body(raw_text: str) -> QueryBody:
    """Parse an untrusted model response into a QueryBody. Raises
    PlanParseError on anything that isn't exactly one envelope-free query
    body. Never crashes, never passes garbage downstream."""
    if not isinstance(raw_text, str):
        raise PlanParseError("model output is not text")

    objs = _extract_json_objects(raw_text)
    if len(objs) == 0:
        raise PlanParseError("no JSON object found in model output (not a structured body)")
    if len(objs) > 1:
        raise PlanParseError("multiple JSON objects in model output; refusing to guess which is the body")

    obj = objs[0]

    # Ownership boundary: the model must not assert envelope fields.
    present = _ENVELOPE_FIELDS.intersection(obj.keys())
    if present:
        raise PlanParseError(
            f"model output contains envelope field(s) it may not set: {sorted(present)}"
        )

    # Must look like a query body.
    if "operation" not in obj:
        raise PlanParseError("model output is not a query body (missing 'operation')")

    try:
        return QueryBody.from_dict(obj)
    except Exception as e:  # malformed element shapes, wrong types, etc.
        raise PlanParseError(f"could not coerce model output into a query body: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Plan construction (skill-owned envelope + model body)
# ---------------------------------------------------------------------------

def propose_plan(
    objective: str,
    source_model: SourceModel,
    model_fn: PlanModelFn,
    *,
    connection_handle: str,
    source_type: str = "sql",
    purpose: Optional[str] = None,
    estimated_row_scope: str = "small",
) -> ExecutionPlan:
    """Ask the model for a body and build a full ExecutionPlan with a
    SKILL-OWNED envelope. Raises PlanParseError if the model output can't be
    turned into a valid body. The envelope is constructed from skill/runtime
    values — the parsed JSON is never spread into the plan."""
    system_prompt = build_system_prompt(source_model)
    raw = model_fn(system_prompt, objective)
    body = parse_body(raw)   # raises PlanParseError on bad/owned/multiple output

    # Envelope is skill-owned. NOTE: we attach ONLY `body` from the model;
    # every other field comes from skill/runtime variables.
    return ExecutionPlan(
        plan_version=PLAN_VERSION,
        intent_type="query",
        connection_handle=connection_handle,
        source_type=source_type,
        source_model_version=source_model.version,
        purpose=purpose or objective,
        estimated_row_scope=estimated_row_scope,
        body=body,
    )


def run_objective(
    objective: str,
    connection: Any,
    source_model: SourceModel,
    model_fn: PlanModelFn,
    *,
    connection_handle: str,
    adapter: Any = None,
    governance: Any = None,
    scope: Any = None,
) -> DataIntelligenceResult:
    """Full skill flow: objective → model body → constructed plan → EXISTING
    governed pipeline. A parse/ownership failure returns before pipeline
    entry (no plan constructed); otherwise the pipeline outcome is returned."""
    try:
        plan = propose_plan(
            objective, source_model, model_fn,
            connection_handle=connection_handle,
        )
    except PlanParseError as e:
        return DataIntelligenceResult(ok=False, parse_error=e.detail)

    pipe = run_plan(
        plan, connection, adapter=adapter, source_model=source_model,
        governance=governance, scope=scope,
    )
    return DataIntelligenceResult(ok=pipe.ok, plan=plan, pipeline=pipe)


# ---------------------------------------------------------------------------
# Production model wrapper (lazy import — keeps the package model-free)
# ---------------------------------------------------------------------------

def make_call_model_fn(
    model_id: str = DEFAULT_MODEL_ID,
    *,
    max_tokens: int = 1024,
    temperature: float = 0.0,
) -> PlanModelFn:
    """Wire the injectable seam to ADAM's real provider-agnostic dispatcher.

    The registry is loaded from config (providers.json / models.json), so the
    backend (cloud → in-house) is a CONFIG change, not a skill change. Imports
    are LAZY so merely importing the pipeline never pulls in adam.core."""
    def fn(system_prompt: str, objective: str) -> str:
        from adam.core.client_dispatch import call_model
        from adam.core.config_loader import load_and_validate_config
        providers, models, _agents = load_and_validate_config()
        return call_model(
            model_id=model_id,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": objective}],
            max_tokens=max_tokens,
            temperature=temperature,
            models=models,
            providers=providers,
        )
    return fn
