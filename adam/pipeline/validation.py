"""
Validation — structural, deterministic, runs BEFORE Sentinel.

Returns a structured ``ValidationOutcome`` (never raises for a rejected
plan). Categories, per interface §9:

  * ``VALIDATION_ERROR``    — plan malformed (envelope, intent/operation,
                              projection/limit, credential-like content,
                              closed-enum violations).
  * ``SOURCE_MODEL_ERROR``  — entity/field does not resolve in the ratified
                              source model, or the model version is unknown.
  * ``CAPABILITY_ERROR``    — plan needs an operation the adapter does not
                              advertise (e.g. joins without supports_join).

Scope of this slice: only ``intent_type="query"`` / ``operation="select"``.
``mutation`` and ``raw_statement`` are rejected as out of scope here (the
real pipeline would route them differently; this slice does not implement
them).

Check order is fixed so a given malformed plan yields a stable category:
envelope → intent → operation → credentials → entities-present →
projection → limit → closed enums → model-known → entity-resolve →
field-resolve → capabilities.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

from .adapter_capabilities import AdapterCapabilities
from .execution_plan import ExecutionPlan, QueryBody
from .source_model import SourceModel, get_source_model

# Categories
VALIDATION_ERROR = "VALIDATION_ERROR"
SOURCE_MODEL_ERROR = "SOURCE_MODEL_ERROR"
CAPABILITY_ERROR = "CAPABILITY_ERROR"

# Closed enum sets (interface §4).
FILTER_OPS = {"eq", "ne", "lt", "lte", "gt", "gte", "in", "not_in",
              "between", "like", "is_null", "not_null"}
JOIN_TYPES = {"inner", "left", "right", "full"}
AGG_FNS = {"count", "sum", "avg", "min", "max"}
ORDER_DIRECTIONS = {"asc", "desc"}
ROW_SCOPES = {"single", "small", "large"}

# intent types that exist in the contract but are out of scope for this slice.
_OUT_OF_SCOPE_INTENTS = {"mutation", "raw_statement"}


@dataclass(frozen=True)
class ValidationConfig:
    max_limit: int = 1000
    # Whether the instance permits raw_statement. Out of scope this slice;
    # kept so the rejection message is honest ("not permitted / out of scope").
    allow_raw_statement: bool = False


@dataclass(frozen=True)
class ValidationOutcome:
    ok: bool
    category: Optional[str] = None
    detail: Optional[str] = None


def _ok() -> ValidationOutcome:
    return ValidationOutcome(ok=True)


def _err(category: str, detail: str) -> ValidationOutcome:
    return ValidationOutcome(ok=False, category=category, detail=detail)


# ---------------------------------------------------------------------------
# Credential-like detection
# ---------------------------------------------------------------------------
#
# A small, explicit, documented pattern set — NOT a clever heuristic. Each
# pattern targets a concrete credential shape. We scan every string value in
# the plan EXCEPT `connection_handle` (which is an opaque ID and may legibly
# look like `conn_powerschool_ro`). Patterns require the tell-tale `=`/`:`
# delimiter so ordinary prose ("password reset policy") does not trip them.
#
#   embedded_uri_credentials : scheme://user:pass@host  (connection string)
#   password_kv              : password=...
#   pwd_kv                   : pwd=...
#   authorization_header     : Authorization: ...
#   bearer_token             : Bearer <token>
#   private_key_block        : -----BEGIN ... PRIVATE KEY-----
#   api_key_kv               : api_key= / apikey= / api-key=
#   secret_kv                : secret=
#   access_token_kv          : access_token= / access-token=
_CREDENTIAL_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("embedded_uri_credentials", re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^/\s:@]+:[^/\s@]+@")),
    ("password_kv",              re.compile(r"password\s*=", re.IGNORECASE)),
    ("pwd_kv",                   re.compile(r"\bpwd\s*=", re.IGNORECASE)),
    ("authorization_header",     re.compile(r"authorization\s*:", re.IGNORECASE)),
    ("bearer_token",             re.compile(r"\bBearer\s+\S")),
    ("private_key_block",        re.compile(r"BEGIN[A-Z ]*PRIVATE KEY", re.IGNORECASE)),
    ("api_key_kv",               re.compile(r"\bapi[_\-]?key\s*=", re.IGNORECASE)),
    ("secret_kv",                re.compile(r"\bsecret\s*=", re.IGNORECASE)),
    ("access_token_kv",          re.compile(r"\baccess[_\-]?token\s*=", re.IGNORECASE)),
]


def _iter_plan_strings(plan: ExecutionPlan):
    """Yield (location, string) for every string value in the plan EXCEPT
    connection_handle. Non-string values (numbers, None) are skipped."""
    # Envelope strings (connection_handle deliberately excluded).
    for loc, val in (
        ("plan_version", plan.plan_version),
        ("intent_type", plan.intent_type),
        ("source_type", plan.source_type),
        ("source_model_version", plan.source_model_version),
        ("purpose", plan.purpose),
        ("estimated_row_scope", plan.estimated_row_scope),
    ):
        if isinstance(val, str):
            yield loc, val

    body = plan.body
    if not isinstance(body, QueryBody):
        return
    if isinstance(body.operation, str):
        yield "body.operation", body.operation
    for e in body.entities:
        if isinstance(e, str):
            yield "body.entities", e
    for p in body.projection:
        if isinstance(p, str):
            yield "body.projection", p
    for f in body.filters:
        if isinstance(f.field, str):
            yield "filter.field", f.field
        if isinstance(f.value, str):
            yield "filter.value", f.value
        elif isinstance(f.value, (list, tuple)):
            for v in f.value:
                if isinstance(v, str):
                    yield "filter.value", v
    for j in body.joins:
        for v in (j.left, j.right, j.type):
            if isinstance(v, str):
                yield "join", v
    for g in body.group_by:
        if isinstance(g, str):
            yield "group_by", g
    for a in body.aggregations:
        for v in (a.fn, a.field, a.as_):
            if isinstance(v, str):
                yield "aggregation", v
    for o in body.order_by:
        for v in (o.field, o.direction):
            if isinstance(v, str):
                yield "order_by", v


def scan_for_credentials(plan: ExecutionPlan) -> Optional[Tuple[str, str]]:
    """Return (location, pattern_name) for the first credential-like string
    found, or None. connection_handle is never scanned."""
    for loc, s in _iter_plan_strings(plan):
        for name, rx in _CREDENTIAL_PATTERNS:
            if rx.search(s):
                return (loc, name)
    return None


# ---------------------------------------------------------------------------
# Field-reference resolution
# ---------------------------------------------------------------------------

def _all_field_refs(body: QueryBody):
    """Yield (location, ref, alias_allowed) for every field reference that
    must resolve. alias_allowed=True where an aggregation output alias is a
    legal reference (projection, order_by)."""
    for p in body.projection:
        yield ("projection", p, True)
    for f in body.filters:
        yield ("filter.field", f.field, False)
    for j in body.joins:
        yield ("join.left", j.left, False)
        yield ("join.right", j.right, False)
    for g in body.group_by:
        yield ("group_by", g, False)
    for a in body.aggregations:
        yield ("aggregation.field", a.field, False)
    for o in body.order_by:
        yield ("order_by", o.field, True)


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------

def validate(
    plan: ExecutionPlan,
    capabilities: AdapterCapabilities,
    config: ValidationConfig = ValidationConfig(),
    model_resolver: Callable[[str], Optional[SourceModel]] = get_source_model,
) -> ValidationOutcome:
    # 1. Envelope completeness + recognized plan_version.
    if plan.plan_version != "1.0":
        return _err(VALIDATION_ERROR, f"unrecognized plan_version: {plan.plan_version!r}")
    for fname in ("intent_type", "connection_handle", "source_type",
                  "source_model_version", "purpose", "estimated_row_scope"):
        val = getattr(plan, fname, None)
        if not isinstance(val, str) or not val:
            return _err(VALIDATION_ERROR, f"missing or empty envelope field: {fname}")
    if plan.estimated_row_scope not in ROW_SCOPES:
        return _err(VALIDATION_ERROR,
                    f"estimated_row_scope must be one of {sorted(ROW_SCOPES)}")

    # 2. intent_type — closed set; only `query` is in scope this slice.
    if plan.intent_type in _OUT_OF_SCOPE_INTENTS:
        return _err(VALIDATION_ERROR,
                    f"intent_type {plan.intent_type!r} is out of scope for this slice "
                    f"(only 'query' is implemented)")
    if plan.intent_type != "query":
        return _err(VALIDATION_ERROR, f"unknown intent_type: {plan.intent_type!r}")

    # body must be a structured QueryBody.
    body = plan.body
    if not isinstance(body, QueryBody):
        return _err(VALIDATION_ERROR, "missing or malformed body")

    # 3. operation — query supports select only.
    if body.operation != "select":
        return _err(VALIDATION_ERROR,
                    f"operation {body.operation!r} not allowed; query supports 'select' only")

    # 4. No credential-like content anywhere except connection_handle.
    hit = scan_for_credentials(plan)
    if hit is not None:
        loc, name = hit
        return _err(VALIDATION_ERROR,
                    f"credential-like content detected ({name}) in {loc}")

    # 5. entities present.
    if not body.entities:
        return _err(VALIDATION_ERROR, "body.entities must be non-empty")

    # 6. projection present and not implicit select-all.
    if not body.projection:
        return _err(VALIDATION_ERROR, "projection must be non-empty (no implicit select-all)")
    if list(body.projection) == ["*"]:
        return _err(VALIDATION_ERROR, "projection ['*'] is not allowed (no implicit select-all)")

    # 7. limit present and within bounds.
    if body.limit is None:
        return _err(VALIDATION_ERROR, "limit is required")
    if not isinstance(body.limit, int) or isinstance(body.limit, bool) or body.limit <= 0:
        return _err(VALIDATION_ERROR, "limit must be a positive integer")
    if body.limit > config.max_limit:
        return _err(VALIDATION_ERROR,
                    f"limit {body.limit} exceeds configured max {config.max_limit}")

    # 8. Closed-enum checks for body elements.
    for f in body.filters:
        if f.op not in FILTER_OPS:
            return _err(VALIDATION_ERROR, f"unknown filter op: {f.op!r}")
    for j in body.joins:
        if j.type not in JOIN_TYPES:
            return _err(VALIDATION_ERROR, f"unknown join type: {j.type!r}")
    for a in body.aggregations:
        # Case-insensitive backstop: the canonical plan is already lowercased at
        # parse time, but compare lowercased so a directly-constructed plan with
        # an uppercase fn (e.g. "COUNT") is still accepted. The allowed SET is
        # unchanged — only case variants of the existing five are tolerated.
        if a.fn.lower() not in AGG_FNS:
            return _err(VALIDATION_ERROR, f"unknown aggregation fn: {a.fn!r}")
    for o in body.order_by:
        if o.direction not in ORDER_DIRECTIONS:
            return _err(VALIDATION_ERROR, f"unknown order direction: {o.direction!r}")

    # 9. Source model known.
    model = model_resolver(plan.source_model_version)
    if model is None:
        return _err(SOURCE_MODEL_ERROR,
                    f"unknown/unratified source_model_version: {plan.source_model_version!r}")

    # 10. Entities resolve in the model.
    for e in body.entities:
        if not model.has_entity(e):
            return _err(SOURCE_MODEL_ERROR, f"unresolved entity: {e!r}")

    # 11. Field references resolve (aggregation aliases allowed where noted).
    aliases = {a.as_ for a in body.aggregations if isinstance(a.as_, str)}
    for loc, ref, alias_allowed in _all_field_refs(body):
        if alias_allowed and ref in aliases:
            continue
        if model.resolve(ref, body.entities) is None:
            return _err(SOURCE_MODEL_ERROR, f"unresolved field reference in {loc}: {ref!r}")

    # 12. Capability gating (statically detectable).
    if body.joins and not capabilities.supports_join:
        return _err(CAPABILITY_ERROR, "joins require adapter capability supports_join")
    if body.group_by and not capabilities.supports_grouping:
        return _err(CAPABILITY_ERROR, "group_by requires adapter capability supports_grouping")
    if body.aggregations and not capabilities.supports_aggregation:
        return _err(CAPABILITY_ERROR, "aggregations require adapter capability supports_aggregation")
    if body.order_by and not capabilities.supports_ordering:
        return _err(CAPABILITY_ERROR, "order_by requires adapter capability supports_ordering")

    return _ok()
