"""
Config loading and validation for ADAM.

Owns three public functions:
  - load_and_validate_config()        -> (providers, models, agents) tuple
  - load_and_validate_runtime_config() -> the runtime.json cfg dict
  - _load_json(path, label)            -> shared JSON reader (raises ConfigError)

Plus the runtime config state:
  - _RUNTIME_CONFIG: Dict[str, Any]   -- module-global, populated at startup
  - _rt(*path)                         -- legacy dotted-path accessor

Behavior preserved exactly. The only structural change from the inline
version is that load_and_validate_runtime_config() mutates _RUNTIME_CONFIG
in place (clear + update) rather than rebinding the name. The rebinding
behavior would silently break callers that imported _RUNTIME_CONFIG by
reference -- mutate-in-place gives all importers a single consistent view.

The per-subsystem accessors (_rt_truthseeker in adam.verifier._config,
_rt_context in adam.context._config, _rt_skills in adam.skills_runtime._config)
remain separate from _rt(). Each subsystem owns its slice of config and
reads it via its own accessor. _rt() is the runtime's residual accessor
for the slices that haven't been moved into a subpackage yet
(session_defaults, operator_continuations, etc.).
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from adam.core.exceptions import ConfigError
from adam.verifier import TRUTHSEEKER_MODEL_ID


# ============================================================
# Paths & defaults
# ============================================================

CONFIG_DIR        = Path("config")
PROVIDERS_PATH    = CONFIG_DIR / "providers.json"
MODELS_PATH       = CONFIG_DIR / "models.json"
AGENTS_PATH       = CONFIG_DIR / "agents.json"
RUNTIME_PATH      = CONFIG_DIR / "runtime.json"


# ============================================================
# Runtime config state
# ============================================================

# Module-global dict, populated by load_and_validate_runtime_config().
# Other modules (adam_agent_chat, the per-subsystem _config modules)
# may import _RUNTIME_CONFIG directly; mutate-in-place semantics in
# the loader keep all importers consistent.
_RUNTIME_CONFIG: Dict[str, Any] = {}


def _rt(*path: str) -> Any:
    """
    Look up a value in the runtime config by dotted path.
    Example: _rt('session_defaults', 'max_turns') -> int

    The per-subsystem accessors (_rt_truthseeker, _rt_context,
    _rt_skills) are preferred for new code, but _rt() remains the
    legacy entry point for runtime-side slices that haven't moved
    into a subpackage.
    """
    node: Any = _RUNTIME_CONFIG
    for key in path:
        node = node[key]
    return node


def get_runtime_config() -> Dict[str, Any]:
    """
    Read-only view of the full runtime config dict. Returns the live
    dict (not a copy) -- callers should treat it as read-only after
    load_and_validate_runtime_config has run.
    """
    return _RUNTIME_CONFIG


# ============================================================
# Config loading & validation
# ============================================================


def _load_json(path: Path, label: str) -> Dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"{label} not found at {path}")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{label} at {path} is not valid JSON: {e}")


def load_and_validate_config() -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """
    Load providers.json, models.json, agents.json and validate cross-references.
    Returns (providers, models, agents). Raises ConfigError on any problem.
    """
    providers = _load_json(PROVIDERS_PATH, "providers.json")
    models    = _load_json(MODELS_PATH,    "models.json")
    agents    = _load_json(AGENTS_PATH,    "agents.json")

    # Validate provider entries
    for pid, p in providers.items():
        for required in ("api_key_env", "sdk_module", "sdk_class"):
            if required not in p:
                raise ConfigError(f"providers.json: '{pid}' is missing required field '{required}'")
        if not os.environ.get(p["api_key_env"], "").strip():
            raise ConfigError(
                f"providers.json: '{pid}' requires environment variable "
                f"'{p['api_key_env']}', but it is not set."
            )
        # Validate retry config if present (it's required for the call_model
        # retry wrapper; reject providers that omit it rather than picking
        # defaults silently, so behavior is explicit per-provider)
        if "retry" not in p:
            raise ConfigError(
                f"providers.json: '{pid}' is missing required 'retry' block. "
                f"See providers.json for an example."
            )
        r = p["retry"]
        for required in (
            "max_attempts", "initial_backoff_seconds", "backoff_multiplier",
            "max_backoff_seconds", "respect_retry_after_header",
        ):
            if required not in r:
                raise ConfigError(
                    f"providers.json: '{pid}' retry block is missing '{required}'"
                )
        if not isinstance(r["max_attempts"], int) or r["max_attempts"] < 1:
            raise ConfigError(f"providers.json: '{pid}' retry.max_attempts must be a positive integer")
        if not isinstance(r["initial_backoff_seconds"], (int, float)) or r["initial_backoff_seconds"] < 0:
            raise ConfigError(f"providers.json: '{pid}' retry.initial_backoff_seconds must be >= 0")
        if not isinstance(r["backoff_multiplier"], (int, float)) or r["backoff_multiplier"] < 1.0:
            raise ConfigError(f"providers.json: '{pid}' retry.backoff_multiplier must be >= 1.0")
        if not isinstance(r["max_backoff_seconds"], (int, float)) or r["max_backoff_seconds"] < 0:
            raise ConfigError(f"providers.json: '{pid}' retry.max_backoff_seconds must be >= 0")
        if not isinstance(r["respect_retry_after_header"], bool):
            raise ConfigError(f"providers.json: '{pid}' retry.respect_retry_after_header must be true/false")

    # Validate model entries
    valid_endpoint_types = {"openai_chat_completions", "anthropic_messages"}
    for mid, m in models.items():
        for required in ("provider", "endpoint_type"):
            if required not in m:
                raise ConfigError(f"models.json: '{mid}' is missing required field '{required}'")
        if m["provider"] not in providers:
            raise ConfigError(f"models.json: '{mid}' references unknown provider '{m['provider']}'")
        if m["endpoint_type"] not in valid_endpoint_types:
            raise ConfigError(
                f"models.json: '{mid}' has unknown endpoint_type '{m['endpoint_type']}'. "
                f"Valid types: {sorted(valid_endpoint_types)}"
            )

    # Validate agent entries
    valid_roles = {"advisory", "scheduled", "predicate-triggered", "service"}
    for aname, a in agents.items():
        for required in ("model_id", "prime_file", "role"):
            if required not in a:
                raise ConfigError(f"agents.json: '{aname}' is missing required field '{required}'")
        if a["model_id"] not in models:
            raise ConfigError(f"agents.json: '{aname}' references unknown model_id '{a['model_id']}'")
        if a["role"] not in valid_roles:
            raise ConfigError(
                f"agents.json: '{aname}' has unknown role '{a['role']}'. "
                f"Valid roles: {sorted(valid_roles)}"
            )
        prime_path = Path(a["prime_file"])
        if not prime_path.exists():
            raise ConfigError(f"agents.json: '{aname}' references prime_file '{prime_path}' which does not exist")
        if "temperature" in a:
            t = a["temperature"]
            if not isinstance(t, (int, float)) or not (0.0 <= t <= 2.0):
                raise ConfigError(f"agents.json: '{aname}' has invalid temperature {t!r} (must be 0.0-2.0)")
        if "max_tokens" in a:
            mt = a["max_tokens"]
            if not isinstance(mt, int) or mt <= 0:
                raise ConfigError(f"agents.json: '{aname}' has invalid max_tokens {mt!r} (must be positive int)")
        if "max_tokens_wrap_up" in a:
            mtw = a["max_tokens_wrap_up"]
            if not isinstance(mtw, int) or mtw <= 0:
                raise ConfigError(
                    f"agents.json: '{aname}' has invalid max_tokens_wrap_up {mtw!r} "
                    f"(must be positive int; this field is optional and used only "
                    f"when the agent is invoked during the wrap-up phase)"
                )
        if "max_tokens_artifact" in a:
            mta = a["max_tokens_artifact"]
            if not isinstance(mta, int) or mta <= 0:
                raise ConfigError(
                    f"agents.json: '{aname}' has invalid max_tokens_artifact {mta!r} "
                    f"(must be positive int; this field is optional and used only "
                    f"when the agent is invoked as a non-wrap-up Operator turn with "
                    f"executable artifact skills available, where the response will "
                    f"likely emit a skill_call carrying an artifact body)"
                )

    # Validate Truthseeker's hardcoded model exists
    if TRUTHSEEKER_MODEL_ID not in models:
        raise ConfigError(
            f"Truthseeker's internal model '{TRUTHSEEKER_MODEL_ID}' is not declared in models.json. "
            f"Add it there before running."
        )

    # Validate runtime dependencies. Without these, Truthseeker either cannot
    # do its job at all (requests) or can only judge search snippets, which
    # per policy can never reach VERIFIED (trafilatura). Previous behavior was
    # to silently degrade and spam mid-run errors per fetch -- now we fail
    # loudly at startup so the operator knows before any API credits are spent.
    missing_modules: List[Tuple[str, str]] = []
    try:
        import trafilatura  # noqa: F401
    except ImportError:
        missing_modules.append((
            "trafilatura",
            "Truthseeker page extraction (without it, snippet-only sources "
            "cannot satisfy the VERIFIED policy rules)"
        ))
    try:
        import requests  # noqa: F401
    except ImportError:
        missing_modules.append((
            "requests",
            "Truthseeker SearXNG search (without it, no claims can be verified "
            "and every verification turn will error out)"
        ))
    try:
        import docx  # noqa: F401   (provided by python-docx package)
    except ImportError:
        missing_modules.append((
            "python-docx",
            "Context Loader .docx text extraction (without it, .docx files "
            "passed via --context-dir/--context-file will soft-fail with a "
            "warning; .docx is a common format for K-12 governance documents)"
        ))
    try:
        import pypdf  # noqa: F401
    except ImportError:
        missing_modules.append((
            "pypdf",
            "Context Loader .pdf text extraction (without it, .pdf files "
            "passed via --context-dir/--context-file will soft-fail with a "
            "warning)"
        ))

    if missing_modules:
        lines = ["Required Python modules are not installed:"]
        for name, purpose in missing_modules:
            lines.append(f"  - {name}: needed for {purpose}")
        lines.append("")
        lines.append("Install all ADAM dependencies with:")
        lines.append("  pip install -r requirements.txt")
        lines.append("Or install just the missing ones:")
        lines.append(f"  pip install {' '.join(name for name, _ in missing_modules)}")
        raise ConfigError("\n".join(lines))

    return providers, models, agents


def load_and_validate_runtime_config() -> Dict[str, Any]:
    """
    Load runtime.json (operational tuning knobs) and validate. This config
    is module-level state: the loaded dict is assigned to _RUNTIME_CONFIG
    and accessed via the _rt() helper throughout the file.

    runtime.json holds settings that are LIKELY TO BE TUNED based on observed
    behavior -- timeouts, parallelism, claim caps, feature toggles. Things
    that should NEVER be tuned (model selection for Truthseeker, policy rules)
    stay hardcoded as architectural choices.
    """
    cfg = _load_json(RUNTIME_PATH, "runtime.json")

    # Truthseeker operational settings
    if "truthseeker" not in cfg:
        raise ConfigError("runtime.json: missing required top-level key 'truthseeker'")
    ts = cfg["truthseeker"]

    required_int_positive = (
        "search_top_n", "max_claims_per_turn", "parallel_workers",
        "source_excerpt_chars",
    )
    required_num_positive = (
        "page_fetch_timeout_seconds", "search_http_timeout_seconds",
    )
    required_bool = (
        "skip_tier_5_sources", "judgment_cache_enabled",
    )

    for key in required_int_positive:
        if key not in ts:
            raise ConfigError(f"runtime.json: truthseeker.{key} is required")
        if not isinstance(ts[key], int) or ts[key] < 1:
            raise ConfigError(f"runtime.json: truthseeker.{key} must be a positive integer")

    for key in required_num_positive:
        if key not in ts:
            raise ConfigError(f"runtime.json: truthseeker.{key} is required")
        if not isinstance(ts[key], (int, float)) or ts[key] <= 0:
            raise ConfigError(f"runtime.json: truthseeker.{key} must be > 0")

    for key in required_bool:
        if key not in ts:
            raise ConfigError(f"runtime.json: truthseeker.{key} is required")
        if not isinstance(ts[key], bool):
            raise ConfigError(f"runtime.json: truthseeker.{key} must be true or false")

    # Session defaults (override-able by CLI flags at startup).
    if "session_defaults" not in cfg:
        raise ConfigError(
            "runtime.json: missing required top-level key 'session_defaults' "
            "(this block was added as part of moving session-lifecycle tunables "
            "out of Python constants; see config/runtime.json for an example)."
        )
    sd = cfg["session_defaults"]

    session_required_int_positive = ("max_turns", "history_messages", "synth_cadence")
    for key in session_required_int_positive:
        if key not in sd:
            raise ConfigError(f"runtime.json: session_defaults.{key} is required")
        if not isinstance(sd[key], int) or sd[key] < 1:
            raise ConfigError(f"runtime.json: session_defaults.{key} must be a positive integer")

    if "delay_seconds" not in sd:
        raise ConfigError("runtime.json: session_defaults.delay_seconds is required")
    if not isinstance(sd["delay_seconds"], (int, float)) or sd["delay_seconds"] < 0:
        raise ConfigError("runtime.json: session_defaults.delay_seconds must be >= 0")

    if "seed_file" not in sd:
        raise ConfigError(
            "runtime.json: session_defaults.seed_file is required "
            "(default: 'prompts/seed.md')"
        )
    if not isinstance(sd["seed_file"], str) or not sd["seed_file"].strip():
        raise ConfigError("runtime.json: session_defaults.seed_file must be a non-empty string path")

    # Context Loader configuration (Pass 1: schema validated here; loader
    # itself is built in Pass 2). Block is required to be present so that
    # rolling out Pass 2 doesn't surprise operators who missed adding it.
    if "context" not in cfg:
        raise ConfigError(
            "runtime.json: missing required top-level key 'context' "
            "(controls Context Loader behavior; see config/runtime.json for "
            "an example with all required keys)"
        )
    ctx = cfg["context"]

    context_required_bool = (
        "enabled",
        "require_privacy_confirmation",
        "allow_yes_context_risk_flag",
        "allow_override_context_limit",
    )
    for key in context_required_bool:
        if key not in ctx:
            raise ConfigError(f"runtime.json: context.{key} is required")
        if not isinstance(ctx[key], bool):
            raise ConfigError(f"runtime.json: context.{key} must be true or false")

    context_required_int_positive = (
        "target_context_tokens",
        "soft_warning_tokens",
        "hard_refusal_tokens",
        "estimate_tokens_by_chars_divisor",
        "pdf_min_text_chars",
        "pdf_likely_scanned_size_bytes",
    )
    for key in context_required_int_positive:
        if key not in ctx:
            raise ConfigError(f"runtime.json: context.{key} is required")
        if not isinstance(ctx[key], int) or ctx[key] < 1:
            raise ConfigError(f"runtime.json: context.{key} must be a positive integer")

    # Token-tier ordering constraint -- prevents nonsense configurations like
    # soft_warning < target which would skip the WARNING tier entirely
    if not (ctx["target_context_tokens"]
            <= ctx["soft_warning_tokens"]
            <= ctx["hard_refusal_tokens"]):
        raise ConfigError(
            "runtime.json: context tier thresholds must satisfy "
            "target_context_tokens <= soft_warning_tokens <= hard_refusal_tokens "
            f"(got target={ctx['target_context_tokens']}, "
            f"soft={ctx['soft_warning_tokens']}, "
            f"hard={ctx['hard_refusal_tokens']})"
        )

    if "cache_dir" not in ctx:
        raise ConfigError("runtime.json: context.cache_dir is required")
    if not isinstance(ctx["cache_dir"], str) or not ctx["cache_dir"].strip():
        raise ConfigError("runtime.json: context.cache_dir must be a non-empty string path")

    # Skills block (Pass 1: runtime + discovery; Pass 2/3 add real skills)
    if "skills" not in cfg:
        raise ConfigError(
            "runtime.json: missing required top-level key 'skills' "
            "(controls SkillRuntime behavior; see config/runtime.json "
            "for an example with all required keys)"
        )
    sk = cfg["skills"]

    skills_required_bool = ("enabled", "auto_discover", "load_disabled_skills_metadata")
    for key in skills_required_bool:
        if key not in sk:
            raise ConfigError(f"runtime.json: skills.{key} is required")
        if not isinstance(sk[key], bool):
            raise ConfigError(f"runtime.json: skills.{key} must be true or false")

    if "skill_dir" not in sk:
        raise ConfigError("runtime.json: skills.skill_dir is required")
    if not isinstance(sk["skill_dir"], str) or not sk["skill_dir"].strip():
        raise ConfigError("runtime.json: skills.skill_dir must be a non-empty string path")

    for list_key in ("enabled_skills", "disabled_skills"):
        if list_key not in sk:
            raise ConfigError(f"runtime.json: skills.{list_key} is required (use [] for empty)")
        if not isinstance(sk[list_key], list):
            raise ConfigError(f"runtime.json: skills.{list_key} must be a list of strings")
        for v in sk[list_key]:
            if not isinstance(v, str) or not v.strip():
                raise ConfigError(f"runtime.json: skills.{list_key} entries must be non-empty strings")

    if "max_content_size_bytes" not in sk:
        raise ConfigError("runtime.json: skills.max_content_size_bytes is required")
    if not isinstance(sk["max_content_size_bytes"], int) or sk["max_content_size_bytes"] < 1024:
        raise ConfigError(
            "runtime.json: skills.max_content_size_bytes must be an integer >= 1024"
        )

    # Operator continuations: Operator may request additional turns AFTER
    # the wrap-up turn to complete multi-skill execution chains (e.g. create
    # document, then send email using the resulting artifact path). These
    # continuations are execution-only -- they do NOT reopen deliberation,
    # do NOT route to advisory agents, and do NOT bypass high-risk skill
    # safeguards. The block is optional in runtime.json; missing values
    # default to sensible defaults so existing configs don't break.
    if "operator_continuations" not in cfg:
        cfg["operator_continuations"] = {}
    oc = cfg["operator_continuations"]
    if not isinstance(oc, dict):
        raise ConfigError("runtime.json: operator_continuations must be an object")
    oc.setdefault("enabled", True)
    oc.setdefault("max_operator_continuations", 4)
    oc.setdefault("hard_cap", 10)
    if not isinstance(oc["enabled"], bool):
        raise ConfigError(
            "runtime.json: operator_continuations.enabled must be true or false"
        )
    if not isinstance(oc["max_operator_continuations"], int) or oc["max_operator_continuations"] < 0:
        raise ConfigError(
            "runtime.json: operator_continuations.max_operator_continuations must be "
            "a non-negative integer"
        )
    if not isinstance(oc["hard_cap"], int) or oc["hard_cap"] < 0 or oc["hard_cap"] > 50:
        raise ConfigError(
            "runtime.json: operator_continuations.hard_cap must be an integer "
            "between 0 and 50 (defensive ceiling)"
        )
    if oc["max_operator_continuations"] > oc["hard_cap"]:
        raise ConfigError(
            f"runtime.json: operator_continuations.max_operator_continuations "
            f"({oc['max_operator_continuations']}) exceeds hard_cap ({oc['hard_cap']}). "
            f"The hard_cap is a defensive ceiling that cannot be exceeded by "
            f"the configurable max."
        )

    # Commit to module-level state. We mutate in place rather than
    # rebinding the name -- this matters because other modules
    # (adam_agent_chat, adam.core helpers) import _RUNTIME_CONFIG and
    # would see a stale empty dict if we did `_RUNTIME_CONFIG = cfg`
    # (rebinding only affects this module's namespace, not the
    # imported references). clear() + update() preserves the dict
    # identity across the load.
    _RUNTIME_CONFIG.clear()
    _RUNTIME_CONFIG.update(cfg)
    return cfg
