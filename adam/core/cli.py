"""
CLI argument parsing and lifecycle helpers.

Owns:
  - parse_args:              the argparse.ArgumentParser definition
  - apply_runtime_defaults:  fold runtime.json session_defaults into argparse Namespace
                             (only fills in values not provided on the CLI)
  - load_seed:               resolve the deliberation seed (--seed > seed_file)
  - _seed_source_label:      build a human-readable description of the seed source
  - fatal:                   print error to stderr and sys.exit(1)

This module is import-safe with one dependency: adam.core.config_loader
(for the _rt accessor that reads runtime.json values). ConfigError is
the canonical signal for "config-time problem"; main() catches it and
calls fatal() to exit cleanly.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from adam.core.exceptions import ConfigError
from adam.core.config_loader import _rt


# ============================================================
# CLI
# ============================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ADAM multi-agent simulation with external verification.")
    # CLI flags default to None so runtime.json values can be applied as
    # the resolved default after the config is loaded. If a flag IS
    # provided, it overrides the runtime.json default for this run.
    p.add_argument("--max-turns",        type=int,   default=None,
                   help="Maximum number of turns for this session. "
                        "Overrides session_defaults.max_turns in runtime.json.")
    p.add_argument("--seed",             type=str,   default=None,
                   help="Deliberation seed (the initial question/prompt). "
                        "Overrides the contents of the seed file "
                        "(session_defaults.seed_file in runtime.json, "
                        "default prompts/seed.md). For long seeds, prefer "
                        "editing the seed file directly or using --seed-file.")
    p.add_argument("--seed-file",        type=str,   default=None,
                   help="Path to a file whose contents are used as the "
                        "deliberation seed. Takes precedence over --seed and "
                        "over the runtime.json session_defaults.seed_file. "
                        "Used by the GUI when it spawns ADAM with a "
                        "GUI-collected seed, but also useful for any "
                        "workflow that wants a per-session seed file "
                        "outside the runtime config.")
    p.add_argument("--session-id",       type=str,   default=None,
                   help="Pre-assigned session ID (UUID-like string). If "
                        "provided, ADAM uses this ID for its session "
                        "directory and event log instead of generating a "
                        "new UUID at startup. Used by the GUI to pre-create "
                        "the session directory (with seed.md + uploaded "
                        "context files in place) before spawning ADAM. "
                        "Must be a clean string with no path separators or "
                        "shell metacharacters.")
    p.add_argument("--delay",            type=float, default=None,
                   help="Inter-turn delay in seconds. "
                        "Overrides session_defaults.delay_seconds.")
    p.add_argument("--history-messages", type=int,   default=None,
                   help="History window size (number of recent messages). "
                        "Overrides session_defaults.history_messages.")
    p.add_argument("--synth-cadence",    type=int,   default=None,
                   help="Force Synthesizer turn every N advisory turns. "
                        "Overrides session_defaults.synth_cadence.")
    p.add_argument("--no-verify",        action="store_true",
                   help="Disable Truthseeker for this run (debugging only)")
    p.add_argument("--director-name",    type=str,   default=None,
                   help="Display name shown in transcript and audit for the human "
                        "operator. If not provided, ADAM prompts interactively at "
                        "startup. Falls back to 'Director' if non-interactive (e.g., "
                        "redirected stdin) or if the prompt is left blank.")
    p.add_argument("--director-user-id", type=str,   default=None,
                   help="v5 multi-user: explicit director user_id (short identifier, "
                        "no @domain). Overrides ADAM_DEFAULT_DIRECTOR from .env. The "
                        "GUI passes this on every spawn to ensure logs land under "
                        "the authenticated user's directory (logs/<user_id>/<session>/), "
                        "not the .env default. For CLI invocations, leave this unset "
                        "and rely on .env.")
    p.add_argument("--director-email",   type=str,   default=None,
                   help="v5 multi-user: explicit director email. Overrides "
                        "ADAM_DEFAULT_DIRECTOR_EMAIL from .env. Used in audit and "
                        "metadata fields. The GUI passes this alongside "
                        "--director-user-id and --director-name based on the "
                        "authenticated user's profile.")
    p.add_argument("--governance-profile-id", type=str, default=None,
                   help="Slice 1/3: the governance profile id governing this "
                        "session (e.g. 'general', 'education'). Recorded in "
                        "session state and used by the runtime policy gate. "
                        "If unset, the policy gate is permissive (off).")
    p.add_argument("--policy-bounds",    type=str,   default=None,
                   help="Slice 3: the resolved policy-bounds ruleset for this "
                        "session, as a compact JSON string. The post-synthesis "
                        "policy gate consults this to decide whether Operator's "
                        "planned action is permitted before Operator runs. If "
                        "unset or unparseable, the gate is permissive (off).")
    p.add_argument("--resume-after-review", action="store_true",
                   help="Slice 4a: this session is a RESUME of one that "
                        "paused at the human-review gate. The director has "
                        "approved/redirected; the synthesis is already "
                        "settled (composed into the seed). Skip deliberation "
                        "and route directly to Operator. The review gate is "
                        "not re-evaluated on a resumed run.")
    # Context Loader flags. Pass 1 accepts these and enumerates files but
    # does not yet inject anything into deliberation (loader is Pass 2).
    p.add_argument("--context-dir",      type=str,   default=None,
                   help="Path to a directory of context files to load as background "
                        "for this deliberation. Text formats (.md, .txt, .docx, .pdf) "
                        "are summarized or passed through. Structured data formats "
                        "(.csv, .xlsx, .json) are detected and recorded in the audit "
                        "but NOT loaded in v1 (use --context-file for explicit per-file "
                        "selection). Combinable with --context-file.")
    p.add_argument("--context-file",     type=str,   action="append", default=None,
                   help="Path to a single context file (repeatable). Combine with or "
                        "use instead of --context-dir.")
    p.add_argument("--yes-context-risk", action="store_true",
                   help="Skip the privacy-confirmation prompt for context files. "
                        "By default, ADAM requires explicit confirmation before any "
                        "context content is sent to model providers. Use only in "
                        "automation contexts where you've already vetted file safety.")
    p.add_argument("--override-context-limit", action="store_true",
                   help="Override the hard refusal limit if context exceeds "
                        "context.hard_refusal_tokens after summarization. The session "
                        "will run with the full context block regardless. Use sparingly "
                        "-- this can substantially inflate per-turn token cost.")
    p.add_argument("--skill-arg", type=str, action="append", default=None,
                   metavar="skill.action.arg=value",
                   help="Provide a generic skill argument that Operator MAY use when "
                        "invoking the named skill+action. Repeatable. The format is "
                        "strict: skill.action.arg=value (three dot-separated identifier "
                        "components on the left of the equals sign). "
                        "Example: --skill-arg email.send.to=user@example.com "
                        "IMPORTANT: skill args are SUGGESTIONS made available to "
                        "Operator, not commands. The presence of a skill arg does NOT "
                        "cause its skill to execute. Operator decides whether to "
                        "invoke a skill based on deliberation outcomes. ADAM core does "
                        "not know which skills, actions, or arg names are valid -- "
                        "it parses generically so new skills can be added without "
                        "core code changes. Skill names appearing here are not "
                        "validated against the catalog at parse time; only the format "
                        "is enforced. Values with whitespace must be shell-quoted: "
                        "--skill-arg email.send.subject=\"Hello world\". Avoid passing "
                        "secrets via this flag -- use environment variables instead.")
    p.add_argument("--disable-skill", type=str, action="append", default=None,
                   metavar="SKILL_NAME",
                   help="Mark a skill as denied for this session. Repeatable. "
                        "The named skill is removed from the catalog presented "
                        "to agents, so Operator can't propose calling it and "
                        "the runtime rejects any attempt to invoke it. "
                        "Used by the GUI to enforce per-user role policy: "
                        "a pilot-role user spawns ADAM with "
                        "--disable-skill email so deliberation never produces "
                        "an email.send call. This is enforcement at the "
                        "registry layer; the existing allowed_callers "
                        "mechanism in SKILL.md is agent-side authorization "
                        "(which agent can invoke), this flag is user-side "
                        "authorization (whether this session can use the "
                        "skill at all). Names are matched case-sensitively "
                        "against the skill catalog. Unknown names are "
                        "silently ignored -- the goal is to deny, not to "
                        "validate the registry. Example: "
                        "--disable-skill email --disable-skill coder")
    return p.parse_args()


def apply_runtime_defaults(args: argparse.Namespace) -> None:
    """
    Apply runtime.json session_defaults to any CLI args that were not
    explicitly provided. Mutates the argparse Namespace in place.
    Must be called AFTER load_and_validate_runtime_config().
    """
    if args.max_turns is None:
        args.max_turns = _rt("session_defaults", "max_turns")
    if args.delay is None:
        args.delay = _rt("session_defaults", "delay_seconds")
    if args.history_messages is None:
        args.history_messages = _rt("session_defaults", "history_messages")
    if args.synth_cadence is None:
        args.synth_cadence = _rt("session_defaults", "synth_cadence")




def load_seed(args: argparse.Namespace) -> str:
    """
    Resolve the deliberation seed text. Resolution order:
      1. --seed CLI flag if provided (literal string used as-is)
      2. --seed-file CLI flag if provided (path read at runtime)
      3. Contents of file at runtime.json session_defaults.seed_file
         (default: prompts/seed.md)
      4. Fail with a clear error if none yields a non-empty seed

    The seed file is text (markdown or plain). Leading/trailing whitespace
    is stripped; an empty file is treated as no seed.

    Records the resolution source on args._seed_source for the startup
    banner (cli_flag | seed_file_flag:<path> | seed_file:<path>). This
    avoids the alternative of re-reading the file later and comparing
    contents, which would be fragile across whitespace and encoding edge
    cases.
    """
    if args.seed is not None and args.seed.strip():
        args._seed_source = "cli_flag"
        return args.seed.strip()

    # Part 9: --seed-file is the canonical path for GUI-launched sessions.
    # The GUI writes the seed to <session_dir>/seed.md and passes the
    # path here, which sidesteps shell-quoting and ps-visibility issues
    # with long seed strings.
    if getattr(args, "seed_file", None):
        seed_file_path = Path(args.seed_file)
        if not seed_file_path.exists():
            raise ConfigError(
                f"--seed-file path does not exist: '{seed_file_path}'."
            )
        try:
            content = seed_file_path.read_text(encoding="utf-8").strip()
        except Exception as e:
            raise ConfigError(
                f"Failed to read --seed-file '{seed_file_path}': "
                f"{type(e).__name__}: {e}"
            )
        if not content:
            raise ConfigError(
                f"--seed-file '{seed_file_path}' is empty."
            )
        args._seed_source = f"seed_file_flag ({seed_file_path})"
        return content

    seed_file_path = Path(_rt("session_defaults", "seed_file"))
    if not seed_file_path.exists():
        raise ConfigError(
            f"No seed provided. Neither --seed nor --seed-file was given "
            f"on the command line, and the configured seed file does not "
            f"exist at '{seed_file_path}'. Create that file with the "
            f"deliberation question, or use --seed \"...\" or "
            f"--seed-file <path> at runtime."
        )
    try:
        content = seed_file_path.read_text(encoding="utf-8").strip()
    except Exception as e:
        raise ConfigError(
            f"Failed to read seed file '{seed_file_path}': {type(e).__name__}: {e}"
        )
    if not content:
        raise ConfigError(
            f"Seed file '{seed_file_path}' is empty. Either populate it with "
            f"the deliberation question or use --seed / --seed-file at runtime."
        )
    args._seed_source = f"seed_file ({seed_file_path})"
    return content


def _seed_source_label(args: argparse.Namespace) -> str:
    """
    Return the human-readable seed source label for the startup banner.
    Set by load_seed() during seed resolution.
    """
    return getattr(args, "_seed_source", "unknown")





def fatal(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)
