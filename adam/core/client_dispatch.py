"""
Generic provider-agnostic LLM dispatch with retry/backoff.

This module owns the single entry point for invoking any LLM in the
ADAM runtime: call_model(). It dispatches based on the endpoint_type
declared in models.json, looking up the SDK client lazily via
get_provider_client(). Retry policy is per-provider and read from
providers.json.

Architectural note on extension: the dispatcher supports two endpoint
shapes today -- openai_chat_completions and anthropic_messages.
Adding a third shape (e.g. google_gemini, aws_bedrock) requires
adding one branch to _call_model_once(). New OpenAI-compatible
providers (Together, Groq, Fireworks, OpenRouter, local llama.cpp
in OpenAI-API mode) work with config-only changes: pick
endpoint_type: "openai_chat_completions" in models.json.

This module is import-safe with no upward dependencies on
adam_agent_chat. ConfigError comes from adam.core.exceptions.
"""
from __future__ import annotations

import importlib
import os
import sys
import time
from typing import Any, Dict, List, Optional

from adam.core.exceptions import ConfigError


_client_cache: Dict[str, Any] = {}


def get_provider_client(provider_id: str, providers: Dict[str, Any]) -> Any:
    """Lazily instantiate (and cache) the SDK client for a provider."""
    if provider_id in _client_cache:
        return _client_cache[provider_id]
    p = providers[provider_id]
    try:
        sdk_module = importlib.import_module(p["sdk_module"])
    except ImportError as e:
        raise ConfigError(
            f"providers.json: '{provider_id}' requires SDK module '{p['sdk_module']}', "
            f"which is not installed. Run: pip install {p['sdk_module']}"
        ) from e
    sdk_class = getattr(sdk_module, p["sdk_class"], None)
    if sdk_class is None:
        raise ConfigError(
            f"providers.json: SDK module '{p['sdk_module']}' has no class '{p['sdk_class']}'"
        )
    api_key = os.environ[p["api_key_env"]].strip()
    kwargs: Dict[str, Any] = {"api_key": api_key}
    if p.get("base_url"):
        kwargs["base_url"] = p["base_url"]
    client = sdk_class(**kwargs)
    _client_cache[provider_id] = client
    return client


def _retry_after_seconds_from_exception(e: Exception) -> Optional[float]:
    """
    Try to extract a Retry-After hint from a provider exception. Both
    Anthropic and OpenAI SDKs expose response headers on rate-limit errors,
    but the access path differs slightly. Returns None if no hint is available.
    """
    # Most provider SDK exceptions carry a `response` attribute with headers
    response = getattr(e, "response", None)
    if response is not None:
        headers = getattr(response, "headers", None)
        if headers:
            for key in ("retry-after", "Retry-After", "retry-after-ms"):
                val = headers.get(key) if hasattr(headers, "get") else None
                if val is None:
                    continue
                try:
                    seconds = float(val)
                    if key == "retry-after-ms":
                        seconds = seconds / 1000.0
                    return max(0.0, seconds)
                except (ValueError, TypeError):
                    continue
    # Some SDKs expose a `.retry_after` attribute directly
    direct = getattr(e, "retry_after", None)
    if direct is not None:
        try:
            return max(0.0, float(direct))
        except (ValueError, TypeError):
            pass
    return None


def _is_retryable_error(e: Exception) -> bool:
    """
    True if the exception is one we should retry. We retry only on transient
    failures (rate limits, connection issues, timeouts). Other exceptions
    indicate bugs or config problems and must propagate so they get fixed
    rather than masked.
    """
    name = type(e).__name__
    # Provider SDK transient errors. Listed by name to avoid hard imports
    # on classes that may not exist in all SDK versions.
    transient_names = {
        "RateLimitError",
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "ServiceUnavailableError",
    }
    return name in transient_names


def call_model(
    model_id:      str,
    system_prompt: str,
    messages:      List[Dict[str, str]],
    max_tokens:    int,
    temperature:   float,
    models:        Dict[str, Any],
    providers:     Dict[str, Any],
) -> str:
    """
    Generic provider-agnostic call with retry/backoff on transient errors.

    Reads retry policy from providers.json (per-provider). Retries on
    RateLimitError, APIConnectionError, APITimeoutError, and a few related
    transient errors. Respects Retry-After header when present and the
    provider's retry config allows it. Other exceptions propagate.
    """
    m            = models[model_id]
    provider_id  = m["provider"]
    provider_cfg = providers[provider_id]
    retry_cfg    = provider_cfg["retry"]

    max_attempts        = retry_cfg["max_attempts"]
    initial_backoff     = retry_cfg["initial_backoff_seconds"]
    backoff_multiplier  = retry_cfg["backoff_multiplier"]
    max_backoff         = retry_cfg["max_backoff_seconds"]
    respect_retry_after = retry_cfg["respect_retry_after_header"]

    attempt = 0
    last_exception: Optional[Exception] = None

    while attempt < max_attempts:
        attempt += 1
        try:
            return _call_model_once(
                model_id=model_id,
                system_prompt=system_prompt,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                models=models,
                providers=providers,
            )
        except Exception as e:
            last_exception = e
            if not _is_retryable_error(e):
                raise

            if attempt >= max_attempts:
                # Out of attempts - propagate the last error
                sys.stderr.write(
                    f"[RETRY] {model_id} exhausted {max_attempts} attempts: "
                    f"{type(e).__name__}: {e}\n"
                )
                raise

            # Compute backoff: exponential with cap, optionally overridden
            # by the server's Retry-After hint
            backoff = min(
                initial_backoff * (backoff_multiplier ** (attempt - 1)),
                max_backoff,
            )
            if respect_retry_after:
                hint = _retry_after_seconds_from_exception(e)
                if hint is not None:
                    backoff = min(max(backoff, hint), max_backoff)

            sys.stderr.write(
                f"[RETRY] {model_id} attempt {attempt}/{max_attempts} failed "
                f"({type(e).__name__}); sleeping {backoff:.1f}s\n"
            )
            time.sleep(backoff)

    # Unreachable, but mypy/pylint will appreciate it
    assert last_exception is not None
    raise last_exception


def _call_model_once(
    model_id:      str,
    system_prompt: str,
    messages:      List[Dict[str, str]],
    max_tokens:    int,
    temperature:   float,
    models:        Dict[str, Any],
    providers:     Dict[str, Any],
) -> str:
    """
    Single-attempt provider dispatch. Wrapped by call_model() which handles
    retry/backoff. Reads endpoint_type from models.json and dispatches.
    """
    m = models[model_id]
    client = get_provider_client(m["provider"], providers)
    endpoint = m["endpoint_type"]

    if endpoint == "openai_chat_completions":
        # OpenAI: system goes in as the first message with role=system
        full_messages = [{"role": "system", "content": system_prompt}] + messages
        kwargs: Dict[str, Any] = {
            "model":      model_id,
            "messages":   full_messages,
            "max_tokens": max_tokens,
        }
        if m.get("supports_temperature", True):
            kwargs["temperature"] = temperature
        resp = client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content.strip()

    if endpoint == "anthropic_messages":
        # Anthropic: system is a top-level parameter
        kwargs = {
            "model":      model_id,
            "system":     system_prompt,
            "messages":   messages,
            "max_tokens": max_tokens,
        }
        if m.get("supports_temperature", True):
            kwargs["temperature"] = temperature
        resp = client.messages.create(**kwargs)
        return resp.content[0].text.strip()

    # Unreachable due to load-time validation
    raise ConfigError(f"Unknown endpoint_type '{endpoint}' for model '{model_id}'")

