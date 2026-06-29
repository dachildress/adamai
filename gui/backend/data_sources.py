"""
Data Sources — web-layer orchestration over the governed execution pipeline.

This module is a THIN orchestration surface. It does NOT reimplement
validation, Sentinel, adapters, SkillResult, or the ingestion lifecycle —
those live in `adam/pipeline/` and are called as-is. It only:

  * resolves the ONE canonical IngestionStore (single path, process lock),
  * tests/introspects a MySQL source for the admin (coarse results; secrets
    never echoed/persisted/logged),
  * exposes the read-only governance default + scope-from-model seam,
  * exposes the model-fns and connection-resolution seams (injectable; live
    query returns MODEL_NOT_CONFIGURED / CONNECTION_NOT_CONFIGURED until a
    safe in-process seam / secret store is wired),
  * runs a governed query via the existing `analyze_objective` skill flow.

Security: a password may arrive in an admin request body and be used to
build a connect_fn, but it is NEVER echoed, persisted, or put in an error.
Connection ops return coarse states only — never raw driver text.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

# The GUI process is historically import-isolated from ADAM. This module is the
# first GUI->pipeline bridge, so ensure the repo root (which holds the `adam`
# package) is importable regardless of how the server was launched.
_REPO_ROOT = Path(__file__).resolve().parents[2]   # .../opt/adam
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from adam.pipeline import (  # noqa: E402
    GovernanceConfig,
    IngestionStore,
    MySQLAdapter,
    MySQLIntrospector,
    ScopeConfig,
    SkillResult,
    analyze_objective,
    get_source_model,
    make_pymysql_connect_fn,
)

# Coarse connection-test states (never raw driver diagnostics).
OK = "ok"
CONNECTION_FAILED = "connection_failed"
AUTHENTICATION_FAILED = "authentication_failed"
NO_TABLES_FOUND = "no_tables_found"

# Query-path outcome codes (config states, not 500s).
MODEL_NOT_CONFIGURED = "MODEL_NOT_CONFIGURED"
CONNECTION_NOT_CONFIGURED = "CONNECTION_NOT_CONFIGURED"

# MySQL access-denied error codes — classify auth vs. unreachable WITHOUT
# importing the driver or surfacing its message.
_AUTH_ERROR_CODES = {1044, 1045, 1698, 1396}

# Seam-injection keys on app.state (so tests can supply fakes). Defaults below.
# Type aliases match the pipeline's PlanningModelFn / InterpretationModelFn.
ModelFns = Tuple[Callable[[str, str], str], Callable[[str, str], str]]
ModelFnsProvider = Callable[[], Optional[ModelFns]]
ConnectionResolver = Callable[[str], Optional[Callable[[], Any]]]


# ---------------------------------------------------------------------------
# Canonical ingestion store (single path + process-local lock)
# ---------------------------------------------------------------------------

_STORE_PATH: Optional[Path] = None
_STORE_LOCK = threading.Lock()


def init_data_sources(base_dir) -> None:
    """Set the ONE canonical store path (mirrors auth.init_auth). Called from
    build_app; tests call it with a temp dir."""
    global _STORE_PATH
    _STORE_PATH = Path(base_dir) / "pipeline_data" / "source_models.json"


def get_pipeline_ingestion_store() -> IngestionStore:
    """Construct the canonical IngestionStore. Constructing it RELOADS the
    persisted candidates/ratified records from disk and re-registers ratified
    SourceModels into the process registry — so this is also the 'reload
    before query' discipline. Every route obtains its store here."""
    if _STORE_PATH is None:
        raise RuntimeError("data sources not initialized (call init_data_sources)")
    return IngestionStore(_STORE_PATH)


def store_lock() -> threading.Lock:
    """Process-local lock guarding submit/approve/reject so concurrent admin
    actions can't clobber state between load and save."""
    return _STORE_LOCK


# ---------------------------------------------------------------------------
# Credential-safe MySQL connection test + introspection (admin only)
# ---------------------------------------------------------------------------

def _classify_connect_error(exc: Exception) -> str:
    code = exc.args[0] if getattr(exc, "args", None) and isinstance(exc.args[0], int) else None
    return AUTHENTICATION_FAILED if code in _AUTH_ERROR_CODES else CONNECTION_FAILED


def test_mysql_connection(*, host: str, port: Any, user: str, password: str,
                          database: str, connect_factory=make_pymysql_connect_fn) -> Dict[str, Any]:
    """Open a connection and count BASE tables. Returns a COARSE status only;
    the password is never echoed and no driver text is surfaced.

    `connect_factory(**kwargs) -> connect_fn` defaults to the real PyMySQL
    factory; tests inject a fake via app.state so no live server is needed."""
    connect_fn = connect_factory(
        host=host, port=int(port or 3306), user=user, password=password, database=database,
    )
    try:
        conn = connect_fn()
    except Exception as e:  # scrub: classify by code, never include the message
        return {"status": _classify_connect_error(e), "ok": False, "table_count": 0}
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_type = 'BASE TABLE' AND table_schema = %s",
            (database,),
        )
        count = int(cur.fetchone()[0])
    except Exception:
        return {"status": CONNECTION_FAILED, "ok": False, "table_count": 0}
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if count == 0:
        # Honest: connection works but minting from an empty schema is
        # meaningless; the UI refuses to proceed.
        return {"status": NO_TABLES_FOUND, "ok": True, "table_count": 0}
    return {"status": OK, "ok": True, "table_count": count}


def introspect_mysql_source(*, host: str, port: Any, user: str, password: str,
                            database: str, source_name: str,
                            connect_factory=make_pymysql_connect_fn):
    """Introspect a real MySQL schema and create a PENDING candidate (does NOT
    ratify). The real MySQLIntrospector is passed EXPLICITLY so submit()'s
    synthetic default can never mint a model under a real source name."""
    connect_fn = connect_factory(
        host=host, port=int(port or 3306), user=user, password=password, database=database,
    )
    introspector = MySQLIntrospector(connect_fn=connect_fn)
    with _STORE_LOCK:
        store = get_pipeline_ingestion_store()
        candidate = store.submit(source_name, introspect_fn=introspector)
    return candidate


# ---------------------------------------------------------------------------
# Governance seam (read-only default; profile->scope bridge deferred)
# ---------------------------------------------------------------------------

def pipeline_governance_for(user: Dict[str, Any], source_model) -> Tuple[GovernanceConfig, ScopeConfig]:
    """SEAM (intentional limitation): the GUI's agent-spawn governance
    profiles are NOT yet mapped to the pipeline's GovernanceConfig/ScopeConfig.
    For now every web query runs READ-ONLY with the ratified source model as
    its own allowlist (entities/fields). The mapping from a web user's profile
    to pipeline scope is a separate, deliberate design step that will live
    here."""
    governance = GovernanceConfig(read_only=True)
    scope = ScopeConfig(
        allowed_entities=set(source_model.entities.keys()),
        denied_entities=set(),
        denied_fields=set(),
    )
    return governance, scope


# ---------------------------------------------------------------------------
# Injectable seams (defaults; overridden on app.state in tests)
# ---------------------------------------------------------------------------

# Model selection / call tuning — env-configurable, never hardcoded model
# literals. Default model id derives from an existing agent's model_id.
_DEFAULT_AGENT = "Operator"   # ADAM's structured-output agent; closest analog to
                              # emitting a structured query body.


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _resolve_model_id(models: Dict[str, Any], agents: Dict[str, Any]) -> Optional[str]:
    """Model id for data-intelligence queries, in priority order:
      1. $ADAM_DATA_INTELLIGENCE_MODEL_ID (explicit override);
      2. else the model_id of an existing agent ($ADAM_DATA_INTELLIGENCE_AGENT,
         default 'Operator') read from agents.json — a config value, not a
         hardcoded model string.
    Returns None if unresolved or not present in models."""
    mid = os.environ.get("ADAM_DATA_INTELLIGENCE_MODEL_ID", "").strip()
    if mid:
        return mid if mid in models else None
    agent_name = os.environ.get("ADAM_DATA_INTELLIGENCE_AGENT", "").strip() or _DEFAULT_AGENT
    agent = agents.get(agent_name) or {}
    candidate = agent.get("model_id")
    return candidate if candidate in models else None


def default_model_fns_provider() -> Optional[ModelFns]:
    """Real in-process model seam for the web query path.

    Returns (planning_fn, interpretation_fn), thin wrappers over ADAM's shared
    `client_dispatch.call_model` (same machinery the agent loop uses; provider/
    key/retry come from config + env — configurable, not hardcoded). Returns
    None -> the query path returns MODEL_NOT_CONFIGURED (never a 500) when a
    usable model can't be resolved.

    Usability depends ONLY on the SELECTED model's provider key — never on
    unrelated providers. We load the raw config via config_loader's shared JSON
    reader (NOT load_and_validate_config, which validates EVERY provider's
    api_key_env and would raise ConfigError when, e.g., only ANTHROPIC_API_KEY
    is set — turning a usable Anthropic setup into a false MODEL_NOT_CONFIGURED).
    """
    from adam.core import client_dispatch, config_loader  # lazy: keep GUI import light
    try:
        providers = config_loader._load_json(config_loader.PROVIDERS_PATH, "providers.json")
        models = config_loader._load_json(config_loader.MODELS_PATH, "models.json")
        agents = config_loader._load_json(config_loader.AGENTS_PATH, "agents.json")
    except Exception:
        return None

    model_id = _resolve_model_id(models, agents)
    if not model_id or model_id not in models:
        return None
    provider_id = models[model_id].get("provider")
    if not provider_id or provider_id not in providers:
        return None
    api_key_env = providers[provider_id].get("api_key_env")
    if not api_key_env or not os.environ.get(api_key_env, "").strip():
        return None

    max_tokens = _env_int("ADAM_DATA_INTELLIGENCE_MAX_TOKENS", 1024)
    temperature = _env_float("ADAM_DATA_INTELLIGENCE_TEMPERATURE", 0.0)  # low: planning must emit parseable JSON

    def planning_fn(system_prompt: str, objective: str) -> str:
        # Returns the raw model string unchanged; the pipeline's parse_body
        # handles it. No post-processing, no swallowing of model errors.
        return client_dispatch.call_model(
            model_id=model_id, system_prompt=system_prompt,
            messages=[{"role": "user", "content": objective}],
            max_tokens=max_tokens, temperature=temperature,
            models=models, providers=providers,
        )

    def interpretation_fn(system_prompt: str, observations: str) -> str:
        return client_dispatch.call_model(
            model_id=model_id, system_prompt=system_prompt,
            messages=[{"role": "user", "content": observations}],
            max_tokens=max_tokens, temperature=temperature,
            models=models, providers=providers,
        )

    return (planning_fn, interpretation_fn)


def default_resolve_connection(handle: str) -> Optional[Callable[[], Any]]:
    """Resolve a named read-only connection handle to a connect_fn. Secret
    storage is not built yet, so this returns None (live query →
    CONNECTION_NOT_CONFIGURED). Tests inject a fake connect_fn via app.state.
    The user query path NEVER accepts credentials — they resolve here."""
    return None


# ---------------------------------------------------------------------------
# Serialization + governed query
# ---------------------------------------------------------------------------

def serialize_skill_result(r: SkillResult) -> Dict[str, Any]:
    """Typed SkillResult → JSON, preserving the fact/judgment separation:
    runtime observations vs. model inferences/recommendations/etc."""
    return {
        "objective": r.objective,
        "status": r.status,                      # ok | empty | policy_denied | validation_error | ...
        "data_analyzed": r.data_analyzed,
        "observations": r.observations,          # RUNTIME facts
        "inferences": r.inferences,              # MODEL judgment
        "recommendations": r.recommendations,    # MODEL judgment
        "assumptions": r.assumptions,            # MODEL judgment
        "limitations": r.limitations,
        "confidence": r.confidence,              # MODEL self-report
        "confidence_rationale": r.confidence_rationale,
        "source_lineage": r.source_lineage,
    }


def run_governed_query(
    *,
    version: str,
    objective: str,
    user: Dict[str, Any],
    model_fns: Optional[ModelFns],
    resolve_connection: ConnectionResolver,
) -> Dict[str, Any]:
    """Reload the canonical store (re-registers ratified models), resolve the
    source model + read-only connection + model seams, then run the EXISTING
    analyze_objective skill flow. Returns a discriminated dict — never raises
    for a config/blocked outcome (the caller maps unknown-version to 404)."""
    # Reload-before-query: constructing the canonical store re-registers
    # ratified models from disk into the process registry.
    get_pipeline_ingestion_store()

    model = get_source_model(version)
    if model is None:
        return {"error": "UNKNOWN_VERSION"}

    if model_fns is None:
        return {"error": MODEL_NOT_CONFIGURED}
    planning_fn, interpretation_fn = model_fns

    connect_fn = resolve_connection(version)   # handle == ratified version for now
    if connect_fn is None:
        return {"error": CONNECTION_NOT_CONFIGURED}

    # The MySQL adapter performs physical planning/execution; the user never
    # supplied credentials — they were resolved from the named handle.
    adapter = MySQLAdapter(model, connect_fn=connect_fn)
    governance, scope = pipeline_governance_for(user, model)

    try:
        result: SkillResult = analyze_objective(
            objective,
            connection=None,
            source_model=model,
            planning_model_fn=planning_fn,
            interpretation_model_fn=interpretation_fn,
            connection_handle=version,
            adapter=adapter,
            governance=governance,
            scope=scope,
        )
    except Exception:
        # A genuine model/execution failure (e.g. a provider error after
        # retries) propagated from the seam. Surface a CLEAN, credential-free
        # outcome — never a 500, never a stack trace / API key to the browser,
        # and never a fabricated empty plan. (Validation/Sentinel blocks are
        # NOT exceptions — they come back as SkillResult.status below.)
        return {"error": "QUERY_FAILED"}
    return {"result": serialize_skill_result(result)}
