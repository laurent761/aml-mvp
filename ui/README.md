# AML Research UI

Research sessions, runs, trajectories, datasets, checkpoints, evaluations and service
health use the same records as the independent Python SDK. Open the connection dialog
to supply a research token (kept in memory). See the backend's
[research integration guide](../backend/docs/RESEARCH_INTEGRATION.md) for scope and deployment.
Run `npm run typecheck` to generate local Cloudflare declarations and check all TypeScript,
alongside the existing lint and test commands.

Operator console for the Adversarial Agent MVP backend. It covers the complete control-plane workflow: target identities and immutable versions, bounded attack tasks, campaigns and episode traces, verified findings, exact reproduction, nearby attack mutations, attacker configuration, evidence downloads, and operational audit events.

## Run locally

```bash
npm ci
BACKEND_API_URL=http://localhost:8000 npm run dev
```

Open `http://localhost:5173` (or the port printed by Vite). The Worker proxy forwards only `/healthz`, `/readyz`, and `/v1/*` to `BACKEND_API_URL`. It uses strict request/response header allowlists, never follows backend redirects, and keeps the browser on one origin.

You can also leave `BACKEND_API_URL` unset and enter an allowed API origin from the connection dialog. The backend must then permit that browser origin through its `CORS_ORIGINS` setting.

## Production build

```bash
npm run lint
npm test
npm run build
BACKEND_API_URL=http://localhost:8000 npm run start
```

The included Dockerfile builds the same Vinext application. In the integrated Compose profile the UI receives `http://api:8000` as its backend origin and is published on `http://localhost:3000` by default.

## Validation scope

`npm test` validates:

- production compilation and server rendering;
- the complete operator navigation shell;
- the silver and slate palette and layered atmospheric background;
- API client error behavior;
- proxy path/method/body forwarding, credential stripping, redirect blocking, and response-header filtering;
- component accessibility semantics supplied by the UI primitives.

The original UI correction was validated by compilation and automated tests. The later
infrastructure acceptance run also exercised live backend records and the UI proxy;
see the [dated validation report](../reports/INF-04_TO_INF-12_IMPLEMENTATION.md).

## Backend contract

The console expects the backend API under `/v1` plus `/healthz` and `/readyz`. Same-origin proxy mode is recommended. Costly operations surface their bounds or require explicit selection, and campaign cancellation uses a confirmation dialog.

The backend includes a synthetic finance reference target for fixture validation.
Real target model configuration and externally launched attacker training remain
researcher choices; see the [root run guide](../README.md).

## Reproduction-only backend update

This UI sends `reproduction_only: true` to the replay endpoint. Install the backend from the corrected full-project bundle. It restores the source campaign conditions and creates an attack replay without changing finding associations or starting defensive workflows. Older backends reject this flag explicitly; the UI reports that an update is required.

## Research scope

The UI excludes legacy administrative events, defensive evidence bundles, and benign regression campaigns. Dashboard outcomes and spend use the loaded attack campaigns and their episodes, with explicit loaded-record labels. Historical backend records and compatible API contracts remain intact.
