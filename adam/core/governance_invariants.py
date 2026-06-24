"""
Hard governance invariants — not configurable per profile.

These boundaries apply to every session regardless of governance profile.
They recognize the CAPABILITY being requested, not polite surface phrasing:
installing/registering skills, granting ADAM new permissions, or modifying
runtime configuration are human-only actions.

The self-modification gate runs on the session seed (and context background)
before deliberation begins, and on Director messages as they arrive.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

GOVERNANCE_BOUNDARY_END_REASON = "governance_boundary_blocked"

_BOUNDARY_EXPLANATION = (
    "ADAM cannot modify its own capabilities, install or register skills, "
    "or grant itself new permissions. That is a human-only action."
)

_OFFER_PROPOSAL = (
    "I can help draft a proposal for a human administrator to review, "
    "but I will not attempt to build or install it."
)

# (pattern, short capability label for the reason string)
_SELF_MODIFICATION_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # Explicit install / register / enable skill requests
    (
        re.compile(
            r"\b(install|register|enable|deploy|add)\b.{0,100}\b(skill|skills)\b",
            re.I | re.S,
        ),
        "install or register a skill on ADAM",
    ),
    (
        re.compile(
            r"\b(skill|skills)\b.{0,60}\b(install|register|enable|deploy|add)\b",
            re.I | re.S,
        ),
        "install or register a skill on ADAM",
    ),
    # Create/build/implement a new skill (capability acquisition — ADAM-directed only)
    (
        re.compile(
            r"\b(create|build|implement|develop|package)\b.{0,50}\b"
            r"(a\s+)?(new\s+)?skills?\b.{0,40}\b("
            r"for\s+adam|on\s+adam|into\s+skills?/|"
            r"that\s+(lets?|allows?|enables?|gives?)\s+(you|adam)|"
            r"(called|named)\b"
            r")",
            re.I | re.S,
        ),
        "create or build a new ADAM skill",
    ),
    (
        re.compile(
            r"\bnew\s+skill\b.{0,40}\b(called|named)\b",
            re.I | re.S,
        ),
        "create or build a new ADAM skill",
    ),
    # Skill framed as granting powers to ADAM
    (
        re.compile(
            r"\b(skill|skills)\b.{0,80}\b(that\s+)?(lets?|allows?|enables?|gives?)\s+"
            r"(you|adam)\b",
            re.I | re.S,
        ),
        "grant ADAM a new capability via a skill",
    ),
    (
        re.compile(
            r"\b(skill|skills)\b.{0,80}\b(filesystem|file\s*system|shell|sudo|"
            r"root\s+access|privilege|permission)\b",
            re.I | re.S,
        ),
        "grant ADAM elevated filesystem or shell access via a skill",
    ),
    # Direct self-grant language
    (
        re.compile(
            r"\b(give|grant)\s+(yourself|adam)\b.{0,80}\b(access|permission|"
            r"capabilit|skill|power|authority)\b",
            re.I | re.S,
        ),
        "grant ADAM new permissions or capabilities",
    ),
    (
        re.compile(
            r"\b(acquire|obtain|gain)\b.{0,80}\bcapabilit",
            re.I | re.S,
        ),
        "acquire new capabilities for ADAM",
    ),
    # "obtain the ability to <action> on your own" — capability by phrasing, not noun
    (
        re.compile(
            r"\b(acquire|obtain|gain)\b.{0,60}\b(the\s+)?ability\s+to\b.{0,100}\b"
            r"(on\s+your\s+own|yourself|for\s+yourself|so\s+you\s+can|you\s+can)\b",
            re.I | re.S,
        ),
        "acquire the ability to act on your own",
    ),
    # Integrations / connectors / plugins that extend what ADAM can do
    (
        re.compile(
            r"\b(enable|install|register|add|deploy)\b.{0,60}\b"
            r"(a\s+)?(new\s+)?(integration|connector|plugin|api\s+access|tool\s+access)\b"
            r".{0,80}\b(so\s+you\s+can|that\s+(lets?|allows?|enables?)\s+you|"
            r"for\s+you\s+to|on\s+your\s+own|yourself|for\s+adam|you\s+can)\b",
            re.I | re.S,
        ),
        "enable a new integration or connector for ADAM",
    ),
    (
        re.compile(
            r"\bset\s+up\b.{0,60}\bcapabilit\w*\b.{0,40}\b"
            r"(for\s+)?(yourself|your\s+own|you|adam)\b",
            re.I | re.S,
        ),
        "set up new capabilities for yourself",
    ),
    (
        re.compile(
            r"\badd\b.{0,50}\b(filesystem|shell|network|sudo|root)\b.{0,40}"
            r"\baccess\b.{0,30}\b(to\s+)?(your\s+own|yourself|adam|you)\b",
            re.I | re.S,
        ),
        "grant yourself elevated filesystem or network access",
    ),
    (
        re.compile(
            r"\b(filesystem|shell|network|sudo)\s+access\b.{0,40}\b"
            r"to\s+(your\s+own|yourself|adam|you)\b",
            re.I | re.S,
        ),
        "grant yourself elevated filesystem or network access",
    ),
    # Runtime / codebase self-modification
    (
        re.compile(
            r"\b(modify|change|update|patch|reconfigure|edit)\b.{0,50}"
            r"\b(your\s+own|adam\'?s?|the\s+runtime|runtime)\b.{0,50}"
            r"\b(capabilit|skill|configuration|governance|code|source|"
            r"permissions?)\b",
            re.I | re.S,
        ),
        "modify ADAM's own runtime or configuration",
    ),
    (
        re.compile(
            r"\b(write|save|copy|deploy)\b.{0,50}\b(to\s+)?"
            r"(skills/|/opt/adam|governance\.json|agents\.json|runtime\.json)\b",
            re.I | re.S,
        ),
        "write into ADAM's runtime directories or configuration",
    ),
]


def evaluate_self_modification_boundary(text: str) -> Optional[str]:
    """
    Return a human-readable block reason if `text` requests self-modification
    or capability acquisition; otherwise None (permitted to proceed).

    Conservative by design: matches on capability intent, not attack surface
    phrasing alone.
    """
    if not text or not str(text).strip():
        return None

    normalized = " ".join(str(text).split())

    for pattern, label in _SELF_MODIFICATION_PATTERNS:
        if pattern.search(normalized):
            return (
                f"This request would {label}. {_BOUNDARY_EXPLANATION} "
                f"{_OFFER_PROPOSAL}"
            )

    return None
