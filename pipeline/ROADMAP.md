# Governed Execution Pipeline — Build Roadmap

Build-status ledger for the pipeline (`adam/pipeline/`). This tracks WHAT IS BUILT
vs. PENDING. The design lives in the two architecture documents
(`governed_execution_pipeline_v1.md`, `interface_executionplan_v1.md`); the strategic
context and decisions live in the pipeline handoff. This file is the progress view.

**Discipline:** one slice per scoped pass. Each slice has an explicit out-of-scope
fence and "stop after this slice." Prompts are written JUST-IN-TIME, grounded in the
real code the previous slice produced — never pre-written from prediction. Do NOT
extract abstractions (Capability, Composition) ahead of a real second implementation.

---

## Status legend
- ✅ done & verified
- 🔨 in progress
- ⬜ pending
- ⏸ deferred by design (do not build until its precondition is real)

---

## Slices

**✅ Slice 1 — ExecutionPlan spine**
ExecutionPlan (query/select only), structured query body, deterministic validation
returning typed `ValidationOutcome` (VALIDATION_ERROR / SOURCE_MODEL_ERROR /
CAPABILITY_ERROR), `plan_id` (SHA-256 of canonical key-sorted plan JSON; runtime
excluded), immutable plan + `ExecutionRequest` runtime wrapper, Sentinel STUB,
minimal adapter capability declaration, SQLite adapter (parameterized values,
allowlisted identifiers via source model — no raw SQL, no interpolation), in-memory
`synthetic-school-v1` source model, synthetic SQLite DB. 29 tests. Behavior-isolated;
no live-loop import; no LLM call.
*Verified: tests pass, isolation confirmed (no live-loop imports), injection test
proves the table survives a `'; DROP TABLE students;--` filter value, plan_id
determinism holds. Note: `purpose` is currently included in the plan_id hash — a
conscious-able choice (operation-identity would exclude it); left as-is.*

**✅ Slice 2 — Real Sentinel predicates**
`adam/pipeline/sentinel.py` replaces the stub in the runtime flow: deterministic
predicates over the structured plan ONLY (never parses SQL) — read-only (via a generic
`is_write()` classifier so future write intents flow through unchanged), entity scope
(allowlist + denylist), field denylist (projection/filters/joins/group_by/aggregations/
order_by), and a cost predicate hook (`AdapterCostEstimate` with numeric `low<medium<high`
ranking). Typed `SentinelOutcome{ok, disposition, category, detail}` with `ALLOWED` /
`POLICY_DENIED` / `APPROVAL_REQUIRED`, distinct from `ValidationOutcome`. In-memory
`GovernanceConfig` / `ScopeConfig` fixtures. Runner enforces flow control: a non-ALLOWED
outcome stops before the adapter (no SQL built or executed). 99 pipeline tests
(45 new this slice). Still synthetic data, no model call, no live-loop import.
*Verified: Sentinel imports no DB/SQL and evaluates only structured fields (a
`'; DROP TABLE…'` filter VALUE is ALLOWED — structural, not SQL parsing); POLICY_DENIED
and APPROVAL_REQUIRED both prove no adapter construction; read-only uses the real
`is_write()` helper, not a hardcoded pass; validation vs Sentinel outcomes are distinct
types. The Slice-1 `sentinel_stub.py` remains only as a vestige referenced by one
Slice-1 unit test; the live flow no longer uses it.*

**✅ Slice 3 — Adapter interface + cost/health**
`adam/pipeline/adapter.py` defines the source-agnostic `Adapter` ABC — four methods,
no SQL in any signature: `capabilities() -> AdapterCapabilities`, `health() ->
AdapterHealth`, `estimate_cost(plan) -> AdapterCostEstimate | None`, `execute(plan) ->
QueryResult` (`translate()` stays private to SQL-family adapters). `AdapterHealth`
{status, checked_at, detail} with READY / DEGRADED / REINDEXING / OFFLINE /
AUTHENTICATION_FAILED and ready/transient/terminal helpers. `SQLiteAdapter` implements
the contract (behavior-preserving) and produces a documented cost HEURISTIC (coarse
COUNT(*) rows capped by limit; complexity from join/agg/group_by count) reusing
Sentinel's `AdapterCostEstimate` — one cost type. Runner lifecycle is now
health → validation → adapter cost → Sentinel → execute: terminal health short-circuits
with ADAPTER_UNAVAILABLE before validation, transient health proceeds with a recorded
warning, and the cost estimate flows adapter → Sentinel with no hand-passing. 150
pipeline tests (51 new this slice). Still synthetic data, no model call, no live-loop.
*Verified: interface signatures mention no sql/cursor/statement/translate (introspected);
forced OFFLINE/AUTHENTICATION_FAILED provably never call execute; cost produced by the
adapter is consumed by the EXISTING Sentinel predicate (POLICY_DENIED end-to-end on a
'low' ceiling); SQLiteAdapter refactor behavior-preserving (Slice 1-2 tests green, same
QueryResult for a normal plan).*

**✅ Slice 4 — Data Intelligence skill emits plans**
`adam/pipeline/skill.py`: objective + source model → model proposes a query BODY (JSON)
→ skill builds the ExecutionPlan with a SKILL-OWNED envelope + model body → existing
governed pipeline. Model output is UNTRUSTED: a prompt builder (entities/fields + closed
body schema + body-only/no-SQL/no-envelope rules), a robust single-JSON parser (handles
prose/fences; multiple-object / SQL / non-JSON / envelope-bearing → typed
PLAN_PARSE_ERROR), and `propose_plan` that NEVER spreads model JSON into the envelope
(connection_handle and all envelope fields are skill-owned — the model can't steer the
connection). Real model seam via the existing `call_model` + `load_and_validate_config`
registry (config-driven, default `claude-sonnet-4-6`), wrapped behind an injectable
`PlanModelFn`; the wrapper imports adam.core LAZILY so the pipeline stays model-free.
193 pipeline tests (43 new), all with a FAKE model — no live call. Bad-but-parseable
bodies are caught END-TO-END by existing governance: hallucinated entity →
SOURCE_MODEL_ERROR, missing limit / ['*'] → VALIDATION_ERROR, out-of-scope → POLICY_DENIED.
*Verified: production wrapper calls the real call_model/registry (lazy, no hardcoded
provider); envelope built field-by-field (no ExecutionPlan(**model_json)); a model-supplied
connection_handle never reaches a constructed plan; ONLY skill.py imports adam.core;
`import adam.pipeline` pulls in NO adam.core; core (plan/validate/sentinel/adapter/runner)
stays deterministic and model-free.*

**✅ Slice 5 — SkillResult + attribution**  ← **SYNTHETIC-DATA DEMO MILESTONE (Framatome-ready)**
Typed `SkillResult` (objective, data_analyzed, observations, inferences, recommendations,
assumptions, limitations, confidence, confidence_rationale, source_lineage) with the
fact/judgment line as SEPARATE typed fields, never a prose blob. The trust boundary from
Slice 4 (model emits body, runtime owns envelope) applied to RESULTS: the RUNTIME computes
`observations` deterministically from QueryResult (`derive_observations`, model-free — no
"facts" field in any model contract); the model receives observations + metadata + lineage
(NEVER raw rows — privacy/FERPA, determinism, weaker-model friendly) and returns ONLY
interpretation, parsed with Slice-4-grade untrusted discipline (model-asserted observations
ignored). Two named seams (`PlanningModelFn` + `InterpretationModelFn`), default-same today,
separately injectable, both lazy. `analyze_objective(...)` assembles it; honest on
denial/empty/failure (no fabricated observations/inferences — POLICY_DENIED / VALIDATION_ERROR
/ ADAPTER_UNAVAILABLE / parse-error / empty all recorded truthfully, interpretation model not
even called). 262 pipeline tests (69 new), all with FAKE seams — no live call.
*Verified: observations runtime-computed & deterministic (values match data); model cannot
add/alter observations; interpretation prompt carries no "rows" key; denied/empty/failed →
honest result; both seams distinct & lazy; `import adam.pipeline` pulls in NO adam.core; only
skill.py touches the model seam. **Synthetic-data demo milestone reached: objective →
governed, attributed answer.***

**✅ Slice 6 — Source-model ingestion lifecycle, approval & persistence**
`adam/pipeline/ingestion.py`: governed lifecycle that PRODUCES ratified SourceModels —
introspect (injected `IntrospectionFn`, synthetic) → generate candidate (RUNTIME,
deterministic, no model) → embed (injected `EmbedFn`, stub) → submit (pending) →
approve/reject (guarded state machine) → ratify (mint immutable `version`) → register into
the ratified registry → persist (atomic JSON) → reload on startup. Records keep identity
(`candidate_id`, per-submission) separate from content (`schema_fingerprint` = SHA-256 over
entities/fields/relationships) separate from governance evidence (`version`, minted only at
approval). Version scheme `<source>-v<N>` (monotonic per source); ratified versions are
IMMUTABLE — a changed schema mints a new version (old stays groundable forever). Persistence
(`{candidates, ratified}` JSON, temp-file + os.replace) reloads ratified models into the
registry and candidate states on restart; isolated to the ingestion module. 304 pipeline
tests (54 new). **Real DB introspection is Slice 7; real embeddings/vector store are a
later slice — both are injected seams here (synthetic/stub).**
*Verified: candidate generation deterministic & model-free (same schema → same fingerprint,
distinct candidate_ids); guarded transitions (only approve ratifies; reject never; terminal
can't re-transition); ratified version grounds validation end-to-end (candidate/rejected →
SOURCE_MODEL_ERROR); persistence survives a simulated restart; failed atomic write leaves the
existing file uncorrupted; no adam.core in ingestion; `import adam.pipeline` pulls in NO
adam.core; Slices 1–5 green.*

**✅ Slice 7 — Real SQL adapter (MySQL)**
`adam/pipeline/mysql_adapter.py`: `MySQLAdapter(Adapter)` executing through a real
(pinned) PyMySQL driver, conforming to the Slice-3 `Adapter` ABC. Proves a genuinely
different SQL dialect fits behind the contract without touching governance: MySQL `%s`
placeholders + backtick quoting stay PRIVATE to this module (grep confirms `%s`/`` `{ ``
appear only here — not in adapter/runner/sentinel/skill). `translate(plan)->TranslatedQuery`
(pure) separated from `execute`; real `health()` (READY/OFFLINE/AUTHENTICATION_FAILED via
CONNECTION_ERROR) with a short TTL cache; typed errors (IDENTIFIER_RESOLUTION/TRANSLATION/
EXECUTION/CONNECTION); allowlist-grounded identifiers + fully-parameterized values (same
injection-safety discipline as SQLite). Driver imported LAZILY (injected-connection seam),
so the pipeline imports and Tier-1 tests run with no server / no driver. **NO shared SQL
base class** — MySQLAdapter's only base is `Adapter` (asserted); sharing would be via free
helpers, not inheritance. Source-neutral refactors: `QueryResult` relocated to
`query_result.py` (re-exported), runner default-adapter is now an overridable factory.
342 pipeline tests (38 new): Tier-1 fake-backed (run everywhere) + Tier-2 opt-in MySQL
integration (env-gated `ADAM_RUN_MYSQL_INTEGRATION`/`ADAM_MYSQL_TEST_DSN`, skips cleanly,
incl. the SQLite≡MySQL `QueryResult`-equivalence test). **Real MySQL schema introspection
(filling Slice 6's IntrospectionFn) is Slice 7b; a non-SQL CSV adapter is Slice 9.**
*Verified: refactors behavior-preserving (prior 304 green); translation allowlist-grounded +
%s-parameterized (injection probe lands in params); health reflects real connection state +
TTL-caches; ABC stays SQL-free; dialect private to mysql_adapter; no base class; lazy driver
(import adam.pipeline pulls in no adam.core and no pymysql).*

**✅ Slice 7b — Real MySQL schema introspector**
`adam/pipeline/mysql_introspector.py`: `MySQLIntrospector` fills Slice 6's `IntrospectionFn`
seam — reads `information_schema.columns` + `key_column_usage` (READ-ONLY; only
information_schema SELECTs) and returns a RICH `IntrospectedSchema`. The schema was enriched
behavior-preservingly: `EntitySchema`/`FieldSchema{name,source_type,nullable,primary_key}`/
`RelationshipSchema` (FKs), with a back-compat `field_names()` down-projection so the
SourceModel GROUNDING contract stays field-names and the Slice-6 lifecycle/flow is unchanged
(constructor also accepts the old name-only shapes). `schema_fingerprint` now covers
type/nullable/PK/FK and NORMALIZES ordering internally (entities/fields/relationships sorted
before hashing) so MySQL's nondeterministic information_schema row order can't change the
hash — schemas differing only in type/nullable/PK/FK now get different fingerprints
(change detection). Records carry an optional `schema_detail` (rich, audit) and reload is
tolerant of pre-7b name-only records. Driver lazy/injected; creds not hardcoded. 377 pipeline
tests (35 new): Tier-1 fake-information_schema (incl. read-only assertion, FK capture,
fingerprint change-detection, order-independence, old-record reload) + Tier-2 opt-in
integration (skips cleanly). **A non-SQL CSV adapter is Slice 9.**
*Verified: rich introspection read-only from information_schema; grounding contract +
lifecycle unchanged (prior 342 green); fingerprint covers detail and is order-normalized;
reload-tolerant; lazy driver (import adam.pipeline pulls in no adam.core and no pymysql).*

**⬜ Slice 8 — Pilot: one real question against a real source**  ← **REAL-DATA MILESTONE (north star)**
Ask a real question against a real source and return a governed, attributed answer.
Everything before this serves this milestone.

**⬜ Slice 9 — Second adapter (CSV)**
A non-SQL adapter. CSV has no joins/query engine, so the SAME ExecutionPlan flowing
through it (filter/group implemented as dataframe ops) proves the abstraction is TRULY
source-agnostic, not SQL-with-extra-steps. (A second SQL adapter proves only dialect
portability; CSV proves the thesis.) Two real adapters now exist → this is what earns
the `Capability` extraction in Slice 12.

**⬜ Slice 10 — mutation + raw_statement intent types** *(conditional)*
Write support + the restricted raw-statement escape hatch (write-blocked always, scope
enforced by connection grants not declarations, approval-gated by default). **Only if
a real use case needs writes.** Read-only analysis may never require this.

**⏸ Slice 11 — Composition (cross-source joins)** *(deferred by design)*
Cross-source join/merge of multiple single-source results. DEFERRED until its hardest
requirement is specified: join key, cardinality-mismatch behavior, merge memory
footprint. Do not build until that spec exists. Possibly never for v1.

**⏸ Slice 12 — Capability root interface** *(deferred by design)*
The `Input → Plan → Governance → Execution → Structured Result` root contract every
capability implements. DEFERRED until ≥2 real capabilities exist — ideally one that
WRITES (e.g. refactoring ADAM's existing document generation to speak this contract).
Extract from two working implementations; never predict from one. The single most
important deferral in the architecture — hold the line under build momentum.

---

## Milestones
- **Synthetic-data demo (≈ Slice 5):** governed, attributed answer on synthetic SQLite.
  Framatome-ready (mechanism shown on safe data; their real data never connects).
- **Real-data pilot (Slice 8):** governed, attributed answer against a real source.
  The north star. Build order past the demo should be PULLED by what real evaluators
  ask for, not pushed by this plan.

## Parallel track (independent timeline) — in-house AI server
Not blocking the pipeline or the demo. 4× RTX PRO 6000 Blackwell (deliberation tier) +
2× RTX 4090 (bounded tier). Confirm Blackwell-cannot-share-a-host-with-Ada (may be TWO
boxes). Standardize on vLLM. Pipeline built to the model-registry seam so swapping the
backend (cloud → in-house) is config, not a rewrite. See the pipeline handoff for the
B1–B4 sub-track and the model-quality caveat.
