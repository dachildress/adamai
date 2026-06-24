"""
Skill execution subsystem.

Module map:
  - manifest.py        SkillManifest, SkillCatalog, _parse_simple_yaml,
                       discover_skills, build_skill_manifest_block,
                       SkillManifestError
  - runtime.py         ParsedSkillCall, parse_skill_calls, SkillRuntime,
                       SKILL_CALL_FENCE_RE
  - cli_args.py        --skill-arg parsing helpers: _parse_one_skill_arg,
                       parse_skill_args, _redact_skill_arg_value,
                       format_skill_args_for_display,
                       build_operator_skill_args_note
  - _config.py         (private) runtime-config layer for this subpackage

Public API:

    from adam.skills_runtime import (
        # Manifest
        SkillManifestError, SkillManifest, SkillCatalog,
        discover_skills, build_skill_manifest_block,
        # Runtime
        ParsedSkillCall, parse_skill_calls, SkillRuntime,
        # CLI args
        parse_skill_args, format_skill_args_for_display,
        build_operator_skill_args_note,
        # Runtime config registration
        set_runtime_config,
    )
"""
from __future__ import annotations

# Config layer
from adam.skills_runtime._config import set_runtime_config

# Manifest discovery
from adam.skills_runtime.manifest import (
    SkillManifestError,
    SkillManifest,
    SkillCatalog,
    discover_skills,
    build_skill_manifest_block,
)

# Runtime execution
from adam.skills_runtime.runtime import (
    SKILL_CALL_FENCE_RE,
    SKILL_CALL_OPEN_FENCE_RE,
    ParsedSkillCall,
    parse_skill_calls,
    SkillRuntime,
)

# CLI argument parsing
from adam.skills_runtime.cli_args import (
    parse_skill_args,
    format_skill_args_for_display,
    build_operator_skill_args_note,
)

__all__ = [
    "set_runtime_config",
    "SkillManifestError",
    "SkillManifest",
    "SkillCatalog",
    "discover_skills",
    "build_skill_manifest_block",
    "SKILL_CALL_FENCE_RE",
    "SKILL_CALL_OPEN_FENCE_RE",
    "ParsedSkillCall",
    "parse_skill_calls",
    "SkillRuntime",
    "parse_skill_args",
    "format_skill_args_for_display",
    "build_operator_skill_args_note",
]
