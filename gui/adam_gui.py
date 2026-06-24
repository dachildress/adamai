"""
ADAM GUI launcher.

Single-file entry point that starts the FastAPI backend and opens
the browser to the GUI. Run from the ADAM project root:

    python adam_gui.py [--port 8765] [--no-browser]

The backend reads:
  - ./logs/                      — sessions to display
  - ./.env                       — director identity (ADAM_DEFAULT_DIRECTOR)

The backend serves:
  - http://localhost:8765/       — React frontend (must be built first)
  - http://localhost:8765/api/*  — JSON API + SSE event stream

First-time setup:
    cd gui/frontend
    npm install
    npm run build
    cd ../..
    python adam_gui.py --open-browser

After that, just `python adam_gui.py --open-browser` to launch.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the backend importable from anywhere
HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(HERE))


# ============================================================
# Dependency check
# ============================================================
#
# The GUI imports a small set of third-party packages indirectly via
# backend.server. If any are missing, FastAPI's import-time errors
# can be loud and obscure (notably the python-multipart RuntimeError
# at app-build time, which appears mid-traceback rather than as a
# clean "missing module" error).
#
# This pre-flight check catches the common cases and prints one clean
# line telling the user exactly what to do. The list mirrors
# gui/requirements.txt; if you add a new GUI dep, add it here too.

_REQUIRED_GUI_PACKAGES = [
    # (import_name, pip_install_name)
    ("fastapi",       "fastapi"),
    ("uvicorn",       "uvicorn"),
    ("sse_starlette", "sse-starlette"),
    ("pydantic",      "pydantic"),
    ("multipart",     "python-multipart"),
]


def _check_gui_deps() -> None:
    """
    Verify that all required GUI packages are importable. Exit cleanly
    with installation instructions if any are missing, rather than
    letting FastAPI's import-time RuntimeError pollute the screen.
    """
    missing: list[tuple[str, str]] = []
    for import_name, pip_name in _REQUIRED_GUI_PACKAGES:
        try:
            __import__(import_name)
        except ImportError:
            missing.append((import_name, pip_name))

    if not missing:
        return

    print("=" * 72, file=sys.stderr)
    print("Missing GUI dependencies", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    print("", file=sys.stderr)
    print("The following packages are required but not installed in this venv:",
          file=sys.stderr)
    for import_name, pip_name in missing:
        print(f"  - {pip_name} (import name: {import_name})", file=sys.stderr)
    print("", file=sys.stderr)
    print("Install with:", file=sys.stderr)
    print("", file=sys.stderr)
    print("    pip install -r gui/requirements.txt", file=sys.stderr)
    print("", file=sys.stderr)
    print("If pip reports success but the packages are still not importable,",
          file=sys.stderr)
    print("the venv may be pointing at a different Python interpreter. Verify with:",
          file=sys.stderr)
    print("", file=sys.stderr)
    print("    which python && which pip", file=sys.stderr)
    print("    head -1 venv/bin/pip", file=sys.stderr)
    print("", file=sys.stderr)
    print("Both should resolve inside this project's venv. If they don't,",
          file=sys.stderr)
    print("recreate the venv (do not copy from another project):", file=sys.stderr)
    print("", file=sys.stderr)
    print("    python3 -m venv venv", file=sys.stderr)
    print("    source venv/bin/activate", file=sys.stderr)
    print("    pip install -r requirements.txt", file=sys.stderr)
    print("    pip install -r gui/requirements.txt", file=sys.stderr)
    print("", file=sys.stderr)
    sys.exit(1)


# Run the check before importing backend.server, because FastAPI's
# import-time errors (notably the python-multipart RuntimeError when
# any endpoint declares Form/File) fire during server import and would
# bypass main()'s try/except.
_check_gui_deps()

from backend.server import build_app, DEFAULT_HOST, DEFAULT_PORT


def main():
    parser = argparse.ArgumentParser(description="ADAM GUI launcher")
    parser.add_argument(
        "--adam-root", type=Path, default=Path("."),
        help="ADAM project root (where .env lives). Default: cwd.",
    )
    parser.add_argument(
        "--logs-dir", type=Path, default=None,
        help="Path to ADAM logs/ dir. Default: <adam-root>/logs.",
    )
    parser.add_argument(
        "--host", type=str, default=DEFAULT_HOST,
        help=f"Host to bind. Default: {DEFAULT_HOST} (localhost only).",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Port to bind. Default: {DEFAULT_PORT}.",
    )
    parser.add_argument(
        "--open-browser", action="store_true",
        help="Open the GUI in the default browser after startup.",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="Don't auto-open the browser even if --open-browser was passed.",
    )
    args = parser.parse_args()

    adam_root = args.adam_root.resolve()
    logs_dir = (args.logs_dir or (adam_root / "logs")).resolve()

    if not adam_root.exists():
        sys.exit(f"ADAM root not found: {adam_root}")

    # Confirm the frontend has been built
    dist = HERE / "frontend" / "dist"
    if not dist.exists() or not (dist / "index.html").exists():
        print("=" * 72)
        print("Frontend build not found at " + str(dist))
        print("=" * 72)
        print()
        print("Before launching the GUI for the first time, build the frontend:")
        print()
        print("    cd " + str(HERE / "frontend"))
        print("    npm install")
        print("    npm run build")
        print()
        print("Then re-run this launcher.")
        print()
        print("Note: starting the backend anyway. The GUI page will show a")
        print("stub message until the frontend build is created.")
        print()

    app = build_app(adam_root, logs_dir)

    if args.open_browser and not args.no_browser:
        import threading
        import time
        import webbrowser
        url = f"http://{args.host}:{args.port}/"
        def _open():
            time.sleep(0.6)
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    print()
    print("=" * 72)
    print(f"ADAM GUI starting")
    print("=" * 72)
    print(f"  ADAM root:   {adam_root}")
    print(f"  Logs dir:    {logs_dir}")
    print(f"  URL:         http://{args.host}:{args.port}/")
    print(f"  API:         http://{args.host}:{args.port}/api/")
    print()
    print(f"Ctrl-C to stop.")
    print()

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
