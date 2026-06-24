---
name: document
description: Render and save documents in markdown, text, HTML, and Word formats with optional audit footer.
version: "1.0"

adam:
  source_type: native_adam
  category: executable
  risk_level: low
  audit_required: true
  llm_access: false
  allowed_callers:
    - Operator
  actions:
    create:
      description: Render the provided markdown content into the requested format and save it via the configured storage backend. Returns artifact_id, path, sha256, and size_bytes for audit lineage.
      required_args:
        - filename
        - format
        - content
      optional_args:
        - title
        - include_audit_footer
        - metadata
        - backend
      supported_formats:
        - md
        - txt
        - html
        - docx
---

# document skill

**Purpose:** Produce durable document artifacts (.md, .txt, .html, .docx) from
markdown content authored during deliberation. Renders content via the requested
format and saves it via a configured StorageBackend. v1 supports local filesystem
only; future versions add Google Drive, OneDrive, and SharePoint backends without
changing this skill's invocation contract.

**Currently-implemented formats in this build:** `md`, `txt`, `html`, `docx`.

**Format guidance:**
- `md` — most editable; ideal when the output will be revised further
- `txt` — most portable; opens cleanly everywhere
- `html` — most styled for browser viewing; good for review and sharing
- `docx` — board-packet quality; cover page, page numbers, styled headings,
  shaded table headers, document properties populated. Best choice when
  the artifact will be printed, distributed in a board management system,
  or imported into Word for further editing.

**When to use:** When you've drafted a closing artifact, board memo, policy
draft, or other document worth preserving and you want a real file the user can
open, edit, share, or attach to a board packet. Prefer this over producing prose
in the transcript when the deliverable is a standalone document.

**When NOT to use:** Mid-deliberation drafts still being negotiated. The
artifact you save becomes a session record — only finalize once you have
ratified the content.

## Action: create

Renders the provided markdown content into the requested format and saves it.

### Required args

- `filename` (string): Base filename. Extension is added automatically based on
  `format`. Sanitized for path traversal, special characters, and Windows
  reserved names. Maximum 200 characters.
- `format` (string): One of `md`, `txt`, `html`, `docx`.
- `content` (string): Document content authored as markdown. Headers, lists,
  tables, emphasis, and code blocks render appropriately for the chosen format.

### Optional args

- `title` (string): Document title. Used as the first heading in `md`/`txt`,
  the `<title>` element in `html`, and the document title property in `docx`.
- `include_audit_footer` (boolean, default `true`): Append a footer with
  session_id, invocation_id, artifact_id, and generation timestamp. Set to
  `false` for board-facing finals where the footer would be inappropriate.
- `metadata` (object): Free-form metadata recorded in the audit trail and
  embedded in `docx` document properties when supported.
- `backend` (string, default `"local_filesystem"`): Storage backend name.
  v1 supports `local_filesystem` only.

### Returns

The `SkillResult` body includes:

- `artifact_id` (UUID): Stable identifier for this artifact across the session
- `path` (string): Full path to the saved file
- `sha256` (string): Hash of the rendered file bytes
- `size_bytes` (int): File size on disk
- `format` (string): Echo of the format produced
- `backend` (string): Which storage backend was used

### Example invocation

```skill_call
{
  "skill_calls": [
    {
      "skill": "document",
      "action": "create",
      "args": {
        "filename": "ACPS_AI_Strategic_Plan_Draft",
        "format": "md",
        "title": "ACPS AI Strategic Plan — Draft",
        "content": "# Executive Summary\n\nThe Amherst County Public Schools AI Strategic Plan proposes a phased approach..."
      }
    }
  ]
}
```

### Notes

- Files are saved to `logs/adam_<stamp>.artifacts/<filename>.<ext>` (per-session
  artifact directory).
- The audit footer is rendered in a style appropriate to the format (HTML hr +
  paragraph, Markdown horizontal rule, plain-text dashes, docx styled footer).
- Filename collisions are resolved by appending `-2`, `-3`, etc. — the artifact
  is never silently overwritten.
- Documents are sanitized for safe filesystem paths but the markdown content is
  rendered verbatim (no HTML escaping in `html` format since you authored it).
