"""
Skill invocation runtime: parsing, validation, execution, audit.

Two responsibilities:

1. **parse_skill_calls**: scan agent output for fenced ```skill_call
   JSON blocks, extract the list of {skill, action, args} entries, and
   return ParsedSkillCall objects plus a parallel list of parse errors
   (for blocks that look like skill calls but failed validation -- e.g.
   truncated by max_tokens, malformed JSON, missing required keys).

2. **SkillRuntime.process_agent_output**: the per-turn entry point.
   For each ParsedSkillCall: validate against the SkillManifest, enforce
   allowed_callers, call the skill handler, capture the result, emit a
   skills.jsonl record, and return both the structured results and a
   transcript-ready summary string suitable for injection back into
   the deliberation history.

The runtime knows nothing about specific skills (document, email, etc.).
It dispatches by handler-module path declared in each skill.yaml. Adding
a new skill requires zero changes here: drop the manifest into
skills/<name>/, ensure the handler module exposes the documented
entry function, and discover_skills picks it up automatically.

No LLM calls. The runtime only orchestrates; skill handlers may
themselves call models, but that happens inside the handler, not here.
"""
from __future__ import annotations

import importlib
import json
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from adam.skills_runtime._config import _rt_skills
from adam.skills_runtime.manifest import SkillCatalog, SkillManifest


# ----------- skill_call block parsing -----------

# Regex for the fenced skill_call block. Tolerant of leading/trailing
# whitespace inside the fence and of the language tag being upper/lower case.
SKILL_CALL_FENCE_RE = re.compile(
    r"```\s*skill_call\s*\n(.*?)\n```",
    re.IGNORECASE | re.DOTALL,
)

# Regex to detect an OPENING skill_call fence without checking for a close.
# Used after the main parse to catch truncated blocks (response hit max_tokens
# mid-content). When opens > closes we know at least one block was truncated.
SKILL_CALL_OPEN_FENCE_RE = re.compile(
    r"```\s*skill_call\s*\n",
    re.IGNORECASE,
)


class ParsedSkillCall:
    """A single skill_call extracted from agent output."""

    def __init__(self, skill: str, action: str, args: Dict[str, Any]) -> None:
        self.skill  = skill
        self.action = action
        self.args   = args


def parse_skill_calls(agent_output: str) -> Tuple[List[ParsedSkillCall], List[Dict[str, str]]]:
    """
    Find and parse all ```skill_call fenced JSON blocks in agent output.

    Returns (parsed_calls, parse_errors). Each parse_error is a dict with
    'kind', 'detail', and a snippet of the offending block for audit. Any
    block that fails to parse becomes an error entry, not an exception.
    The orchestrator then emits a SkillResult(status=failed) per error so
    the agent gets correction feedback.

    Truncation detection: counts opening fences vs the matched closed
    fences. If opens > closes, at least one block was truncated mid-content
    (typically because the agent's response hit max_tokens before the
    closing fence). Surface that as a parse_error rather than silently
    dropping the truncated block.
    """
    parsed: List[ParsedSkillCall] = []
    errors: List[Dict[str, str]] = []

    closed_matches = list(SKILL_CALL_FENCE_RE.finditer(agent_output))
    open_count = len(SKILL_CALL_OPEN_FENCE_RE.findall(agent_output))
    if open_count > len(closed_matches):
        # At least one opening fence had no matching close. Find the last
        # unmatched opening and capture a snippet for audit.
        unclosed_count = open_count - len(closed_matches)
        # Identify the unclosed fence's start position: it's the open
        # fence position that doesn't sit inside any closed match.
        closed_spans = [(m.start(), m.end()) for m in closed_matches]
        for om in SKILL_CALL_OPEN_FENCE_RE.finditer(agent_output):
            inside_closed = any(s <= om.start() < e for s, e in closed_spans)
            if not inside_closed:
                # Capture up to 200 chars after the open for diagnostic
                snippet = agent_output[om.end():om.end() + 200].strip()
                errors.append({
                    "kind":    "parse_error",
                    "detail":  (
                        f"unclosed skill_call fence: opening ```skill_call "
                        f"found with no matching closing ```. The agent's "
                        f"response likely hit max_tokens before the block "
                        f"closed, truncating the skill call mid-content. "
                        f"Re-emit a shorter skill_call (smaller 'content' "
                        f"arg) or chunk the artifact into multiple smaller "
                        f"creates. {unclosed_count} unclosed fence(s) detected."
                    ),
                    "snippet": snippet,
                })
                break  # one error per response is enough; the message
                       # tells Operator how many were unclosed

    for match in closed_matches:
        body = match.group(1).strip()
        snippet = body[:200]
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            errors.append({
                "kind":    "parse_error",
                "detail":  f"malformed skill_call JSON: {e.msg} (line {e.lineno}, column {e.colno})",
                "snippet": snippet,
            })
            continue

        if not isinstance(payload, dict):
            errors.append({
                "kind":    "parse_error",
                "detail":  "skill_call JSON root must be an object",
                "snippet": snippet,
            })
            continue
        calls_list = payload.get("skill_calls")
        if not isinstance(calls_list, list) or not calls_list:
            errors.append({
                "kind":    "parse_error",
                "detail":  "skill_call JSON must contain a non-empty 'skill_calls' list",
                "snippet": snippet,
            })
            continue

        for idx, call in enumerate(calls_list):
            if not isinstance(call, dict):
                errors.append({
                    "kind":    "parse_error",
                    "detail":  f"skill_calls[{idx}] must be an object",
                    "snippet": snippet,
                })
                continue
            skill_name = call.get("skill")
            action     = call.get("action")
            args       = call.get("args", {})
            if not isinstance(skill_name, str) or not skill_name:
                errors.append({
                    "kind":    "parse_error",
                    "detail":  f"skill_calls[{idx}].skill is required and must be a non-empty string",
                    "snippet": snippet,
                })
                continue
            if not isinstance(action, str) or not action:
                errors.append({
                    "kind":    "parse_error",
                    "detail":  f"skill_calls[{idx}].action is required and must be a non-empty string",
                    "snippet": snippet,
                })
                continue
            if not isinstance(args, dict):
                errors.append({
                    "kind":    "parse_error",
                    "detail":  f"skill_calls[{idx}].args must be an object (use {{}} for none)",
                    "snippet": snippet,
                })
                continue
            parsed.append(ParsedSkillCall(skill=skill_name, action=action, args=args))

    return parsed, errors


# ----------- SkillRuntime: orchestration of invocations -----------

class SkillRuntime:
    """
    Runtime orchestrator for skill invocations.

    Responsibilities:
      - Holds the SkillCatalog (loaded once at startup)
      - For each agent turn, scans the output for skill_call blocks
      - For each call: validates against the manifest, enforces
        allowed_callers, calls the handler, captures the result, logs
        to skills.jsonl, and produces a transcript-ready summary
      - For parse/validation failures: emits a failed SkillResult with
        a clear error_class so the agent can self-correct on the next
        turn
    """

    def __init__(
        self,
        catalog:               SkillCatalog,
        skills_log_path:       Path,
        session_id:            str,
        artifacts_root:        Path,
        requested_skill_args:  Optional[Dict[str, Dict[str, Dict[str, str]]]] = None,
    ) -> None:
        self.catalog          = catalog
        self.skills_log_path  = skills_log_path
        self.session_id       = session_id
        # Per-session directory where artifact-producing skills (e.g. the
        # document skill's local backend) write their outputs. Constructed
        # lazily by the backend if the skill is invoked; safe to pass even
        # when no skill needs it.
        self.artifacts_root   = artifacts_root
        # CLI-provided skill args (the --skill-arg extension point). Made
        # available to handlers via context["requested_skill_args"]. Stays
        # an empty dict when no args were provided. The runtime does NOT
        # automatically apply these to skill_call args -- they're available
        # for handlers to consult only if their skill semantics call for it.
        self.requested_skill_args: Dict[str, Dict[str, Dict[str, str]]] = (
            requested_skill_args or {}
        )
        # Track invocations for session_state assembly
        self.invocations: List[Dict[str, Any]] = []

    def _log(self, record: Dict[str, Any]) -> None:
        """Append one record to skills.jsonl."""
        with open(self.skills_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        self.invocations.append(record)

    def _make_failed_result(
        self,
        agent:          str,
        turn:           int,
        skill:          Optional[str],
        action:         Optional[str],
        error_class:    str,
        error_message:  str,
        snippet:        Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "invocation_id":  str(uuid.uuid4()),
            "session_id":     self.session_id,
            "turn":           turn,
            "caller":         agent,
            "skill":          skill,
            "action":         action,
            "status":         "failed",
            "error_class":    error_class,
            "error_message":  error_message,
            "snippet":        snippet,
            "ts":             datetime.now().isoformat(timespec='seconds'),
        }

    def process_agent_output(
        self,
        agent:          str,
        turn:           int,
        agent_output:   str,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """
        Find all skill_call blocks in agent_output, validate, invoke, and
        produce SkillResults.

        Returns (skill_results, transcript_injection_text). The transcript
        text is an abbreviated summary suitable for showing to subsequent
        agents; full results stay in skills.jsonl. Returns (([], None))
        when the output contains no skill_call blocks.
        """
        if "```skill_call" not in agent_output.lower():
            return [], None

        parsed_calls, parse_errors = parse_skill_calls(agent_output)
        all_results: List[Dict[str, Any]] = []

        # Dual-output advisory: if the agent produced both a large block
        # of prose AND a skill_call, that's a risky pattern -- it usually
        # means the agent is duplicating the artifact content (once in
        # prose, once inside the skill_call's args), which wastes the
        # token budget and can cause the skill_call block itself to
        # truncate. Per the architectural rule: artifact delivery is
        # ONE mode at a time. We don't fail the call; we just record
        # an advisory so the audit log shows the pattern.
        if parsed_calls:  # only check when at least one call parsed successfully
            # Strip the skill_call fence regions from the response so we
            # measure prose outside the fences specifically.
            prose_only = SKILL_CALL_FENCE_RE.sub("", agent_output)
            # Also strip wrap_up fenced blocks since those are expected
            # at the top of Operator's response and aren't "artifact prose"
            prose_only = re.sub(
                r"```\s*wrap_up\s*\n.*?\n```",
                "",
                prose_only,
                flags=re.IGNORECASE | re.DOTALL,
            )
            prose_chars = len(prose_only.strip())
            # Threshold: 1500 chars of prose alongside a skill_call is
            # the heuristic for "duplicated artifact." A short closing
            # sentence ("See <filename> for the full plan") is fine;
            # a full strategic plan in prose plus the same content in
            # the skill_call is what triggered the truncation failure
            # we built this detection to catch.
            DUAL_OUTPUT_PROSE_THRESHOLD = 1500
            if prose_chars > DUAL_OUTPUT_PROSE_THRESHOLD:
                advisory = {
                    "invocation_id":  str(uuid.uuid4()),
                    "session_id":     self.session_id,
                    "turn":           turn,
                    "caller":         agent,
                    "skill":          None,
                    "action":         None,
                    "status":         "advisory",
                    "advisory_class": "dual_output_pattern",
                    "advisory_message": (
                        f"Agent emitted {prose_chars} characters of prose "
                        f"alongside {len(parsed_calls)} skill_call(s). "
                        f"This may indicate duplicated artifact content "
                        f"(once in prose, once inside skill_call args). "
                        f"Risk: token-budget exhaustion can truncate the "
                        f"skill_call mid-string, causing artifact creation "
                        f"to fail silently. Per architectural rule: choose "
                        f"transcript-artifact mode OR file-artifact mode, "
                        f"not both."
                    ),
                    "prose_chars":    prose_chars,
                    "skill_call_count": len(parsed_calls),
                    "ts":             datetime.now().isoformat(timespec='seconds'),
                }
                self._log(advisory)

        # First: emit failures for any parse errors
        for err in parse_errors:
            result = self._make_failed_result(
                agent=agent, turn=turn,
                skill=None, action=None,
                error_class=err["kind"],
                error_message=err["detail"],
                snippet=err.get("snippet"),
            )
            self._log(result)
            all_results.append(result)

        # Then: process parsed calls
        for call in parsed_calls:
            result = self._invoke_one(agent=agent, turn=turn, call=call)
            self._log(result)
            all_results.append(result)

        if not all_results:
            return [], None

        # Build transcript summary
        lines: List[str] = []
        lines.append(f"[Skill_Runtime] {agent} attempted {len(all_results)} "
                     f"skill call(s):")
        for r in all_results:
            tag = "OK" if r["status"] == "success" else "FAIL"
            short_inv = r["invocation_id"][:8]
            if r["status"] == "success":
                lines.append(
                    f"  - [{tag}] {r['skill']}.{r['action']} "
                    f"inv {short_inv}.. "
                    + (f"artifact {r['artifact_id'][:8]}.. " if r.get("artifact_id") else "")
                    + (f"path: {r['path']}" if r.get("path") else "")
                )
            else:
                lines.append(
                    f"  - [{tag}] {r.get('error_class','error')}: "
                    f"{r.get('error_message','(no message)')[:160]}"
                )

        return all_results, "\n".join(lines)

    def _invoke_one(
        self,
        agent: str,
        turn:  int,
        call:  ParsedSkillCall,
    ) -> Dict[str, Any]:
        """Validate and execute a single parsed call. Always returns a SkillResult dict."""
        manifest = self.catalog.get(call.skill)
        if manifest is None:
            # Check whether this is a documentation-only skill (visible
            # in catalog but not invocable) so the failure message can
            # explain exactly why.
            doc_only = self.catalog.documentation_only.get(call.skill)
            if doc_only is not None:
                return self._make_failed_result(
                    agent=agent, turn=turn,
                    skill=call.skill, action=call.action,
                    error_class="documentation_only_skill",
                    error_message=(
                        f"Skill '{call.skill}' is documentation-only and "
                        f"cannot be invoked via skill_call. It either has no "
                        f"handler.py or lacks adam.actions in its frontmatter. "
                        f"Read its SKILL.md for guidance instead."
                    ),
                )
            return self._make_failed_result(
                agent=agent, turn=turn,
                skill=call.skill, action=call.action,
                error_class="unknown_skill",
                error_message=(
                    f"Skill '{call.skill}' is not enabled or not installed. "
                    f"Available executable skills: "
                    f"{', '.join(sorted(self.catalog.executable.keys())) or '(none)'}"
                ),
            )

        # allowed_callers enforcement
        if agent not in manifest.allowed_callers:
            return self._make_failed_result(
                agent=agent, turn=turn,
                skill=call.skill, action=call.action,
                error_class="disallowed_caller",
                error_message=(
                    f"Caller '{agent}' is not in allowed_callers for skill "
                    f"'{call.skill}'. Allowed: {manifest.allowed_callers}"
                ),
            )

        # Action validation
        if call.action not in manifest.actions:
            return self._make_failed_result(
                agent=agent, turn=turn,
                skill=call.skill, action=call.action,
                error_class="disallowed_action",
                error_message=(
                    f"Skill '{call.skill}' does not support action '{call.action}'. "
                    f"Supported actions: {sorted(manifest.actions.keys())}"
                ),
            )

        # Required-args check
        action_spec = manifest.actions[call.action]
        required = list(action_spec.get("required_args", []))
        missing = [a for a in required if a not in call.args]
        if missing:
            return self._make_failed_result(
                agent=agent, turn=turn,
                skill=call.skill, action=call.action,
                error_class="missing_required_args",
                error_message=(
                    f"Required args missing for {call.skill}.{call.action}: "
                    f"{missing}. Required: {required}"
                ),
            )

        # Content-size guard (defensive cap before invoking the handler)
        max_bytes = _rt_skills("max_content_size_bytes")
        for arg_name, arg_val in call.args.items():
            if isinstance(arg_val, str) and len(arg_val.encode("utf-8")) > max_bytes:
                return self._make_failed_result(
                    agent=agent, turn=turn,
                    skill=call.skill, action=call.action,
                    error_class="content_too_large",
                    error_message=(
                        f"Arg '{arg_name}' is {len(arg_val.encode('utf-8'))} bytes; "
                        f"max_content_size_bytes is {max_bytes}. "
                        f"Chunk the content into multiple skill calls."
                    ),
                )

        # Invoke the handler
        handler = self.catalog.get_handler(call.skill)
        if handler is None:
            # Shouldn't happen if catalog is well-formed, but defend
            return self._make_failed_result(
                agent=agent, turn=turn,
                skill=call.skill, action=call.action,
                error_class="handler_missing",
                error_message=f"No handler registered for skill '{call.skill}'",
            )

        invocation_id = str(uuid.uuid4())
        context = {
            "invocation_id":         invocation_id,
            "session_id":            self.session_id,
            "turn":                  turn,
            "caller":                agent,
            "artifacts_root":        str(self.artifacts_root),
            # Generic CLI skill args, passed verbatim for handlers that
            # want to consult pre-supplied parameters. Handlers should
            # still PREFER explicit args from the skill_call block; this
            # is a fallback / suggestion layer, not an override.
            "requested_skill_args":  self.requested_skill_args,
        }
        try:
            body = handler(call.action, call.args, context)
            if not isinstance(body, dict):
                raise TypeError(
                    f"skill handler returned {type(body).__name__}, "
                    f"expected dict (SkillResult body)"
                )
        except Exception as e:
            return {
                "invocation_id":  invocation_id,
                "session_id":     self.session_id,
                "turn":           turn,
                "caller":         agent,
                "skill":          call.skill,
                "action":         call.action,
                "status":         "failed",
                "error_class":    "handler_exception",
                "error_message":  f"{type(e).__name__}: {e}",
                "ts":             datetime.now().isoformat(timespec='seconds'),
            }

        # Success: assemble the full SkillResult around the handler body
        result: Dict[str, Any] = {
            "invocation_id":  invocation_id,
            "session_id":     self.session_id,
            "turn":           turn,
            "caller":         agent,
            "skill":          call.skill,
            "action":         call.action,
            "status":         "success",
            "ts":             datetime.now().isoformat(timespec='seconds'),
        }
        # Merge handler body fields. Handler may include artifact_id,
        # path, sha256, etc. We don't overwrite the protected fields above.
        protected = set(result.keys())
        for k, v in body.items():
            if k in protected:
                continue
            result[k] = v
        return result


