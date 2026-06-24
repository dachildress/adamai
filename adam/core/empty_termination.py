"""
Empty-termination invariant — refusal is a complete terminal state.

When ADAM refuses a requested action (unsafe execution, boundary violation,
or synthesis concludes the action cannot/should not run), the session ends
with ZERO artifacts. Operator does not run and no substitute deliverable is
produced unless the Director explicitly asked for that deliverable in the
original seed.

This complements policy_blocked (profile-driven) and governance_boundary_blocked
(invariant-driven pre-deliberation stops). It closes the substitute-action
reflex: refusing the unsafe thing, then producing an adjacent artifact anyway.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

REFUSAL_TERMINATED_END_REASON = "refusal_terminated"

_EXPLANATION = (
    "The deliberation refused the requested action. ADAM ends with no artifact "
    "unless you explicitly asked for one in your original prompt."
)

# Seed / director unsafe-execution requests (Test 1 + Test 3 class).
_UNSAFE_EXECUTION_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (
        re.compile(
            r"\b(run|execute)\b.{0,50}\b(shell|bash|/bin/sh|cmd\.exe|powershell|"
            r"terminal command|shell command)\b",
            re.I | re.S,
        ),
        "run a shell or terminal command",
    ),
    (
        re.compile(
            r"\b(shell command|subprocess|os\.system|popen|exec\()\b",
            re.I | re.S,
        ),
        "execute arbitrary shell code",
    ),
    (
        re.compile(
            r"\b(rm\s+-rf|sudo\s+|chmod\s+\d|curl\s+[^\n]{0,80}\|\s*sh)\b",
            re.I | re.S,
        ),
        "run a destructive or piped shell command",
    ),
    (
        re.compile(
            r"\b(create|write|save|make)\b.{0,50}\b(file|folder|directory)\b"
            r".{0,40}\barbitrary path\b",
            re.I | re.S,
        ),
        "create a file or folder at an arbitrary path",
    ),
    (
        re.compile(
            r"\b(create|write|save)\b.{0,40}\b(file|folder|directory)\b.{0,30}"
            r"\b(at|to|in)\s+['\"]?(?:/etc/|/usr/|/var/|/root/|/opt/(?!adam)|"
            r"C:\\\\|\\\\Windows\\\\)",
            re.I | re.S,
        ),
        "write outside the governed artifact workspace",
    ),
    (
        re.compile(
            r"\b(outside|beyond)\b.{0,30}\b(the\s+)?(workspace|artifact directory|"
            r"allowed path|governed)\b",
            re.I | re.S,
        ),
        "act outside the governed workspace",
    ),
]

# Synthesis refused the action — Operator must not run (no substitute artifacts).
_REFUSAL_SYNTH_PATTERNS: List[re.Pattern] = [
    re.compile(
        r"\b(will not|won't|must not|cannot|can't|unable to|refuse[sd]?|"
        r"decline[sd]?)\b.{0,60}\b(execute|run|install|perform|create|write|"
        r"grant|send|shell|command|action|request)\b",
        re.I | re.S,
    ),
    re.compile(
        r"\b(requested|planned|proposed)\b.{0,40}\b(action|command|request)\b"
        r".{0,40}\b(refused|declined|denied|blocked|not permitted)\b",
        re.I | re.S,
    ),
    re.compile(
        r"\b(action|request|command)\b.{0,30}\b(refused|declined|denied|blocked)\b",
        re.I | re.S,
    ),
    re.compile(
        r"\b(operator|adam)\b.{0,40}\b(will not|does not|must not|cannot)\b"
        r".{0,50}\b(run|execute|produce|create|install|send|write)\b",
        re.I | re.S,
    ),
    re.compile(
        r"\bno\b.{0,30}\b(unauthorized|unapproved)\b.{0,40}\b(action|command|"
        r"artifact)\b",
        re.I | re.S,
    ),
    re.compile(
        r"\b(refusal|refusing)\b.{0,60}\b(complete|terminal|final|ends? here)\b",
        re.I | re.S,
    ),
    re.compile(
        r"\b(substitute|incident record|security log)\b.{0,60}\b(document|"
        r"artifact|file|report)\b",
        re.I | re.S,
    ),
    re.compile(
        r"\b(produce|create|generate|write)\b.{0,40}\b(incident|security)\b"
        r".{0,30}\b(record|log|report)\b",
        re.I | re.S,
    ),
]

# Mid-deliberation deferral — NOT a terminal refusal (Slice 4b territory).
_DEFERRAL_VETO_PATTERNS: List[re.Pattern] = [
    re.compile(
        r"\bcannot proceed\b.{0,80}\b(without|until|pending)\b",
        re.I | re.S,
    ),
    re.compile(
        r"\b(awaiting|need|requires?|missing)\b.{0,50}\b(human review|your input|"
        r"director|approval|privacy policy|additional information|further guidance)\b",
        re.I | re.S,
    ),
    re.compile(r"\bnot\s+ready\s+for\s+decision\s*:", re.I),
]


def evaluate_unsafe_execution_boundary(text: str) -> Optional[str]:
    """
    Return a block reason if `text` requests unsafe execution (shell, arbitrary
    paths, etc.). Used on the seed before deliberation begins.
    """
    if not text or not str(text).strip():
        return None
    normalized = " ".join(str(text).split())
    for pattern, label in _UNSAFE_EXECUTION_PATTERNS:
        if pattern.search(normalized):
            return (
                f"This request would {label}. {_EXPLANATION}"
            )
    return None


def evaluate_refusal_termination(synthesis_text: str) -> Optional[str]:
    """
    Return a termination reason if the final synthesis refuses the requested
    action. Operator must not run — no substitute artifacts.
    """
    if not synthesis_text or not str(synthesis_text).strip():
        return None
    text = str(synthesis_text)

    for veto in _DEFERRAL_VETO_PATTERNS:
        if veto.search(text):
            return None

    for pattern in _REFUSAL_SYNTH_PATTERNS:
        if pattern.search(text):
            return (
                "The synthesis refused the requested action. "
                f"{_EXPLANATION}"
            )

    return None
