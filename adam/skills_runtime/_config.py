"""
Skills-runtime-private runtime configuration.

Internal module of adam.skills_runtime. Submodules import from here
directly to avoid circular imports through the package's __init__.

Mirrors the pattern established by adam.verifier._config and
adam.context._config:
  - set_runtime_config(cfg) registers the 'skills' block of runtime.json
  - _rt_skills(key) reads a tunable; KeyError if missing
  - _rt_skills_get(key, default) soft variant
"""
from __future__ import annotations

from typing import Any, Dict


_runtime_config: Dict[str, Any] = {}


def set_runtime_config(cfg: Dict[str, Any]) -> None:
    """
    Register the skills config block. Called once during session
    startup with the 'skills' subtree of runtime.json.
    """
    global _runtime_config
    _runtime_config = dict(cfg) if cfg else {}


def _rt_skills(key: str) -> Any:
    """Look up a required tunable. Raises KeyError if missing."""
    return _runtime_config[key]


def _rt_skills_get(key: str, default: Any = None) -> Any:
    """Soft variant that returns a default."""
    return _runtime_config.get(key, default)


def get_runtime_config() -> Dict[str, Any]:
    """
    Return the full skills config dict. Used by discover_skills() which
    receives the config inline rather than reading one key at a time.
    """
    return dict(_runtime_config)
