"""
Verifier-private runtime configuration.

This is an internal module of adam.verifier. The package __init__.py
re-exports the public surface (TRUTHSEEKER_MODEL_ID, set_runtime_config,
etc.) for external callers, but verifier-internal modules import from
here directly to avoid circular imports through the package's __init__.

Single-instance assumption: see step 7 (SessionContext migration) for
when this needs to move to per-session state.
"""
from __future__ import annotations

from typing import Any, Dict


# Truthseeker-owned constants. Properties of the verifier subsystem,
# not of the broader runtime.
TRUTHSEEKER_MODEL_ID    = "claude-haiku-4-5-20251001"
TRUTHSEEKER_TEMPERATURE = 0.1


# Verifier-private runtime config. Set once at session startup.
_runtime_config: Dict[str, Any] = {}


def set_runtime_config(cfg: Dict[str, Any]) -> None:
    """
    Register the truthseeker config block. Called once during session
    startup with the 'truthseeker' subtree of runtime.json.
    """
    global _runtime_config
    _runtime_config = dict(cfg) if cfg else {}


def _rt_truthseeker(key: str) -> Any:
    """
    Look up a required tunable in the truthseeker config block.
    Raises KeyError if not present.
    """
    return _runtime_config[key]


def _rt_truthseeker_get(key: str, default: Any = None) -> Any:
    """Soft variant that returns a default."""
    return _runtime_config.get(key, default)
