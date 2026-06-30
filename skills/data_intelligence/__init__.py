"""Data Intelligence skill — governed, read-only data retrieval for agents.

Package layout (so handler.py can use relative imports for its helpers):
  - handler.py : the runtime entry point, handle(action, args, context)
  - store.py   : session-local scope/budget/evidence persistence + the
                 DATA_RESULT builder and the shared-core call.
"""
