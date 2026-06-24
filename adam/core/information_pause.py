"""
Slice 4b — mid-loop information pause.

When the Synthesizer signals that deliberation cannot proceed without
missing input (the prompt's "Not ready for decision:" ending, or an
equivalent deferral), the session pauses resumably instead of cycling
to the turn budget or producing a substitute artifact.

Distinct from Slice 4a (terminal human-review gate before Operator) and
from empty-termination (refusal with zero artifacts).
"""
from __future__ import annotations

import re
from typing import List, Optional, Pattern

INFORMATION_PAUSE_END_REASON = "awaiting_information"

_NOT_READY_RE = re.compile(
    r"not\s+ready\s+for\s+decision\s*:\s*(.+)",
    re.IGNORECASE | re.DOTALL,
)

_DEFERRAL_PATTERNS: List[Pattern[str]] = [
    re.compile(
        r"\bcannot proceed\b.{0,80}\b(without|until|pending)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(awaiting|need|requires?|missing)\b.{0,50}\b(human review|your input|"
        r"director|approval|privacy policy|additional information|further guidance)\b",
        re.IGNORECASE | re.DOTALL,
    ),
]

_DECISIVE_ENDING_RE = re.compile(
    r"(?:decision\s+point\s*:|synthesized\s+recommendation\s*:)",
    re.IGNORECASE,
)


def evaluate_information_pause(agent_text: str, agent_name: str) -> Optional[str]:
    """
    Return a human-readable pause reason when the Synthesizer needs missing
    input mid-deliberation, or None when deliberation should continue.
    """
    if agent_name != "Synthesizer":
        return None
    if not agent_text or not str(agent_text).strip():
        return None

    text = str(agent_text)

    match = _NOT_READY_RE.search(text)
    if match:
        reason = match.group(1).strip().split("\n")[0].strip()
        if len(reason) > 500:
            reason = reason[:497] + "..."
        return reason or "Additional information is required before a decision can be made."

    if _DECISIVE_ENDING_RE.search(text):
        return None

    for pattern in _DEFERRAL_PATTERNS:
        if pattern.search(text):
            return (
                "The Synthesizer identified missing information that must be "
                "provided before deliberation can continue."
            )

    return None
