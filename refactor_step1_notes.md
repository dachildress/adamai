# ADAM refactor — Step 1: scaffold

What this package contains:

- The empty directory structure for the modular `adam/` package
- `__init__.py` files for every subpackage, with docstrings naming what
  each subsystem will own
- Placeholder `.py` stubs for every module that will be populated during
  extraction (each one is a single docstring saying "populated during
  step N")
- The fully-drafted **events.py** schema in `adam/core/events.py` — this
  is the only file with real content. Typed dataclasses for every event
  the runtime will emit. Importable, constructible, serializable. Step 6
  will wire emission sites; the schema is locked in now so extraction
  doesn't bury fields the GUI will need.
- A 30-line `main.py` placeholder that raises NotImplementedError until
  step 7 finishes
- `tests/`, `docs/`, `logs/` directories
- An architecture note placeholder

What's NOT in this package:

- The existing `adam_agent_chat.py` (you have it, don't overwrite it)
- The `config/`, `prompts/`, `skills/` directories (they stay at the
  repo root unchanged)
- The patched `adam_agent_chat.py` runtime (still drives sessions during
  the refactor)
- The `test_continuation_budget.py` and `test_trust_registry.py` test
  files (keep where they are; move to `tests/` during a later step)

## How to apply

From the directory containing your existing `adam_agent_chat.py`,
`config/`, `prompts/`, etc.:

```bash
tar -xzf adam_scaffold.tar.gz --strip-components=1
```

Or extract to a side directory first and inspect:

```bash
tar -xzf adam_scaffold.tar.gz
diff -r adam_scaffold/ .   # see what would change
```

Then `cp -rn adam_scaffold/* .` to copy only files that don't already
exist (won't overwrite anything).

## Smoke test after applying

```bash
python3 -c "
import adam
import adam.core
import adam.core.events
import adam.verifier
import adam.context
import adam.skills_runtime
import adam.cli
print(f'adam package imports cleanly, version: {adam.__version__}')

from adam.core.events import (
    SessionStartedEvent, AgentTurnEvent, VerificationEvent,
    SkillCallEvent, ArtifactEvent, VerdictStatus, TurnRole
)
print('events module classes import: ok')
"
```

If that prints "adam package imports cleanly" and "events module classes
import: ok", step 1 is complete and you're ready for step 2 (verifier
extraction).

## What changes in your existing codebase

Nothing. The scaffold is additive — it sits alongside the existing
single-file `adam_agent_chat.py` and does not modify it. The runtime
continues to work exactly as before. The new `adam/` package is empty
of behavior; it's just a destination for extraction work that begins
in step 2.

You can run a session with `python adam_agent_chat.py ...` and it works
identically. You can also `from adam.core.events import AgentTurnEvent`
and use the dataclasses (e.g. in tests or a separate experiment), but
the runtime doesn't emit them yet.

## What's next

**Step 2: verifier extraction.** Move the Truthseeker subsystem from
`adam_agent_chat.py` into the four `adam/verifier/*.py` files:

- `claim_extractor.py` — CLAIM_CANDIDATE_PATTERNS, extract_claim_candidates,
  extract_structured_claims, extract_document_grounded_claims
- `trust_boundary.py` — TrustRegistry, _TRUST_REGISTRY_MIN_LENGTH,
  build_trust_registry
- `web_search.py` — searxng_search, trafilatura_extract, source tier
  classification
- `policy_rules.py` — verify_claim, apply_verification_policy,
  format_verification_summary, format_verification_for_transcript

The verifier is the right place to start because:

1. It's the most self-contained subsystem (no upward dependencies on
   the rest of the runtime; everything is verifier → runtime, not the
   other way)
2. It has the best test coverage: `test_trust_registry.py` catches
   regressions immediately
3. The trust-boundary fix is recent — the code is fresh in our minds
   and well-documented

After step 2 lands, `adam_agent_chat.py` will import from
`adam.verifier.*` instead of having the verifier code inline. The
single file shrinks by roughly 1,200 lines. The runtime behavior is
identical. The tests pass identically.
