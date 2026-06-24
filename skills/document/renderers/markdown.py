"""
Markdown renderer.

For .md output, the content IS markdown -- we just prepend an optional
title heading and append an optional audit footer. No transformation
beyond that.
"""

from typing import Any, Dict, Optional


def render(
    content:              str,
    title:                Optional[str]      = None,
    include_audit_footer: bool               = True,
    audit_metadata:       Optional[Dict[str, Any]] = None,
) -> bytes:
    """Render markdown content as a complete .md document."""
    parts = []
    if title:
        parts.append(f"# {title}")
        parts.append("")
    parts.append(content.rstrip())
    if include_audit_footer and audit_metadata:
        parts.append("")
        parts.append("---")
        parts.append("")
        parts.append("**Audit metadata**")
        parts.append("")
        for k, v in audit_metadata.items():
            parts.append(f"- **{k}**: {v}")
    return ("\n".join(parts) + "\n").encode("utf-8")
