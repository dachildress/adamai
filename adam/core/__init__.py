"""
ADAM core orchestration.

This package owns the deliberation loop, agent routing, model dispatch,
config loading, and session lifecycle. It is the brain of the runtime.

Module map (refactor in progress):
  - config_loader.py    runtime/agents/models/providers JSON loaders & validators
  - client_dispatch.py  provider client cache, call_model with retry/backoff
  - router.py           select_next_speaker, Sentinel/WrapUp/Director state
  - session.py          SessionContext, log dir setup, signal handlers, session_state
  - loop.py             the per-turn while-loop body
  - events.py           typed event records for GUI consumption (step 6)
"""
