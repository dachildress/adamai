# ADAM coder skill v1.0.1

This package contains the updated governed ADAM `coder` skill.

## What changed from v1.0.0

- Added a top-level `path` field so the GUI can discover coder artifacts the same way it discovers document artifacts.
- Added `filename`.
- Added `artifact_type`.
- Added `absolute_path` inside `files_created`.
- Added escaped HTML detection for `.html` and `.htm` files.
- Updated `SKILL.md` with the raw source code rule.

## Install

Copy the `skills/coder/` folder into your ADAM repo:

```bash
cp -R skills/coder /path/to/adam/skills/
```

If your `runtime.json` uses an explicit enabled list, confirm `coder` is enabled.

## Expected GUI behavior

For `coder.create_files`, the result now includes:

```json
{
  "artifact_id": "code_...",
  "artifact_type": "code_workspace",
  "filename": "timer.html",
  "path": "/full/path/to/timer.html",
  "workspace_path": "/full/path/to/workspace",
  "manifest_path": "/full/path/to/CODE_MANIFEST.json"
}
```

If the GUI already recognizes top-level `path`, it should now have enough data to show the primary generated file.

## Safety design

The skill still does not:

- call an LLM
- execute code
- run shell commands
- install packages
- call Git
- modify the live repo
- access the network
