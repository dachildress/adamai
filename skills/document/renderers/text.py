"""
Plain text renderer.

Strips markdown formatting to produce a readable .txt. We don't aim for
perfect markdown-to-text conversion -- we aim for "readable when opened
in Notepad / TextEdit." Headers become underlined sections, bullets
keep their - prefix, emphasis markers are stripped, code blocks are
preserved with indentation.
"""

import re
from typing import Any, Dict, Optional


def _strip_inline(line: str) -> str:
    """Strip simple inline markdown: bold, italic, code spans."""
    # Bold (**x** and __x__)
    line = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)
    line = re.sub(r"__([^_]+)__",     r"\1", line)
    # Italic (*x* and _x_) - avoid stripping inside words
    line = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"\1", line)
    line = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)",   r"\1", line)
    # Inline code `x`
    line = re.sub(r"`([^`]+)`", r"\1", line)
    # Markdown links [text](url) -> "text (url)"
    line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", line)
    return line


def render(
    content:              str,
    title:                Optional[str]            = None,
    include_audit_footer: bool                     = True,
    audit_metadata:       Optional[Dict[str, Any]] = None,
) -> bytes:
    """Render markdown content as plain text."""
    lines = content.splitlines()
    out: list = []

    if title:
        out.append(title)
        out.append("=" * len(title))
        out.append("")

    in_code_block = False
    for line in lines:
        # Code fence toggles
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            out.append("")  # blank line as visual separator
            continue
        if in_code_block:
            out.append("    " + line)
            continue

        # Headers -> underlined
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            text = _strip_inline(m.group(2).strip())
            out.append("")
            out.append(text)
            # Use === for H1, --- for H2, blank for deeper
            if level == 1:
                out.append("=" * len(text))
            elif level == 2:
                out.append("-" * len(text))
            out.append("")
            continue

        # Strip horizontal rules
        if re.match(r"^[-*_]{3,}\s*$", line):
            out.append("")
            out.append("-" * 40)
            out.append("")
            continue

        out.append(_strip_inline(line))

    if include_audit_footer and audit_metadata:
        out.append("")
        out.append("-" * 40)
        out.append("AUDIT METADATA")
        for k, v in audit_metadata.items():
            out.append(f"  {k}: {v}")

    return ("\n".join(out) + "\n").encode("utf-8")
