---
name: coder
description: Create governed code workspaces, file packages, and patch drafts from an approved ADAM build prompt.
version: "1.0.2"

adam:
  source_type: native_adam
  category: executable
  risk_level: medium
  audit_required: true
  llm_access: false
  external_network_access: false
  write_access: true
  repo_write_access: false
  shell_access: false
  human_approval_required: false
  allowed_callers:
    - Operator
  actions:
    plan:
      description: Persist an approved implementation plan and build prompt as a reviewable code planning artifact.
      required_args:
        - task_title
        - build_prompt
      optional_args:
        - plan
        - target_language
        - constraints
        - metadata
    create_files:
      description: Create a governed code workspace containing raw source files supplied by Operator. Writes only inside the session artifact workspace.
      required_args:
        - task_title
        - build_prompt
        - files
      optional_args:
        - target_language
        - constraints
        - metadata
        - plan
    create_patch:
      description: Persist a reviewable patch/diff file supplied by Operator. Does not apply the patch.
      required_args:
        - task_title
        - build_prompt
        - patch
      optional_args:
        - base_path
        - target_language
        - constraints
        - metadata
        - plan
---

# coder skill

## Purpose

`coder` gives ADAM a controlled way to turn an approved build prompt into reviewable code artifacts.

The agents should work out the engineering prompt first. Operator then invokes this skill with the final build prompt, proposed files, or patch content.

This skill **does not call an LLM**, **does not execute code**, **does not run shell commands**, and **does not modify the live repository**. It writes only to a governed artifact workspace so a human can review the output before applying it.

## Design Rule

Agents design. Operator invokes. Coder writes only to a controlled workspace. A human decides whether to copy or apply the generated files.

## Raw Source Code Rule

For `coder.create_files`, the `content` field must contain the exact raw file content to write.

If creating an HTML app, the file content must begin with actual raw HTML such as:

```html
<!DOCTYPE html>
<html lang="en">
```

Do **not** wrap the code in another HTML document.

Do **not** escape tags as `&lt;`, `&gt;`, `&lt;html&gt;`, `&lt;script&gt;`, or similar.

Do **not** convert source code into prose, Markdown, a rendered document, an HTML preview, or a documentation page unless the user specifically asked for documentation.

The content supplied for each file should be exactly what a user would expect to save directly to disk and run, open, import, or edit.

### Correct HTML file content

```json
{
  "path": "timer_app.html",
  "content": "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"UTF-8\">\n  <title>Timer</title>\n</head>\n<body>\n  <h1>Timer</h1>\n  <script>\n    console.log('running');\n  </script>\n</body>\n</html>"
}
```

### Incorrect HTML file content

```json
{
  "path": "timer_app.html",
  "content": "<!doctype html><html><body><p>&lt;!DOCTYPE html&gt; &lt;html&gt; ... &lt;/html&gt;</p></body></html>"
}
```

The incorrect example creates a web page that displays escaped source code instead of running the app.

## Actions

### `plan`

Persists a build prompt and optional implementation plan as a planning artifact.

Required args:

- `task_title` string: Short task name.
- `build_prompt` string: Final approved engineering prompt.

Optional args:

- `plan` string: Implementation plan or notes.
- `target_language` string.
- `constraints` array of strings.
- `metadata` object.

### `create_files`

Creates a code workspace containing files supplied by Operator.

Required args:

- `task_title` string.
- `build_prompt` string.
- `files` array of objects.

Each file object:

```json
{
  "path": "relative/path/to/file.py",
  "content": "raw file contents here",
  "purpose": "optional explanation"
}
```

Important:

- `path` must be a relative path.
- `content` must be raw source or raw text for that exact file.
- Do not use absolute paths.
- Do not use `../` path traversal.
- Do not include rendered previews unless the preview itself is the intended file.

### `create_patch`

Persists a patch/diff file supplied by Operator. This does not apply the patch.

Required args:

- `task_title` string.
- `build_prompt` string.
- `patch` string.

## Success response shape

The `SkillResult` body includes a top-level `path` field to match artifact behavior used by the document skill.

For `create_files`, `path` points to the primary generated file when there is one. It also returns `workspace_path` for the full code package.

Since v1.0.2 (Part 9.2), the response also includes `relpath` and `workspace_relpath`: session-artifacts-relative paths used by the GUI to construct artifact download URLs. The GUI joins `relpath` onto `/api/sessions/<id>/artifacts/` to produce the link in the artifact card. `path` and `workspace_path` remain absolute filesystem paths for the manifest and audit.

```json
{
  "ok": true,
  "status": "success",
  "skill": "coder",
  "action": "create_files",
  "artifact_id": "code_...",
  "artifact_type": "code_workspace",
  "filename": "timer.html",
  "path": "/abs/.../logs/.../artifacts/coder/<slug>_<id>/timer.html",
  "relpath": "coder/<slug>_<id>/timer.html",
  "workspace_path": "/abs/.../logs/.../artifacts/coder/<slug>_<id>",
  "workspace_relpath": "coder/<slug>_<id>",
  "manifest_path": "/abs/.../logs/.../artifacts/coder/<slug>_<id>/CODE_MANIFEST.json",
  "files_created": [
    {
      "path": "timer.html",
      "absolute_path": "/abs/.../logs/.../artifacts/coder/<slug>_<id>/timer.html",
      "size_bytes": 1234,
      "sha256": "..."
    }
  ]
}
```

## Failure response shape

```json
{
  "ok": false,
  "status": "failed",
  "skill": "coder",
  "action": "create_files",
  "error_class": "invalid_args",
  "error_message": "files must be a non-empty array"
}
```

## Governance

- Risk level: medium
- External network access: false
- Shell access: false
- Live repo write access: false
- Writes only inside the session artifact workspace
- Audit required: true
- Allowed caller: Operator

## What this skill must not do

This skill must not:

- run generated code
- execute shell commands
- install packages
- call Git
- delete files
- edit the live repository
- write outside its artifact workspace
- send email
- access the network
- read secrets or environment variables other than normal artifact context
