"""
Tests for the Data Sources web integration (governed query pipeline).

Fully fake-backed: no live MySQL, no live model. A fake MySQL connection is
injected via app.state.mysql_connect_factory / resolve_connection; fake model
functions via app.state.pipeline_model_fns_provider.

Run:  python gui/backend/test_data_sources.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent           # .../gui/backend
GUI_ROOT = HERE.parent                            # .../gui
PROJ_ROOT = GUI_ROOT.parent                       # .../opt/adam
sys.path.insert(0, str(PROJ_ROOT))
sys.path.insert(0, str(GUI_ROOT))
sys.path.insert(0, str(HERE))

from backend import server, auth, csrf  # noqa: E402

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


# ---- Fake MySQL connection (covers test / introspect / query SQL) ----

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._result = []

    def execute(self, sql, params=None):
        self.conn.executed.append(sql)
        s = sql.strip().lower()
        if "information_schema.tables" in s:
            self._result = [(self.conn.table_count,)]
            self.description = [("c", None, None, None, None, None, None)]
        elif "information_schema.columns" in s:
            self._result = list(self.conn.col_rows)
            self.description = None
        elif "key_column_usage" in s:
            self._result = list(self.conn.fk_rows)
            self.description = None
        elif s.startswith("select count(*) from `"):
            self._result = [(self.conn.row_total,)]
            self.description = [("c", None, None, None, None, None, None)]
        else:  # the adapter-generated SELECT
            self._result = list(self.conn.query_rows)
            self.description = self.conn.query_desc

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None


class FakeConn:
    def __init__(self, *, table_count=0, col_rows=None, fk_rows=None,
                 query_rows=None, query_desc=None, row_total=0):
        self.table_count = table_count
        self.col_rows = col_rows or []
        self.fk_rows = fk_rows or []
        self.query_rows = query_rows or []
        self.query_desc = query_desc or []
        self.row_total = row_total
        self.executed = []

    def cursor(self):
        return FakeCursor(self)

    def ping(self, *a, **k):
        pass

    def close(self):
        pass


# A distinctive schema (entity "widgets") so we can prove the INJECTED
# introspector was used, not the synthetic schools/attendance default.
WIDGET_COLS = [
    ("widgets", "id", "int", "NO", "PRI", 1),
    ("widgets", "name", "varchar", "YES", "", 2),
    ("widgets", "price", "double", "YES", "", 3),
]


def make_app(tmp: Path):
    logs = tmp / "logs"; gui = tmp / "gui"
    logs.mkdir(parents=True, exist_ok=True); gui.mkdir(parents=True, exist_ok=True)
    auth.init_auth(gui)
    auth.add_user(username="admin", display_name="Admin", email="a@e.com", role="admin",
                  password="adminpass12", status="active", sessions_remaining=-1, max_turns_per_session=-1)
    auth.add_user(username="pilot", display_name="Pilot", email="p@e.com", role="pilot",
                  password="pilotpass12", status="active", sessions_remaining=3, max_turns_per_session=10)
    app = server.build_app(adam_root=tmp, logs_dir=logs)
    return app


def ctx(username):
    tok = auth.create_login_session(username)
    ct = csrf.issue_token(tok)
    return {"adam_login": tok, "adam_csrf": ct}, {"X-CSRF-Token": ct}


def set_connect_factory(app, conn):
    app.state.mysql_connect_factory = lambda **kw: (lambda: conn)


# ============================================================
# Tests
# ============================================================

def test_mysql_test_zero_tables():
    from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory() as raw:
        app = make_app(Path(raw)); c = TestClient(app)
        set_connect_factory(app, FakeConn(table_count=0))
        cook, hdr = ctx("admin")
        r = c.post("/api/admin/data-sources/mysql/test", cookies=cook, headers=hdr,
                   json={"host": "h", "port": 3306, "user": "u", "password": "secret", "database": "d"})
        check("test 0 tables -> 200", r.status_code == 200, r.text[:120])
        b = r.json()
        check("status no_tables_found", b["status"] == "no_tables_found", str(b))
        check("ok true, table_count 0", b["ok"] is True and b["table_count"] == 0)
        check("password never echoed", "secret" not in r.text)
        # positive
        set_connect_factory(app, FakeConn(table_count=4))
        r = c.post("/api/admin/data-sources/mysql/test", cookies=cook, headers=hdr,
                   json={"host": "h", "user": "u", "password": "x", "database": "d"})
        check("test with tables -> ok status", r.json()["status"] == "ok" and r.json()["table_count"] == 4)


def test_introspect_pending_uses_injected_introspector():
    from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory() as raw:
        app = make_app(Path(raw)); c = TestClient(app)
        set_connect_factory(app, FakeConn(col_rows=WIDGET_COLS, fk_rows=[]))
        cook, hdr = ctx("admin")
        r = c.post("/api/admin/data-sources/mysql/introspect", cookies=cook, headers=hdr,
                   json={"host": "h", "user": "u", "password": "secret", "database": "d", "source_name": "inventory"})
        check("introspect -> 200", r.status_code == 200, r.text[:160])
        b = r.json()
        check("candidate is pending", b["status"] == "pending", str(b.get("status")))
        check("candidate has no version (not ratified)", b.get("version") is None)
        ents = {e["name"] for e in (b.get("schema_detail") or {}).get("entities", [])}
        check("reflects INJECTED introspector (widgets), not synthetic", ents == {"widgets"}, str(ents))
        check("password never echoed", "secret" not in r.text)
        # not ratified yet
        r2 = c.get("/api/admin/source-models", cookies=cook, headers=hdr)
        check("no ratified models yet", r2.json()["source_models"] == [])


def test_approve_records_admin_and_one_version():
    from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory() as raw:
        app = make_app(Path(raw)); c = TestClient(app)
        set_connect_factory(app, FakeConn(col_rows=WIDGET_COLS))
        cook, hdr = ctx("admin")
        cand = c.post("/api/admin/data-sources/mysql/introspect", cookies=cook, headers=hdr,
                      json={"host": "h", "user": "u", "password": "x", "database": "d", "source_name": "inventory"}).json()
        cid = cand["candidate_id"]
        r = c.post(f"/api/admin/source-model-candidates/{cid}/approve", cookies=cook, headers=hdr)
        check("approve -> 200", r.status_code == 200, r.text[:160])
        rec = r.json()
        check("approved_by = admin username", rec["approved_by"] == "admin", str(rec))
        check("version minted", rec["version"] == "inventory-v1")
        # second approve -> clean 409, not 500
        r2 = c.post(f"/api/admin/source-model-candidates/{cid}/approve", cookies=cook, headers=hdr)
        check("re-approve -> 409 (not 500)", r2.status_code == 409, f"got {r2.status_code}")
        # exactly one ratified version
        models = c.get("/api/admin/source-models", cookies=cook, headers=hdr).json()["source_models"]
        check("exactly one ratified version", len(models) == 1 and models[0]["version"] == "inventory-v1")


def test_non_admin_403():
    from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory() as raw:
        app = make_app(Path(raw)); c = TestClient(app)
        cook, hdr = ctx("pilot")
        routes = [
            ("post", "/api/admin/data-sources/mysql/test", {"host": "h", "user": "u", "password": "x", "database": "d"}),
            ("post", "/api/admin/data-sources/mysql/introspect", {"host": "h", "user": "u", "password": "x", "database": "d", "source_name": "s"}),
            ("get", "/api/admin/source-model-candidates", None),
            ("post", "/api/admin/source-model-candidates/abc/approve", None),
            ("post", "/api/admin/source-model-candidates/abc/reject", None),
            ("get", "/api/admin/source-models", None),
        ]
        all403 = True
        for method, url, jb in routes:
            if method == "get":
                r = c.get(url, cookies=cook, headers=hdr)
            else:
                r = c.post(url, cookies=cook, headers=hdr, json=jb or {})
            if r.status_code != 403:
                all403 = False
                check(f"non-admin 403 on {url}", False, f"got {r.status_code}")
        check("non-admin gets 403 on every admin data-source route", all403)


def _ratify_widgets(app, c, cook, hdr):
    set_connect_factory(app, FakeConn(col_rows=WIDGET_COLS))
    cand = c.post("/api/admin/data-sources/mysql/introspect", cookies=cook, headers=hdr,
                  json={"host": "h", "user": "u", "password": "x", "database": "d", "source_name": "inventory"}).json()
    rec = c.post(f"/api/admin/source-model-candidates/{cand['candidate_id']}/approve",
                 cookies=cook, headers=hdr).json()
    return rec["version"]


def test_query_skillresult_and_same_store():
    from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory() as raw:
        app = make_app(Path(raw)); c = TestClient(app)
        admin_cook, admin_hdr = ctx("admin")
        version = _ratify_widgets(app, c, admin_cook, admin_hdr)  # admin path ratifies

        # Fake model fns: planning returns a body grounded in widgets; interp JSON.
        plan_body = json.dumps({"operation": "select", "entities": ["widgets"],
                                "projection": ["widgets.name"], "limit": 10})
        interp = json.dumps({"inferences": ["widgets exist"], "recommendations": ["stock more"],
                             "assumptions": ["sample is representative"], "limitations": ["synthetic"],
                             "confidence": "medium", "confidence_rationale": "small sample"})
        app.state.pipeline_model_fns_provider = lambda: (
            (lambda system, obj: plan_body), (lambda system, payload: interp))
        # Query connection resolves (read-only) to a fake conn returning rows.
        qconn = FakeConn(query_rows=[("Widget A",), ("Widget B",)],
                         query_desc=[("name", None, None, None, None, None, None)], row_total=2)
        app.state.resolve_connection = lambda handle: (lambda: qconn)

        ucook, uhdr = ctx("pilot")   # query path: any authenticated user
        r = c.post("/api/data-intelligence/query", cookies=ucook, headers=uhdr,
                   json={"version": version, "objective": "what widgets exist?"})
        check("query -> 200 (query path findable via same store)", r.status_code == 200, r.text[:200])
        res = r.json().get("result")
        check("returns a SkillResult", res is not None and res.get("status") == "ok", str(r.json())[:200])
        check("runtime observations present", any(o["label"] == "rows_returned" for o in res["observations"]))
        check("model inferences present + separate", res["inferences"] == ["widgets exist"])
        check("recommendations separate field", res["recommendations"] == ["stock more"])
        check("confidence carried", res["confidence"] == "medium")
        check("source_lineage traceable", res["source_lineage"].get("plan_id"))


def test_query_unknown_version():
    from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory() as raw:
        app = make_app(Path(raw)); c = TestClient(app)
        app.state.pipeline_model_fns_provider = lambda: ((lambda s, o: "{}"), (lambda s, p: "{}"))
        app.state.resolve_connection = lambda h: (lambda: FakeConn())
        ucook, uhdr = ctx("pilot")
        r = c.post("/api/data-intelligence/query", cookies=ucook, headers=uhdr,
                   json={"version": "does-not-exist", "objective": "x"})
        check("unknown version -> 404 clean error (not 500)", r.status_code == 404, f"got {r.status_code}")


def test_query_blocked_surfaces_stage():
    from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory() as raw:
        app = make_app(Path(raw)); c = TestClient(app)
        admin_cook, admin_hdr = ctx("admin")
        version = _ratify_widgets(app, c, admin_cook, admin_hdr)
        # Planning fn proposes a HALLUCINATED entity -> validation SOURCE_MODEL_ERROR.
        bad_body = json.dumps({"operation": "select", "entities": ["gadgets"],
                               "projection": ["gadgets.name"], "limit": 10})
        app.state.pipeline_model_fns_provider = lambda: (
            (lambda s, o: bad_body), (lambda s, p: "{}"))
        app.state.resolve_connection = lambda h: (lambda: FakeConn())
        ucook, uhdr = ctx("pilot")
        r = c.post("/api/data-intelligence/query", cookies=ucook, headers=uhdr,
                   json={"version": version, "objective": "x"})
        check("blocked query -> 200 (not 500)", r.status_code == 200, f"got {r.status_code}")
        res = r.json().get("result")
        check("SkillResult records the block (validation_error)",
              res and res["status"] == "validation_error", str(r.json())[:200])
        check("limitations explain why it stopped", any("not executed" in l for l in res["limitations"]))


def test_query_model_not_configured():
    from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory() as raw:
        app = make_app(Path(raw)); c = TestClient(app)
        admin_cook, admin_hdr = ctx("admin")
        version = _ratify_widgets(app, c, admin_cook, admin_hdr)
        # Default provider returns None -> MODEL_NOT_CONFIGURED (no 500).
        app.state.resolve_connection = lambda h: (lambda: FakeConn())
        ucook, uhdr = ctx("pilot")
        r = c.post("/api/data-intelligence/query", cookies=ucook, headers=uhdr,
                   json={"version": version, "objective": "x"})
        check("no model seam -> 200 (not 500)", r.status_code == 200, f"got {r.status_code}")
        check("returns MODEL_NOT_CONFIGURED", r.json().get("error") == "MODEL_NOT_CONFIGURED", str(r.json()))


def test_query_requires_csrf_and_auth():
    from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory() as raw:
        app = make_app(Path(raw)); c = TestClient(app)
        # unauthenticated -> 401
        r = c.post("/api/data-intelligence/query", json={"version": "v", "objective": "x"})
        check("query unauthenticated -> 401", r.status_code == 401, f"got {r.status_code}")
        # authed but no CSRF -> 403
        tok = auth.create_login_session("pilot")
        r = c.post("/api/data-intelligence/query", cookies={"adam_login": tok},
                   json={"version": "v", "objective": "x"})
        check("query without CSRF -> 403", r.status_code == 403, f"got {r.status_code}")


def test_query_body_has_no_credentials():
    # Firm requirement: the query request model carries no connection fields.
    fields = set(server.DataIntelligenceQueryRequest.model_fields.keys())
    check("query body fields are exactly {version, objective}",
          fields == {"version", "objective"}, str(fields))
    for bad in ("host", "user", "password", "database", "dsn"):
        check(f"query body has no '{bad}' field", bad not in fields)


# ---- Model-seam provider (buildquery follow-up) ----

import contextlib  # noqa: E402
import os  # noqa: E402
from backend import data_sources  # noqa: E402


@contextlib.contextmanager
def env(**overrides):
    """Set/unset env vars within the block; restore after. A value of None
    deletes the var."""
    saved = {k: os.environ.get(k) for k in overrides}
    try:
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


def test_provider_none_when_key_unset():
    # Default model (Operator -> claude-sonnet-4-6 -> anthropic). With no
    # anthropic key, the provider is unusable -> None.
    with env(ADAM_DATA_INTELLIGENCE_MODEL_ID=None, ADAM_DATA_INTELLIGENCE_AGENT=None,
             ANTHROPIC_API_KEY=None, OPENAI_API_KEY=None):
        check("provider None when selected provider key unset",
              data_sources.default_model_fns_provider() is None)


def test_provider_none_when_model_unknown():
    with env(ADAM_DATA_INTELLIGENCE_MODEL_ID="no-such-model", ANTHROPIC_API_KEY="x"):
        check("provider None when model id not in models.json",
              data_sources.default_model_fns_provider() is None)


def test_single_provider_usability_no_trap():
    # Selected model's provider key set (anthropic), unrelated provider key
    # (openai) UNSET. The all-providers config validator would raise here, but
    # the provider must still be usable. Guards the ConfigError trap.
    with env(ADAM_DATA_INTELLIGENCE_MODEL_ID="claude-sonnet-4-6",
             ANTHROPIC_API_KEY="anthropic-key", OPENAI_API_KEY=None):
        fns = data_sources.default_model_fns_provider()
        check("usable with selected provider key only (no all-providers trap)",
              fns is not None and len(fns) == 2)


def test_planning_fn_calls_call_model_unchanged():
    from adam.core import client_dispatch
    captured = {}

    def fake_call_model(*, model_id, system_prompt, messages, max_tokens, temperature, models, providers):
        captured.update(model_id=model_id, system_prompt=system_prompt, messages=messages)
        return "RAW MODEL OUTPUT"

    orig = client_dispatch.call_model
    client_dispatch.call_model = fake_call_model
    try:
        with env(ADAM_DATA_INTELLIGENCE_MODEL_ID="claude-sonnet-4-6", ANTHROPIC_API_KEY="k"):
            fns = data_sources.default_model_fns_provider()
            check("provider returns two callables", fns is not None and len(fns) == 2)
            planning_fn, interp_fn = fns
            out = planning_fn("SYS", "what widgets exist?")
            check("planning fn returns model string unchanged", out == "RAW MODEL OUTPUT")
            check("call_model got the objective in messages",
                  captured["messages"] == [{"role": "user", "content": "what widgets exist?"}])
            check("call_model got the configured model id", captured["model_id"] == "claude-sonnet-4-6")
    finally:
        client_dispatch.call_model = orig


def test_model_error_surfaces_clean_not_500():
    from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory() as raw:
        app = make_app(Path(raw)); c = TestClient(app)
        admin_cook, admin_hdr = ctx("admin")
        version = _ratify_widgets(app, c, admin_cook, admin_hdr)
        # A provider whose planning fn raises (provider error after retries).
        def boom(*a, **k):
            raise RuntimeError("provider exploded after retries")
        app.state.pipeline_model_fns_provider = lambda: (boom, boom)
        app.state.resolve_connection = lambda h: (lambda: FakeConn())
        ucook, uhdr = ctx("pilot")
        r = c.post("/api/data-intelligence/query", cookies=ucook, headers=uhdr,
                   json={"version": version, "objective": "x"})
        check("model error -> 200 (not 500)", r.status_code == 200, f"got {r.status_code}")
        check("model error -> clean QUERY_FAILED (no result, no stack)",
              r.json().get("error") == "QUERY_FAILED", str(r.json()))
        check("no fabricated result on model error", r.json().get("result") is None)


def main():
    print("Data Sources web integration tests")
    print("=" * 60)
    for t in [
        test_mysql_test_zero_tables,
        test_introspect_pending_uses_injected_introspector,
        test_approve_records_admin_and_one_version,
        test_non_admin_403,
        test_query_skillresult_and_same_store,
        test_query_unknown_version,
        test_query_blocked_surfaces_stage,
        test_query_model_not_configured,
        test_query_requires_csrf_and_auth,
        test_query_body_has_no_credentials,
        test_provider_none_when_key_unset,
        test_provider_none_when_model_unknown,
        test_single_provider_usability_no_trap,
        test_planning_fn_calls_call_model_unchanged,
        test_model_error_surfaces_clean_not_500,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
