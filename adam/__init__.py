"""
ADAM — Adaptive Dynamic Agent Multiplex
Governance-first multi-agent deliberation runtime.

The runtime is decomposed into five subsystems:
  - adam.core         orchestration, routing, session lifecycle
  - adam.verifier     Truthseeker claim extraction, trust boundary, verification
  - adam.context      context file detection, extraction, budget management
  - adam.skills_runtime  skill manifest loading and execution
  - adam.cli          command-line argument parsing

Runtime assets (configs, prompts, skill manifests) live at the repo root
in config/, prompts/, and skills/, not inside this package.
"""

__version__ = "0.9.4"
