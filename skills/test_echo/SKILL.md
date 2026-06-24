---
name: test_echo
description: Test skill used to verify the SkillRuntime end-to-end by echoing input back.
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
    echo:
      description: Echoes the provided 'message' value verbatim as part of the SkillResult.
      required_args:
        - message
      optional_args:
        - prefix
---

# test_echo skill

**Purpose:** Test skill used to verify that the SkillRuntime is wired up
correctly. Echoes the provided message back. Has no real-world side effects.

**When to use:** Only when explicitly asked to test the skill plumbing.
Do not use this skill in normal deliberation work.

## Actions

### echo

Echoes the provided message back as a SkillResult.

**Required args:**
- `message` (string) — the text to echo.

**Optional args:**
- `prefix` (string) — if provided, the echoed text is `<prefix>: <message>`.

**Example invocation:**

```skill_call
{
  "skill_calls": [
    {
      "skill": "test_echo",
      "action": "echo",
      "args": {
        "message": "ADAM skill runtime is working",
        "prefix": "echo"
      }
    }
  ]
}
```
