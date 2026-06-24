# ADAM GUI

Live observer for ADAM deliberation sessions. Renders the events.jsonl
stream as a real-time dashboard: transcript, verifications, skill
registry, session sidebar.

## What this is

A separate process that watches the ADAM logs directory and streams
events to a browser. It does not modify ADAM, does not share Python
imports with ADAM, and cannot mutate session state. The contract
between ADAM and the GUI is the on-disk file layout in
`logs/<user_id>/<session_id>/`.

This is **Phase B** (live monitor): read-only observation with live
updates. Phase C work — director input from the GUI, pause/resume,
approval gates, mid-session attachment — is intentionally not in
scope here. Aspirational fields and buttons from the design mockup
are rendered honestly with "not configured" indicators or disabled
tooltips, never with fake values.

## Architecture

```
   ADAM (separate process)                ADAM GUI (this)
   ─────────────────────                  ──────────────────────────
   adam_agent_chat.py            ── writes ──>   logs/<user>/<id>/
                                                       │
                                                       │ tails
                                                       ▼
                                              backend/server.py (FastAPI)
                                                       │ SSE
                                                       ▼
                                              frontend/ (React+Vite)
                                                       │ HTTP
                                                       ▼
                                              browser
```

## First-time setup

You need Python 3.10+, Node.js 18+, and npm.

```bash
# 1. Backend dependencies (use the same venv as ADAM)
pip install fastapi sse-starlette uvicorn

# 2. Build the frontend
cd gui/frontend
npm install
npm run build
cd ../..

# 3. Launch
python gui/adam_gui.py --open-browser
```

If `--open-browser` is not passed, the launcher prints the URL.
Default is `http://localhost:8765/`.

## Daily use

```bash
# Just launch (browser opens automatically)
python gui/adam_gui.py --open-browser

# Or start ADAM and the GUI side-by-side
python adam_agent_chat.py &       # in one terminal
python gui/adam_gui.py --open-browser   # in another
```

The GUI auto-refreshes the session list every 5 seconds, so a new
session started in the ADAM terminal will appear in the sidebar
without needing to reload the browser.

## Development mode

When iterating on the frontend, run Vite's dev server (with
hot reload) and the FastAPI backend separately:

```bash
# Terminal 1: backend
python -m gui.backend.server --adam-root . --port 8765

# Terminal 2: frontend dev server (proxies /api/* to :8765)
cd gui/frontend
npm run dev
# Opens at http://localhost:5173/
```

## Deployment

### Laptop (default)

```bash
python gui/adam_gui.py --open-browser
# Binds to 127.0.0.1:8765 (localhost only)
```

### Server (internal network)

```bash
python gui/adam_gui.py --host 0.0.0.0 --port 8765
# Binds to all interfaces. Reachable from other machines on the network.
```

For server deployments, put a reverse proxy in front (nginx/caddy)
to terminate TLS and bolt on authentication. ADAM's
session-discovery is single-tenant — one director per deployment,
identified by the .env. Multi-user auth is Phase C and would
plug in via LDAPS (same pattern as Paperclip).

## API surface

Read-only REST + one SSE endpoint:

```
GET  /api/health
GET  /api/director
GET  /api/sessions
GET  /api/sessions/{id}/state
GET  /api/sessions/{id}/events
GET  /api/sessions/{id}/stream          (SSE)
GET  /api/sessions/{id}/verifications
GET  /api/sessions/{id}/skills
GET  /api/sessions/{id}/artifacts/{filename}
```

The SSE endpoint catches up from the start of events.jsonl on each
connection, then tails for new events. Reducer-side seq-idempotence
makes reconnects safe.

## Aspirational fields

These are visible in the UI but not backed by real data:

| Field | What it would mean | Status |
|---|---|---|
| `GOVERNANCE PROFILE` | Active governance profile setting | "not configured" |
| `HUMAN REVIEW` | Per-turn approval gate state | "not configured" |
| `POLICY BOUNDS` | Policy-bounds checker verdict | "not configured" |
| `PAUSE` button | Mid-session pause | disabled, tooltip |
| `HALT` button | GUI halt (only Ctrl+C in terminal today) | disabled, tooltip |
| `REQUEST APPROVAL` | Human approval gate | disabled, tooltip |
| `+ATTACH` | Mid-session context attach | disabled, tooltip |
| Director input bar | GUI director input (use `>>` in terminal today) | disabled, placeholder |

When the underlying ADAM feature ships, the binding is already in the
GUI — just wire the new event to the reducer and the indicator lights up.

## Where files live

```
gui/
├── adam_gui.py              # Launcher (run from ADAM project root)
├── backend/
│   ├── __init__.py
│   └── server.py            # FastAPI app, SSE tail
├── frontend/
│   ├── package.json
│   ├── vite.config.js
│   ├── index.html
│   ├── src/
│   │   ├── main.jsx
│   │   ├── App.jsx
│   │   ├── components/      # Header, Sidebar, MainPanel, RightBar, PromptBar
│   │   ├── lib/             # api.js (SSE client), reducer.js, agents.js
│   │   └── styles/global.css
│   └── dist/                # `npm run build` output (committed for releases)
└── README.md
```
