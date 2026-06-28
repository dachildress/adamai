"""
Source-model ingestion lifecycle (Slice 6).

Replaces "a human hand-wrote the SourceModel fixture" with a governed
lifecycle that PRODUCES ratified SourceModels:

    introspect source (injected IntrospectionFn, synthetic)
      → generate candidate SourceModel (runtime, deterministic — NO model)
      → embed (injected EmbedFn, stub)
      → submit for approval (status: pending)
      → human approve / reject (guarded state machine)
      → on approve: ratify (mint immutable version) + register + persist
      → reload ratified models + candidates on startup

Trust boundary (mirrors Slices 4–5): candidate generation is RUNTIME and
deterministic — no model owns the source model, exactly as none owns the
plan or the facts. A `version` is governance EVIDENCE: it exists only after
human approval. Before approval a candidate has a `candidate_id` (identity,
per-submission) and a `schema_fingerprint` (content, per-schema) — never a
version.

Seams are INJECTED so live infrastructure plugs in later with no lifecycle
change: `IntrospectionFn` (real DB introspection is Slice 7) and `EmbedFn`
(real embeddings/vector store is a later slice). This module is model-free
and isolated; only it touches the ingestion store / disk.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .source_model import (
    SourceModel,
    get_source_model,
    register_source_model,
)

# Statuses for the approval state machine.
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
_TERMINAL = {APPROVED, REJECTED}


# ---------------------------------------------------------------------------
# Introspected schema + seams
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Rich introspected schema (Slice 7b) — per-field detail + structured FKs.
#
# Behavior-preserving: the ratified SourceModel grounding contract is still
# field-NAMES only (via `field_names()`); this just carries richer detail for
# provenance/fingerprint/audit. The constructor accepts the OLD shapes too
# (entities as a {name: (fieldnames,)} dict; relationships as (left, right,
# kind) tuples) so Slice-6 code/tests/persisted records keep working.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FieldSchema:
    name: str
    source_type: Optional[str] = None
    nullable: bool = True
    primary_key: bool = False

    def to_dict(self) -> Dict:
        return {"name": self.name, "source_type": self.source_type,
                "nullable": bool(self.nullable), "primary_key": bool(self.primary_key)}

    @classmethod
    def from_dict(cls, d) -> "FieldSchema":
        if isinstance(d, str):
            return cls(name=d)
        return cls(name=d["name"], source_type=d.get("source_type"),
                   nullable=bool(d.get("nullable", True)),
                   primary_key=bool(d.get("primary_key", False)))


@dataclass(frozen=True)
class EntitySchema:
    name: str
    fields: Tuple[FieldSchema, ...]

    def to_dict(self) -> Dict:
        return {"name": self.name, "fields": [f.to_dict() for f in self.fields]}

    @classmethod
    def from_dict(cls, d) -> "EntitySchema":
        return cls(name=d["name"],
                   fields=tuple(FieldSchema.from_dict(f) for f in d.get("fields", [])))


@dataclass(frozen=True)
class RelationshipSchema:
    from_entity: str
    from_field: str
    to_entity: str
    to_field: str
    relationship_type: str = "foreign_key"

    def to_dict(self) -> Dict:
        return {"from_entity": self.from_entity, "from_field": self.from_field,
                "to_entity": self.to_entity, "to_field": self.to_field,
                "relationship_type": self.relationship_type}

    @classmethod
    def from_dict(cls, d) -> "RelationshipSchema":
        return cls(from_entity=d["from_entity"], from_field=d["from_field"],
                   to_entity=d["to_entity"], to_field=d["to_field"],
                   relationship_type=d.get("relationship_type", "foreign_key"))


def _coerce_entities(value) -> Tuple[EntitySchema, ...]:
    # Back-compat: a {name: (fieldnames,)} dict OR an iterable of EntitySchema/dicts.
    if isinstance(value, dict):
        return tuple(
            EntitySchema(name=k, fields=tuple(
                f if isinstance(f, FieldSchema) else FieldSchema.from_dict(f) for f in v))
            for k, v in value.items()
        )
    out = []
    for e in value:
        out.append(e if isinstance(e, EntitySchema) else EntitySchema.from_dict(e))
    return tuple(out)


def _coerce_relationships(value) -> Tuple[RelationshipSchema, ...]:
    out = []
    for r in value or ():
        if isinstance(r, RelationshipSchema):
            out.append(r)
        elif isinstance(r, dict):
            out.append(RelationshipSchema.from_dict(r))
        elif isinstance(r, (tuple, list)) and len(r) >= 2:
            # Old (left="ent.field", right="ent.field", kind) form.
            fe, _, ff = str(r[0]).partition(".")
            te, _, tf = str(r[1]).partition(".")
            kind = r[2] if len(r) > 2 else "foreign_key"
            out.append(RelationshipSchema(fe, ff, te, tf, kind))
    return tuple(out)


@dataclass(frozen=True)
class IntrospectedSchema:
    """Adapter-neutral schema: entities with per-field detail + structured FK
    relationships. NOT SQL-specific. Accepts old name-only shapes for
    back-compat; `field_names()` is the grounding-facing down-projection."""
    entities: Tuple[EntitySchema, ...]
    relationships: Tuple[RelationshipSchema, ...] = ()
    source_name: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "entities", _coerce_entities(self.entities))
        object.__setattr__(self, "relationships", _coerce_relationships(self.relationships))

    def field_names(self) -> Dict[str, Tuple[str, ...]]:
        """Down-project to the {entity: (field_names,)} view the SourceModel
        grounding contract uses. Back-compat accessor."""
        return {e.name: tuple(f.name for f in e.fields) for e in self.entities}

    def to_dict(self) -> Dict:
        return {
            "source_name": self.source_name,
            "entities": [e.to_dict() for e in self.entities],
            "relationships": [r.to_dict() for r in self.relationships],
        }

    @classmethod
    def from_dict(cls, d) -> "IntrospectedSchema":
        return cls(
            entities=tuple(EntitySchema.from_dict(e) for e in d.get("entities", [])),
            relationships=tuple(RelationshipSchema.from_dict(r) for r in d.get("relationships", [])),
            source_name=d.get("source_name"),
        )


# (source_name) -> introspected schema. Real MySQL introspection is in
# mysql_introspector.py (Slice 7b).
IntrospectionFn = Callable[[str], IntrospectedSchema]

# (source_name, schema) -> placeholder embedding reference. Real embeddings
# are a later slice; the stub just records that it ran.
EmbedFn = Callable[[str, IntrospectedSchema], str]


def synthetic_introspection(source_name: str) -> IntrospectedSchema:
    """Synthetic introspector — a fixed school-like schema (now with per-field
    detail + FK relationships) so the lifecycle exercises the rich structure
    without a live DB. field_names() still matches synthetic-school-v1."""
    return IntrospectedSchema(
        source_name=source_name,
        entities=(
            EntitySchema("students", (
                FieldSchema("id", "int", False, True),
                FieldSchema("name", "varchar", True, False),
                FieldSchema("school_id", "int", True, False),
                FieldSchema("grade_level", "varchar", True, False),
                FieldSchema("enrolled", "tinyint", True, False),
            )),
            EntitySchema("attendance", (
                FieldSchema("id", "int", False, True),
                FieldSchema("student_id", "int", True, False),
                FieldSchema("school_id", "int", True, False),
                FieldSchema("period", "varchar", True, False),
                FieldSchema("rate", "double", True, False),
                FieldSchema("date", "date", True, False),
            )),
            EntitySchema("schools", (
                FieldSchema("id", "int", False, True),
                FieldSchema("name", "varchar", True, False),
                FieldSchema("level", "varchar", True, False),
            )),
        ),
        relationships=(
            RelationshipSchema("attendance", "school_id", "schools", "id", "foreign_key"),
            RelationshipSchema("students", "school_id", "schools", "id", "foreign_key"),
        ),
    )


def stub_embed(source_name: str, schema: IntrospectedSchema) -> str:
    """Stub embedder — reserves the place where real embedding will live. No
    vector store; returns a deterministic placeholder handle."""
    fp = schema_fingerprint(schema)
    return f"embed-stub:{source_name}:{fp[:12]}"


# ---------------------------------------------------------------------------
# Schema fingerprint (content identity — deterministic, order-normalized)
# ---------------------------------------------------------------------------

def schema_fingerprint(schema: IntrospectedSchema) -> str:
    """SHA-256 over the RICH schema: entity/field names PLUS source_type,
    nullable, primary_key, and FK relationships. Deterministic.

    Ordering is normalized INSIDE this function (entities by name, fields by
    name, relationships by tuple) BEFORE hashing — never relying on the
    introspector to sort. MySQL's information_schema returns rows in
    server-determined order; without this, the SAME schema could hash
    differently just because rows arrived in a different order, silently
    breaking change/duplicate detection. The fingerprint is a function of
    CONTENT, never row-arrival order.
    """
    canonical = {
        "entities": [
            {
                "name": e.name,
                "fields": [
                    {"name": f.name, "source_type": f.source_type,
                     "nullable": bool(f.nullable), "primary_key": bool(f.primary_key)}
                    for f in sorted(e.fields, key=lambda x: x.name)
                ],
            }
            for e in sorted(schema.entities, key=lambda x: x.name)
        ],
        "relationships": sorted(
            [[r.from_entity, r.from_field, r.to_entity, r.to_field, r.relationship_type]
             for r in schema.relationships]
        ),
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    """Pre-approval record. Carries identity (candidate_id), content
    (schema_fingerprint + entities), and lifecycle status — but NO version.

    `entities` is the grounding-facing name view ({entity: (field_names,)}).
    `schema_detail` is the full RICH schema (Slice 7b) serialized for audit;
    it is optional so PRE-7b persisted records (which lack it) reload fine."""
    candidate_id: str
    status: str
    source_name: str
    schema_fingerprint: str
    created_at: str
    embedding_ref: Optional[str]
    entities: Dict[str, Tuple[str, ...]]            # name view, used at ratification
    version: Optional[str] = None                   # set only once approved+ratified
    schema_detail: Optional[Dict] = None            # rich schema (7b); may be absent

    def to_dict(self) -> Dict:
        return {
            "candidate_id": self.candidate_id,
            "status": self.status,
            "source_name": self.source_name,
            "schema_fingerprint": self.schema_fingerprint,
            "created_at": self.created_at,
            "embedding_ref": self.embedding_ref,
            "entities": {e: list(f) for e, f in self.entities.items()},
            "version": self.version,
            "schema_detail": self.schema_detail,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "Candidate":
        # Reload-tolerant: pre-7b records have name-only entities and no
        # schema_detail; both load with sensible defaults (no crash).
        return cls(
            candidate_id=d["candidate_id"], status=d["status"],
            source_name=d["source_name"], schema_fingerprint=d["schema_fingerprint"],
            created_at=d["created_at"], embedding_ref=d.get("embedding_ref"),
            entities={e: tuple(f) for e, f in d.get("entities", {}).items()},
            version=d.get("version"),
            schema_detail=d.get("schema_detail"),
        )


@dataclass
class RatifiedRecord:
    """Post-approval, IMMUTABLE governance record. The `version` is the proof
    that a human approved this exact schema."""
    version: str
    source_name: str
    schema_fingerprint: str
    approved_by: str
    approved_at: str
    created_at: str
    entities: Dict[str, Tuple[str, ...]]
    candidate_id: str
    schema_detail: Optional[Dict] = None            # rich schema (7b); may be absent

    def to_source_model(self) -> SourceModel:
        return SourceModel(version=self.version, entities=dict(self.entities))

    def to_dict(self) -> Dict:
        return {
            "version": self.version,
            "source_name": self.source_name,
            "schema_fingerprint": self.schema_fingerprint,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "created_at": self.created_at,
            "entities": {e: list(f) for e, f in self.entities.items()},
            "candidate_id": self.candidate_id,
            "schema_detail": self.schema_detail,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "RatifiedRecord":
        return cls(
            version=d["version"], source_name=d["source_name"],
            schema_fingerprint=d["schema_fingerprint"], approved_by=d["approved_by"],
            approved_at=d["approved_at"], created_at=d["created_at"],
            entities={e: tuple(f) for e, f in d["entities"].items()},
            candidate_id=d.get("candidate_id", ""),
            schema_detail=d.get("schema_detail"),
        )


class IngestionError(Exception):
    """Raised on an illegal lifecycle transition (e.g. approving a terminal
    candidate). Distinct from validation/Sentinel outcomes."""


# ---------------------------------------------------------------------------
# Ingestion store (state machine + persistence + reload)
# ---------------------------------------------------------------------------

class IngestionStore:
    """Owns candidate + ratified state, persists to a JSON file, and reloads
    on construction. Registering ratified models populates the shared
    source-model registry so validation can ground plans against them.

    Persistence format (one JSON file at `path`):
        {"candidates": {candidate_id: {...}}, "ratified": {version: {...}}}
    Written atomically (temp file + os.replace) so a failed write never
    corrupts the existing file.
    """

    def __init__(self, path, *, now_fn: Callable[[], str] = None) -> None:
        self.path = Path(path)
        self._now = now_fn or (lambda: datetime.now().isoformat(timespec="seconds"))
        self.candidates: Dict[str, Candidate] = {}
        self.ratified: Dict[str, RatifiedRecord] = {}
        self._load()

    # -- persistence ------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.candidates = {
            cid: Candidate.from_dict(c)
            for cid, c in (data.get("candidates") or {}).items()
        }
        self.ratified = {
            v: RatifiedRecord.from_dict(r)
            for v, r in (data.get("ratified") or {}).items()
        }
        # Re-register ratified models so validation grounds against them after
        # a restart.
        for rec in self.ratified.values():
            register_source_model(rec.to_source_model())

    def _save(self) -> None:
        payload = {
            "candidates": {cid: c.to_dict() for cid, c in self.candidates.items()},
            "ratified": {v: r.to_dict() for v, r in self.ratified.items()},
        }
        blob = json.dumps(payload, indent=2, sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: temp file in the same dir, then replace.
        tmp = self.path.with_suffix(self.path.suffix + f".tmp-{uuid.uuid4().hex[:8]}")
        tmp.write_text(blob, encoding="utf-8")
        os.replace(tmp, self.path)

    # -- lifecycle --------------------------------------------------------

    def submit(
        self,
        source_name: str,
        introspect_fn: IntrospectionFn = synthetic_introspection,
        embed_fn: EmbedFn = stub_embed,
    ) -> Candidate:
        """Introspect → generate candidate (deterministic) → embed (stub) →
        persist as pending. Each submission gets a FRESH candidate_id even for
        an identical schema; the schema_fingerprint is stable per schema."""
        schema = introspect_fn(source_name)
        fingerprint = schema_fingerprint(schema)
        embedding_ref = embed_fn(source_name, schema)
        candidate = Candidate(
            candidate_id=uuid.uuid4().hex,
            status=PENDING,
            source_name=source_name,
            schema_fingerprint=fingerprint,
            created_at=self._now(),
            embedding_ref=embedding_ref,
            # Grounding uses NAMES only; the rich detail rides along for audit.
            entities=schema.field_names(),
            schema_detail=schema.to_dict(),
        )
        self.candidates[candidate.candidate_id] = candidate
        self._save()
        return candidate

    def _next_version(self, source_name: str) -> str:
        """Version scheme: '<source_name>-v<N>' where N is a monotonic counter
        over already-ratified models for that source (existing + 1). Stable,
        unique, human-legible, and never reuses a retired number."""
        n = sum(1 for r in self.ratified.values() if r.source_name == source_name) + 1
        return f"{source_name}-v{n}"

    def approve(self, candidate_id: str, approved_by: str = "system") -> RatifiedRecord:
        """pending → approved: mint an immutable version, build + register the
        SourceModel, persist. Refuses to approve a terminal candidate."""
        cand = self._require(candidate_id)
        if cand.status != PENDING:
            raise IngestionError(
                f"cannot approve candidate in status {cand.status!r} (must be pending)"
            )
        version = self._next_version(cand.source_name)
        record = RatifiedRecord(
            version=version,
            source_name=cand.source_name,
            schema_fingerprint=cand.schema_fingerprint,
            approved_by=approved_by,
            approved_at=self._now(),
            created_at=cand.created_at,
            entities=dict(cand.entities),
            candidate_id=cand.candidate_id,
            schema_detail=cand.schema_detail,
        )
        # Register first so a registration conflict aborts before we mutate
        # candidate state. (Immutable: never overwrites an existing version.)
        register_source_model(record.to_source_model())
        self.ratified[version] = record
        cand.status = APPROVED
        cand.version = version
        self._save()
        return record

    def reject(self, candidate_id: str) -> Candidate:
        """pending → rejected: never ratified, retained for audit. Refuses to
        reject a terminal candidate."""
        cand = self._require(candidate_id)
        if cand.status != PENDING:
            raise IngestionError(
                f"cannot reject candidate in status {cand.status!r} (must be pending)"
            )
        cand.status = REJECTED
        self._save()
        return cand

    # -- queries ----------------------------------------------------------

    def _require(self, candidate_id: str) -> Candidate:
        cand = self.candidates.get(candidate_id)
        if cand is None:
            raise IngestionError(f"unknown candidate_id: {candidate_id!r}")
        return cand

    def get_candidate(self, candidate_id: str) -> Optional[Candidate]:
        return self.candidates.get(candidate_id)

    def list_candidates(self) -> List[Candidate]:
        return list(self.candidates.values())

    def list_ratified(self) -> List[RatifiedRecord]:
        return list(self.ratified.values())
