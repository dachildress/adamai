"""
ADAM thin entry point.

After the refactor, this file does roughly three things:
  1. Parse CLI args (delegated to adam.cli.args)
  2. Build the SessionContext (delegated to adam.core.session)
  3. Run the deliberation loop (delegated to adam.core.loop)

While extraction is in progress, this file may temporarily import from
adam_agent_chat.py at the repo root. That import disappears once
extraction completes.
"""

def main() -> int:
    """Entry point. Returns process exit code."""
    # During refactor:
    #   from adam.cli.args import parse_args
    #   from adam.core.session import build_session_context
    #   from adam.core.loop import run
    #   args = parse_args()
    #   ctx = build_session_context(args)
    #   return run(ctx)
    raise NotImplementedError("Refactor in progress")


if __name__ == "__main__":
    import sys
    sys.exit(main())
