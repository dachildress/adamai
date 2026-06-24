"""
Skill manifest discovery, parsing, and validation.

Three responsibilities:

1. **YAML parsing**: A subset of YAML sufficient for skill.yaml files
   (scalars, lists, nested mappings, folded multi-line strings). Avoids
   the PyYAML dependency for the canonical skill manifests we ship.

2. **Manifest validation**: SkillManifest captures everything declared
   in a skill.yaml -- name, version, description, risk_level,
   allowed_callers, actions (with required_args/optional_args), llm_access,
   audit_required, handler module path, etc. Invalid manifests fail loud
   at startup with SkillManifestError naming the offending field.

3. **Catalog assembly**: discover_skills() walks skills/ at the repo root,
   parses each subfolder's skill.yaml, and partitions results into
   executable / documentation_only / disabled / unsupported buckets.
   build_skill_manifest_block() renders the catalog into the dynamic
   system-prompt supplement injected into agent primes whose role is
   allowed to invoke skills.

No LLM calls, no network, no model dispatch. Pure config-driven
discovery + validation.
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from adam.skills_runtime._config import _rt_skills, _rt_skills_get


class SkillManifestError(Exception):
    """skill.yaml parsing / validation failure."""


def _parse_simple_yaml(text: str) -> Dict[str, Any]:
    """
    Parse a subset of YAML sufficient for skill.yaml files. Supports:
      - key: scalar (string, int, bool)
      - key: [list, items]
      - key:
          subkey: value
      - key: >
          multi-line folded string
      - lists via "  - item" indented under a key

    Does NOT support: anchors, references, multi-doc files, complex
    flow syntax. If a skill.yaml needs those, install PyYAML and switch
    to it; for the canonical skill manifests we ship, this is enough.
    """
    lines = text.splitlines()
    result: Dict[str, Any] = {}
    # Stack of (dict_or_list, indent_level) currently being filled
    stack: List[Tuple[Any, int]] = [(result, -1)]
    # Track the pending key for nested values
    pending_key: Optional[str] = None
    # Folded-string accumulator state
    folded_lines: Optional[List[str]] = None
    folded_indent: int = -1
    folded_target_key: Optional[str] = None
    folded_target_container: Any = None

    def _strip_comment(s: str) -> str:
        # Strip trailing # comments (but not inside quoted strings; for
        # our subset, quoted strings don't contain # so we're safe)
        idx = s.find("#")
        return s[:idx].rstrip() if idx >= 0 else s

    def _coerce_scalar(s: str) -> Any:
        s = s.strip()
        if not s:
            return ""
        if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
            return s[1:-1]
        if s.lower() in ("true", "yes"):
            return True
        if s.lower() in ("false", "no"):
            return False
        if s.lower() in ("null", "none", "~"):
            return None
        try:
            return int(s)
        except ValueError:
            pass
        try:
            return float(s)
        except ValueError:
            pass
        return s

    def _commit_folded() -> None:
        nonlocal folded_lines, folded_target_key, folded_target_container
        if folded_lines is None:
            return
        value = " ".join(line.strip() for line in folded_lines if line.strip())
        folded_target_container[folded_target_key] = value
        folded_lines = None
        folded_target_key = None
        folded_target_container = None

    for raw_line in lines:
        line = _strip_comment(raw_line.rstrip())
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.lstrip()

        # Folded string continuation
        if folded_lines is not None:
            if indent > folded_indent:
                folded_lines.append(stripped)
                continue
            _commit_folded()  # falls through to process this line normally

        # Pop the stack to a level <= this indent
        while stack and stack[-1][1] >= indent:
            stack.pop()
        if not stack:
            raise SkillManifestError(f"unexpected indentation at line: {raw_line!r}")
        container, _ = stack[-1]

        # List item
        if stripped.startswith("- "):
            item_text = stripped[2:].strip()
            if not isinstance(container, list):
                # The container should already be a list; if it's a dict,
                # we're missing the parent key
                raise SkillManifestError(f"list item outside a list context: {raw_line!r}")
            container.append(_coerce_scalar(item_text))
            continue

        # key: value or key: (nested)
        if ":" not in stripped:
            raise SkillManifestError(f"expected 'key: value' format, got: {raw_line!r}")
        key, _, value_part = stripped.partition(":")
        key = key.strip()
        value_part = value_part.strip()

        if not value_part:
            # Nested mapping or list to follow at deeper indent. We don't
            # know yet which; peek by creating an empty dict and converting
            # to list when the next line starts with '- '.
            new_container: Any = {}
            if isinstance(container, dict):
                container[key] = new_container
            else:
                raise SkillManifestError(f"unexpected key in list context: {raw_line!r}")
            # The next item might be a list. We'll handle the conversion
            # by detecting "- " at deeper indent and replacing with list.
            stack.append((new_container, indent))
            # Look ahead: if next non-empty line is indented further AND
            # starts with "- ", convert to list
            ahead = lines[lines.index(raw_line) + 1:] if raw_line in lines else []
            for ahead_line in ahead:
                a_stripped = _strip_comment(ahead_line.rstrip())
                if not a_stripped.strip():
                    continue
                a_indent = len(a_stripped) - len(a_stripped.lstrip())
                if a_indent <= indent:
                    break
                if a_stripped.lstrip().startswith("- "):
                    # Convert to list
                    new_list: List[Any] = []
                    container[key] = new_list
                    stack[-1] = (new_list, indent)
                break
            continue

        if value_part == ">":
            # Folded multi-line string begins on next line
            if not isinstance(container, dict):
                raise SkillManifestError(f"folded string outside dict context: {raw_line!r}")
            folded_lines = []
            folded_indent = indent
            folded_target_key = key
            folded_target_container = container
            continue

        # Inline list?
        if value_part.startswith("[") and value_part.endswith("]"):
            inner = value_part[1:-1].strip()
            if not inner:
                container[key] = []
            else:
                container[key] = [_coerce_scalar(p.strip()) for p in inner.split(",")]
            continue

        if isinstance(container, dict):
            container[key] = _coerce_scalar(value_part)
        else:
            raise SkillManifestError(f"key:value in list context: {raw_line!r}")

    _commit_folded()
    return result


FRONTMATTER_DELIM_RE = re.compile(r"^---\s*$", re.MULTILINE)


def _extract_frontmatter(skill_md_text: str) -> Tuple[str, str]:
    """
    Split a SKILL.md file into (frontmatter_yaml, body_markdown).

    SKILL.md format (Claude Code-compatible):

        ---
        name: my_skill
        description: ...
        version: 1.0
        adam:
          allowed_callers: [Operator]
          actions:
            ...
        ---

        # Markdown body of the SKILL.md file...

    Returns ('', whole_text) if no frontmatter delimiters are found, which
    is the documentation-without-metadata case. Returns (frontmatter, body)
    when both delimiters are present.
    """
    text = skill_md_text.lstrip()
    if not text.startswith("---"):
        return "", skill_md_text

    # Find the closing --- on its own line
    matches = list(FRONTMATTER_DELIM_RE.finditer(skill_md_text))
    if len(matches) < 2:
        # Opened frontmatter but never closed - treat as invalid frontmatter
        return "", skill_md_text

    first = matches[0]
    second = matches[1]
    if first.start() > 5:
        # First --- is not near the top -- not real frontmatter
        return "", skill_md_text

    frontmatter = skill_md_text[first.end():second.start()].strip("\n")
    body = skill_md_text[second.end():].lstrip("\n")
    return frontmatter, body


class SkillManifest:
    """
    Validated representation of a skill's metadata.

    Source format: YAML frontmatter at the top of SKILL.md, Claude Code-
    compatible. Standard fields (name, description, version) live at the
    top level; ADAM governance extensions live under the namespaced 'adam:'
    block so they don't conflict with other ecosystems.
    """

    def __init__(self, name: str, raw: Dict[str, Any], folder: Path, body: str) -> None:
        # Top-level (Claude Code-compatible) fields
        self.name            = name
        self.folder          = folder
        self.version         = str(raw.get("version", "0.0"))
        self.description     = raw.get("description", "")
        self.body_markdown   = body  # SKILL.md body sans frontmatter

        # ADAM extensions (namespaced under 'adam:' in frontmatter)
        adam_block = raw.get("adam", {}) or {}
        if not isinstance(adam_block, dict):
            adam_block = {}
        self.risk_level      = adam_block.get("risk_level", "unknown")
        self.audit_required  = bool(adam_block.get("audit_required", True))
        self.llm_access      = bool(adam_block.get("llm_access", False))
        self.allowed_callers = list(adam_block.get("allowed_callers", []))
        self.actions         = dict(adam_block.get("actions", {}))
        self.source_type     = adam_block.get("source_type", "native_adam")

        # Category is set by discover_skills() after handler.py presence check
        # ("executable" or "documentation_only" or "unsupported")
        self.category: str = "unknown"

    @classmethod
    def load(cls, folder: Path) -> "SkillManifest":
        skill_md_path = folder / "SKILL.md"
        if not skill_md_path.exists():
            raise SkillManifestError(
                f"skill folder {folder} is missing SKILL.md"
            )
        try:
            text = skill_md_path.read_text(encoding="utf-8")
        except Exception as e:
            raise SkillManifestError(
                f"could not read {skill_md_path}: {type(e).__name__}: {e}"
            )

        frontmatter_text, body = _extract_frontmatter(text)
        if not frontmatter_text.strip():
            raise SkillManifestError(
                f"{skill_md_path}: missing YAML frontmatter between '---' delimiters. "
                f"Every skill must declare at least 'name' and 'description' in "
                f"frontmatter at the top of SKILL.md."
            )

        try:
            raw = _parse_simple_yaml(frontmatter_text)
        except SkillManifestError:
            raise
        except Exception as e:
            raise SkillManifestError(
                f"could not parse frontmatter in {skill_md_path}: "
                f"{type(e).__name__}: {e}"
            )

        # Top-level required fields (Claude Code-compatible)
        for required in ("name", "description"):
            if required not in raw:
                raise SkillManifestError(
                    f"{skill_md_path}: frontmatter missing required top-level "
                    f"field '{required}'"
                )
        if not isinstance(raw["name"], str) or not raw["name"].strip():
            raise SkillManifestError(
                f"{skill_md_path}: frontmatter 'name' must be a non-empty string"
            )

        # ADAM extensions validation: only required if we expect this skill
        # to be executable. A skill without an adam: block (or with one
        # lacking allowed_callers/actions) is treated as documentation-only
        # later in discovery -- not an error at parse time.
        adam_block = raw.get("adam", {}) or {}
        if adam_block and isinstance(adam_block, dict):
            # If allowed_callers is present, validate it
            if "allowed_callers" in adam_block:
                if (not isinstance(adam_block["allowed_callers"], list)
                        or not adam_block["allowed_callers"]):
                    raise SkillManifestError(
                        f"{skill_md_path}: adam.allowed_callers must be a "
                        f"non-empty list when present"
                    )
            # If actions is present, validate each action
            if "actions" in adam_block:
                actions = adam_block["actions"]
                if not isinstance(actions, dict) or not actions:
                    raise SkillManifestError(
                        f"{skill_md_path}: adam.actions must be a non-empty "
                        f"mapping when present"
                    )
                for action_name, action_spec in actions.items():
                    if not isinstance(action_spec, dict):
                        raise SkillManifestError(
                            f"{skill_md_path}: adam.actions.{action_name} "
                            f"must be a mapping"
                        )
                    for arg_key in ("required_args", "optional_args"):
                        if arg_key in action_spec:
                            if not isinstance(action_spec[arg_key], list):
                                raise SkillManifestError(
                                    f"{skill_md_path}: adam.actions."
                                    f"{action_name}.{arg_key} must be a list"
                                )

        return cls(name=raw["name"], raw=raw, folder=folder, body=body)

    def read_skill_md(self) -> str:
        """
        Return the SKILL.md body (without frontmatter) for injection into
        the LLM-facing skill manifest block. For pure documentation skills
        the body IS the skill -- it's the instructions the LLM reads.
        """
        return self.body_markdown


class SkillCatalog:
    """
    Runtime registry of discovered and enabled skills.

    Loaded once at startup by SkillRuntime. Tracks four categories:
      - executable        : skills with valid manifest AND handler.py;
                            invocable via skill_call
      - documentation_only: skills with valid manifest but no handler.py;
                            visible to agents as reference material only
      - disabled          : skills present on disk but disabled via runtime.json
      - unsupported       : folders that don't conform to the manifest schema
                            (e.g., missing SKILL.md, malformed frontmatter)
    """

    def __init__(self) -> None:
        self.executable:         Dict[str, SkillManifest] = {}
        self.documentation_only: Dict[str, SkillManifest] = {}
        self.disabled:           List[Tuple[str, str]]    = []
        self.unsupported:        List[Tuple[str, str]]    = []
        self.handlers:           Dict[str, callable]      = {}

    def list_executable(self) -> List[SkillManifest]:
        return list(self.executable.values())

    def list_documentation_only(self) -> List[SkillManifest]:
        return list(self.documentation_only.values())

    # Backward compatibility: many call sites use `enabled` / `list_enabled`
    # to mean "the things the catalog will let agents invoke." Keep those
    # aliases pointing at the executable set.
    @property
    def enabled(self) -> Dict[str, SkillManifest]:
        return self.executable

    def list_enabled(self) -> List[SkillManifest]:
        return self.list_executable()

    def get(self, name: str) -> Optional[SkillManifest]:
        return self.executable.get(name)

    def get_any(self, name: str) -> Optional[SkillManifest]:
        """Lookup across both executable and documentation-only catalogs.
        Used by SkillRuntime to produce documentation_only_skill error
        when an agent tries to invoke a doc-only skill."""
        return self.executable.get(name) or self.documentation_only.get(name)

    def get_handler(self, name: str) -> Optional[callable]:
        return self.handlers.get(name)


def _import_skill_handler(skill_folder: Path) -> Optional[callable]:
    """
    Import skills/<name>/handler.py and return its handle() function.
    Returns None if the module cannot be loaded or if no handler.py
    exists. Caller distinguishes 'no handler.py' (documentation-only)
    from 'handler.py present but broken' via the file's presence on disk.

    Supports two skill layouts:
      - Flat: skills/<name>/handler.py (e.g., test_echo)
      - Package: skills/<name>/{__init__.py, handler.py, subpackages...}
        for skills like 'document' that have renderers/ and backends/
        with relative imports.

    For package-layout skills, we add the skills/ parent directory to
    sys.path (idempotently) so Python's normal import machinery can
    resolve the package and its subpackages naturally.
    """
    handler_path = skill_folder / "handler.py"
    if not handler_path.exists():
        return None

    init_path = skill_folder / "__init__.py"
    is_package = init_path.exists()

    try:
        import importlib
        import importlib.util
        if is_package:
            # Make sure skills/ parent is on sys.path so the package
            # imports cleanly. Idempotent: don't add twice.
            skills_parent = str(skill_folder.parent.resolve())
            if skills_parent not in sys.path:
                sys.path.insert(0, skills_parent)
            # Use the folder name as the package name. Wipe any prior
            # cached version of this skill so reloads get the fresh code.
            pkg_name = skill_folder.name
            for cached in list(sys.modules.keys()):
                if cached == pkg_name or cached.startswith(pkg_name + "."):
                    del sys.modules[cached]
            mod = importlib.import_module(f"{pkg_name}.handler")
        else:
            spec = importlib.util.spec_from_file_location(
                f"_adam_skill_{skill_folder.name}", handler_path,
            )
            if spec is None or spec.loader is None:
                return None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
    except Exception:
        return None

    handle = getattr(mod, "handle", None)
    if callable(handle):
        return handle
    return None


def discover_skills(skills_cfg: Dict[str, Any]) -> SkillCatalog:
    """
    Walk the configured skill directory and build a SkillCatalog reflecting
    which skills loaded successfully, are documentation-only, are disabled,
    or are unsupported. Honors per-runtime enabled_skills and disabled_skills.

    Manifest source: YAML frontmatter at the top of SKILL.md (Claude Code-
    compatible). ADAM-specific governance metadata lives under the
    namespaced 'adam:' block.

    Category determination:
      - SKILL.md valid + handler.py present + adam.actions populated
        => executable
      - SKILL.md valid + (no handler.py OR no adam.actions)
        => documentation_only
      - SKILL.md missing or frontmatter malformed
        => unsupported (only flagged loudly if explicitly enabled)
    """
    catalog = SkillCatalog()

    if not skills_cfg.get("enabled", True):
        return catalog

    skill_dir = Path(skills_cfg["skill_dir"])
    if not skill_dir.exists() or not skill_dir.is_dir():
        return catalog

    enabled_list  = list(skills_cfg.get("enabled_skills", []))
    disabled_list = list(skills_cfg.get("disabled_skills", []))
    load_disabled_meta = bool(skills_cfg.get("load_disabled_skills_metadata", True))

    for entry in sorted(skill_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith(".") or entry.name.startswith("_"):
            continue
        folder_name = entry.name

        if folder_name in disabled_list:
            catalog.disabled.append((folder_name, "disabled in runtime.json"))
            continue

        if enabled_list and folder_name not in enabled_list:
            catalog.disabled.append((folder_name, "not in enabled_skills list"))
            continue

        # Per C5: malformed disabled or documentation-only skills warn but
        # do not block. Malformed enabled executable skills fail loud.
        skill_md_path = entry / "SKILL.md"
        if not skill_md_path.exists():
            catalog.unsupported.append((folder_name, "missing SKILL.md"))
            continue

        try:
            manifest = SkillManifest.load(entry)
        except SkillManifestError as e:
            catalog.unsupported.append((folder_name, f"manifest error: {e}"))
            continue

        # Check handler.py presence to classify
        handler = _import_skill_handler(entry)
        has_handler = handler is not None
        has_actions = bool(manifest.actions)
        has_callers = bool(manifest.allowed_callers)

        if has_handler and has_actions and has_callers:
            manifest.category = "executable"
            catalog.executable[manifest.name] = manifest
            catalog.handlers[manifest.name] = handler
        else:
            # Documentation-only: visible in the catalog, readable as
            # reference, but NOT invocable via skill_call.
            manifest.category = "documentation_only"
            catalog.documentation_only[manifest.name] = manifest

    return catalog


def build_skill_manifest_block(catalog: SkillCatalog) -> str:
    """
    Build the dynamic skill manifest supplement injected into the system
    prompts of agents whose role is allowed to invoke skills. Lists each
    executable skill with its actions and SKILL.md body. Also lists any
    documentation-only skills separately so the agent knows they exist
    but can't be invoked via skill_call.
    """
    if not catalog.executable and not catalog.documentation_only:
        return ""

    parts: List[str] = []
    parts.append("# AVAILABLE SKILLS")
    parts.append("")

    if catalog.executable:
        parts.append("## How to invoke")
        parts.append("")
        parts.append(
            "You may invoke any of the executable skills listed below by emitting a "
            "fenced ```skill_call JSON block in your response. The block must follow "
            "this exact structure (one or more calls allowed per block):"
        )
        parts.append("")
        parts.append("```skill_call")
        parts.append('{')
        parts.append('  "skill_calls": [')
        parts.append('    {')
        parts.append('      "skill": "<skill_name>",')
        parts.append('      "action": "<action_name>",')
        parts.append('      "args": { ... }')
        parts.append('    }')
        parts.append('  ]')
        parts.append('}')
        parts.append("```")
        parts.append("")
        parts.append("The SkillRuntime will execute each call, audit it, and inject "
                     "a result summary into the transcript. If the call fails, you "
                     "will see a clear error message and can correct on a later turn.")
        parts.append("")

        parts.append("## Executable skills")
        parts.append("")
        for manifest in catalog.list_executable():
            actions_list = ", ".join(manifest.actions.keys())
            parts.append(f"### {manifest.name} (v{manifest.version}) - actions: {actions_list}")
            parts.append("")
            skill_md = manifest.read_skill_md()
            if skill_md.strip():
                parts.append(skill_md.strip())
            else:
                parts.append(manifest.description or "(no description)")
            parts.append("")

    if catalog.documentation_only:
        parts.append("## Discovered documentation-only skills")
        parts.append("")
        parts.append(
            "These skills provide guidance but CANNOT be invoked through skill_call "
            "in this ADAM runtime. Do not attempt to call them -- attempting will "
            "fail with error_class='documentation_only_skill'. You may, however, "
            "read their SKILL.md content as reference material when reasoning."
        )
        parts.append("")
        for manifest in catalog.list_documentation_only():
            parts.append(f"### {manifest.name} (v{manifest.version}) - documentation only")
            parts.append("")
            parts.append(manifest.description or "(no description)")
            parts.append("")

    return "\n".join(parts)
