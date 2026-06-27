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

**⬜ Slice 3 — Adapter interface + cost/health**
Formalize the adapter contract every adapter implements: capability advertisement,
health states (READY / DEGRADED / REINDEXING / OFFLINE / AUTHENTICATION_FAILED),
adapter-supplied cost estimate, and the cost Sentinel predicate consuming it. SQLite
adapter conforms to the new interface. (Before the skill, so the model later plans
against a stable adapter contract.)

**⬜ Slice 4 — Data Intelligence skill emits plans**
First model involvement: a model turns an objective ("which schools have the highest
absenteeism?") into a structured ExecutionPlan, via the existing model-registry /
LlmClient seam (so the backend stays swappable for the in-house server). Semantic
planning only; physical planning stays in the adapter.

**⬜ Slice 5 — SkillResult + attribution**  ← **SYNTHETIC-DATA DEMO MILESTONE (Framatome-ready)**
Typed output: data_analyzed, reasoning, assumptions, limitations, confidence,
confidence_rationale, source_lineage; fact / inference / recommendation separation
(Truthseeker pattern), carried as structured data not closing prose. After this,
question → governed, attributed answer on synthetic data — demoable.

**⬜ Slice 6 — RAG source-model ingestion + publish-time validation**
Admin connects a source → runtime generates/embeds/ratifies a versioned source model
into the RAG corpus (connect, validate access, pull metadata for allowed entities,
generate model, embed, admin approves, ratify with a new `source_model_version`).
Required before real-data use. (Ahead of the Capability interface: it unblocks pilots;
the Capability interface unblocks nothing real yet.)

**⬜ Slice 7 — Real SQL adapter**
Postgres or SQL Server adapter against a real (sanitized) source. Conforms to the
Slice-3 adapter interface.

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
