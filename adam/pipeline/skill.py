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
import re
from dataclasses import dataclass, field, replace
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
        '"having": [{"field","op","value"}], '
        '"order_by": [{"field","direction"}], "limit": <int>}'
    )
    lines.append("")
    lines.append("Rules:")
    lines.append("  - operation MUST be 'select'.")
    lines.append("  - Reference ONLY the entities and fields listed above; do not invent any.")
    lines.append("  - 'projection' MUST be non-empty and MUST NEVER be ['*'].")
    lines.append(
        "  - Output ONLY the JSON body. No prose, no markdown, no SQL."
    )
    lines.append(
        "  - Do NOT include any envelope fields. Specifically, do NOT emit "
        "connection_handle, source_type, source_model_version, plan_version, "
        "intent_type, or purpose. Those are set by the runtime, not by you."
    )
    lines.append("")
    lines.append("Field references:")
    lines.append(
        "  - EVERY field reference uses the form entity.field (e.g. students.school_id)."
    )
    lines.append(
        "  - This applies to projection, filters[].field, joins[].left, joins[].right, "
        "group_by, aggregations[].field, and order_by[].field."
    )
    lines.append(
        "  - NEVER use a bare entity/table name where a field is expected (e.g. use "
        "students.id, not students). In 'joins' you join on a column, not a table."
    )
    lines.append(
        "  - EXCEPTION (aggregation aliases): order_by[].field and projection entries "
        "may be EITHER an entity.field reference OR an aggregation alias that exactly "
        "matches an aggregations[].as value. Aliases are NOT allowed in filters, joins, "
        "group_by, or aggregations[].field. Example: if aggregations has "
        '{"fn":"count","field":"students.id","as":"student_count"}, then order_by may '
        'use {"field":"student_count","direction":"desc"}.'
    )
    lines.append("")
    lines.append("Filtering on an AGGREGATE (HAVING):")
    lines.append(
        "  - To filter on an AGGREGATE (e.g. 'students with MORE THAN 5 absences', "
        "'schools with AT LEAST 100 students', 'fewer than N of X'), use 'having', "
        "NOT 'filters'. 'filters' apply to raw rows BEFORE grouping; 'having' "
        "applies to the aggregate AFTER grouping."
    )
    lines.append(
        "  - having[].field MUST be an aggregation alias declared in this plan's "
        "aggregations[].as (NOT an entity.field). So such an objective REQUIRES "
        "group_by + an aggregation (with an 'as' alias) + a having entry that "
        "references that alias, and usually order_by on the same alias."
    )
    lines.append(
        "  - When the objective says 'most/top/more than N/at least N/fewer than N "
        "of <thing>', aggregate that <thing> and use having/order_by on the "
        "aggregate alias — NEVER order by an unrelated column like id."
    )
    lines.append("")
    lines.append("Closed value sets (use EXACTLY these tokens, lowercase):")
    lines.append(
        "  - filters[].op: eq, ne, lt, lte, gt, gte, in, not_in, between, like, "
        "is_null, not_null"
    )
    lines.append("  - joins[].type: inner, left, right, full")
    lines.append("  - aggregations[].fn: count, sum, avg, min, max")
    lines.append("  - having[].op: eq, ne, lt, lte, gt, gte")
    lines.append("  - order_by[].direction: asc, desc")
    lines.append("")
    lines.append("Limit:")
    lines.append("  - limit is REQUIRED, a positive integer, and MUST be <= 1000.")
    lines.append("")
    lines.append("Correct example (shape only; use the real entities/fields listed above):")
    lines.append(
        '  {"operation":"select","entities":["students","schools"],'
        '"projection":["schools.name"],'
        '"joins":[{"left":"students.school_id","right":"schools.id","type":"inner"}],'
        '"group_by":["schools.name"],'
        '"aggregations":[{"fn":"count","field":"students.id","as":"student_count"}],'
        '"order_by":[{"field":"student_count","direction":"desc"}],"limit":10}'
    )
    lines.append(
        "Aggregate-threshold example ('students with more than 5 absences, ordered "
        "by absences' — group per student, count, then HAVING on the count alias):"
    )
    lines.append(
        '  {"operation":"select","entities":["students","attendance"],'
        '"projection":["students.id","students.name"],'
        '"joins":[{"left":"students.id","right":"attendance.student_id","type":"inner"}],'
        '"group_by":["students.id","students.name"],'
        '"aggregations":[{"fn":"count","field":"attendance.id","as":"total_absences"}],'
        '"having":[{"field":"total_absences","op":"gt","value":5}],'
        '"order_by":[{"field":"total_absences","direction":"desc"}],"limit":100}'
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Untrusted-output parsing
# ---------------------------------------------------------------------------

# A single leading+trailing markdown code fence wrapping the whole payload,
# e.g. ```json\n{...}\n``` . Models emit this despite instructions; strip it so
# a fenced-but-complete object is never missed (balanced-brace extraction below
# is the fallback for fences that don't wrap the whole string).
_CODE_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


def _strip_code_fences(text: str) -> str:
    """If the whole string is wrapped in one markdown code fence, return its
    inner content; otherwise return the text unchanged. Never raises."""
    if not isinstance(text, str):
        return text
    m = _CODE_FENCE_RE.match(text.strip())
    return m.group(1) if m else text


def _extract_json_objects(text: str) -> List[Dict[str, Any]]:
    """Extract all TOP-LEVEL balanced {...} objects that parse as JSON dicts,
    ignoring surrounding prose / markdown fences. A whole-string code fence is
    stripped first (explicit), then balanced-brace matching respects string
    literals so braces inside string values don't confuse it. Nested objects are
    part of their parent, not counted separately."""
    text = _strip_code_fences(text)
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

# Row/list intent: phrases that ask for INDIVIDUAL students (a roster), as
# opposed to aggregate/summary phrasing. Detected on the original objective; when
# no row phrase matches we stay aggregate-only (the safer default).
_ROW_INTENT_PHRASES = (
    "list", "roster", "which students", "who are the students", "show students",
    "show me the students", "names of", "name of each", "individual students",
    "each student", "students who", "students with",
)


def _is_row_intent(objective: str) -> bool:
    o = (objective or "").lower()
    return any(p in o for p in _ROW_INTENT_PHRASES)


def _governed_rows(objective, qr, plan, data_scope, model):
    """Governed ROW-LEVEL output for list/roster objectives. Returns a single
    {"label":"rows","value":{columns, rows}} observation, or None.

    Authority is the DataScope: rows are emitted ONLY when the profile permits
    student-level detail AND the objective asks for individual students. Output
    columns are mapped back to their SOURCE field via the PLAN (never the display
    name): a projection column is kept only if permits_output_field allows it; a
    computed aggregation alias is kept unless the alias itself is denied; an
    unmapped column is excluded. Rows are capped at max_rows_returned. These rows
    NEVER enter the interpretation prompt — they go only into the result."""
    if data_scope is None or not getattr(data_scope, "student_level_allowed", False):
        return None
    if not _is_row_intent(objective):
        return None
    if not isinstance(plan.body, QueryBody):
        return None
    body = plan.body
    alias_set = {a.as_ for a in body.aggregations}
    # Reconstruct the output-column order EXACTLY as the translator built SELECT:
    # projection items (excluding alias refs), then aggregation aliases.
    plan_cols = []
    for p in body.projection:
        if p in alias_set:
            continue
        plan_cols.append(("projection", p))
    for a in body.aggregations:
        plan_cols.append(("aggregation", a.as_))
    cols = list(qr.columns)
    if len(plan_cols) != len(cols):
        return None  # cannot safely map columns -> emit nothing (conservative)

    keep_idx, keep_names = [], []
    for i, (kind, ref) in enumerate(plan_cols):
        if kind == "aggregation":
            # A computed aggregate alias is allowed unless the alias is denied;
            # its underlying field already passed the Sentinel denylist.
            if ref not in data_scope.denied_fields:
                keep_idx.append(i)
                keep_names.append(cols[i])
        else:
            resolved = model.resolve(ref, body.entities) if model is not None else None
            if resolved is None:
                continue  # unmapped projection ref -> exclude
            entity, fieldname = resolved
            if data_scope.permits_output_field(entity, fieldname):
                keep_idx.append(i)
                keep_names.append(cols[i])
    if not keep_idx:
        return None

    cap = getattr(data_scope, "max_rows_returned", None)
    rows_src = qr.rows[:cap] if isinstance(cap, int) and cap > 0 else list(qr.rows)
    out_rows = [[row[i] for i in keep_idx] for row in rows_src]
    return {
        "label": "rows",
        "value": {"columns": keep_names, "rows": out_rows},
        "detail": f"{len(out_rows)} row(s); governed student-level output "
                  f"(permitted columns only, capped at max_rows_returned).",
    }


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


def _log_interp_parse_failure(raw: Any) -> None:
    """Surface WHY interpretation didn't parse: log the raw output length and
    its head/tail so the operator can tell 'truncated' (ends without a closing
    brace → budget too small) from 'prose / no JSON'. No fabrication, no silent
    opacity. The interpretation output carries no credentials (observations +
    model judgment only)."""
    import logging
    s = raw if isinstance(raw, str) else repr(raw)
    logging.getLogger("adam.data_sources").error(
        "interpretation output unparseable: len=%d head=%r tail=%r",
        len(s), s[:200], s[-200:],
    )


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
        _log_interp_parse_failure(raw)
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
    max_rows: Optional[int] = None,
    data_scope: Any = None,
) -> SkillResult:
    """Objective → governed plan → execute → runtime observations → model
    interpretation → assembled SkillResult. Honest on denial/empty/failure:
    no fabricated observations or inferences when there's nothing (or nothing
    permitted) to interpret.

    ``max_rows`` (agent path) CLAMPS the plan's effective limit to
    min(plan.limit, max_rows) before execution — a budget cap, not a rejection."""
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

    # Clamp the effective row limit DOWN to the caller's budget (never up). The
    # plan is frozen, so rebuild it (and its body) with the clamped limit.
    if (max_rows is not None and isinstance(plan.body, QueryBody)
            and plan.body.limit and plan.body.limit > max_rows):
        plan = replace(plan, body=replace(plan.body, limit=max_rows))

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

    # Governed ROW-LEVEL output (list/roster objectives, profile-permitted only).
    # ADDITIVE: keep the aggregate observations; append a scoped `rows` entry to
    # the RESULT only — it was NOT in the observations the model interpreted, so
    # the "model never sees raw rows" privacy rule stays intact.
    result_observations = observations
    rows_obs = _governed_rows(objective, qr, plan, data_scope, source_model)
    if rows_obs is not None:
        result_observations = observations + [rows_obs]

    return SkillResult(
        objective=objective, status="ok", data_analyzed=data_analyzed,
        observations=result_observations,
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
