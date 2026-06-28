"""
Slice 6 Phase 1: ingestion lifecycle (state machine, fingerprint, ratify).

Run:  python tests/pipeline/test_ingestion.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ_ROOT))

from adam.pipeline import (  # noqa: E402
    IngestionStore, IngestionError, IntrospectedSchema,
    schema_fingerprint, synthetic_introspection,
    get_source_model, reset_ratified,
    PENDING, APPROVED, REJECTED,
)

PASSED = 0
FAILED = 0


def check(name, cond, detail=""):
    # Hardened (test_fix): a FALSE condition now RAISES so the failure surfaces
    # loudly and located, under both pytest and the direct runner. The PASSED
    # counter is kept for the direct runner's RESULT line.
    global PASSED, FAILED
    if not cond:
        FAILED += 1
        raise AssertionError(f"{name}" + (f" -- {detail}" if detail else ""))
    PASSED += 1
    print(f"  PASS  {name}")


def store():
    """A fresh store on a temp path; ratified registry reset to built-ins."""
    reset_ratified()
    tmp = Path(tempfile.mkdtemp()) / "ingestion.json"
    return IngestionStore(tmp)


class EmbedSpy:
    def __init__(self):
        self.calls = []

    def __call__(self, source_name, schema):
        self.calls.append((source_name, schema))
        return f"embed:{source_name}"


def test_fingerprint_deterministic():
    s = synthetic_introspection("powerschool")
    check("same schema -> same fingerprint",
          schema_fingerprint(s) == schema_fingerprint(synthetic_introspection("x")))
    different = IntrospectedSchema(entities={"a": ("id",)})
    check("different schema -> different fingerprint",
          schema_fingerprint(s) != schema_fingerprint(different))


def test_submit_pending_not_ratified():
    st = store()
    spy = EmbedSpy()
    cand = st.submit("powerschool", embed_fn=spy)
    check("submit -> pending", cand.status == PENDING)
    check("candidate has no version yet", cand.version is None)
    check("candidate has a fingerprint", len(cand.schema_fingerprint) == 64)
    check("embed stub invoked", len(spy.calls) == 1)
    check("candidate is NOT ratified", get_source_model(cand.candidate_id) is None)
    reset_ratified()


def test_identity_vs_content():
    st = store()
    c1 = st.submit("powerschool")
    c2 = st.submit("powerschool")
    check("same schema -> same fingerprint", c1.schema_fingerprint == c2.schema_fingerprint)
    check("each submission -> distinct candidate_id", c1.candidate_id != c2.candidate_id)
    reset_ratified()


def test_approve_ratifies_and_registers():
    st = store()
    cand = st.submit("powerschool")
    rec = st.approve(cand.candidate_id, approved_by="alice")
    check("approve -> candidate approved", st.get_candidate(cand.candidate_id).status == APPROVED)
    check("ratified version minted", rec.version == "powerschool-v1")
    check("approved_by recorded", rec.approved_by == "alice")
    check("approved_at recorded", bool(rec.approved_at))
    check("fingerprint carried for provenance", rec.schema_fingerprint == cand.schema_fingerprint)
    # Now groundable.
    model = get_source_model(rec.version)
    check("ratified model registered + groundable", model is not None and model.has_entity("schools"))
    reset_ratified()


def test_reject_never_ratified():
    st = store()
    cand = st.submit("powerschool")
    out = st.reject(cand.candidate_id)
    check("reject -> rejected", out.status == REJECTED)
    check("rejected candidate has no version", out.version is None)
    check("nothing registered for a rejected candidate",
          all(get_source_model(v) is None for v in (cand.candidate_id, "powerschool-v1")))
    reset_ratified()


def test_terminal_no_retransition():
    st = store()
    c1 = st.submit("powerschool")
    st.approve(c1.candidate_id)
    try:
        st.approve(c1.candidate_id)
        check("re-approve refused", False, "no error")
    except IngestionError:
        check("re-approve a terminal candidate refused", True)
    try:
        st.reject(c1.candidate_id)
        check("reject-after-approve refused", False, "no error")
    except IngestionError:
        check("reject after approve refused", True)

    c2 = st.submit("powerschool")
    st.reject(c2.candidate_id)
    try:
        st.approve(c2.candidate_id)
        check("approve-after-reject refused", False, "no error")
    except IngestionError:
        check("approve after reject refused", True)
    reset_ratified()


def test_new_version_per_schema_change_old_immutable():
    st = store()
    c1 = st.submit("powerschool")
    r1 = st.approve(c1.candidate_id)
    # Re-ingest a CHANGED schema -> new version, old left intact.
    changed = lambda name: IntrospectedSchema(entities={  # noqa: E731
        "students": ("id", "name", "school_id", "grade_level", "enrolled", "homeroom"),
        "attendance": ("id", "student_id", "school_id", "period", "rate", "date"),
        "schools": ("id", "name", "level"),
    })
    c2 = st.submit("powerschool", introspect_fn=changed)
    r2 = st.approve(c2.candidate_id)
    check("changed schema mints a new version", r2.version == "powerschool-v2" and r1.version == "powerschool-v1")
    check("different fingerprint for changed schema", r1.schema_fingerprint != r2.schema_fingerprint)
    check("old version still groundable + unchanged",
          get_source_model("powerschool-v1") is not None
          and "homeroom" not in get_source_model("powerschool-v1").entities["students"])
    check("new version reflects the change",
          "homeroom" in get_source_model("powerschool-v2").entities["students"])
    reset_ratified()


def main():
    print("Slice 6 Phase 1: ingestion lifecycle")
    print("=" * 60)
    for t in [
        test_fingerprint_deterministic,
        test_submit_pending_not_ratified,
        test_identity_vs_content,
        test_approve_ratifies_and_registers,
        test_reject_never_ratified,
        test_terminal_no_retransition,
        test_new_version_per_schema_change_old_immutable,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
