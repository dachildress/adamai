"""
Shared exception types for ADAM.

These are general-purpose exceptions used across multiple subsystems
(runtime, verifier, context, skills). Living in adam.core.exceptions
gives every subpackage a stable import path without depending on
adam_agent_chat.py.
"""
from __future__ import annotations


class ConfigError(Exception):
    """
    Raised during config validation when something is wrong with
    providers.json, models.json, agents.json, runtime.json, the env
    file, CLI args, or context file enumeration. The runtime's main()
    catches this near the top of startup and calls fatal() with the
    error message, producing a clean exit-code-1 termination.
    """


class ContextLoadAborted(Exception):
    """
    Raised by adam.context.budget_manager.load_context_block when the
    operator aborts the context-load assessment (or when running
    non-interactively without --yes-context-risk and the budget is
    above the warning threshold).

    The runtime catches this near the top of main() and calls fatal()
    with the message, same as ConfigError. Keeping it a separate
    exception lets the audit log distinguish "bad config" from
    "operator declined to proceed" without string-matching the message.
    """
