"""
Slice 6 Phase 2: persistence + reload + end-to-end grounding.

Run:  python tests/pipeline/test_ingestion_persistence.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ_ROOT))

from adam.pipeline import (  # noqa: E402
    IngestionStore, ExecutionPlan, SQLITE_CAPABILITIES, validate, ValidationConfig,
    get_source_model, reset_ratified, SOURCE_MODEL_ERROR,
    PENDING, APPROVED, REJECTED,
)
from adam.pipeline import ingestion as ingestion_mod  # noqa: E402

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


def tmp_path():
    return Path(tempfile.mkdtemp()) / "ingestion.json"


def plan_on(version, entities=("schools",), projection=("schools.name",)):
    return ExecutionPlan.from_dict({
        "plan_version": "1.0", "intent_type": "query",
        "connection_handle": "conn_x", "source_type": "sql",
        "source_model_version": version, "purpose": "t", "estimated_row_scope": "small",
        "body": {"operation": "select", "entities": list(entities),
                 "projection": list(projection), "limit": 10},
    })


def test_reload_survives_restart():
    reset_ratified()
    path = tmp_path()
    st = IngestionStore(path)
    c_appr = st.submit("powerschool")
    rec = st.approve(c_appr.candidate_id)
    version = rec.version
    c_pending = st.submit("hr")
    c_rej = st.submit("hr"); st.reject(c_rej.candidate_id)

    # Simulate a restart: wipe the in-memory registry, build a NEW store from
    # the same path. Reload must re-register the ratified model and restore
    # candidate states.
    reset_ratified()
    check("after reset, ratified version is gone from registry", get_source_model(version) is None)
    st2 = IngestionStore(path)
    check("reload re-registers ratified model", get_source_model(version) is not None)
    check("approved candidate state survived", st2.get_candidate(c_appr.candidate_id).status == APPROVED)
    check("pending candidate state survived", st2.get_candidate(c_pending.candidate_id).status == PENDING)
    check("rejected candidate state survived", st2.get_candidate(c_rej.candidate_id).status == REJECTED)
    check("ratified record survived with metadata",
          st2.ratified[version].approved_by and st2.ratified[version].schema_fingerprint)
    reset_ratified()


def test_persistence_file_format_and_no_tmp_leftover():
    reset_ratified()
    path = tmp_path()
    st = IngestionStore(path)
    cand = st.submit("powerschool")
    st.approve(cand.candidate_id)
    check("persistence file exists", path.exists())
    data = json.loads(path.read_text(encoding="utf-8"))
    check("file has candidates + ratified sections", "candidates" in data and "ratified" in data)
    check("full candidate metadata persisted (not just entities)",
          "schema_fingerprint" in next(iter(data["candidates"].values())))
    leftovers = [p.name for p in path.parent.iterdir() if ".tmp-" in p.name]
    check("no leftover temp files after atomic write", leftovers == [], str(leftovers))
    reset_ratified()


def test_failed_write_does_not_corrupt_existing():
    reset_ratified()
    path = tmp_path()
    st = IngestionStore(path)
    st.approve(st.submit("powerschool").candidate_id)
    good = path.read_text(encoding="utf-8")

    # Force the atomic replace to fail mid-save; the existing file must be
    # left intact (that is the point of temp-file + os.replace).
    orig = ingestion_mod.os.replace
    ingestion_mod.os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    try:
        try:
            st.submit("hr")  # triggers _save -> os.replace -> raises
            check("save failure surfaced", False, "no error")
        except OSError:
            check("save failure surfaced", True)
    finally:
        ingestion_mod.os.replace = orig
    check("existing persisted file uncorrupted after failed write",
          path.read_text(encoding="utf-8") == good)
    # clean up any temp file left by the failed write
    for p in path.parent.iterdir():
        if ".tmp-" in p.name:
            p.unlink()
    reset_ratified()


def test_ratified_version_grounds_validation_end_to_end():
    reset_ratified()
    path = tmp_path()
    st = IngestionStore(path)
    cand = st.submit("powerschool")

    # Before approval: a plan on the candidate_id (no version exists) is
    # SOURCE_MODEL_ERROR.
    out = validate(plan_on(cand.candidate_id), SQLITE_CAPABILITIES, ValidationConfig())
    check("candidate version not groundable -> SOURCE_MODEL_ERROR",
          out.category == SOURCE_MODEL_ERROR, str(out))

    rec = st.approve(cand.candidate_id)
    # After approval: the ratified version grounds validation (passes).
    out = validate(plan_on(rec.version), SQLITE_CAPABILITIES, ValidationConfig())
    check("ratified version grounds a valid plan", out.ok, f"{out.category}: {out.detail}")
    reset_ratified()


def test_rejected_version_not_groundable():
    reset_ratified()
    path = tmp_path()
    st = IngestionStore(path)
    cand = st.submit("powerschool")
    st.reject(cand.candidate_id)
    out = validate(plan_on(cand.candidate_id), SQLITE_CAPABILITIES, ValidationConfig())
    check("rejected candidate is not groundable", out.category == SOURCE_MODEL_ERROR, str(out))
    reset_ratified()


def test_ingestion_is_model_free():
    # The ingestion module must not pull in adam.core (no LLM in ingestion).
    check("no adam.core imported via ingestion/pipeline",
          not any(m.startswith("adam.core") for m in sys.modules))
    import inspect
    src = inspect.getsource(ingestion_mod)
    check("ingestion source references no adam.core",
          "adam.core" not in src and "call_model" not in src)


def main():
    print("Slice 6 Phase 2: persistence + reload + e2e grounding")
    print("=" * 60)
    for t in [
        test_reload_survives_restart,
        test_persistence_file_format_and_no_tmp_leftover,
        test_failed_write_does_not_corrupt_existing,
        test_ratified_version_grounds_validation_end_to_end,
        test_rejected_version_not_groundable,
        test_ingestion_is_model_free,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
