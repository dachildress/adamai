"""
Slice 7b Phase 1: rich IntrospectedSchema — fingerprint detail + order
normalization + reload tolerance + grounding-contract preservation.

Run:  python tests/pipeline/test_ingestion_richschema.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ_ROOT))

from adam.pipeline import (  # noqa: E402
    IntrospectedSchema, EntitySchema, FieldSchema, RelationshipSchema,
    schema_fingerprint, synthetic_introspection,
    IngestionStore, get_source_model, reset_ratified,
    ExecutionPlan, SQLITE_CAPABILITIES, validate, ValidationConfig,
)

PASSED = 0
FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}" + (f"  -- {detail}" if detail else ""))


def test_synthetic_is_rich_but_names_unchanged():
    s = synthetic_introspection("powerschool")
    # Grounding-facing name view matches synthetic-school-v1 exactly.
    names = s.field_names()
    check("down-projection yields name view",
          names["schools"] == ("id", "name", "level"))
    check("attendance names preserved",
          names["attendance"] == ("id", "student_id", "school_id", "period", "rate", "date"))
    # Rich per-field detail is present.
    schools = next(e for e in s.entities if e.name == "schools")
    id_field = next(f for f in schools.fields if f.name == "id")
    check("field carries source_type", id_field.source_type == "int")
    check("field carries primary_key", id_field.primary_key is True)
    check("FK relationships present", any(
        r.from_entity == "attendance" and r.to_entity == "schools" for r in s.relationships))


def test_fingerprint_change_detection():
    base = IntrospectedSchema(entities=(EntitySchema("t", (FieldSchema("c", "int", True, False),)),))
    same = IntrospectedSchema(entities=(EntitySchema("t", (FieldSchema("c", "int", True, False),)),))
    check("identical rich schema -> same fingerprint",
          schema_fingerprint(base) == schema_fingerprint(same))

    diff_type = IntrospectedSchema(entities=(EntitySchema("t", (FieldSchema("c", "varchar", True, False),)),))
    check("differ ONLY by source_type -> different fingerprint",
          schema_fingerprint(base) != schema_fingerprint(diff_type))

    diff_null = IntrospectedSchema(entities=(EntitySchema("t", (FieldSchema("c", "int", False, False),)),))
    check("differ ONLY by nullable -> different fingerprint",
          schema_fingerprint(base) != schema_fingerprint(diff_null))

    diff_pk = IntrospectedSchema(entities=(EntitySchema("t", (FieldSchema("c", "int", True, True),)),))
    check("differ ONLY by primary_key -> different fingerprint",
          schema_fingerprint(base) != schema_fingerprint(diff_pk))

    with_fk = IntrospectedSchema(
        entities=(EntitySchema("t", (FieldSchema("c", "int", True, False),)),
                  EntitySchema("u", (FieldSchema("id", "int", False, True),))),
        relationships=(RelationshipSchema("t", "c", "u", "id", "foreign_key"),))
    without_fk = IntrospectedSchema(
        entities=(EntitySchema("t", (FieldSchema("c", "int", True, False),)),
                  EntitySchema("u", (FieldSchema("id", "int", False, True),))))
    check("differ ONLY by FK relationship -> different fingerprint",
          schema_fingerprint(with_fk) != schema_fingerprint(without_fk))


def test_fingerprint_order_independent():
    # Same content, different input order of entities/fields/relationships.
    a = IntrospectedSchema(
        entities=(
            EntitySchema("students", (FieldSchema("id", "int", False, True), FieldSchema("name", "varchar"))),
            EntitySchema("schools", (FieldSchema("id", "int", False, True), FieldSchema("level", "varchar"))),
        ),
        relationships=(RelationshipSchema("students", "id", "schools", "id"),))
    b = IntrospectedSchema(
        entities=(
            EntitySchema("schools", (FieldSchema("level", "varchar"), FieldSchema("id", "int", False, True))),
            EntitySchema("students", (FieldSchema("name", "varchar"), FieldSchema("id", "int", False, True))),
        ),
        relationships=(RelationshipSchema("students", "id", "schools", "id"),))
    check("input ordering does NOT change fingerprint",
          schema_fingerprint(a) == schema_fingerprint(b), f"{schema_fingerprint(a)} vs {schema_fingerprint(b)}")


def test_old_shape_record_reloads():
    reset_ratified()
    path = Path(tempfile.mkdtemp()) / "ingestion.json"
    # Hand-craft a PRE-7b persisted file: name-only entities, legacy
    # "relationships" key, NO schema_detail.
    legacy = {
        "candidates": {
            "old1": {
                "candidate_id": "old1", "status": "approved", "source_name": "legacy",
                "schema_fingerprint": "abc", "created_at": "2026-01-01T00:00:00",
                "embedding_ref": "e", "entities": {"schools": ["id", "name"]},
                "relationships": [["a.b", "schools.id", "many_to_one"]],
                "version": "legacy-v1",
            }
        },
        "ratified": {
            "legacy-v1": {
                "version": "legacy-v1", "source_name": "legacy", "schema_fingerprint": "abc",
                "approved_by": "system", "approved_at": "2026-01-01T00:00:00",
                "created_at": "2026-01-01T00:00:00", "entities": {"schools": ["id", "name"]},
                "candidate_id": "old1",
            }
        },
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")
    st = IngestionStore(path)  # must not crash
    check("legacy candidate reloads", st.get_candidate("old1") is not None)
    check("legacy candidate schema_detail defaults to None",
          st.get_candidate("old1").schema_detail is None)
    check("legacy ratified reloads + registers", get_source_model("legacy-v1") is not None)
    check("legacy ratified grounds field names",
          get_source_model("legacy-v1").has_field("schools", "name"))
    reset_ratified()


def test_lifecycle_unchanged_grounds_plan():
    reset_ratified()
    path = Path(tempfile.mkdtemp()) / "ingestion.json"
    st = IngestionStore(path)
    cand = st.submit("powerschool")  # uses richer synthetic introspector
    check("candidate carries rich schema_detail", cand.schema_detail is not None)
    rec = st.approve(cand.candidate_id)
    plan = ExecutionPlan.from_dict({
        "plan_version": "1.0", "intent_type": "query",
        "connection_handle": "c", "source_type": "sql",
        "source_model_version": rec.version, "purpose": "t",
        "estimated_row_scope": "small",
        "body": {"operation": "select", "entities": ["schools"],
                 "projection": ["schools.name"], "limit": 10},
    })
    out = validate(plan, SQLITE_CAPABILITIES, ValidationConfig())
    check("ratified rich-schema version grounds a plan (contract unchanged)", out.ok,
          f"{out.category}: {out.detail}")
    reset_ratified()


def main():
    print("Slice 7b Phase 1: rich IntrospectedSchema")
    print("=" * 60)
    for t in [
        test_synthetic_is_rich_but_names_unchanged,
        test_fingerprint_change_detection,
        test_fingerprint_order_independent,
        test_old_shape_record_reloads,
        test_lifecycle_unchanged_grounds_plan,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
