# Test suite

Run from the repository root. Prerequisites are Python 3.12+, uv, Node 22.13+,
GNU `timeout`, and Docker for the complete infrastructure suite.

```bash
make test-setup  # Install locked dependencies and Chromium, Firefox, WebKit.
make test-all   # Run every backend gate, then the built UI's entire test suite.
```

On Linux, install browser system libraries with
`cd ui && npx playwright install --with-deps chromium firefox webkit`.
On macOS, GNU `timeout` is supplied by Homebrew `coreutils`; add its `libexec/gnubin`
directory to `PATH` as described in README.md.

## Commands

| Command | Coverage |
| --- | --- |
| `make check` | Python lint and type checks across source, tests, scripts, migrations and SDK examples; frontend lint and type checks. Also runs before `make test` and `make test-all`. |
| `make test-all` | Complete backend suite with Docker/PostgreSQL/S3, Node regressions, and all four browser projects. Infrastructure tests must not skip. |
| `make test` | All locally runnable tests and browsers. Backend infrastructure gates report skips unless their environment is configured. |
| `make test-regression` | Focused backend regressions and all Node UI tests. |
| `make test-e2e` | Backend end-to-end workflows and all browser tests. Infrastructure-dependent backend cases may skip. |
| `make test-backend-live` | Build isolated runtime images, provision temporary PostgreSQL/S3, run the complete backend suite, then clean up. |
| `cd backend && uv run python scripts/test-live.py --live-only` | Only the nine infrastructure gates, with provisioning and cleanup. |
| `cd ui && npm run test:e2e -- --project=chromium` | Build and run one browser engine. |
| `cd ui && npm run test:e2e:run -- --grep 'partial refresh'` | Run a focused browser test against an already built UI. |

`test:e2e:run` requires a current `npm run build`. The normal `test:e2e` and
`test:all` commands build first. The Node test command `npm test` retains its
existing behavior; `npm run test:all` includes the browser suite too.

## What the suites verify

- Existing Python unit, contract, integration, containment, acceptance and campaign
  tests retain coverage of budgets, policy evaluation, deterministic verification,
  storage lineage, supervisor fencing, target inference, intervention delivery,
  recovery and replay.
- `backend/tests/regression/` exercises HTTP byte/depth limits, malformed input,
  authentication and owner isolation, shared rate limits, atomic idempotency,
  conflicting retries, rollback, immutable event history and pagination.
- `backend/tests/end_to_end/test_research_sdk_workflows.py` drives the public SDK
  over real loopback HTTP through the API and background worker, separate database
  engines, local artifacts, Blue gateway and reference target. It covers
  generation/action/evidence/export/training lineage, JSONL/Parquet and research/model
  views, dropped responses, timeout recovery, cancellation, checkpoint transfers,
  and API restarts. Model inference and container allocation are deterministic
  substitutes in this tier; API responses and persistence are real.
- The live suite verifies real PostgreSQL transactions and migrations, multipart S3
  integrity, actual Docker network/credential containment, reference target execution
  and the inference relay. It uses a scripted provider, without paid model calls.
- `ui/tests/*regression.test.mjs` checks client credential scoping, errors and aborts,
  actual HTTP proxy transport, binary integrity, header isolation, redirects,
  outcome semantics and usage provenance.
- `ui/e2e/` drives the production build against a disposable real API/database in
  Chromium, Firefox, WebKit and mobile Chromium. It checks navigation/history/deep
  links, target registration and inspection, form validation, escaping untrusted
  content, readiness/budget gating, API failure recovery, credential clearing and
  WCAG A/AA accessibility. Network interception is confined to failure scenarios
  and an explicitly marked readiness-gate test; that test controls supervisor
  readiness responses to verify both enabled and disabled launch states.

## Isolation and diagnostics

The browser API uses a temporary SQLite database and artifact directory and ignores
exported settings, credentials and `backend/.env`. Its reference bundle uses scripted fixture mode. Browser
tests do not connect to the developer's running application or execute campaigns
against an external supervisor. Playwright refuses to reuse occupied server ports;
override `AML_E2E_UI_PORT` and `AML_E2E_API_PORT` if 4173/8173 are unavailable.

The live runner creates unique service names, image tags, random credentials and
loopback ports. It does not reuse application databases or alter running application
containers. It removes its services and image tags on exit. Docker build caches
and downloaded base images can remain for subsequent runs. OpenTelemetry export
is disabled by the runner, including when invoked directly. Each run retains its
JUnit report in `backend/test-results/live-<run-id>.xml`, including test failures
and skipped-test diagnostics.

Live capsule tests also use a unique managed-resource label for each test. Capsule
creation, ownership verification, inventory and orphan reconciliation all share
that test namespace, so they cannot select application capsules or capsules from
another test run on the same Docker daemon.

Tests use bounded polling and observable completion signals. Browser retries are
disabled so intermittent failures remain failures; focused tests are forbidden.
Test data is isolated by temporary directories or distinct record identities.

Every browser test automatically audits HTTP responses, transport failures and
uncaught browser exceptions. Unexpected HTTP 4xx/5xx responses fail the test even
when the page's UI assertions pass. Negative scenarios declare the exact method,
endpoint, status and bounded occurrence count before triggering the failure;
required failures must actually occur. Each result includes an `api-audit` JSON
attachment with expected and unexpected errors. Browser-reported request
cancellations are recorded separately, since navigation and component unmounts
intentionally abort requests. Direct Playwright API setup requests use their own
response assertions. Run browser commands sequentially when sharing report and
artifact directories.

The full Python suite enforces an 85% line coverage floor. Focused commands disable
that aggregate gate because they intentionally execute only part of the codebase.
Browser failures retain screenshots, video and traces under `ui/test-results/`;
the HTML report is in `ui/playwright-report/`. Open it with
`cd ui && npx playwright show-report`. These generated files are ignored by Git.

GitHub Actions runs the complete infrastructure suite, frontend lint/type checks,
Node tests and all browser projects, and uploads coverage and failure diagnostics.
Live model performance and external Firecracker/KVM deployments are outside this
suite; passing fixture tests does not establish those guarantees.
