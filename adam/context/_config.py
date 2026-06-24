"""
Context-subsystem-private runtime configuration.

Internal module of adam.context. Submodules import from here directly
to avoid circular imports through the package's __init__.

Mirrors the pattern established by adam.verifier._config:
  - set_runtime_config(cfg) registers the 'context' block of runtime.json
  - _rt_context(key) reads a tunable; KeyError if missing
  - _rt_context_get(key, default) soft variant
"""
from __future__ import annotations

from typing import Any, Dict


_runtime_config: Dict[str, Any] = {}


def set_runtime_config(cfg: Dict[str, Any]) -> None:
    """
    Register the context config block. Called once during session
    startup with the 'context' subtree of runtime.json.
    """
    global _runtime_config
    _runtime_config = dict(cfg) if cfg else {}


def _rt_context(key: str) -> Any:
    """Look up a required tunable. Raises KeyError if missing."""
    return _runtime_config[key]


def _rt_context_get(key: str, default: Any = None) -> Any:
    """Soft variant that returns a default."""
    return _runtime_config.get(key, default)
