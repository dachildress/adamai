"""
ADAM GUI user management CLI.

Run from the V4/V5 project root:

    python gui/manage_users.py add
    python gui/manage_users.py list
    python gui/manage_users.py grant <username> --sessions N
    python gui/manage_users.py disable <username>
    python gui/manage_users.py enable <username>

All commands operate on gui/users.json under file lock, so it's safe
to run them while the GUI server is also running -- but obviously you
should only do that if you know what you're doing.

`add` is interactive: it prompts for each required field, validates as
you go, and asks for the password twice (hidden). The password gets
bcrypt-hashed before being written; the plaintext never touches disk
and is not stored in shell history.

This script is intentionally simple. It doesn't have config files,
doesn't have plugins, doesn't have a fancy UI. It exists so you don't
have to edit users.json by hand and so passwords are never typed in
plaintext on the command line.

Note on running: this is a CLI script, not a module. It uses argparse
subcommands and is meant to be invoked directly. The auth module
init is done at top of main() so the script knows where users.json
lives.
"""
from __future__ import annotations

import argparse
import getpass
import re
import sys
from pathlib import Path

# Make 'backend.auth' importable when run from the project root.
HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(HERE))

from backend import auth  # noqa: E402


# ============================================================
# Helpers
# ============================================================

def _prompt(label: str, *, default: str = None, required: bool = True,
            validator=None) -> str:
    """
    Prompt for a single field. Re-prompts on invalid input. Empty
    input returns the default if one is provided.
    """
    while True:
        suffix = f" [{default}]" if default else ""
        value = input(f"{label}{suffix}: ").strip()
        if not value and default is not None:
            value = default
        if not value:
            if required:
                print("  (required)")
                continue
            return ""
        if validator:
            err = validator(value)
            if err:
                print(f"  {err}")
                continue
        return value


def _prompt_password(label: str = "password") -> str:
    """
    Prompt for a password twice (hidden) and confirm they match.
    Re-prompts the whole pair on mismatch.
    """
    while True:
        a = getpass.getpass(f"{label}: ")
        if not a:
            print("  (required)")
            continue
        if len(a) < 8:
            print("  password must be at least 8 characters")
            continue
        b = getpass.getpass(f"{label} (confirm): ")
        if a != b:
            print("  passwords do not match; try again")
            continue
        return a


def _validate_username(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9_.-]+", value):
        return "username must be lowercase letters, digits, underscore, dot, or hyphen"
    if len(value) > 64:
        return "username must be 64 characters or fewer"
    return ""


def _validate_email(value: str) -> str:
    # Loose validation: one @, dot in domain. We're not running an
    # SMTP server -- we just need it to look like an email.
    if value.count("@") != 1:
        return "email must contain exactly one @"
    local, domain = value.split("@")
    if not local or "." not in domain:
        return "email does not look valid"
    return ""


def _validate_int(value: str) -> str:
    try:
        n = int(value)
        if n < -1:
            return "must be -1 (unlimited) or a non-negative integer"
        return ""
    except ValueError:
        return "must be an integer"


# ============================================================
# Commands
# ============================================================

def cmd_add(args: argparse.Namespace) -> int:
    """
    Interactively add a new user. Prompts for all required fields.
    Uses sensible defaults for sessions_remaining (3) and
    max_turns_per_session (10), which the user can override.
    """
    roles = auth.list_roles()
    if not roles:
        print("ERROR: no roles defined in users.json -- run nothing else first to seed defaults",
              file=sys.stderr)
        return 1

    role_names = sorted(roles.keys())
    print()
    print("=" * 60)
    print(" Add a new ADAM user")
    print("=" * 60)
    print()
    print("Available roles:")
    for name in role_names:
        desc = roles[name].get("description", "")
        denied = roles[name].get("skills_denied", [])
        denied_str = f"  (denies: {', '.join(denied)})" if denied else ""
        print(f"  - {name}{denied_str}")
        if desc:
            print(f"      {desc}")
    print()

    username = _prompt("username", validator=_validate_username)
    existing = auth.get_user(username)
    if existing is not None:
        print(f"ERROR: user {username!r} already exists", file=sys.stderr)
        return 1

    display_name = _prompt("display name")
    email = _prompt("email", validator=_validate_email)

    while True:
        role = _prompt("role", default=role_names[-1])
        if role in role_names:
            break
        print(f"  unknown role; choose from: {', '.join(role_names)}")

    # Suggest sensible defaults based on role
    if role == "admin":
        default_sessions = "-1"
        default_turns    = "-1"
    else:
        default_sessions = "3"
        default_turns    = "10"

    sessions = int(_prompt(
        "sessions_remaining (-1 for unlimited)",
        default=default_sessions, validator=_validate_int,
    ))
    max_turns = int(_prompt(
        "max_turns_per_session (-1 for unlimited)",
        default=default_turns, validator=_validate_int,
    ))

    while True:
        status = _prompt("status (active or suspended)", default="active")
        if status in ("active", "suspended"):
            break
        print("  status must be 'active' or 'suspended'")

    password = _prompt_password()

    print()
    print("Creating user:")
    print(f"  username:               {username}")
    print(f"  display_name:           {display_name}")
    print(f"  email:                  {email}")
    print(f"  role:                   {role}")
    print(f"  sessions_remaining:     {sessions if sessions != -1 else 'unlimited'}")
    print(f"  max_turns_per_session:  {max_turns if max_turns != -1 else 'unlimited'}")
    print(f"  status:                 {status}")
    print()

    confirm = input("Proceed? [y/N]: ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return 1

    try:
        auth.add_user(
            username=username,
            display_name=display_name,
            email=email,
            role=role,
            password=password,
            sessions_remaining=sessions,
            max_turns_per_session=max_turns,
            status=status,
        )
    except (ValueError, KeyError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print()
    print(f"User {username!r} created.")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """List all users in a table."""
    users = auth.list_users()
    if not users:
        print("No users defined.")
        return 0

    # Column widths sized to content
    headers = ["username", "role", "status", "sessions", "max_turns", "email", "last_login"]
    rows = []
    for username, u in sorted(users.items()):
        sessions = u.get("sessions_remaining", 0)
        max_turns = u.get("max_turns_per_session", 0)
        rows.append([
            username,
            u.get("role", ""),
            u.get("status", ""),
            "unlimited" if sessions == -1 else str(sessions),
            "unlimited" if max_turns == -1 else str(max_turns),
            u.get("email", ""),
            u.get("last_login_at") or "(never)",
        ])

    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for row in rows:
        print(fmt.format(*row))
    print(f"\n({len(rows)} users)")
    return 0


def cmd_grant(args: argparse.Namespace) -> int:
    """
    Bump sessions_remaining by N for a user. Refuses if the user is
    already unlimited (no point bumping infinity).
    """
    user = auth.get_user(args.username)
    if user is None:
        print(f"ERROR: user not found: {args.username}", file=sys.stderr)
        return 1
    if user.get("sessions_remaining") == -1:
        print(f"ERROR: {args.username} is already unlimited; nothing to grant",
              file=sys.stderr)
        return 1
    if args.sessions <= 0:
        print("ERROR: --sessions must be a positive integer", file=sys.stderr)
        return 1

    def _mod(u):
        u["sessions_remaining"] = u.get("sessions_remaining", 0) + args.sessions
    updated = auth.update_user(args.username, _mod)
    print(f"Granted {args.sessions} sessions to {args.username}. "
          f"New total: {updated['sessions_remaining']}.")
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    """Set a user's status to suspended."""
    user = auth.get_user(args.username)
    if user is None:
        print(f"ERROR: user not found: {args.username}", file=sys.stderr)
        return 1
    if user.get("status") == "suspended":
        print(f"{args.username} is already suspended.")
        return 0
    def _mod(u):
        u["status"] = "suspended"
    auth.update_user(args.username, _mod)
    print(f"User {args.username!r} suspended.")
    print("Their active login sessions will be invalidated on next request.")
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    """Reactivate a suspended user."""
    user = auth.get_user(args.username)
    if user is None:
        print(f"ERROR: user not found: {args.username}", file=sys.stderr)
        return 1
    if user.get("status") == "active":
        print(f"{args.username} is already active.")
        return 0
    def _mod(u):
        u["status"] = "active"
    auth.update_user(args.username, _mod)
    print(f"User {args.username!r} reactivated.")
    return 0


# ============================================================
# Entry point
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="ADAM GUI user management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--gui-root", type=Path, default=HERE,
        help="Path to the gui/ directory containing users.json. "
             "Defaults to the dir this script lives in.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("add", help="Interactively add a new user").set_defaults(func=cmd_add)
    sub.add_parser("list", help="List all users").set_defaults(func=cmd_list)

    p_grant = sub.add_parser("grant", help="Grant more sessions to a user")
    p_grant.add_argument("username")
    p_grant.add_argument("--sessions", type=int, required=True,
                         help="Number of sessions to add")
    p_grant.set_defaults(func=cmd_grant)

    p_disable = sub.add_parser("disable", help="Suspend a user")
    p_disable.add_argument("username")
    p_disable.set_defaults(func=cmd_disable)

    p_enable = sub.add_parser("enable", help="Reactivate a suspended user")
    p_enable.add_argument("username")
    p_enable.set_defaults(func=cmd_enable)

    args = parser.parse_args()
    auth.init_auth(args.gui_root)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
