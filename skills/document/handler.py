"""
document skill handler.

Wires renderers + backends behind the SkillRuntime's handle() contract:
    handle(action: str, args: dict, context: dict) -> dict

The runtime adds invocation_id, status, session_id, etc. on top of the
dict we return.

Pass 2 ships .md, .txt, .html via local_filesystem.
Pass 3 will activate .docx (currently raises NotImplementedError).
v2 will add google_drive and onedrive backends.
"""

import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

# Import renderers and backends from the skill package
from .renderers import markdown as renderer_markdown
from .renderers import text     as renderer_text
from .renderers import html     as renderer_html
from .renderers import docx     as renderer_docx
from .backends import get_backend


SUPPORTED_FORMATS = {"md", "txt", "html", "docx"}

# MIME types per format. Used by the storage backend for cloud metadata
# tagging (Google Drive / OneDrive will read this) and for HTTP-Content-Type
# headers if a future backend exposes them.
MIME_TYPES = {
    "md":   "text/markdown",
    "txt":  "text/plain",
    "html": "text/html",
    "docx": ("application/vnd.openxmlformats-officedocument."
             "wordprocessingml.document"),
}

# Filename sanitization: forbid path traversal, forbid Windows reserved
# names, forbid characters that misbehave on common filesystems.
_FORBIDDEN_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def _sanitize_filename(raw: str, ext: str) -> str:
    """
    Produce a safe filename from raw input. Strips path components, removes
    forbidden characters, rejects Windows reserved names, ensures the
    extension matches the chosen format, and caps length at 200.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("filename must be a non-empty string")
    # Strip any path components -- only the basename is allowed
    raw = os.path.basename(raw)
    # Remove extension if present, we'll add the correct one
    stem = Path(raw).stem
    # Replace forbidden chars with _
    stem = _FORBIDDEN_CHARS_RE.sub("_", stem)
    # Trim whitespace and dots from edges (Windows hates trailing dots)
    stem = stem.strip(" .")
    if not stem:
        raise ValueError("filename reduces to empty after sanitization")
    # Reject Windows reserved names
    if stem.upper() in _WIN_RESERVED:
        raise ValueError(
            f"filename '{stem}' is a Windows reserved name; choose another"
        )
    # Cap length (stem only; extension is short)
    if len(stem) > 200:
        stem = stem[:200]
    return f"{stem}.{ext}"


def _render(fmt: str, content: str, title, footer, audit_meta):
    """Dispatch to the right renderer."""
    if fmt == "md":
        return renderer_markdown.render(
            content=content, title=title,
            include_audit_footer=footer, audit_metadata=audit_meta,
        )
    if fmt == "txt":
        return renderer_text.render(
            content=content, title=title,
            include_audit_footer=footer, audit_metadata=audit_meta,
        )
    if fmt == "html":
        return renderer_html.render(
            content=content, title=title,
            include_audit_footer=footer, audit_metadata=audit_meta,
        )
    if fmt == "docx":
        return renderer_docx.render(
            content=content, title=title,
            include_audit_footer=footer, audit_metadata=audit_meta,
        )
    raise ValueError(f"unknown format: {fmt!r}")


def handle(action: str, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Entry point called by SkillRuntime.

    The runtime has already:
      - Confirmed allowed_callers (Operator only)
      - Confirmed action is 'create'
      - Confirmed required_args are present
    But it has NOT validated arg TYPES yet. We do that here and raise
    ValueError on bad input; the runtime wraps the exception into a
    failed SkillResult.
    """
    if action != "create":
        raise ValueError(f"unsupported action: {action!r}")

    # --- arg unpack + validate ---
    filename = args.get("filename")
    fmt = args.get("format")
    content = args.get("content")
    title = args.get("title")
    include_audit_footer = args.get("include_audit_footer", True)
    metadata = args.get("metadata", {}) or {}
    backend_name = args.get("backend", "local_filesystem")

    if not isinstance(fmt, str) or fmt.lower() not in SUPPORTED_FORMATS:
        raise ValueError(
            f"format must be one of {sorted(SUPPORTED_FORMATS)}, got {fmt!r}"
        )
    fmt = fmt.lower()
    if not isinstance(content, str) or not content:
        raise ValueError("content must be a non-empty string of markdown")
    if title is not None and not isinstance(title, str):
        raise ValueError("title must be a string when provided")
    if not isinstance(include_audit_footer, bool):
        raise ValueError("include_audit_footer must be a boolean when provided")
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object/dict when provided")
    if not isinstance(backend_name, str) or not backend_name:
        raise ValueError("backend must be a non-empty string when provided")

    # --- artifact_id + audit metadata ---
    artifact_id = str(uuid.uuid4())
    now_iso = datetime.now().isoformat(timespec="seconds")
    audit_meta = {
        "session_id":    context.get("session_id", ""),
        "invocation_id": context.get("invocation_id", ""),
        "artifact_id":   artifact_id,
        "generated":     now_iso,
        "format":        fmt,
        "status":        "Draft",
    }
    # Caller-supplied metadata is appended so it shows under the
    # universal fields without overwriting them
    for k, v in metadata.items():
        if k not in audit_meta:
            audit_meta[k] = v

    # --- sanitize filename ---
    safe_filename = _sanitize_filename(filename, fmt)

    # --- render ---
    body_bytes = _render(
        fmt=fmt, content=content, title=title,
        footer=include_audit_footer, audit_meta=audit_meta,
    )

    # --- resolve backend ---
    # The artifacts_root for this session is provided by the runtime via
    # context. Falls back to logs/orphan_artifacts/ if context didn't
    # supply one (should not happen in normal flow).
    artifacts_root = Path(context.get(
        "artifacts_root", "logs/orphan_artifacts"
    ))
    backend = get_backend(backend_name, artifacts_root)
    if backend is None:
        raise ValueError(
            f"backend {backend_name!r} is not registered. "
            f"Available in this build: local_filesystem"
        )

    save_result = backend.save(
        content=body_bytes,
        filename=safe_filename,
        mime_type=MIME_TYPES[fmt],
        metadata=audit_meta,
    )

    # --- assemble SkillResult body ---
    # The runtime adds invocation_id/session_id/status/etc.; we add the
    # artifact-specific fields. save_result already includes path,
    # sha256, size_bytes, backend, mime_type.
    body: Dict[str, Any] = {
        "artifact_id":  artifact_id,
        "filename":     safe_filename,
        "format":       fmt,
        "title":        title,
        "audit_footer_included": include_audit_footer,
    }
    body.update(save_result)
    return body
