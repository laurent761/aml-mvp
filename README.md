# AML — Adversarial Agent Research Platform

AML helps you test whether an AI agent can be persuaded to do something it should
not do. You choose an agent, run a controlled test, and inspect the recorded actions
and results. The included finance example uses synthetic data and simulated business
tools.

## Start here

| What you want to do | Where to start |
|---|---|
| Try AML on your computer | Follow [Run locally](#run-locally), steps 1–5. No model account or API key is needed for the included example. |
| Stop AML or fix a failed run | See steps 6–7 in [Run locally](#run-locally). |
| Write Python scripts to run tests | Complete the local setup, then follow [Python SDK](#python-sdk). |
| Repeat a result to see whether it happens again | Read [Repeat a previous experiment](#reproduction). |
| Change AML's source code | Read [Change the code and check your changes](#development-and-checks). This is optional. |
| Understand the system in detail | Open the [architecture guide](architecture-guide.html). |

### Words you will see

- **Target:** the agent being tested. The reference target is the included finance example.
- **Fixture:** a scripted example with predefined behavior, useful for trying the platform.
  **Model mode** uses an actual AI model instead.
- **Campaign:** a test run that groups attack attempts against a target.
- **Experiment / episode:** one attempt within a test run.
- **Trajectory:** the ordered record of actions and responses in an experiment.
- **Finding:** a recorded case where the target reached a state the test forbids.
  Replaying it checks whether that result happens again.
- **UI:** the web interface you open in your browser. **API:** the service used by the
  UI and Python scripts to communicate with AML.
- **SDK:** the Python library for controlling AML from your own code.

## Run locally

Follow these steps to start AML on your computer and run the included finance example.
The default `fixture` mode does not need a model API key. Run the commands in order;
each step states which directory to use.

### 1. Check that the required tools are installed

You need these tools before running the commands below:

| Tool | What AML uses it for |
|---|---|
| Docker with Compose v2 | Starts AML's services in containers, which package each service with what it needs to run. |
| Python 3.12+ and `uv` | Runs the Python tools and installs their dependencies. |
| `make` | Runs the project's setup command. |
| Node 22.13+ and npm | Installs and runs the web interface's tools. |
| `curl` | Checks that the API is responding. |

Start Docker before continuing. The first installation and build need internet access.
The commands below use a Bash-compatible terminal, such as Terminal on macOS or a
Linux shell. On Windows, use a Linux shell through WSL with Docker integration.
Open a terminal in your downloaded or cloned `aml-mvp` folder.

From the repository root (the directory containing this README), check your tools:

```bash
docker info
docker compose version
python3 --version
uv --version
make --version
node --version
npm --version
```

`docker info` must reach a running Docker daemon. Resolve missing tools or version
mismatches before the next step.

### 2. Install dependencies and create the configuration

From the repository root:

```bash
make setup
```

This installs the project's required Python and JavaScript packages at the versions
recorded in the repository, installs the SDK into `backend/.venv`, and copies
`backend/.env.example` to `backend/.env` if that file does not already exist.
Re-running it preserves your existing `.env`.

Open `backend/.env` in a text editor. This file contains the settings AML reads at
startup. The supplied defaults work for the local example on typical Docker Desktop
setups; Linux users should check `DOCKER_GID` below.

| Setting | What to use for the first local run |
|---|---|
| `BIND_ADDRESS` | Keep `127.0.0.1` for local access. |
| `API_PORT`, `UI_PORT` | Defaults are `8000` and `3000`; change them if occupied. |
| `DOCKER_SOCKET` | The socket for the Docker daemon that will run the target containers. |
| `DOCKER_GID` | On Linux, use the socket group ID from the command below. Docker Desktop commonly uses `0`. |
| `RESEARCH_AUTH_REQUIRED`, `RESEARCH_AUTH_TOKENS` | Keep `false` and `{}` for the browser-based local walkthrough. See the SDK section for authenticated access. |
| `TARGET_MODEL_PROVIDER`, `ATTACKER_MODEL_PROVIDER` | Keep `disabled` and `heuristic` for the fixture walkthrough. |

On Linux, find the socket group ID (adjust the path if you changed `DOCKER_SOCKET`):

```bash
stat -c '%g' /var/run/docker.sock
```

The capsule supervisor is the service that starts and stops isolated target
containers. Run only one supervisor against the same Docker installation at a time. The example credentials
are for local development; review the [architecture guide](architecture-guide.html)
before remote use.

### 3. Start the services

From the repository root, enter `backend/` and start all the services:

```bash
cd backend
docker compose up -d --build --wait
docker compose ps -a
```

Stay in `backend/` for the remaining terminal commands in this walkthrough.
The first build may take several minutes. `--build` builds the service images,
`-d` keeps them running in the background, and `--wait` waits for startup checks.

Compose starts the browser interface, API, background worker, target supervisor,
and supporting storage and monitoring services. It also prepares the database and
file storage automatically. You do not need to start each service separately.

The startup command should finish successfully. The `migration` and `minio-init`
containers are one-time jobs, so `Exited (0)` is expected for them. If startup fails,
use the troubleshooting commands below before continuing.

Check that the API is running and ready to accept requests (replace `8000` if you
changed `API_PORT`). Both commands should finish without an HTTP error:

```bash
curl --fail http://localhost:8000/healthz
curl --fail http://localhost:8000/readyz
```

### 4. Add the included finance agent to AML

From `backend/`, prepare the finance target with one command:

```bash
bash scripts/prepare-reference.sh
```

This builds the target, publishes it under a unique build tag in the persistent
local registry, records its registry digest, validates the bundle and registers
an immutable target version. Repeating the same bundle is idempotent. A changed
build creates a new version; existing campaigns and exact replays keep their
original version. Refresh registration in AML to select the new version.

The registry listens only on `127.0.0.1:5001` and retains images in the separate
`target-images` Docker volume. Service rebuilds and image-cache pruning do not
delete this volume. Back it up along with the AML database; do not remove it with
`docker compose down -v`. No automatic registry deletion or garbage collection is
configured: retained campaign evidence may still reference any saved image.

The supervisor verifies the exact image on its execution daemon before any
experiments and again when provisioning. Missing registry-backed images are
pulled by digest, never by a moving tag. If restoration fails, the campaign is
`BLOCKED` with no security result from unexecuted tests. Retry recovery from the
UI, then launch a new campaign; historical campaign results are not rewritten.

Registration alone does not imply readiness. The Targets view and campaign
launcher can check/prepare the selected version. Readiness is a point-in-time
image/platform check, not a claim that the target passed a security test or that
its application will start successfully.

For a remote deployment, pass `--repository registry.example/team/finance-reference`
and configure the supervisor daemon's registry access. It must be able to pull
the same digest. `build-reference --local-only` is an explicit disposable test
mode; it records only a local image ID and cannot restore a removed image.
Legacy versions with bare `sha256:` IDs require the original image backup or a
newly prepared version. A rebuild must never be silently substituted for them.

The command defaults to fixture mode. To preserve a model target's mode, supply
`--inference-profile path/to/profile.json`; the profile contains no credentials.

### 5. Run your first campaign in the browser

1. Open **http://localhost:3000** (or your configured `UI_PORT`). API documentation
   is available at **http://localhost:8000/docs**.
2. Open the quickstart tour and choose the scripted fixture agent. With the default
   local authentication settings, leave the connection token unset.
3. Refresh registration, then select the finance reference target version and its
   matching attack task. The previous step has already registered the fixture.
4. Review the legitimate task, attack objective, and campaign limits. Use
   **Review and launch campaign** to review the configuration and submit it.
5. Open **Attack Campaigns** and follow the run from queued to running to completed.
   A queued record alone does not confirm that the target ran.
6. Inspect **Experiments** and **Trajectories** for actions and observations. If a
   verified finding was recorded, inspect it in **Verified Exploits**. A completed
   campaign does not necessarily produce a finding.

This walkthrough exercises the platform with scripted behavior. To test a real
model, use the tour's model path to configure target inference, export the running
profile, and register a model bundle. Fixture results do not establish model performance.

### 6. Troubleshoot an incomplete run

Run these commands from `backend/`:

```bash
docker compose ps -a
docker compose logs --tail=100 api worker capsule-supervisor
docker compose logs --tail=100 migration minio-init
```

| Symptom | What to check |
|---|---|
| Docker commands cannot connect | Start Docker and verify `DOCKER_SOCKET`. |
| Supervisor cannot access Docker | Verify socket permissions and `DOCKER_GID`, then recreate the supervisor with `docker compose up -d --force-recreate capsule-supervisor`. |
| Startup reports an occupied port | Change the corresponding port in `.env`, rerun step 3, and use the new URL. |
| No target is available | Complete step 4 and refresh registration in the UI. |
| Campaign stays queued | Check that `worker` is running and inspect its logs. |
| Experiment fails while starting a target | Inspect supervisor logs and confirm the registered image exists in the supervisor's Docker daemon. |
| API returns 401 | Clear a stale browser token for the default local setup, or supply a token configured in `RESEARCH_AUTH_TOKENS`. |

### 7. Stop and restart AML

From `backend/`, stop the services while retaining stored data:

```bash
docker compose down
```

Restart with `docker compose up -d --wait`. Adding `-v` to `docker compose down`
deletes the stack's persistent volumes, including its database and stored artifacts;
after that, repeat startup and target registration.

## Python SDK

This section is optional. Use it if you want to run experiments from Python instead
of the browser. First complete local setup through step 4, or obtain the address
and access token for an existing AML installation.

The SDK can be installed separately with Python 3.10+; developing the backend still
requires Python 3.12+. It communicates with AML over the API.

### 1. Choose a Python environment

A virtual environment keeps this project's Python packages separate from other
projects on your computer. If you ran `make setup`, the SDK is already installed in `backend/.venv`; use
`backend/.venv/bin/python` from the repository root. For a separate environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ./sdk
```

### 2. Configure authentication and the API URL

An access token is a secret string that identifies your client and its permissions.
The SDK always sends this token with its requests (a “bearer token”). Use a token configured by your deployment's
operator; an arbitrary token is rejected even when local authentication is disabled.
For a local authenticated setup, generate a token and its configuration with:

```bash
python3 - <<'PYTHON'
import hashlib
import json
import secrets

token = secrets.token_urlsafe(32)
digest = hashlib.sha256(token.encode()).hexdigest()
identity = {"owner_id": "local-researcher", "scopes": ["research", "evaluation", "evidence", "operator"]}
print("Client token:", token)
print("RESEARCH_AUTH_REQUIRED=true")
print("RESEARCH_AUTH_TOKENS=" + json.dumps({digest: identity}, separators=(",", ":")))
PYTHON
```

Save the printed client token, then replace the two matching settings in
`backend/.env` with the printed configuration lines. The mapping stores the hash;
the client uses the original token. These scopes enable the full local walkthrough.
Apply changes from `backend/` with `docker compose up -d --force-recreate api`.
Enter the same original token in the UI connection dialog when using this
authenticated stack.

In the shell where you will run the SDK, set:

```bash
export AML_API_URL=http://localhost:8000
export AML_TOKEN='your-configured-token'
```

Replace the URL and token with your deployment's values. Keep the stack running.

### 3. Run one episode

From the repository root, using the environment created by `make setup`:

```bash
backend/.venv/bin/python sdk/examples/workflows.py episode
```

For the separate environment, use `python sdk/examples/workflows.py episode` instead.
The example selects the first catalog bundle by default; set `AML_BUNDLE_ID` to
choose a specific registered bundle. It prints the public response, session ID,
episode ID, and outcome.

### 4. Adapt the example

The following shows the same reset-and-step lifecycle for your own script:

```python
import asyncio
import os
from aml_research import Client

async def main():
    async with Client(os.environ["AML_API_URL"], os.environ["AML_TOKEN"]) as client:
        bundles = await client.catalog()
        if not bundles:
            raise RuntimeError("Register a target bundle before running this example.")
        async with client.session(bundle_id=bundles[0]["bundle_id"]) as session:
            initial = await session.reset()
            # Give only initial.public_observation to your attacker.
            result = await session.step({
                "channel": "user_message", "payload": {"text": "Process invoice-001."}
            })
            print(result.outcome)  # Research output, separate from attacker input.

asyncio.run(main())
```

<details>
<summary>Advanced: recovering interrupted operations and handling files</summary>

- `client.session()` bounds concurrency and maintains heartbeats. Keep `session.id` and
  `session.last_operation_id`; use `client.operation(id)` to recover results and
  `attach_session()` to resume a live session. `OperationTimeout` exposes its ID.
- Never blindly retry `IndeterminateOperation`. Interruptions retire the episode.
  Retries preserve JSON and idempotency keys; recover operations instead of guessing step indices.
- Failed uploads restart the whole file with the same key; crashed active transfers
  must expire or be cancelled first. Failed transfers cannot create checkpoints.
  Downloads verify SHA-256 before replacing files.
- Outcomes, measurements, rewards, and usage are research outputs. Reported training
  rewards never change verified outcomes.

[Workflow examples](sdk/examples/workflows.py) cover concurrency, exports, training
records, checkpoints, runtimes, and evaluation. Runtime registration requires
operator scope; fixture runtimes and checkpoints are wiring examples. See the [architecture guide](architecture-guide.html)
for lifecycle, isolation, persistence, and model-inference details.

</details>

<a id="reproduction"></a>

## Repeat a previous experiment

A finding records what happened in one experiment. Replaying that experiment checks
whether the same result happens again. AI models can respond differently on repeated
runs, even when given the same random seed.

For browser use, open a finding in **Verified Exploits** and use its replay action.
Inspect the new run separately from the original finding.

<details>
<summary>For API users: replay endpoints and request options</summary>

| API | Behavior |
|---|---|
| `POST /v1/research-episodes/{id}/reproduce` | Returns 202 with a fresh replay session using the source bundle, policy, seed, and completed actions. Retains source limits with `max_episodes=1`. Requires a finished owned episode, `evaluation` or `operator` scope, and an idempotency key. |
| `GET /v1/research-sessions/{id}/reproduction` | Read progress, success, and observation divergence. |
| `POST /v1/findings/{id}/replay` | Operator replay used by the UI. Send `{"reproduction_only": true, "search_nearby_bypasses": false}` to retain original target/task, policy, attacker configuration, and lineage without starting hardening. |

For authenticated requests, send `Authorization: Bearer <token>`. The SDK sends
`X-AML-API-Version: aml.research.v1`; the API defaults to that version if omitted.
Research reproduction POSTs require an ASCII `Idempotency-Key` of 1–200 characters.
Setting `search_nearby_bypasses` to `true` requests adaptive mutations; a conflicting
`policy_version_id` returns 422. Finding replay returns `replay_campaign_id` and its
execution contract. `reproduction_only` defaults to `false` for compatibility;
the UI always sends `true`. Deploy matching UI/backend versions and apply migrations
through `0005`; the replay flag itself requires no migration.

</details>

<a id="development-and-checks"></a>

## Change the code and check your changes

**This section is only for people editing AML's source code.** If you only want to
use AML, the Docker walkthrough above is enough.

Start from the repository root. Run `make setup` if you have not already done so.
Choose the instructions below for the part you are changing; these are separate
workflows, not additional steps required to start AML.

### If you are changing the browser interface

Keep the Docker services from the local walkthrough running. In a new terminal,
open the repository root and run:

```bash
cd ui
BACKEND_API_URL=http://localhost:8000 npm run dev
```

`BACKEND_API_URL` tells the interface where to find the API. Change `8000` if your
API uses a different port. Open the address printed in the terminal, usually
**http://localhost:5173**. Use that address while editing the UI; the Docker version
at port `3000` will not automatically pick up your source changes.

The development server updates the browser as you edit files. Leave its terminal
open, and press **Ctrl+C** when you want to stop it. If you enabled authentication,
enter your configured token in the interface's connection dialog.

### If you are changing the Python API

For API-only work, you can run a separate API process directly on your computer.
This example uses a separate local SQLite database and port `8001` so it does not
conflict with the Docker API on port `8000`.

In a new terminal, open the repository root and run:

```bash
cd backend
export DEPLOYMENT_ENVIRONMENT=development SERVICE_ROLE=api
export DATABASE_URL=sqlite:///./adversarial_mvp.db ARTIFACT_BACKEND=local OTEL_ENABLED=false
uv run alembic upgrade head
uv run uvicorn adversarial_agent_mvp.api:create_app --factory --reload --port 8001
```

`alembic upgrade head` creates or updates the database tables. The last command
starts the API and reloads it when Python files change. Open
**http://localhost:8001/docs** to try its endpoints, and press **Ctrl+C** to stop it.
Authentication settings in `backend/.env` still apply.

This separate database will not contain the targets or experiments from Docker.
This API-only setup also does not start the worker or target supervisor needed to
execute experiments. Use the full Docker setup for complete test runs. After
changing backend code, rebuild the running services from `backend/` with:

```bash
docker compose up -d --build --wait
```

If you want the UI development server to use the API-only process, start the UI
with `BACKEND_API_URL=http://localhost:8001 npm run dev` from `ui/`.
See the [architecture guide](architecture-guide.html) for configuring workers,
shared storage, and a supervisor outside Docker Compose.

### Check your changes before sharing them

Automated tests and their supporting setup are maintained on the
`tests/project-suite` branch.

These commands check for programming errors and confirm that the packages can be
built. Run the group for the code you changed. Each block starts from the repository
root and uses parentheses to return you there when it finishes. `set -e` stops the
group if a command fails, so fix that failure before running the remaining checks.

**Python backend:**

```bash
(
  set -e
  cd backend
  uv run ruff check .              # Check Python coding rules and common mistakes.
  uv run pyright                   # Check that values have the expected types.
  uv build                        # Build the installable backend package.
)
```

**Browser interface:** builds need GNU `timeout` on your command search
path (`PATH`). On macOS with Homebrew, install and enable it in your current terminal:

```bash
brew install coreutils
export PATH="$(brew --prefix coreutils)/libexec/gnubin:$PATH"
```

Then run from the repository root:

```bash
(
  set -e
  cd ui
  npm run lint       # Check JavaScript and TypeScript coding rules.
  npm run typecheck  # Check types and generate runtime declarations.
  npm run build      # Build the production interface.
)
```

**Python SDK package:** from the repository root:

```bash
uv build sdk
```

Read each command's output. A nonzero exit code needs attention before you share
the change.

To build the UI, use `npm run build` from `ui/`. To serve that
build, run `BACKEND_API_URL=http://localhost:8000 npm run start` from the same folder.

### If you changed this README or the architecture guide

**Ask AML** is the documentation assistant in the web interface. It reads a generated
copy of the documentation, so updating the source text also requires refreshing
that copy. From the repository root:

```bash
(
  set -e
  cd backend
  uv run adversarial-guide --repo ..
  uv run adversarial-guide --repo .. --check
)
```

The first command refreshes the documentation index. The second checks that it
matches the source files. Include the generated
`backend/src/adversarial_agent_mvp/guide_knowledge.json.gz` file with your changes.
For Docker, rebuild and restart the API to load the refreshed documentation:

```bash
(cd backend && docker compose up -d --build api)
```

For an API running directly on your computer, stop and restart its process.
Ask AML requires operator permissions. Document search works without a model key;
generating answers also requires the backend's guide-model configuration.

## Current implementation

The project includes one Python finance agent, which can run as a scripted example
or use a configured AI model. AML stores test sessions, action histories, and results.
It also supports exporting datasets, recording model checkpoints, comparing evaluations,
and replaying experiments. Resetting a research session starts a new experiment in
a fresh isolated environment and keeps previous records.

Real-model acceptance, broader benchmarks, verified checkpoint loading, clean image
build validation, and a production Firecracker/KVM runner remain outstanding.
Training runs externally: researchers supply models, credentials, hardware, and
training/restoration code. Fixture success does not establish model performance.

| Directory | Contents |
|---|---|
| `backend/src/` | API, workers, Red/Blue, storage, verifiers, reference agent, and target protocol |
| `backend/migrations/` | Database migrations |
| `sdk/` | Independent Python client and executable examples |
| `ui/` | Web console, API proxy, and reusable components |

## References

- [Architecture guide](architecture-guide.html): system design, lifecycle, isolation, persistence, and operational boundaries.
- [UI product scope](ui/PRODUCT.md): research views and outcome interpretation.
