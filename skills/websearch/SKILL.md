---
name: websearch
description: Search the public web through ADAM's configured self-hosted SearXNG instance for current or external candidate evidence.
version: 1.1.0
adam:
  risk_level: medium_low
  external_network_access: true
  write_access: false
  audit_required: true
  human_approval_required: false
  truthseeker_followup_required: true
  allowed_callers: [Operator, Seeker]
  actions:
    search:
      required_args:
        - query
      optional_args:
        - top_n
        - safe_search
        - language
        - category
---

# Websearch Skill

## Purpose

`websearch` allows authorized ADAM agents to search the public web through the configured SearXNG instance when the user explicitly asks for current information, or when uploaded artifacts, local context, and session state are not enough.

This skill returns candidate public sources only. It does not verify claims. Truthseeker remains responsible for verification of factual claims made from search results.

## Design Rules

### Artifact Preference

Always prefer uploaded context documents, session state, and local artifacts first. Use `websearch` only when required external information is missing.

### Candidate Sources Only

Search results are not verified facts. Any factual claim based on search results must still be eligible for Truthseeker verification before final output.

### Strictly Read-Only

This skill does not:

- fetch full page bodies
- execute JavaScript
- download files
- submit forms
- authenticate to websites
- follow non-HTTP/HTTPS protocols
- write files
- send email
- create documents
- create artifacts
- perform any external write action

## Action: `search`

Searches the public web using SearXNG.

### Arguments

- `query` string, required: Search query. Must be non-empty.
- `top_n` integer, optional: Number of results to return. Defaults to 5. Clamped to 1 through 10.
- `safe_search` integer, optional: SearXNG safe search level. Defaults to 1. Clamped to 0 through 2.
- `language` string, optional: Language filter, such as `en` or `en-US`.
- `category` string, optional: SearXNG category filter.

## Example Call

```json
{
  "skill": "websearch",
  "action": "search",
  "args": {
    "query": "Virginia public school student retention law",
    "top_n": 5
  }
}
```

## Success Response Shape

```json
{
  "ok": true,
  "status": "success",
  "skill": "websearch",
  "action": "search",
  "query": "Virginia public school student retention law",
  "results": [
    {
      "title": "...",
      "url": "https://...",
      "domain": "example.gov",
      "snippet": "...",
      "source_tier": "TIER_1",
      "source_type": "government"
    }
  ],
  "audit_meta": {
    "io_operation": "external_network_read",
    "endpoint_host": "localhost:8080",
    "results_returned": 5,
    "write_access_asserted": false
  },
  "note": "Search results are candidate sources, not verified claims. Claims based on these results should still be checked by Truthseeker."
}
```

## Failure Response Shape

```json
{
  "ok": false,
  "status": "failed",
  "skill": "websearch",
  "action": "search",
  "error_class": "missing_required_args",
  "error_message": "The 'query' argument is required and must be a non-empty string."
}
```

## Governance

- Risk level: medium_low
- External network access: true
- Write access: false
- Audit required: true
- Human approval required: false
- Truthseeker follow-up required: true when search results are used to support factual claims
- Allowed callers: Operator, Seeker
