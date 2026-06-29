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
            ("delete", "/api/admin/data-sources/some-v1/connection", None),
        ]
        all403 = True
        for method, url, jb in routes:
            if method == "get":
                r = c.get(url, cookies=cook, headers=hdr)
            elif method == "delete":
                r = c.delete(url, cookies=cook, headers=hdr)
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


def _write_providers_json(adam_root: Path):
    (adam_root / "config").mkdir(parents=True, exist_ok=True)
    (adam_root / "config" / "providers.json").write_text(
        json.dumps({"anthropic": {"api_key_env": "ANTHROPIC_API_KEY"},
                    "openai": {"api_key_env": "OPENAI_API_KEY"}}),
        encoding="utf-8")


def test_dotenv_exports_provider_key_when_absent():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        (tmp / ".env").write_text("ANTHROPIC_API_KEY=xyz\nUNRELATED=nope\n", encoding="utf-8")
        _write_providers_json(tmp)
        with env(ANTHROPIC_API_KEY=None, UNRELATED=None):
            make_app(tmp)  # build_app exports keys from .env
            check("provider key set from .env when absent in os.environ",
                  os.environ.get("ANTHROPIC_API_KEY") == "xyz")
            check("unrelated .env entries are NOT exported",
                  os.environ.get("UNRELATED") is None)


def test_dotenv_does_not_override_existing_env():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        (tmp / ".env").write_text("ANTHROPIC_API_KEY=from-dotenv\n", encoding="utf-8")
        _write_providers_json(tmp)
        with env(ANTHROPIC_API_KEY="from-real-env"):
            make_app(tmp)
            check("existing os.environ value wins (not overwritten)",
                  os.environ.get("ANTHROPIC_API_KEY") == "from-real-env")


def test_dotenv_exports_encryption_key():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        (tmp / ".env").write_text("ADAM_DATA_SOURCE_ENCRYPTION_KEY=k-from-dotenv\n", encoding="utf-8")
        _write_providers_json(tmp)
        with env(ADAM_DATA_SOURCE_ENCRYPTION_KEY=None):
            make_app(tmp)
            check("encryption key exported from .env (single secret source)",
                  os.environ.get("ADAM_DATA_SOURCE_ENCRYPTION_KEY") == "k-from-dotenv")


# ---- Connection seam (encrypted profiles) ----

from cryptography.fernet import Fernet  # noqa: E402

DB_PW = "db-r3ad0nly-pass!"


def _introspect(app, c, cook, hdr, source_name="inventory"):
    set_connect_factory(app, FakeConn(col_rows=WIDGET_COLS))
    return c.post("/api/admin/data-sources/mysql/introspect", cookies=cook, headers=hdr,
                  json={"host": "h", "user": "u", "password": "x", "database": "inv",
                        "source_name": source_name}).json()


def _approve_with_conn(app, c, cook, hdr, key, source_name="inventory"):
    cand = _introspect(app, c, cook, hdr, source_name)
    with env(ADAM_DATA_SOURCE_ENCRYPTION_KEY=key):
        r = c.post(f"/api/admin/source-model-candidates/{cand['candidate_id']}/approve",
                   cookies=cook, headers=hdr,
                   json={"host": "db.example", "port": 3306, "user": "ro",
                         "password": DB_PW, "database": "inv", "display_name": "Inventory"})
    return r


def test_approve_writes_encrypted_profile():
    from fastapi.testclient import TestClient
    key = Fernet.generate_key().decode()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw); app = make_app(tmp); c = TestClient(app)
        cook, hdr = ctx("admin")
        r = _approve_with_conn(app, c, cook, hdr, key)
        check("approve-with-connection -> 200", r.status_code == 200, r.text[:160])
        check("approve response carries NO password", DB_PW not in r.text)
        check("approve response carries no encrypted_password", "encrypted_password" not in r.text)
        conn_path = tmp / "pipeline_data" / "source_connections.json"
        on_disk = conn_path.read_text(encoding="utf-8")
        check("plaintext password NOT in connection store on disk", DB_PW not in on_disk)
        check("on-disk profile has an encrypted_password token", "encrypted_password" in on_disk)


def test_approve_with_conn_missing_key_is_clean_and_not_ratified():
    from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw); app = make_app(tmp); c = TestClient(app)
        cook, hdr = ctx("admin")
        cand = _introspect(app, c, cook, hdr)
        with env(ADAM_DATA_SOURCE_ENCRYPTION_KEY=None):
            r = c.post(f"/api/admin/source-model-candidates/{cand['candidate_id']}/approve",
                       cookies=cook, headers=hdr,
                       json={"host": "h", "user": "ro", "password": DB_PW, "database": "inv"})
        check("approve-with-conn, no key -> 400 clean", r.status_code == 400, f"got {r.status_code}")
        check("error mentions encryption key, not password", "encryption key" in r.text and DB_PW not in r.text)
        # Not ratified: fail BEFORE minting a version.
        models = c.get("/api/admin/source-models", cookies=cook, headers=hdr).json()["source_models"]
        check("candidate NOT ratified when key missing", models == [])


def test_resolve_connection_decrypts():
    from fastapi.testclient import TestClient
    from backend import data_sources as ds
    key = Fernet.generate_key().decode()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw); app = make_app(tmp); c = TestClient(app)
        cook, hdr = ctx("admin")
        version = _approve_with_conn(app, c, cook, hdr, key).json()["version"]
        captured = {}

        def fake_factory(**kw):
            captured.update(kw)
            return lambda: "CONN_SENTINEL"

        orig = ds.make_pymysql_connect_fn
        ds.make_pymysql_connect_fn = fake_factory
        try:
            with env(ADAM_DATA_SOURCE_ENCRYPTION_KEY=key):
                connect_fn = ds.default_resolve_connection(version)
            check("resolve returns a connect_fn", callable(connect_fn))
            check("connect_fn builds the connection", connect_fn() == "CONN_SENTINEL")
            check("password decrypted and passed to factory", captured.get("password") == DB_PW)
            check("host/user/database resolved from profile",
                  captured.get("host") == "db.example" and captured.get("user") == "ro"
                  and captured.get("database") == "inv")
        finally:
            ds.make_pymysql_connect_fn = orig


def test_query_e2e_real_resolve():
    from fastapi.testclient import TestClient
    from backend import data_sources as ds
    key = Fernet.generate_key().decode()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw); app = make_app(tmp); c = TestClient(app)
        cook, hdr = ctx("admin")
        version = _approve_with_conn(app, c, cook, hdr, key).json()["version"]
        # Real resolve_connection (NOT overridden); patch the factory so the
        # decrypted connect_fn yields a fake DB conn — fully offline.
        qconn = FakeConn(query_rows=[("Widget A",)],
                         query_desc=[("name", None, None, None, None, None, None)], row_total=1)
        plan_body = json.dumps({"operation": "select", "entities": ["widgets"],
                                "projection": ["widgets.name"], "limit": 10})
        interp = json.dumps({"inferences": ["i"], "recommendations": ["r"],
                             "assumptions": ["a"], "limitations": ["l"],
                             "confidence": "low", "confidence_rationale": "x"})
        app.state.pipeline_model_fns_provider = lambda: (
            (lambda s, o: plan_body), (lambda s, p: interp))
        orig = ds.make_pymysql_connect_fn
        ds.make_pymysql_connect_fn = lambda **kw: (lambda: qconn)
        try:
            ucook, uhdr = ctx("pilot")
            with env(ADAM_DATA_SOURCE_ENCRYPTION_KEY=key):
                r = c.post("/api/data-intelligence/query", cookies=ucook, headers=uhdr,
                           json={"version": version, "objective": "what widgets?"})
        finally:
            ds.make_pymysql_connect_fn = orig
        check("e2e real-resolve query -> 200", r.status_code == 200, r.text[:200])
        res = r.json().get("result")
        check("returns SkillResult ok", res and res["status"] == "ok", str(r.json())[:160])
        check("no password leaked in query response", DB_PW not in r.text)


def test_query_no_profile_connection_not_configured():
    from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw); app = make_app(tmp); c = TestClient(app)
        cook, hdr = ctx("admin")
        version = _ratify_widgets(app, c, cook, hdr)  # approve with NO connection body
        app.state.pipeline_model_fns_provider = lambda: ((lambda s, o: "{}"), (lambda s, p: "{}"))
        # real resolve_connection (not overridden) -> no profile -> None
        ucook, uhdr = ctx("pilot")
        r = c.post("/api/data-intelligence/query", cookies=ucook, headers=uhdr,
                   json={"version": version, "objective": "x"})
        check("no profile -> CONNECTION_NOT_CONFIGURED (200)",
              r.status_code == 200 and r.json().get("error") == "CONNECTION_NOT_CONFIGURED", str(r.json()))


def test_query_missing_key_resolution_failed():
    from fastapi.testclient import TestClient
    key = Fernet.generate_key().decode()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw); app = make_app(tmp); c = TestClient(app)
        cook, hdr = ctx("admin")
        version = _approve_with_conn(app, c, cook, hdr, key).json()["version"]
        app.state.pipeline_model_fns_provider = lambda: ((lambda s, o: "{}"), (lambda s, p: "{}"))
        ucook, uhdr = ctx("pilot")
        # Key UNSET at query time -> decrypt fails -> clean CONNECTION_RESOLUTION_FAILED, not 500.
        with env(ADAM_DATA_SOURCE_ENCRYPTION_KEY=None):
            r = c.post("/api/data-intelligence/query", cookies=ucook, headers=uhdr,
                       json={"version": version, "objective": "x"})
        check("missing key at query -> 200 (not 500)", r.status_code == 200, f"got {r.status_code}")
        check("missing key -> CONNECTION_RESOLUTION_FAILED",
              r.json().get("error") == "CONNECTION_RESOLUTION_FAILED", str(r.json()))


def test_has_connection_flag_safe():
    from fastapi.testclient import TestClient
    key = Fernet.generate_key().decode()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw); app = make_app(tmp); c = TestClient(app)
        cook, hdr = ctx("admin")
        v_conn = _approve_with_conn(app, c, cook, hdr, key, source_name="withconn").json()["version"]
        v_noconn = _ratify_widgets(app, c, cook, hdr)   # source "inventory", no profile
        ucook, uhdr = ctx("pilot")
        models = c.get("/api/data-intelligence/source-models", cookies=ucook, headers=uhdr).json()["source_models"]
        by_v = {m["version"]: m for m in models}
        check("source with profile -> has_connection True", by_v[v_conn]["has_connection"] is True)
        check("source without profile -> has_connection False", by_v[v_noconn]["has_connection"] is False)
        # No credentials in the browser-facing list.
        text = json.dumps(models)
        check("picker exposes no username/password fields",
              "password" not in text and "username" not in text and "encrypted" not in text)


def test_approve_partial_connection_rejected_not_ratified():
    from fastapi.testclient import TestClient
    key = Fernet.generate_key().decode()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw); app = make_app(tmp); c = TestClient(app)
        cook, hdr = ctx("admin")
        # host+user present but password/database blank -> partial intent.
        cand = _introspect(app, c, cook, hdr)
        with env(ADAM_DATA_SOURCE_ENCRYPTION_KEY=key):
            r = c.post(f"/api/admin/source-model-candidates/{cand['candidate_id']}/approve",
                       cookies=cook, headers=hdr,
                       json={"host": "db.example", "user": "ro", "password": "", "database": ""})
        check("partial connection -> 400 clean", r.status_code == 400, f"got {r.status_code}: {r.text[:160]}")
        check("partial connection error mentions incomplete connection",
              "incomplete connection" in r.text, r.text[:160])
        # Side effect must NOT have ratified a connectionless source.
        models = c.get("/api/admin/source-models", cookies=cook, headers=hdr).json()["source_models"]
        check("partial-connection approve did NOT ratify", models == [], str(models))
        # whitespace-only is also blank -> still partial -> 400
        with env(ADAM_DATA_SOURCE_ENCRYPTION_KEY=key):
            r2 = c.post(f"/api/admin/source-model-candidates/{cand['candidate_id']}/approve",
                        cookies=cook, headers=hdr,
                        json={"host": "db.example", "user": "ro", "password": "  ", "database": "inv"})
        check("whitespace-only field treated as blank -> 400", r2.status_code == 400, f"got {r2.status_code}")


def test_approve_no_connection_fields_ratifies_connectionless():
    from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw); app = make_app(tmp); c = TestClient(app)
        cook, hdr = ctx("admin")
        cand = _introspect(app, c, cook, hdr)
        # Fully-absent connection body: the legitimate attach-later path.
        r = c.post(f"/api/admin/source-model-candidates/{cand['candidate_id']}/approve",
                   cookies=cook, headers=hdr, json={})
        check("no-connection approve -> 200 (attach later allowed)", r.status_code == 200, r.text[:160])
        version = r.json()["version"]
        models = {m["version"]: m for m in
                  c.get("/api/admin/source-models", cookies=cook, headers=hdr).json()["source_models"]}
        check("ratified connectionless source has_connection False", models[version]["has_connection"] is False)


def test_admin_source_models_has_connection_flag():
    from fastapi.testclient import TestClient
    key = Fernet.generate_key().decode()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw); app = make_app(tmp); c = TestClient(app)
        cook, hdr = ctx("admin")
        v_conn = _approve_with_conn(app, c, cook, hdr, key, source_name="withconn").json()["version"]
        v_noconn = _ratify_widgets(app, c, cook, hdr)   # no connection body
        models = c.get("/api/admin/source-models", cookies=cook, headers=hdr).json()["source_models"]
        by_v = {m["version"]: m for m in models}
        check("admin list: connected source -> has_connection True", by_v[v_conn]["has_connection"] is True)
        check("admin list: unconnected source -> has_connection False", by_v[v_noconn]["has_connection"] is False)
        check("admin list exposes no credentials",
              "password" not in json.dumps(models) and "username" not in json.dumps(models))


def test_delete_connection_revokes_credential_keeps_record():
    from fastapi.testclient import TestClient
    key = Fernet.generate_key().decode()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw); app = make_app(tmp); c = TestClient(app)
        cook, hdr = ctx("admin")
        version = _approve_with_conn(app, c, cook, hdr, key).json()["version"]

        # Connected before delete.
        before = c.get("/api/admin/source-models", cookies=cook, headers=hdr).json()["source_models"]
        check("connected before delete", {m["version"]: m for m in before}[version]["has_connection"] is True)

        r = c.delete(f"/api/admin/data-sources/{version}/connection", cookies=cook, headers=hdr)
        check("delete connection -> 200", r.status_code == 200, r.text[:160])
        check("delete reports removed:true", r.json().get("removed") is True, r.text)
        check("delete response carries no credential data",
              DB_PW not in r.text and "encrypted" not in r.text)

        # Ratified record preserved, but no longer queryable.
        after = c.get("/api/admin/source-models", cookies=cook, headers=hdr).json()["source_models"]
        by_v = {m["version"]: m for m in after}
        check("ratified record still present after delete (history kept)", version in by_v)
        check("source now has_connection False after delete", by_v[version]["has_connection"] is False)

        # Deleting again is graceful (no profile to remove).
        r2 = c.delete(f"/api/admin/data-sources/{version}/connection", cookies=cook, headers=hdr)
        check("re-delete -> 200 removed:false (graceful)",
              r2.status_code == 200 and r2.json().get("removed") is False, r2.text)

        # Querying a source whose connection was removed -> CONNECTION_NOT_CONFIGURED.
        app.state.pipeline_model_fns_provider = lambda: ((lambda s, o: "{}"), (lambda s, p: "{}"))
        ucook, uhdr = ctx("pilot")
        q = c.post("/api/data-intelligence/query", cookies=ucook, headers=uhdr,
                   json={"version": version, "objective": "x"})
        check("query after connection removed -> CONNECTION_NOT_CONFIGURED",
              q.status_code == 200 and q.json().get("error") == "CONNECTION_NOT_CONFIGURED", str(q.json()))


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
        test_dotenv_exports_provider_key_when_absent,
        test_dotenv_does_not_override_existing_env,
        test_dotenv_exports_encryption_key,
        test_approve_writes_encrypted_profile,
        test_approve_with_conn_missing_key_is_clean_and_not_ratified,
        test_resolve_connection_decrypts,
        test_query_e2e_real_resolve,
        test_query_no_profile_connection_not_configured,
        test_query_missing_key_resolution_failed,
        test_has_connection_flag_safe,
        test_approve_partial_connection_rejected_not_ratified,
        test_approve_no_connection_fields_ratifies_connectionless,
        test_admin_source_models_has_connection_flag,
        test_delete_connection_revokes_credential_keeps_record,
    ]:
        print(f"\n{t.__name__}:")
        t()
    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
