#!/usr/bin/env python3
"""
ADAM email skill smoke test.

Standalone test harness that exercises skills/email/handler.py directly,
without running any LLM agents. Use this to verify:

  - .env credentials are correct
  - SMTP authentication works against your Workspace / mail server
  - The recipient allowlist accepts your test target
  - Attachments are resolved from the artifacts directory

Cost: ~$0. No API calls. Runs in seconds.

Usage:
    # Draft only (no SMTP transaction). Always safe.
    python test_email_skill.py --action draft --to childrda@lcps.k12.va.us

    # Actually send. Requires SMTP env vars and recipient on allowlist.
    python test_email_skill.py --action send --to childrda@lcps.k12.va.us

    # With attachment from the artifacts directory.
    python test_email_skill.py --action draft --to ... --attachment plan.docx

    # Override the artifacts directory (default: ./test_artifacts)
    python test_email_skill.py --artifacts-dir ./my_artifacts ...

The script will:
  1. Load .env from the project root (same loader ADAM uses).
  2. Print which env vars are set / missing.
  3. Create a tiny test artifact in the artifacts directory.
  4. Call the email handler directly with the action you chose.
  5. Print a structured result -- success details, or the error_class
     and remediation hint on failure.

If you see [smtp_auth_failed] -> fix ADAM_SMTP_USERNAME / PASSWORD in .env.
If you see [recipient_not_allowlisted] -> add the recipient to ADAM_EMAIL_RECIPIENT_ALLOWLIST.
If you see [allowlist_not_configured] -> set ADAM_EMAIL_RECIPIENT_ALLOWLIST in .env.
If you see [from_address_not_configured] -> set ADAM_EMAIL_FROM in .env.
If you see [smtp_connection_failed] -> check ADAM_SMTP_HOST / PORT and network.
If you see [smtp_tls_failed] -> check ADAM_SMTP_USE_TLS / USE_SSL combination.

The script exits 0 on success, 1 on handler failure, 2 on environment
or argument errors. Use this in CI or git pre-commit hooks if desired.
"""

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


# ============================================================
# .env loader (mirrors adam_agent_chat.py's loader exactly)
# ============================================================
# Reusing the same logic so behavior matches what ADAM sees at runtime.
# Existing environment variables take precedence over .env values, just
# like in adam_agent_chat.py, so a developer can override .env for a
# single test run without editing the file.

DOTENV_PATH = Path(".env")


def load_dotenv(path: Path = DOTENV_PATH) -> bool:
    """
    Read .env in the current directory and populate os.environ for any
    keys not already set. Returns True if the file was loaded, False
    if it wasn't found. Silently ignores malformed lines.
    """
    if not path.exists():
        return False
    with path.open(encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip surrounding quotes if present
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
    return True


# ============================================================
# Environment diagnostic
# ============================================================

# Keys grouped by whether they affect draft (FROM only) vs send (all of them).
DRAFT_REQUIRED = ["ADAM_EMAIL_FROM"]
SEND_REQUIRED = [
    "ADAM_EMAIL_FROM",
    "ADAM_SMTP_HOST",
    "ADAM_SMTP_USERNAME",
    "ADAM_SMTP_PASSWORD",
    "ADAM_EMAIL_RECIPIENT_ALLOWLIST",
]
SEND_OPTIONAL = [
    "ADAM_SMTP_PORT",        # default 587
    "ADAM_SMTP_USE_TLS",     # default true
    "ADAM_SMTP_USE_SSL",     # default false
]


def _redact(value: str) -> str:
    """Mask the middle of a value so logs can show 'something is set' without
    exposing the actual credential. SMTP passwords get fully masked; usernames
    show domain only."""
    if not value:
        return "(empty)"
    if len(value) <= 6:
        return "***"
    return f"{value[:3]}***{value[-3:]} (length {len(value)})"


def diagnose_env(action: str) -> Dict[str, Any]:
    """Print the state of every email-skill env var. Returns a structured
    diagnostic that the caller can use to decide whether to proceed."""
    print()
    print("=== Email skill environment ===")
    missing = []
    set_keys = {}

    required = SEND_REQUIRED if action == "send" else DRAFT_REQUIRED

    for key in required:
        value = os.environ.get(key, "").strip()
        if not value:
            missing.append(key)
            print(f"  {key:42s} = (NOT SET)")
        else:
            if "PASSWORD" in key:
                shown = _redact(value)
            elif "ALLOWLIST" in key:
                # Show the list contents -- not secret
                shown = value
            else:
                shown = value
            set_keys[key] = value
            print(f"  {key:42s} = {shown}")

    # Optional ones for send
    if action == "send":
        for key in SEND_OPTIONAL:
            value = os.environ.get(key, "").strip()
            print(f"  {key:42s} = {value or '(default)'}")

    print()
    if missing:
        print(f"Missing required env vars for action={action!r}: {missing}")
        print("Set them in .env or export them before running this test.")
    else:
        print(f"All required env vars present for action={action!r}.")

    # Sanity-check ADAM_EMAIL_FROM vs ADAM_SMTP_USERNAME for Workspace/Gmail
    # (Gmail enforces that From matches the authenticated user)
    if action == "send" and not missing:
        email_from = os.environ.get("ADAM_EMAIL_FROM", "").strip().lower()
        smtp_user  = os.environ.get("ADAM_SMTP_USERNAME", "").strip().lower()
        if email_from and smtp_user and email_from != smtp_user:
            print()
            print(f"WARNING: ADAM_EMAIL_FROM ({email_from}) does NOT match "
                  f"ADAM_SMTP_USERNAME ({smtp_user}).")
            print("Gmail / Google Workspace enforces that the From address "
                  "matches the authenticated user. The send may be silently "
                  "rewritten or rejected. Set both to the same value unless "
                  "you have a specific reason not to.")

    return {"missing": missing, "set": list(set_keys.keys())}


# ============================================================
# Test artifact setup
# ============================================================

def prepare_test_artifact(artifacts_dir: Path) -> Path:
    """Create a tiny .md file in the artifacts directory so we have
    something legitimate to attach (or not, depending on the test)."""
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifact = artifacts_dir / "smoke_test_artifact.md"
    artifact.write_text(
        f"# ADAM Email Skill Smoke Test\n\n"
        f"This is a test artifact produced by test_email_skill.py.\n\n"
        f"Generated: {datetime.now().isoformat(timespec='seconds')}\n"
    )
    return artifact


# ============================================================
# Handler invocation
# ============================================================

def call_handler(
    action:         str,
    to:             str,
    subject:        str,
    body:           str,
    artifacts_dir:  Path,
    attachment:     Optional[str] = None,
) -> Dict[str, Any]:
    """
    Import the email handler from skills/email/handler.py and call it
    directly with a test context. Returns the result dict on success,
    raises the underlying ValueError on failure.
    """
    # Import the handler from the project's skills directory. We use
    # importlib so we don't pollute sys.path with the skills directory
    # globally -- the handler imports cleanly as a standalone module.
    handler_path = Path("skills/email/handler.py")
    if not handler_path.exists():
        raise FileNotFoundError(
            f"Could not find {handler_path}. Run this script from the "
            f"project root (the directory containing adam_agent_chat.py)."
        )

    import importlib.util
    spec = importlib.util.spec_from_file_location("emailskill", handler_path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Build the same context shape SkillRuntime would provide
    context = {
        "invocation_id":        str(uuid.uuid4()),
        "session_id":           "smoke-test-" + datetime.now().strftime("%Y%m%d_%H%M%S"),
        "turn":                 1,
        "caller":               "Operator",
        "artifacts_root":       str(artifacts_dir),
        "requested_skill_args": {},
    }

    args: Dict[str, Any] = {
        "to":      to,
        "subject": subject,
        "body":    body,
    }
    if attachment:
        args["attachments"] = [attachment]

    return mod.handle(action, args, context)


# ============================================================
# Main
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Smoke-test ADAM's email skill without running agents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[1] if "Usage:" in (__doc__ or "") else "",
    )
    parser.add_argument(
        "--action", choices=("draft", "send"), default="draft",
        help="draft (default; produces .eml locally, no SMTP) or send "
             "(actually transmits via SMTP)",
    )
    parser.add_argument(
        "--to", required=True,
        help="Recipient email address. For 'send', must be on the "
             "ADAM_EMAIL_RECIPIENT_ALLOWLIST configured in .env.",
    )
    parser.add_argument(
        "--subject", default="ADAM email skill smoke test",
        help="Email subject line",
    )
    parser.add_argument(
        "--body", default=None,
        help="Email body. If omitted, a default test body is used.",
    )
    parser.add_argument(
        "--attachment", default="smoke_test_artifact.md",
        help="Attachment filename (within the artifacts directory). Pass "
             "an empty string to skip attaching anything.",
    )
    parser.add_argument(
        "--no-attachment", action="store_true",
        help="Skip the attachment entirely (no file is attached).",
    )
    parser.add_argument(
        "--artifacts-dir", default="./test_artifacts",
        help="Directory where the test artifact is created and where "
             "the resulting .eml (for draft) is saved. Default: "
             "./test_artifacts",
    )
    parser.add_argument(
        "--no-prepare-artifact", action="store_true",
        help="Don't create a test artifact in the artifacts directory. "
             "Useful when testing 'attachment_not_found' behavior.",
    )

    args = parser.parse_args()

    # Step 1: Load .env so credentials become available
    dotenv_loaded = load_dotenv()
    if dotenv_loaded:
        print(f"Loaded .env from {DOTENV_PATH.resolve()}")
    else:
        print(f"No .env file found at {DOTENV_PATH.resolve()}; relying "
              f"on shell environment variables.")

    # Step 2: Diagnose env state
    diag = diagnose_env(args.action)
    if diag["missing"]:
        print()
        print("Cannot proceed -- required env vars are missing. Fix .env "
              "and re-run.")
        return 2

    # Step 3: Prepare a test artifact (so attachments work)
    artifacts_dir = Path(args.artifacts_dir).resolve()
    if not args.no_prepare_artifact:
        artifact = prepare_test_artifact(artifacts_dir)
        print()
        print(f"Test artifact: {artifact}")
    else:
        print()
        print(f"Skipping artifact preparation per --no-prepare-artifact")

    # Step 4: Build the body
    body = args.body or (
        f"This is an automated smoke test of the ADAM email skill.\n\n"
        f"Sent from test_email_skill.py at "
        f"{datetime.now().isoformat(timespec='seconds')}.\n\n"
        f"If you received this message, the email pipeline is working "
        f"end-to-end: .env credentials, SMTP authentication, recipient "
        f"allowlist, attachment resolution, and message delivery all "
        f"succeeded.\n\n"
        f"No action required."
    )

    # Step 5: Call the handler
    attachment_arg = None if args.no_attachment or not args.attachment else args.attachment

    print()
    print(f"=== Invoking email.{args.action} ===")
    print(f"  to:         {args.to}")
    print(f"  subject:    {args.subject}")
    print(f"  attachment: {attachment_arg or '(none)'}")
    print(f"  artifacts:  {artifacts_dir}")
    print()

    try:
        result = call_handler(
            action=args.action,
            to=args.to,
            subject=args.subject,
            body=body,
            artifacts_dir=artifacts_dir,
            attachment=attachment_arg,
        )
    except ValueError as e:
        # The handler raises ValueError with a structured error_class
        # in the message (and on the exception attribute). Extract and
        # display both so the user sees the precise failure mode.
        error_class = getattr(e, "adam_error_class", None) or "unknown"
        print(f"FAILED: [{error_class}]")
        print()
        print(f"  {e}")
        print()
        print(f"Look up [{error_class}] in skills/email/SKILL.md or "
              f"skills/email/handler.py for remediation guidance.")
        return 1
    except Exception as e:
        # Unexpected exception class -- the handler should always raise
        # ValueError, so this indicates a different issue (probably a
        # bug in this test harness or the handler itself).
        print(f"UNEXPECTED ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1

    # Step 6: Display the result
    print(f"SUCCESS")
    print()
    print(f"Result:")
    # Pretty-print the result dict, masking anything that looks
    # like a credential (defense in depth -- the handler is supposed
    # to not return credentials, but we double-check)
    safe_result = {}
    for k, v in result.items():
        if k.lower() in ("password", "smtp_password", "credential"):
            safe_result[k] = "<redacted>"
        else:
            safe_result[k] = v
    print(json.dumps(safe_result, indent=2, default=str))

    if args.action == "draft":
        path = result.get("path")
        if path and Path(path).exists():
            print()
            print(f"The .eml file is at: {path}")
            print(f"Open it in your mail client (Outlook, Apple Mail, "
                  f"Thunderbird) to verify the recipient, subject, "
                  f"body, and attachment look correct.")
    elif args.action == "send":
        print()
        print(f"Email transmitted. Check {args.to} inbox.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
