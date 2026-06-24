"""
test_echo skill handler.

Implements the SkillHandler protocol expected by SkillRuntime:
    handle(action: str, args: dict, context: dict) -> dict (SkillResult-shaped)

The SkillRuntime takes care of validation, allowed_callers enforcement,
invocation_id assignment, error wrapping, and audit recording. The
handler just produces the operation-specific result body.
"""

from typing import Any, Dict


def handle(action: str, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pass 1 test handler. Returns the echoed payload as the result body.

    The SkillRuntime wraps this return value into a full SkillResult
    dict (adding invocation_id, status, timestamps, audit fields).
    Raising an exception here causes the runtime to produce a
    status='failed' SkillResult with the exception class/message.
    """
    if action != "echo":
        # Should not happen -- the runtime checks action against the
        # manifest before calling us -- but defend defensively anyway.
        raise ValueError(f"unsupported action: {action!r}")

    message = args.get("message")
    if not isinstance(message, str) or not message:
        raise ValueError("'message' is required and must be a non-empty string")

    prefix = args.get("prefix")
    if prefix is not None and not isinstance(prefix, str):
        raise ValueError("'prefix' must be a string when provided")

    echoed = f"{prefix}: {message}" if prefix else message

    return {
        "echoed":     echoed,
        "raw_message": message,
        "prefix_used": prefix,
        # The runtime adds invocation_id, status, etc. on top of this.
    }
