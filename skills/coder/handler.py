"""
ADAM coder skill.

Creates governed code workspaces, file packages, and patch drafts from an
approved ADAM build prompt.

v1.0.1:
- Adds top-level path/filename/artifact_type for GUI artifact discovery.
- Adds escaped HTML source detection for .html/.htm files.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SKILL_NAME = "coder"
SUPPORTED_ACTIONS = {"plan", "create_files", "create_patch"}

MAX_TITLE_LEN = 160
MAX_PROMPT_CHARS = 120_000
MAX_PLAN_CHARS = 120_000
MAX_PATCH_CHARS = 500_000
MAX_FILES = 60
MAX_FILE_BYTES = 500_000
MAX_TOTAL_BYTES = 5_000_000

_FORBIDDEN_CHARS_RE = re.compile(r'[<>:"\\|?*\x00-\x1f]')
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._ -]+$")

TEXT_EXTENSIONS = {
    ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".html", ".htm", ".css", ".js",
    ".ts", ".tsx", ".jsx", ".xml", ".sql", ".sh", ".bash", ".zsh", ".ps1",
    ".dockerfile", ".env.example", ".gitignore", ".toml", ".ini", ".cfg",
    ".csv", ".svg", ".vue", ".php", ".rb", ".go", ".rs", ".java", ".cs",
    ".cpp", ".c", ".h", ".hpp", ".swift", ".kt", ".r", ".lua", ".pl",
}


def _fail(action: str, error_class: str, error_message: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "status": "failed",
        "skill": SKILL_NAME,
        "action": action,
        "error_class": error_class,
        "error_message": error_message,
    }


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sanitize_title(raw: str) -> str:
    raw = raw.strip()
    raw = _FORBIDDEN_CHARS_RE.sub("_", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" .")
    return raw[:MAX_TITLE_LEN] or "ADAM Code Workspace"


def _slugify(raw: str) -> str:
    raw = raw.lower().strip()
    raw = re.sub(r"[^a-z0-9]+", "_", raw)
    raw = raw.strip("_")
    return raw[:64] or "code_workspace"


def _get_artifacts_root(context: Dict[str, Any]) -> Path:
    """
    Resolve the session artifact root.

    Supports the document skill convention, context['artifacts_root'], plus
    earlier artifact context keys used by other ADAM skills.
    """
    for key in ("artifacts_root", "artifact_dir", "artifacts_dir", "output_dir"):
        value = context.get(key)
        if value:
            return Path(str(value)).expanduser().resolve()

    log_dir = context.get("log_dir") or context.get("session_dir")
    if log_dir:
        return (Path(str(log_dir)).expanduser().resolve() / "artifacts")

    return (Path.cwd() / "artifacts").resolve()


def _workspace_root(context: Dict[str, Any], task_title: str) -> Tuple[str, Path, Path]:
    """
    Part 9.2: returns (artifact_id, workspace, artifacts_root).
    artifacts_root is included so callers can compute the
    session-artifacts-relative path used for GUI URL construction.
    """
    artifacts_root = _get_artifacts_root(context)
    artifact_id = f"code_{uuid.uuid4().hex[:12]}"
    workspace = artifacts_root / "coder" / f"{_slugify(task_title)}_{artifact_id}"
    workspace.mkdir(parents=True, exist_ok=False)
    return artifact_id, workspace, artifacts_root


def _is_safe_relative_path(raw_path: Any) -> Tuple[Optional[Path], Optional[str]]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None, "file path must be a non-empty string"

    raw = raw_path.strip().replace("\\", "/").strip("/")
    if not raw:
        return None, "file path reduces to empty after normalization"

    if raw.startswith("~") or raw.startswith("/") or ":" in raw:
        return None, "absolute paths, drive letters, and home-relative paths are not allowed"

    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts:
        return None, "file path has no usable segments"
    if any(p == ".." for p in parts):
        return None, "path traversal is not allowed"
    if len(parts) > 12:
        return None, "file path is too deeply nested"

    cleaned_parts: List[str] = []
    for part in parts:
        if not _SAFE_SEGMENT_RE.match(part):
            part = _FORBIDDEN_CHARS_RE.sub("_", part)
            part = re.sub(r"[^A-Za-z0-9._ -]", "_", part)
        part = part.strip(" .")
        if not part or part in {".", ".."}:
            return None, "invalid path segment"
        cleaned_parts.append(part[:120])

    rel = Path(*cleaned_parts)
    suffix = rel.suffix.lower()
    if suffix and suffix not in TEXT_EXTENSIONS:
        if rel.name not in {"Dockerfile", "Makefile", "LICENSE", "README", ".gitignore"}:
            return None, f"file extension {suffix!r} is not allowed in coder v1"

    return rel, None


def _safe_join(workspace: Path, rel_path: Path) -> Path:
    target = (workspace / rel_path).resolve()
    root = workspace.resolve()
    if root not in target.parents and target != root:
        raise ValueError("resolved path escaped workspace")
    return target


def _looks_like_escaped_html_source(path: str, content: str) -> bool:
    """Detect when HTML source was escaped and wrapped as documentation."""
    if not path.lower().endswith((".html", ".htm")):
        return False

    lower = content.lower()
    escaped_markers = [
        "&lt;!doctype",
        "&lt;!doctype html",
        "&lt;html",
        "&lt;head",
        "&lt;body",
        "&lt;script",
        "&lt;style",
        "&lt;/html",
    ]
    hits = sum(1 for marker in escaped_markers if marker in lower)
    return hits >= 2


def _write_bytes(path: Path, data: bytes) -> Dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {
        "path": str(path),
        "size_bytes": len(data),
        "sha256": _sha256_bytes(data),
    }


def _relative_display_path(path: Path, workspace: Path) -> str:
    try:
        return str(path.relative_to(workspace)).replace("\\", "/")
    except Exception:
        return str(path)


def _artifacts_relative_path(path: Path, artifacts_root: Path) -> str:
    """
    Part 9.2: compute the path of `path` relative to the session's
    artifacts root, using forward slashes. This is the canonical form
    the GUI uses to build artifact URLs:

        /api/sessions/<session_id>/artifacts/<relpath>

    Example: for a workspace at
        /.../logs/<user>/<sess>/artifacts/coder/<slug>_<id>/timer.html
    with artifacts_root at
        /.../logs/<user>/<sess>/artifacts/
    this returns
        "coder/<slug>_<id>/timer.html"

    Backslashes are normalized for Windows callers. Returns None if the
    path is not under the artifacts root (a deeper invariant violation;
    the GUI link will fall back to filename in that case).
    """
    try:
        return str(path.resolve().relative_to(artifacts_root.resolve())).replace("\\", "/")
    except Exception:
        return None


def _coerce_constraints(value: Any) -> List[str]:
    if value is None or not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value[:40]:
        if isinstance(item, str) and item.strip():
            out.append(item.strip()[:500])
    return out


def _coerce_metadata(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out: Dict[str, Any] = {}
    for k, v in list(value.items())[:80]:
        if not isinstance(k, str) or not k.strip():
            continue
        key = re.sub(r"[^A-Za-z0-9_.-]", "_", k.strip())[:80]
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[key] = v
        else:
            out[key] = str(v)[:1000]
    return out


def _base_audit_meta(context: Dict[str, Any], artifact_id: str, action: str, task_title: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
    audit_meta = {
        "session_id": context.get("session_id", ""),
        "invocation_id": context.get("invocation_id", ""),
        "artifact_id": artifact_id,
        "generated": _now_iso(),
        "skill": SKILL_NAME,
        "action": action,
        "task_title": task_title,
        "io_operation": "local_artifact_write",
        "repo_write_access_asserted": False,
        "shell_access_asserted": False,
        "external_network_access_asserted": False,
    }
    for k, v in metadata.items():
        if k not in audit_meta:
            audit_meta[k] = v
    return audit_meta


def _write_manifest(workspace: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    result = _write_bytes(workspace / "CODE_MANIFEST.json", data)
    result["absolute_path"] = result["path"]
    result["path"] = _relative_display_path(Path(result["path"]), workspace)
    return result


def _write_readme(workspace: Path, *, task_title: str, build_prompt: str, plan: Optional[str],
                  target_language: Optional[str], constraints: List[str], audit_meta: Dict[str, Any]) -> Dict[str, Any]:
    lines: List[str] = [
        f"# {task_title}", "",
        "## Purpose", "",
        "This workspace was generated by ADAM's `coder` skill as a governed code artifact package.", "",
        "## Target Language", "", target_language or "not specified", "",
        "## Build Prompt", "", build_prompt, "",
    ]
    if plan:
        lines.extend(["## Implementation Plan", "", plan, ""])
    if constraints:
        lines.extend(["## Constraints", ""])
        for c in constraints:
            lines.append(f"- {c}")
        lines.append("")
    lines.extend([
        "## Governance", "",
        "- This package was written to an artifact workspace only.",
        "- The skill did not execute code.",
        "- The skill did not apply a patch.",
        "- The skill did not modify the live repository.",
        "- Human review is required before copying or applying these files.",
        "",
        "## Audit Metadata", "",
        "```json",
        json.dumps(audit_meta, indent=2, sort_keys=True),
        "```", "",
    ])
    result = _write_bytes(workspace / "README_CODE_PACKAGE.md", "\n".join(lines).encode("utf-8"))
    result["absolute_path"] = result["path"]
    result["path"] = _relative_display_path(Path(result["path"]), workspace)
    return result


def _validate_common_args(action: str, args: Dict[str, Any]):
    if not isinstance(args, dict):
        return None, None, None, [], {}, _fail(action, "invalid_args", "args must be an object/dictionary.")

    raw_task_title = args.get("task_title")
    if not isinstance(raw_task_title, str) or not raw_task_title.strip():
        return None, None, None, [], {}, _fail(action, "missing_required_args", "task_title is required and must be a non-empty string.")
    task_title = _sanitize_title(raw_task_title)

    build_prompt = args.get("build_prompt")
    if not isinstance(build_prompt, str) or not build_prompt.strip():
        return None, None, None, [], {}, _fail(action, "missing_required_args", "build_prompt is required and must be a non-empty string.")
    build_prompt = build_prompt.strip()[:MAX_PROMPT_CHARS]

    target_language = args.get("target_language")
    if target_language is not None:
        if not isinstance(target_language, str):
            return None, None, None, [], {}, _fail(action, "invalid_args", "target_language must be a string when provided.")
        target_language = target_language.strip()[:80] or None

    constraints = _coerce_constraints(args.get("constraints"))
    metadata = _coerce_metadata(args.get("metadata"))
    return task_title, build_prompt, target_language, constraints, metadata, None


def _handle_plan(action: str, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    task_title, build_prompt, target_language, constraints, metadata, err = _validate_common_args(action, args)
    if err:
        return err

    plan = args.get("plan")
    if plan is not None and not isinstance(plan, str):
        return _fail(action, "invalid_args", "plan must be a string when provided.")
    if isinstance(plan, str):
        plan = plan.strip()[:MAX_PLAN_CHARS] or None

    artifact_id, workspace, artifacts_root = _workspace_root(context, task_title)
    audit_meta = _base_audit_meta(context, artifact_id, action, task_title, metadata)
    readme = _write_readme(workspace, task_title=task_title, build_prompt=build_prompt, plan=plan,
                           target_language=target_language, constraints=constraints, audit_meta=audit_meta)

    primary_path     = str(workspace / "README_CODE_PACKAGE.md")
    # Part 9.2: compute session-artifacts-relative paths for GUI URL
    # construction. These are forward-slash strings of the form
    # "coder/<slug>_<artifact_id>/<file>" that the GUI joins onto
    # /api/sessions/<id>/artifacts/ to produce a valid download URL.
    workspace_relpath = _artifacts_relative_path(workspace, artifacts_root)
    primary_relpath   = (
        f"{workspace_relpath}/README_CODE_PACKAGE.md"
        if workspace_relpath else None
    )
    manifest = {
        "artifact_id": artifact_id,
        "artifact_type": "code_plan",
        "action": action,
        "task_title": task_title,
        "filename": "README_CODE_PACKAGE.md",
        "path": primary_path,
        "relpath": primary_relpath,
        "workspace_path": str(workspace),
        "workspace_relpath": workspace_relpath,
        "target_language": target_language,
        "constraints": constraints,
        "files": [readme],
        "audit_meta": audit_meta,
    }
    manifest_file = _write_manifest(workspace, manifest)

    return {
        "ok": True,
        "status": "success",
        "skill": SKILL_NAME,
        "action": action,
        "artifact_id": artifact_id,
        "artifact_type": "code_plan",
        "filename": "README_CODE_PACKAGE.md",
        "path": primary_path,
        # Part 9.2: relpath is the GUI's canonical path field. The
        # frontend builds /api/sessions/<id>/artifacts/<relpath>.
        # When None (artifacts root resolution failed for some reason),
        # the GUI falls back to filename. workspace_relpath lets the GUI
        # link into the workspace directory if a future UI feature
        # surfaces a "browse this package" affordance.
        "relpath": primary_relpath,
        "workspace_path": str(workspace),
        "workspace_relpath": workspace_relpath,
        "manifest_path": str(workspace / "CODE_MANIFEST.json"),
        "files_created": [readme, manifest_file],
        "audit_meta": audit_meta,
        "summary": "Created a governed code planning workspace.",
        "next_steps": ["Review README_CODE_PACKAGE.md.", "Use create_files or create_patch when implementation content is ready."],
    }


def _handle_create_files(action: str, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    task_title, build_prompt, target_language, constraints, metadata, err = _validate_common_args(action, args)
    if err:
        return err

    files = args.get("files")
    if not isinstance(files, list) or not files:
        return _fail(action, "invalid_args", "files must be a non-empty array.")
    if len(files) > MAX_FILES:
        return _fail(action, "too_many_files", f"files may contain at most {MAX_FILES} entries.")

    artifact_id, workspace, artifacts_root = _workspace_root(context, task_title)
    audit_meta = _base_audit_meta(context, artifact_id, action, task_title, metadata)
    created: List[Dict[str, Any]] = []
    total_bytes = 0
    seen_rel_paths = set()
    primary_file: Optional[Dict[str, Any]] = None
    primary_absolute_path: Optional[str] = None

    plan = args.get("plan") if isinstance(args.get("plan"), str) else None
    readme = _write_readme(workspace, task_title=task_title, build_prompt=build_prompt, plan=plan,
                           target_language=target_language, constraints=constraints, audit_meta=audit_meta)
    created.append(readme)

    for idx, item in enumerate(files):
        if not isinstance(item, dict):
            return _fail(action, "invalid_file_entry", f"files[{idx}] must be an object.")

        rel_path, path_err = _is_safe_relative_path(item.get("path"))
        if path_err:
            return _fail(action, "invalid_file_path", f"files[{idx}].path invalid: {path_err}")

        rel_display = str(rel_path).replace("\\", "/")
        if rel_display in seen_rel_paths:
            return _fail(action, "duplicate_file_path", f"Duplicate file path: {rel_display}")
        seen_rel_paths.add(rel_display)

        content = item.get("content")
        if not isinstance(content, str):
            return _fail(action, "invalid_file_content", f"files[{idx}].content must be a string.")

        if _looks_like_escaped_html_source(rel_display, content):
            return _fail(
                action,
                "likely_escaped_source",
                f"files[{idx}] appears to contain escaped HTML source. For HTML apps, content must be raw HTML beginning with <!DOCTYPE html>, not escaped text such as &lt;html&gt; or &lt;script&gt;.",
            )

        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            return _fail(action, "file_too_large", f"files[{idx}] exceeds {MAX_FILE_BYTES} bytes.")
        total_bytes += len(data)
        if total_bytes > MAX_TOTAL_BYTES:
            return _fail(action, "workspace_too_large", f"Total file content exceeds {MAX_TOTAL_BYTES} bytes.")

        target = _safe_join(workspace, rel_path)
        write_meta = _write_bytes(target, data)
        write_meta["absolute_path"] = write_meta["path"]
        write_meta["path"] = rel_display
        if isinstance(item.get("purpose"), str) and item.get("purpose").strip():
            write_meta["purpose"] = item.get("purpose").strip()[:500]
        created.append(write_meta)

        if primary_file is None:
            primary_file = write_meta
            primary_absolute_path = str(target)

    primary_filename = Path(primary_file["path"]).name if primary_file else "README_CODE_PACKAGE.md"
    primary_path = primary_absolute_path or str(workspace / "README_CODE_PACKAGE.md")
    # Part 9.2: session-artifacts-relative paths for GUI URLs.
    # workspace_relpath: "coder/<slug>_<artifact_id>"
    # primary_relpath:   "coder/<slug>_<artifact_id>/<primary_filename>"
    # The GUI joins primary_relpath onto /api/sessions/<id>/artifacts/
    # to download the primary file. If relpath can't be computed
    # (artifacts root resolution failed), the GUI falls back to
    # filename, which will 404 for nested files -- but that's the
    # safer failure mode than emitting a broken URL.
    workspace_relpath = _artifacts_relative_path(workspace, artifacts_root)
    primary_relpath   = (
        f"{workspace_relpath}/{Path(primary_file['path']).as_posix()}"
        if (workspace_relpath and primary_file) else None
    )

    manifest = {
        "artifact_id": artifact_id,
        "artifact_type": "code_workspace",
        "action": action,
        "task_title": task_title,
        "filename": primary_filename,
        "path": primary_path,
        "relpath": primary_relpath,
        "workspace_path": str(workspace),
        "workspace_relpath": workspace_relpath,
        "target_language": target_language,
        "constraints": constraints,
        "files_created": created,
        "audit_meta": audit_meta,
        "next_steps": ["Review generated files.", "Copy files into the live repository only after human approval.", "Run tests manually outside ADAM."],
    }
    manifest_file = _write_manifest(workspace, manifest)
    created.append(manifest_file)

    return {
        "ok": True,
        "status": "success",
        "skill": SKILL_NAME,
        "action": action,
        "artifact_id": artifact_id,
        "artifact_type": "code_workspace",
        "filename": primary_filename,
        "path": primary_path,
        "relpath": primary_relpath,
        "workspace_path": str(workspace),
        "workspace_relpath": workspace_relpath,
        "manifest_path": str(workspace / "CODE_MANIFEST.json"),
        "files_created": created,
        "audit_meta": audit_meta,
        "summary": f"Created governed code workspace with {len(created)} files including manifest.",
        "next_steps": ["Review the generated workspace.", "Apply changes manually only after approval.", "Run tests manually outside ADAM."],
    }


def _handle_create_patch(action: str, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    task_title, build_prompt, target_language, constraints, metadata, err = _validate_common_args(action, args)
    if err:
        return err

    patch = args.get("patch")
    if not isinstance(patch, str) or not patch.strip():
        return _fail(action, "missing_required_args", "patch is required and must be a non-empty string.")
    patch = patch.strip()
    patch_bytes = patch.encode("utf-8")
    if len(patch_bytes) > MAX_PATCH_CHARS:
        return _fail(action, "patch_too_large", f"patch exceeds {MAX_PATCH_CHARS} bytes.")

    base_path = args.get("base_path")
    if base_path is not None and not isinstance(base_path, str):
        return _fail(action, "invalid_args", "base_path must be a string when provided.")
    base_path = base_path.strip()[:500] if isinstance(base_path, str) else ""

    artifact_id, workspace, artifacts_root = _workspace_root(context, task_title)
    audit_meta = _base_audit_meta(context, artifact_id, action, task_title, metadata)
    if base_path:
        audit_meta["base_path_note"] = base_path

    plan = args.get("plan") if isinstance(args.get("plan"), str) else None
    readme = _write_readme(workspace, task_title=task_title, build_prompt=build_prompt, plan=plan,
                           target_language=target_language, constraints=constraints, audit_meta=audit_meta)
    patch_file = _write_bytes(workspace / "changes.patch", patch_bytes)
    patch_file["absolute_path"] = patch_file["path"]
    patch_file["path"] = "changes.patch"

    # Part 9.2: relpath for GUI URL construction.
    workspace_relpath = _artifacts_relative_path(workspace, artifacts_root)
    primary_relpath   = (
        f"{workspace_relpath}/changes.patch"
        if workspace_relpath else None
    )

    manifest = {
        "artifact_id": artifact_id,
        "artifact_type": "patch_draft",
        "action": action,
        "task_title": task_title,
        "filename": "changes.patch",
        "path": str(workspace / "changes.patch"),
        "relpath": primary_relpath,
        "workspace_path": str(workspace),
        "workspace_relpath": workspace_relpath,
        "target_language": target_language,
        "constraints": constraints,
        "patch_file": patch_file,
        "readme": readme,
        "audit_meta": audit_meta,
        "next_steps": ["Review changes.patch.", "Apply manually only after approval.", "Run tests manually outside ADAM."],
    }
    manifest_file = _write_manifest(workspace, manifest)

    return {
        "ok": True,
        "status": "success",
        "skill": SKILL_NAME,
        "action": action,
        "artifact_id": artifact_id,
        "artifact_type": "patch_draft",
        "filename": "changes.patch",
        "path": str(workspace / "changes.patch"),
        "relpath": primary_relpath,
        "workspace_path": str(workspace),
        "workspace_relpath": workspace_relpath,
        "manifest_path": str(workspace / "CODE_MANIFEST.json"),
        "files_created": [readme, patch_file, manifest_file],
        "patch_path": str(workspace / "changes.patch"),
        "audit_meta": audit_meta,
        "summary": "Created a governed patch draft. The patch was not applied.",
        "next_steps": ["Review changes.patch.", "Apply manually only after approval.", "Run tests manually outside ADAM."],
    }


def handle(action: str, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """ADAM skill handler entry point."""
    if action not in SUPPORTED_ACTIONS:
        return _fail(action, "disallowed_action", f"Action {action!r} is unrecognized. Supported actions: {sorted(SUPPORTED_ACTIONS)}.")

    context = context or {}
    try:
        if action == "plan":
            return _handle_plan(action, args, context)
        if action == "create_files":
            return _handle_create_files(action, args, context)
        if action == "create_patch":
            return _handle_create_patch(action, args, context)
    except Exception as e:
        return _fail(action, "coder_exception", f"Coder skill failed safely: {type(e).__name__}: {e}")

    return _fail(action, "internal_error", "Unhandled coder action.")
