"""
HTML renderer.

Markdown -> HTML using a small hand-rolled converter. We don't pull in
markdown2 / mistletoe / cmark as a dependency for v1; the conversion
covers headers, lists, emphasis, code blocks, links, tables, and
horizontal rules, which is enough for the kind of document content
Operator produces.

If richer HTML output is needed later (footnotes, definition lists,
math), swap in a real markdown library; this renderer can be retired
without changing the skill's external contract.
"""

import html as html_escape_module
import re
from typing import Any, Dict, List, Optional


def _escape(s: str) -> str:
    return html_escape_module.escape(s, quote=False)


def _render_inline(text: str) -> str:
    """Inline markdown -> inline HTML. Run on a single line of text."""
    text = _escape(text)
    # Inline code first so its content isn't processed further
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # Bold (**x** and __x__)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__([^_]+)__",     r"<strong>\1</strong>", text)
    # Italic (*x* / _x_) - avoid mid-word matches
    text = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"<em>\1</em>", text)
    text = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)",   r"<em>\1</em>", text)
    # Links [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


def _md_to_html_body(content: str) -> str:
    """Convert markdown body to HTML body fragment."""
    lines = content.splitlines()
    out: List[str] = []

    i = 0
    in_para: List[str] = []

    def flush_para() -> None:
        if in_para:
            joined = " ".join(_render_inline(l) for l in in_para)
            out.append(f"<p>{joined}</p>")
            in_para.clear()

    while i < len(lines):
        line = lines[i]

        # Code fence
        if line.strip().startswith("```"):
            flush_para()
            lang = line.strip()[3:].strip()
            code_lines: List[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            code_class = f' class="language-{_escape(lang)}"' if lang else ""
            out.append(f"<pre><code{code_class}>" +
                       _escape("\n".join(code_lines)) +
                       "</code></pre>")
            i += 1
            continue

        # Header
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush_para()
            level = len(m.group(1))
            text = _render_inline(m.group(2).strip())
            out.append(f"<h{level}>{text}</h{level}>")
            i += 1
            continue

        # Horizontal rule
        if re.match(r"^[-*_]{3,}\s*$", line):
            flush_para()
            out.append("<hr/>")
            i += 1
            continue

        # Unordered list
        if re.match(r"^\s*[-*+]\s+", line):
            flush_para()
            list_items: List[str] = []
            while i < len(lines) and re.match(r"^\s*[-*+]\s+", lines[i]):
                item_text = re.sub(r"^\s*[-*+]\s+", "", lines[i])
                list_items.append(f"<li>{_render_inline(item_text)}</li>")
                i += 1
            out.append("<ul>")
            out.extend(list_items)
            out.append("</ul>")
            continue

        # Ordered list
        if re.match(r"^\s*\d+\.\s+", line):
            flush_para()
            list_items = []
            while i < len(lines) and re.match(r"^\s*\d+\.\s+", lines[i]):
                item_text = re.sub(r"^\s*\d+\.\s+", "", lines[i])
                list_items.append(f"<li>{_render_inline(item_text)}</li>")
                i += 1
            out.append("<ol>")
            out.extend(list_items)
            out.append("</ol>")
            continue

        # Table: pipe-style with header separator row
        if "|" in line and i + 1 < len(lines) and re.match(r"^\s*\|?[-:\s|]+\|?\s*$", lines[i + 1]):
            flush_para()
            header_cells = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2  # skip the separator
            body_rows: List[List[str]] = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                row = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                body_rows.append(row)
                i += 1
            out.append("<table>")
            out.append("<thead><tr>" +
                       "".join(f"<th>{_render_inline(h)}</th>" for h in header_cells) +
                       "</tr></thead>")
            out.append("<tbody>")
            for row in body_rows:
                out.append("<tr>" +
                           "".join(f"<td>{_render_inline(c)}</td>" for c in row) +
                           "</tr>")
            out.append("</tbody></table>")
            continue

        # Blank line -> paragraph break
        if not line.strip():
            flush_para()
            i += 1
            continue

        # Default: accumulate into paragraph
        in_para.append(line.strip())
        i += 1

    flush_para()
    return "\n".join(out)


def render(
    content:              str,
    title:                Optional[str]            = None,
    include_audit_footer: bool                     = True,
    audit_metadata:       Optional[Dict[str, Any]] = None,
) -> bytes:
    """Render markdown content as a complete HTML document."""
    body_html = _md_to_html_body(content)

    title_text = title or "Document"
    parts = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        f'  <meta charset="utf-8"/>',
        f"  <title>{_escape(title_text)}</title>",
        "  <style>",
        "    body { font-family: -apple-system, Segoe UI, sans-serif; "
        "max-width: 780px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }",
        "    h1, h2, h3 { line-height: 1.25; }",
        "    pre { background: #f5f5f5; padding: 1rem; overflow-x: auto; }",
        "    code { background: #f5f5f5; padding: 0.1em 0.3em; }",
        "    pre code { padding: 0; background: transparent; }",
        "    table { border-collapse: collapse; width: 100%; }",
        "    th, td { border: 1px solid #ccc; padding: 0.4em 0.6em; text-align: left; }",
        "    th { background: #f5f5f5; }",
        "    .audit-footer { margin-top: 3rem; padding-top: 1rem; "
        "border-top: 1px solid #ccc; color: #666; font-size: 0.85em; }",
        "    .audit-footer dl { display: grid; grid-template-columns: max-content 1fr; "
        "gap: 0.25rem 1rem; }",
        "  </style>",
        "</head>",
        "<body>",
    ]
    if title:
        parts.append(f"<h1>{_escape(title)}</h1>")
    parts.append(body_html)

    if include_audit_footer and audit_metadata:
        parts.append('<div class="audit-footer">')
        parts.append("<p><strong>Audit metadata</strong></p>")
        parts.append("<dl>")
        for k, v in audit_metadata.items():
            parts.append(f"<dt>{_escape(str(k))}</dt><dd>{_escape(str(v))}</dd>")
        parts.append("</dl>")
        parts.append("</div>")

    parts.append("</body>")
    parts.append("</html>")
    return ("\n".join(parts) + "\n").encode("utf-8")
