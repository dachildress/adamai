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

@dataclass(frozen=True)
class IntrospectedSchema:
    """Adapter-neutral description of a source: entities → field names, plus
    optional relationships. Deliberately NOT SQL-specific (a real introspector
    returns schema, not a 'SQL connection') so the seam survives Slice 7."""
    entities: Dict[str, Tuple[str, ...]]
    relationships: Tuple[Tuple[str, str, str], ...] = ()   # (left, right, kind)


# (source_name) -> introspected schema. Real DB introspection is Slice 7.
IntrospectionFn = Callable[[str], IntrospectedSchema]

# (source_name, schema) -> placeholder embedding reference. Real embeddings
# are a later slice; the stub just records that it ran.
EmbedFn = Callable[[str, IntrospectedSchema], str]


def synthetic_introspection(source_name: str) -> IntrospectedSchema:
    """Synthetic introspector — a fixed school-like schema so the lifecycle is
    exercised end to end without a live DB. Matches synthetic-school-v1."""
    return IntrospectedSchema(
        entities={
            "students":   ("id", "name", "school_id", "grade_level", "enrolled"),
            "attendance": ("id", "student_id", "school_id", "period", "rate", "date"),
            "schools":    ("id", "name", "level"),
        },
        relationships=(
            ("attendance.school_id", "schools.id", "many_to_one"),
            ("students.school_id", "schools.id", "many_to_one"),
        ),
    )


def stub_embed(source_name: str, schema: IntrospectedSchema) -> str:
    """Stub embedder — reserves the place where real embedding will live. No
    vector store; returns a deterministic placeholder handle."""
    fp = schema_fingerprint(schema)
    return f"embed-stub:{source_name}:{fp[:12]}"


# ---------------------------------------------------------------------------
# Schema fingerprint (content identity — deterministic)
# ---------------------------------------------------------------------------

def schema_fingerprint(schema: IntrospectedSchema) -> str:
    """SHA-256 over the introspected schema (entities, fields, relationships).
    Deterministic: same schema in → same fingerprint out. Independent of
    candidate identity. Gives change/duplicate detection and provenance."""
    canonical = {
        "entities": {e: list(schema.entities[e]) for e in sorted(schema.entities)},
        "relationships": sorted([list(r) for r in schema.relationships]),
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    """Pre-approval record. Carries identity (candidate_id), content
    (schema_fingerprint + entities), and lifecycle status — but NO version."""
    candidate_id: str
    status: str
    source_name: str
    schema_fingerprint: str
    created_at: str
    embedding_ref: Optional[str]
    entities: Dict[str, Tuple[str, ...]]            # content, used at ratification
    relationships: Tuple[Tuple[str, str, str], ...] = ()
    version: Optional[str] = None                   # set only once approved+ratified

    def to_dict(self) -> Dict:
        return {
            "candidate_id": self.candidate_id,
            "status": self.status,
            "source_name": self.source_name,
            "schema_fingerprint": self.schema_fingerprint,
            "created_at": self.created_at,
            "embedding_ref": self.embedding_ref,
            "entities": {e: list(f) for e, f in self.entities.items()},
            "relationships": [list(r) for r in self.relationships],
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "Candidate":
        return cls(
            candidate_id=d["candidate_id"], status=d["status"],
            source_name=d["source_name"], schema_fingerprint=d["schema_fingerprint"],
            created_at=d["created_at"], embedding_ref=d.get("embedding_ref"),
            entities={e: tuple(f) for e, f in d.get("entities", {}).items()},
            relationships=tuple(tuple(r) for r in d.get("relationships", [])),
            version=d.get("version"),
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
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "RatifiedRecord":
        return cls(
            version=d["version"], source_name=d["source_name"],
            schema_fingerprint=d["schema_fingerprint"], approved_by=d["approved_by"],
            approved_at=d["approved_at"], created_at=d["created_at"],
            entities={e: tuple(f) for e, f in d["entities"].items()},
            candidate_id=d.get("candidate_id", ""),
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
            entities=dict(schema.entities),
            relationships=tuple(schema.relationships),
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
