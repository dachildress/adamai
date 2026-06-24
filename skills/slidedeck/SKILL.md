---
name: slidedeck
description: Create governed PowerPoint slide decks from ADAM-approved content, uploaded artifacts, or structured slide plans.
version: 1.0.0
adam:
  risk_level: medium
  external_network_access: false
  write_access: true
  audit_required: true
  human_approval_required: false
  truthseeker_followup_required: true
  allowed_callers: [Operator]
  actions:
    create:
      required_args:
        - title
      optional_args:
        - subtitle
        - content
        - slides
        - output_filename
        - theme
        - aspect_ratio
        - footer
        - author
        - include_agenda
        - include_section_dividers
        - max_slides
---

# Slidedeck Skill

## Purpose

`slidedeck` allows ADAM to create a PowerPoint `.pptx` presentation from structured slide content or a simple markdown-style outline.

This skill is intended for governed real-world artifact creation, similar to the document skill. It creates a local slide deck file and returns the artifact path to ADAM.

## Design Rules

### Governed Artifact Creation

Use this skill only after ADAM has completed enough deliberation to produce a presentation-worthy artifact. The skill should not be used to bypass Truthseeker, Sentinel, or human review.

### Candidate Claims and Verification

This skill creates slides. It does not verify factual claims. Any factual claim used in the deck should already be supported by uploaded context, Truthseeker findings, or be clearly caveated.

### Local File Write Only

This skill writes a `.pptx` file to the local artifact/output directory. It does not send email, upload to cloud storage, publish externally, fetch web content, or call an external service.

### Prefer Visual Simplicity

The handler creates clean, readable presentation decks. Agents should keep slide text brief and use speaker notes or document artifacts for long explanations.

## Action: `create`

Creates a PowerPoint slide deck.

### Arguments

- `title` string, required: Deck title.
- `subtitle` string, optional: Deck subtitle for the title slide.
- `slides` array, optional: Structured slide definitions.
- `content` string, optional: Markdown-style outline to convert into slides when `slides` is not provided.
- `output_filename` string, optional: Output filename. Defaults to a safe name based on the title.
- `theme` string, optional: One of `governance`, `light`, `dark`, or `simple`. Defaults to `governance`.
- `aspect_ratio` string, optional: `wide` or `standard`. Defaults to `wide`.
- `footer` string, optional: Footer text.
- `author` string, optional: Stored in presentation metadata.
- `include_agenda` boolean, optional: Adds an agenda slide when true. Defaults to false.
- `include_section_dividers` boolean, optional: Adds divider slides for slides with `layout: section`. Defaults to true.
- `max_slides` integer, optional: Clamp generated slides. Defaults to 40, maximum 80.

### Structured Slide Format

Each slide may include:

```json
{
  "title": "Slide title",
  "subtitle": "Optional subtitle",
  "layout": "title | bullets | two_column | quote | section | closing",
  "bullets": ["Point one", "Point two"],
  "left_title": "Left side",
  "left_bullets": ["Left point"],
  "right_title": "Right side",
  "right_bullets": ["Right point"],
  "quote": "Short quote or key message",
  "speaker_notes": "Private speaker notes"
}
```

### Example Call

```json
{
  "skill": "slidedeck",
  "action": "create",
  "args": {
    "title": "ADAM Governance Overview",
    "subtitle": "How ADAM reasons, verifies, governs, and acts",
    "theme": "governance",
    "footer": "ADAM Governance Core",
    "slides": [
      {
        "title": "What ADAM Is",
        "layout": "bullets",
        "bullets": [
          "A governed decision system",
          "Uses specialist agents and policy gates",
          "Creates auditable real-world artifacts"
        ]
      },
      {
        "title": "Governance Flow",
        "layout": "two_column",
        "left_title": "Before action",
        "left_bullets": ["Deliberation", "Verification", "Risk review"],
        "right_title": "After approval",
        "right_bullets": ["Create artifact", "Log action", "Preserve audit trail"]
      }
    ]
  }
}
```

## Success Response Shape

```json
{
  "ok": true,
  "status": "success",
  "skill": "slidedeck",
  "action": "create",
  "artifact_id": "SLIDEDECK-20260525-012345",
  "path": "logs/session/artifacts/adam_governance_overview.pptx",
  "filename": "adam_governance_overview.pptx",
  "slide_count": 3,
  "format": "pptx",
  "audit_meta": {
    "io_operation": "local_file_write",
    "write_access_asserted": true,
    "external_network_access_asserted": false
  }
}
```

## Failure Response Shape

```json
{
  "ok": false,
  "status": "failed",
  "skill": "slidedeck",
  "action": "create",
  "error_class": "missing_required_args",
  "error_message": "The 'title' argument is required and must be a non-empty string."
}
```

## Governance

- Risk level: medium
- External network access: false
- Write access: true
- Audit required: true
- Human approval required: false by default, unless Sentinel or policy requires it
- Truthseeker follow-up required for factual claims used in final slides
- Allowed callers: Operator
