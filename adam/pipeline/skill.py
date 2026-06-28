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
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .execution_plan import PLAN_VERSION, ExecutionPlan, QueryBody
from .runner import PipelineResult, run_plan
from .source_model import SourceModel

# Injectable model seams: (system_prompt, user_text) -> raw model text.
# Slice 5 splits the single Slice-4 seam into two NAMED responsibilities so
# planning and interpretation can use different models/backends later with no
# refactor. Both default to the same configured model today.
#   PlanningModelFn       — proposes the plan body (Slice-4 role).
#   InterpretationModelFn — interprets runtime-computed observations.
PlanningModelFn = Callable[[str, str], str]
InterpretationModelFn = Callable[[str, str], str]
# Back-compat alias for the Slice-4 name.
PlanModelFn = PlanningModelFn

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


# ===========================================================================
# Slice 5: SkillResult + attribution
#
# Trust boundary on the RESULT side, mirroring Slice 4 on the plan side:
#   - the RUNTIME computes observations deterministically from QueryResult
#     (no model, no "facts" field in any model contract);
#   - the model receives observations + metadata + lineage (NEVER raw rows)
#     and returns ONLY interpretation (inferences/recommendations/etc.);
#   - SkillResult assembles runtime observations + model interpretation, with
#     the fact/judgment line visible at a glance (separate typed fields).
# ===========================================================================

@dataclass
class Observation:
    """A single machine-derived fact about THIS result set. Runtime-owned."""
    label: str
    value: Any = None
    detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"label": self.label, "value": self.value, "detail": self.detail}


@dataclass
class SkillResult:
    """Typed, attributed answer. observations are RUNTIME facts; inferences /
    recommendations / assumptions / confidence are MODEL judgment — kept in
    separate fields so the fact/judgment line is legible, never a prose blob."""
    objective: str
    status: str                                    # see _STATUSES
    data_analyzed: Dict[str, Any] = field(default_factory=dict)
    observations: List[Dict[str, Any]] = field(default_factory=list)   # runtime
    inferences: List[str] = field(default_factory=list)                # model
    recommendations: List[str] = field(default_factory=list)           # model
    assumptions: List[str] = field(default_factory=list)               # model
    limitations: List[str] = field(default_factory=list)               # runtime + model
    confidence: Optional[str] = None                                   # model self-report
    confidence_rationale: Optional[str] = None                         # model
    source_lineage: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# ---------------------------------------------------------------------------
# Observations: runtime-owned, deterministic (NO model, ever)
# ---------------------------------------------------------------------------

def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def derive_observations(query_result, plan) -> List[Observation]:
    """Deterministically derive observations from a QueryResult (+ plan
    metadata). NO model involvement — pure, checkable. Counts, simple
    aggregates over numeric columns, and a top record; nothing that requires
    interpretation (that is the model's job, in inferences)."""
    qr = query_result
    obs: List[Observation] = [Observation("rows_returned", qr.row_count)]
    entities = list(plan.body.entities) if isinstance(plan.body, QueryBody) else []
    obs.append(Observation("entities_queried", entities))
    if qr.row_count == 0:
        # Empty set: do NOT fabricate any "highest/max" observation.
        return obs

    cols = list(qr.columns)
    # A column is numeric only if EVERY returned value in it is a number.
    numeric_cols: Dict[str, int] = {}
    for idx, col in enumerate(cols):
        col_vals = [row[idx] for row in qr.rows]
        if col_vals and all(_is_number(v) for v in col_vals):
            numeric_cols[col] = idx

    for col, idx in numeric_cols.items():
        vals = [row[idx] for row in qr.rows]
        obs.append(Observation(f"max:{col}", max(vals)))
        obs.append(Observation(f"min:{col}", min(vals)))
        obs.append(Observation(f"mean:{col}", round(sum(vals) / len(vals), 4)))

    # Top record by the first numeric column, labeled by the first non-numeric
    # column (e.g. "highest avg_rate: Maple Elementary").
    label_idx = next((i for i, c in enumerate(cols) if c not in numeric_cols), None)
    if numeric_cols and label_idx is not None:
        ncol = next(iter(numeric_cols))
        nidx = numeric_cols[ncol]
        top = max(qr.rows, key=lambda r: r[nidx])
        obs.append(Observation(
            f"top_by:{ncol}", top[label_idx],
            detail=f"{cols[label_idx]}={top[label_idx]}, {ncol}={top[nidx]}",
        ))

    has_missing = any(any(v is None for v in row) for row in qr.rows)
    obs.append(Observation("has_missing_values", has_missing))
    return obs


# ---------------------------------------------------------------------------
# Interpretation: model sees observations, NEVER raw rows
# ---------------------------------------------------------------------------

def build_interpretation_system_prompt() -> str:
    return "\n".join([
        "You are a data analyst. You are given OBSERVATIONS already computed "
        "from a query result. You do NOT see the raw rows.",
        "",
        "Return ONLY a single JSON object with these keys:",
        '  {"inferences": [..], "recommendations": [..], "assumptions": [..], '
        '"limitations": [..], "confidence": "low|medium|high", '
        '"confidence_rationale": ".."}',
        "",
        "Rules:",
        "  - Reason ONLY over the observations provided. Do not invent data.",
        "  - Do NOT include an 'observations' or 'facts' field — observations are "
        "computed by the runtime, not by you; any you supply are ignored.",
        "  - inferences/recommendations are your interpretation, clearly beyond the "
        "raw observations.",
        "  - Output ONLY the JSON object. No prose, no markdown, no SQL.",
    ])


def _as_str_list(value: Any) -> List[str]:
    """Coerce an untrusted value into a list of strings (drop non-strings)."""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    if isinstance(value, str):
        return [value]
    return []


def _interpret(objective, data_analyzed, observations, lineage, model_fn):
    """Call the interpretation model with observations + metadata + lineage
    (NOT rows). Returns (interpretation_dict, error_or_None). Untrusted-output
    discipline: exactly one JSON object; ignore any observations/facts the
    model asserts; never crash, never fabricate."""
    system = build_interpretation_system_prompt()
    # The payload deliberately carries observations/metadata/lineage only —
    # there is no "rows" key. Raw records never enter model context.
    payload = json.dumps({
        "objective": objective,
        "data_analyzed": data_analyzed,
        "observations": observations,
        "source_lineage": lineage,
    })
    raw = model_fn(system, payload)
    objs = _extract_json_objects(raw)
    if len(objs) == 0:
        return None, "no JSON object in interpretation output"
    if len(objs) > 1:
        return None, "multiple JSON objects in interpretation output"
    o = objs[0]
    conf = o.get("confidence")
    interp = {
        # observations/facts keys are intentionally NOT read — runtime owns them.
        "inferences": _as_str_list(o.get("inferences")),
        "recommendations": _as_str_list(o.get("recommendations")),
        "assumptions": _as_str_list(o.get("assumptions")),
        "limitations": _as_str_list(o.get("limitations")),
        "confidence": conf if conf in ("low", "medium", "high") else None,
        "confidence_rationale": o.get("confidence_rationale")
        if isinstance(o.get("confidence_rationale"), str) else None,
    }
    return interp, None


# ---------------------------------------------------------------------------
# analyze_objective — the attributed-answer entry point
# ---------------------------------------------------------------------------

def analyze_objective(
    objective: str,
    connection: Any,
    source_model: SourceModel,
    planning_model_fn: PlanningModelFn,
    interpretation_model_fn: InterpretationModelFn,
    *,
    connection_handle: str,
    adapter: Any = None,
    governance: Any = None,
    scope: Any = None,
) -> SkillResult:
    """Objective → governed plan → execute → runtime observations → model
    interpretation → assembled SkillResult. Honest on denial/empty/failure:
    no fabricated observations or inferences when there's nothing (or nothing
    permitted) to interpret."""
    # Slice-4 flow: build a plan (skill-owned envelope + model body).
    try:
        plan = propose_plan(objective, source_model, planning_model_fn,
                            connection_handle=connection_handle)
    except PlanParseError as e:
        return SkillResult(
            objective=objective, status="plan_parse_error",
            limitations=[f"No governed plan could be built from the model output: {e.detail}"],
            confidence="low", confidence_rationale="No plan was produced; nothing was executed.",
        )

    pipe = run_plan(plan, connection, adapter=adapter, source_model=source_model,
                    governance=governance, scope=scope)
    lineage = {"plan_id": plan.plan_id, "source_model_version": plan.source_model_version}
    data_analyzed: Dict[str, Any] = {
        "plan_id": plan.plan_id,
        "entities": list(plan.body.entities) if isinstance(plan.body, QueryBody) else [],
        "row_count": None,
    }

    # Honest failure: denied / invalid / adapter-unavailable → no fabrication.
    if not pipe.ok:
        status = {
            "validation": "validation_error",
            "adapter_health": "adapter_unavailable",
        }.get(pipe.stage)
        if status is None and pipe.stage == "sentinel":
            disp = getattr(pipe.sentinel, "disposition", None)
            status = "approval_required" if disp == "APPROVAL_REQUIRED" else "policy_denied"
        status = status or "error"
        return SkillResult(
            objective=objective, status=status, data_analyzed=data_analyzed,
            limitations=[f"The request was not executed ({pipe.stage}): {pipe.detail}"],
            confidence="low",
            confidence_rationale="The request was not executed; there is no data to interpret.",
            source_lineage=lineage,
        )

    qr = pipe.result
    data_analyzed = {
        "plan_id": plan.plan_id,
        "entities": list(plan.body.entities),
        "row_count": qr.row_count,
        "columns": list(qr.columns),
    }
    lineage = dict(qr.source_lineage or {})
    lineage.setdefault("plan_id", plan.plan_id)
    lineage.setdefault("source_model_version", plan.source_model_version)

    observations = [o.to_dict() for o in derive_observations(qr, plan)]

    # Empty result: record honestly; do NOT call the model to interpret nothing.
    if qr.row_count == 0:
        return SkillResult(
            objective=objective, status="empty", data_analyzed=data_analyzed,
            observations=observations,
            limitations=["The query returned no rows; no findings can be drawn from an empty result."],
            confidence="low", confidence_rationale="Empty result set.",
            source_lineage=lineage,
        )

    # Interpretation over runtime observations (model never sees rows).
    interp, err = _interpret(objective, data_analyzed, observations, lineage, interpretation_model_fn)
    if err is not None:
        return SkillResult(
            objective=objective, status="interpretation_error", data_analyzed=data_analyzed,
            observations=observations,
            limitations=[f"The interpretation could not be parsed: {err}"],
            confidence=None, confidence_rationale=None, source_lineage=lineage,
        )

    # Runtime structural limitation always present for this slice.
    limitations = ["Synthetic data; results are illustrative, not authoritative."]
    limitations.extend(interp["limitations"])

    return SkillResult(
        objective=objective, status="ok", data_analyzed=data_analyzed,
        observations=observations,
        inferences=interp["inferences"],
        recommendations=interp["recommendations"],
        assumptions=interp["assumptions"],
        limitations=limitations,
        confidence=interp["confidence"],
        confidence_rationale=interp["confidence_rationale"],
        source_lineage=lineage,
    )


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


# Both seams default to the SAME wrapper today; kept as a distinct name so a
# later config can back interpretation with a different (e.g. local) model
# without touching call sites.
make_interpretation_model_fn = make_call_model_fn
