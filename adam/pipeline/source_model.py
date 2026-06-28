"""
Source model — the adapter-neutral description of what a source contains.

For this slice it is an in-memory, ratified fixture (no RAG ingestion).
Each ratified model carries an immutable `version`; validation grounds
entity/field references against it, and the adapter derives its
name allowlist from it (names are never trusted from the plan).

A field reference in a plan may be:
  - qualified  "entity.field"  -> resolves if entity is one of the plan's
                                  entities and field is allowed on it;
  - bare       "field"         -> resolves if exactly one of the plan's
                                  entities declares that field (ambiguous
                                  bare references do not resolve).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class SourceModel:
    version: str
    # entity name -> tuple of allowed field names
    entities: Dict[str, Tuple[str, ...]]

    def has_entity(self, entity: str) -> bool:
        return entity in self.entities

    def has_field(self, entity: str, field: str) -> bool:
        return entity in self.entities and field in self.entities[entity]

    def resolve(self, ref: str, plan_entities: Tuple[str, ...]) -> Optional[Tuple[str, str]]:
        """Resolve a field reference to (entity, field) within the plan's
        declared entities, or None if it does not resolve.

        Only entities the plan actually declares are considered, so a field
        that exists in the model but on an entity the plan didn't name does
        not resolve.
        """
        if not isinstance(ref, str) or not ref:
            return None
        if "." in ref:
            entity, _, fieldname = ref.partition(".")
            if entity in plan_entities and self.has_field(entity, fieldname):
                return (entity, fieldname)
            return None
        # Bare field: resolves only if exactly one declared entity has it.
        matches = [e for e in plan_entities if self.has_field(e, ref)]
        if len(matches) == 1:
            return (matches[0], ref)
        return None


# ---------------------------------------------------------------------------
# Ratified fixtures
# ---------------------------------------------------------------------------

SYNTHETIC_SCHOOL_V1 = SourceModel(
    version="synthetic-school-v1",
    entities={
        "students":   ("id", "name", "school_id", "grade_level", "enrolled"),
        "attendance": ("id", "student_id", "school_id", "period", "rate", "date"),
        "schools":    ("id", "name", "level"),
    },
)

# Registry of ratified models, keyed by version. An unknown/unratified
# version is a SOURCE-model rejection at validation.
RATIFIED_MODELS: Dict[str, SourceModel] = {
    SYNTHETIC_SCHOOL_V1.version: SYNTHETIC_SCHOOL_V1,
}

# The built-in versions present at import. Slice 6's ingestion lifecycle
# registers ADDITIONAL ratified models here at runtime; reset_ratified()
# restores just the built-ins (used by tests to avoid cross-test leakage).
_BUILTIN_VERSIONS = frozenset(RATIFIED_MODELS.keys())


def get_source_model(version: str) -> Optional[SourceModel]:
    """Return the ratified model for a version, or None if not ratified."""
    return RATIFIED_MODELS.get(version)


def register_source_model(model: SourceModel) -> None:
    """Register a ratified SourceModel so validation can ground plans against
    its version. Used by the ingestion lifecycle (Slice 6) at ratification
    and on reload. Ratified versions are immutable: re-registering an existing
    version with DIFFERENT content is refused (a changed schema must mint a
    new version, never overwrite an old one)."""
    existing = RATIFIED_MODELS.get(model.version)
    if existing is not None and existing != model:
        raise ValueError(
            f"refusing to overwrite ratified version {model.version!r} with different content"
        )
    RATIFIED_MODELS[model.version] = model


def reset_ratified() -> None:
    """Restore the ratified registry to just the built-in models. Test helper
    so ingestion tests don't leak registered versions across the suite."""
    for v in list(RATIFIED_MODELS.keys()):
        if v not in _BUILTIN_VERSIONS:
            del RATIFIED_MODELS[v]
