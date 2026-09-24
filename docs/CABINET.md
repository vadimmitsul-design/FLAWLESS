# Next.js customer cabinet

The new customer cabinet is in `frontend/` (Next.js App Router and TypeScript).
FastAPI remains responsible for authentication, balances, model prices and all
mutations. The browser calls the same-origin Next route `/api/backend/*`; that
adapter forwards only explicitly allowed cabinet routes to FastAPI.

The interface includes the overview, seven-day usage, model selection, saved
conversations, chat, API key creation/revocation and top-up requests. Top-up
requests still require the existing administrator confirmation: they do not
credit a balance immediately. Issued API keys are shown once.

## Start the isolated local preview on Windows

Requirements: Python 3.12 with `.venv` and `requirements-dev.lock` installed,
and Node.js 20.9 or newer. From the repository root:

```powershell
npm --prefix frontend ci
powershell -ExecutionPolicy Bypass -File scripts/start_cabinet_preview.ps1
```

Open **http://127.0.0.1:3000**. Local-only credentials:

- Email: `demo@flawless.local`
- Password: `Neon-Demo-2026!`

The launcher starts hidden Next.js and Uvicorn processes, bound only to
`127.0.0.1`, on ports 3000 and 8001. Use `-FrontendPort` and `-BackendPort` if
those ports are occupied. It refuses to terminate an existing unrelated listener.
The launcher prints the URL while the development server finishes starting;
the first page may take a few seconds to compile.

The fixture is synthetic: 467 calls across seven days, four model prices,
two API keys and three saved conversations. Wallet ledger totals match the
displayed balance. Repeat launches preserve changes to the preview database.
Fixture amounts and prices are demonstration data, not live provider quotations.

All preview state lives under ignored `dist/cabinet-preview/`: SQLite database,
model fixture, generated session secret, logs and process records. The backend
uses that working directory, so it does not read the repository `.env`. Child
processes receive a minimal environment without inherited provider credentials,
Telegram tokens or production database configuration. The model fixture uses a
fake key and an explicit loopback endpoint; a new chat request returns a provider
unavailable error without calling a real LLM. Reading saved dialogues works.

Stop only the recorded preview process trees, preserving the demo database:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_cabinet_preview.ps1 -Stop
```

The stop command verifies the process executable and start time before stopping
it, so a reused PID does not identify an unrelated application as the preview.

## Connect a configured backend

Set the server-side `FLAWLESS_BACKEND_URL` to the desired FastAPI origin, and
start Next.js. Do not put provider credentials or session secrets in
`NEXT_PUBLIC_*`. Existing accounts and the signed session cookie are used; the
frontend does not implement a second identity or billing system.

Behind a TLS reverse proxy, also set `FLAWLESS_FRONTEND_ORIGIN` to the exact
public origin (for example, `https://app.example.com`). Otherwise the proxy
compares browser Origin/Referer against the incoming Host and URL scheme. It
does not use client-supplied forwarded host headers for this comparison.

```powershell
$env:FLAWLESS_BACKEND_URL = 'http://127.0.0.1:8000'
npm --prefix frontend run dev
```

For deployment, run `npm ci`, `npm run build` and `npm run start` inside
`frontend/`; configure HTTPS and the existing backend production settings.
The JSON API is additive; existing server-rendered cabinet routes remain available.

The proxy validates browser Origin/Referer before rewriting Origin for the
backend, forwards cookies without exposing them to browser JavaScript, forbids
redirects and arbitrary proxy paths, caps request bodies at 8 MiB, uses a
140-second upstream timeout and disables response caching. It never trusts or
forwards client-supplied forwarded IP headers. All proxied logins therefore share
the backend limiter identity; production per-client rate limiting should also
be enforced at the trusted edge.

## Checks

```powershell
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend run format:check
npm --prefix frontend run build
npm --prefix frontend run test:api
.venv\Scripts\python.exe -m pytest tests/test_cabinet_json.py
```

For a local smoke test, verify login/logout, all cabinet sections, key creation
and revocation, top-up request feedback and the saved dialogue reader. Check that
cross-site mutations return 403 and unlisted proxy paths return 404. The preview
does not exercise real provider responses, payments, Telegram or deployment.

`test:api` uses Playwright's HTTP client against the running local preview; it
requires no browser installation and refuses non-local hosts. Its eight checks
cover session access, valid/invalid login, logout, Origin/Referer, proxy allowlists
and request size. The frontend workflow also checks formatting, types, lint and
the production build. ESLint 9 is pinned to the version compatible with the
React plugins bundled by the selected Next.js version.

Production dependencies are limited to the requested Next.js framework, React,
React DOM and Lucide's consistent accessible SVG icon set. The torus and chart
use Canvas/CSS/SVG without graphics libraries. Onest and JetBrains Mono are served
locally with their OFL licenses; the browser does not request Google Fonts.

## Verified locally

- Full Python regression suite: **446 passed** (three existing third-party warnings).
- Next-to-FastAPI HTTP tests: **8 passed**.
- Ruff: lint and format clean across 132 files; Mypy: 84 source files clean.
- ESLint, TypeScript, Prettier and the optimized Next.js production build pass.
- Edge: desktop and mobile overview/chat, sign-in, saved conversation loading,
  model filtering, command search, usage and key listings, and a demo top-up
  request checked. The pending request left the demo balance unchanged.
- No real provider, payment or Telegram calls; no production deployment, load
  test, full WCAG audit or remote GitHub Actions run.

The BFF forwards session cookies only on login/logout. Ignoring rolling cookies
on read and chat responses prevents a late response from restoring the cookie
after logout. Session expiry returns the UI to sign-in. Aborting a chat request
does not guarantee that an already-running backend/provider operation is canceled.
The existing server-side billing and reservation cleanup remain authoritative.

Optional shop, prompts, resources, archive and child-account management continue
to use the existing Python-rendered pages; they have not been migrated to Next.js.
