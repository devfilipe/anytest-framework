---
name: web-api
description: Work on the FastAPI backend and the single-file SPA dashboard safely. Use when adding/changing an API endpoint or the web UI.
---

# Work on the web API + dashboard

## Backend — `atf/web/server.py`

- FastAPI app built in `build_app()`. All routes are under `/api/*`.
- **Auth gate**: the `_login_gate` middleware requires a login token for every `/api/*` path
  except an explicit allow-list (login, `whoami`, agent-facing endpoints, the SSE stream, and the
  API docs `/api/docs` · `/api/redoc` · `/api/openapi.json`). New public endpoints must be added to
  that allow-list *deliberately*; everything else is authenticated by default.
- Use `Depends(require_login)` / `Depends(require_admin)` for per-route gating. Admin-only actions
  must use `require_admin`.
- The config store is reached via `repo` (an `atf.store` repo). Never touch SQLite directly here.
- `_meta()` is the catalog the SPA reads (checks/boards/suites/benches). Keep its shape stable.

## Frontend — `atf/web/static/index.html`

- The **entire** SPA is this one file: vanilla JS, inline CSS, **no build step, no external
  requests** (fonts are the only remote link). Keep it self-contained.
- Rendering is string-templated into `innerHTML`; **escape** user/data strings with `esc()`.
- Markdown is rendered by the in-file `md()` — reuse it, don't add a library.
- Two themes via `:root[data-theme]` (`dark` default, `light`); brand is text-only (no logo).
- Auth: the SPA holds a bearer token and sends it on `/api/*`; the login gate enforces it.

## Verify

```bash
make check                      # compile + import sanity + ruff (Python side)
.venv/bin/atf web               # click through the changed view; check /api/docs for the endpoint
```

- Test an endpoint's auth: unauthenticated `/api/<new>` should be `401` unless intentionally public.
- The API schema at `/api/openapi.json` should list the new route.
