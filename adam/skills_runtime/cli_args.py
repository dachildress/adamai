"""
Skill-argument CLI helpers: parsing, validation, redaction, display.

The --skill-arg CLI flag lets the Director suggest specific argument
values for skill invocations without forcing them: --skill-arg
email.send.to=alice@example.com tells the system "if Operator decides
to send an email this session, alice is the intended recipient."

Critical architectural invariant: skill args are SUGGESTIONS, not
commands. The presence of --skill-arg email.send.to=... does not
cause an email to be sent. Only Operator, via a fenced skill_call
block, can invoke a skill. Skill args are made available; they are
not executed.

The output of parse_skill_args is a nested dict shaped as
{skill: {action: {arg: value}}} that the SkillRuntime makes available
to handlers via context["requested_skill_args"]. Handlers consult
these only if their skill semantics call for it.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from adam.core.exceptions import ConfigError


# Identifier pattern for skill, action, and arg names. Conservative on
# purpose: simple ASCII identifier characters plus hyphen, no leading
# digit, no special chars. Skill folders use the same pattern in practice.
_SKILL_ARG_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")

# Patterns that suggest a value is a secret. Used to redact display
# only; values are still stored verbatim in session_state for handler
# access (since some skills may legitimately need credentials). The
# redaction is for the startup banner, the audit log display string,
# and the Operator note -- the underlying value is preserved for any
# skill that actually needs it via context.
_SECRET_HINT_PATTERN = re.compile(
    r"(password|token|secret|api[_-]?key|credential|auth|bearer)",
    re.IGNORECASE,
)

# Max characters to display for any single skill-arg value in banners,
# logs, and Operator notes. Long values get truncated with "..." so
# transcripts stay readable.
_SKILL_ARG_DISPLAY_MAX_CHARS = 120


def _parse_one_skill_arg(raw: str) -> Tuple[str, str, str, str]:
    """
    Parse a single --skill-arg value of the form skill.action.arg=value.
    Returns (skill, action, arg, value). Raises ConfigError on any
    malformed input with a message that names the offending value so
    the operator can fix the CLI invocation.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(
            "--skill-arg requires a non-empty value of the form "
            "skill.action.arg=value"
        )

    if "=" not in raw:
        raise ConfigError(
            f"Invalid --skill-arg {raw!r}. Expected format "
            f"skill.action.arg=value (no '=' found)"
        )

    key_part, _, value = raw.partition("=")
    value = value.strip()
    if not value:
        raise ConfigError(
            f"Invalid --skill-arg {raw!r}. Value is empty. If you "
            f"intended an empty string, omit the flag entirely."
        )

    parts = key_part.split(".")
    if len(parts) != 3:
        raise ConfigError(
            f"Invalid --skill-arg {raw!r}. Expected exactly three "
            f"dot-separated components on the left of '=', got "
            f"{len(parts)} (key: {key_part!r}). Format: "
            f"skill.action.arg=value"
        )

    skill, action, arg = parts
    for name, label in ((skill, "skill"), (action, "action"), (arg, "arg")):
        if not name:
            raise ConfigError(
                f"Invalid --skill-arg {raw!r}. The '{label}' component "
                f"is empty. Format: skill.action.arg=value"
            )
        if not _SKILL_ARG_IDENT_RE.match(name):
            raise ConfigError(
                f"Invalid --skill-arg {raw!r}. The '{label}' component "
                f"{name!r} contains characters that aren't allowed. "
                f"Identifiers must start with a letter or underscore "
                f"and contain only letters, digits, hyphens, and "
                f"underscores."
            )

    return skill, action, arg, value


def parse_skill_args(
    raw_args:    Optional[List[str]],
    audit_fn:    Optional[Callable] = None,
) -> Dict[str, Dict[str, Dict[str, str]]]:
    """
    Parse the list of raw --skill-arg values into a nested dict:
        {skill: {action: {arg: value, ...}, ...}, ...}

    Each raw value is validated via _parse_one_skill_arg.

    Duplicate (skill, action, arg) tuples are last-write-wins, with the
    overridden value logged to audit_fn if provided. The function emits
    no stdout/stderr output directly -- the caller decides what to
    surface in the startup banner.
    """
    parsed: Dict[str, Dict[str, Dict[str, str]]] = {}
    overrides: List[Tuple[str, str, str, str, str]] = []  # for audit

    if not raw_args:
        return parsed

    for raw in raw_args:
        skill, action, arg, value = _parse_one_skill_arg(raw)
        skill_block = parsed.setdefault(skill, {})
        action_block = skill_block.setdefault(action, {})
        if arg in action_block and action_block[arg] != value:
            overrides.append((skill, action, arg, action_block[arg], value))
        action_block[arg] = value

    if overrides and audit_fn is not None:
        for skill, action, arg, prior, new in overrides:
            audit_fn({
                "kind":          "skill_arg_override",
                "skill":         skill,
                "action":        action,
                "arg":           arg,
                "previous":      _redact_skill_arg_value(arg, prior),
                "current":       _redact_skill_arg_value(arg, new),
                "ts":            datetime.now().isoformat(timespec='seconds'),
            })

    return parsed


def _redact_skill_arg_value(arg_name: str, value: str) -> str:
    """
    Return a display-safe rendering of a skill-arg value. If the arg
    name suggests a secret (password, token, secret, key, credential,
    auth, bearer), return a redacted placeholder. Otherwise truncate
    long values for readability.

    The underlying stored value is NOT modified -- this function is
    used only for display (banner, audit log entries, Operator note).
    Skill handlers that legitimately need the secret value will still
    receive the verbatim string via context["requested_skill_args"].
    """
    if _SECRET_HINT_PATTERN.search(arg_name):
        return "<redacted: arg name suggests secret>"
    if len(value) > _SKILL_ARG_DISPLAY_MAX_CHARS:
        return value[:_SKILL_ARG_DISPLAY_MAX_CHARS] + "..."
    return value


def format_skill_args_for_display(
    parsed: Dict[str, Dict[str, Dict[str, str]]],
) -> List[str]:
    """
    Render a parsed skill_args dict as a list of display lines suitable
    for the startup banner or the Operator note. One line per leaf value.
    """
    lines: List[str] = []
    for skill in sorted(parsed.keys()):
        for action in sorted(parsed[skill].keys()):
            for arg in sorted(parsed[skill][action].keys()):
                raw_value = parsed[skill][action][arg]
                display_value = _redact_skill_arg_value(arg, raw_value)
                lines.append(f"  - {skill}.{action}.{arg} = {display_value}")
    return lines


def build_operator_skill_args_note(
    parsed: Dict[str, Dict[str, Dict[str, str]]],
) -> str:
    """
    Build the Operator-facing note describing CLI-provided skill args.
    Empty string when no args were provided. The note's wording is
    intentionally cautious about the suggestion-not-command rule
    because the human bias is to treat CLI args as imperative.
    """
    if not parsed:
        return ""

    lines: List[str] = []
    lines.append("DIRECTOR-PROVIDED SKILL ARGS (suggestions, not commands):")
    lines.append("")
    lines.extend(format_skill_args_for_display(parsed))
    lines.append("")
    lines.append("Interpretation rules:")
    lines.append("  - These are values the Director made available for the "
                 "named (skill, action, arg) triples. They are NOT commands "
                 "to invoke any skill.")
    lines.append("  - Whether to invoke a skill at all remains your decision "
                 "based on the deliberation outcome, the wrap-up plan, and "
                 "the artifact strategy.")
    lines.append("  - If you do invoke a listed (skill, action), prefer the "
                 "provided arg value over anything inferred from the "
                 "transcript. The Director's CLI value is authoritative.")
    lines.append("  - If a skill arg name suggests a secret (password, token, "
                 "key, credential), its value is redacted in this note but is "
                 "available to the skill handler via context.")
    lines.append("  - Do not invent missing values. If a skill needs args you "
                 "weren't given, either skip the skill or note the gap in your "
                 "wrap_up.open_questions.")
    return "\n".join(lines)
