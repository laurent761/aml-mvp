# INF-02 — Target inference integration

Target inference is configured independently from the attacker. No model is selected by default. The supported implementation uses Docker capsules and an operator-selected OpenAI-compatible chat-completions endpoint.

## Route and credentials

```text
isolated target → Blue inference mailbox
                       ↑ claim / complete through docker exec
                 trusted supervisor → configured provider or local model

isolated target → Blue business tools → virtual world only
```

Target and Blue retain exactly one internal network and exactly two members. Neither container receives an external network attachment, host mount, provider endpoint, or provider credential. The supervisor polls a bounded mailbox through its existing Docker control channel, calls the configured provider, and returns a sanitized completion. Inference is a dedicated protocol, not a generic HTTP proxy or a simulated business effect.

The target receives `BLUE_INFERENCE_CAPABILITY`, a signed, expiring episode capability restricted to `__target_inference__`. Its business-tool capability cannot use inference; its inference capability cannot use business tools or private administration. The reserved inference destination cannot be declared as a business route. Runtime containment verification checks actual network membership, inference capability scope, and unexpected credential environment variables.

Compose passes `TARGET_MODEL_*` only to the supervisor. The worker retains separate `ATTACKER_MODEL_*` settings. Model mode never falls back to the deterministic reference fixture.

## Operator configuration

Set the operator-selected endpoint and model in your environment or `backend/.env`:

For deployed model execution, use the complete `compose.yaml` profile. The separate
`compose.research-smoke.yaml` fixes target inference to `disabled`; changing `.env`
alone does not enable a model in that fixture profile.

```dotenv
TARGET_MODEL_PROVIDER=local_openai_compatible
TARGET_MODEL_BASE_URL=http://your-model-service:8000/v1/
TARGET_MODEL_NAME=your-selected-model
TARGET_MODEL_API_KEY=
TARGET_MODEL_REVISION=
```

For a hosted endpoint use `hosted_openai_compatible`, HTTPS, and an API key. Local endpoints support HTTP/HTTPS with an optional key. The URL resolves from the **supervisor's** network namespace: a container's loopback address is not the host's loopback address. Embedded URL credentials, query strings, fragments, redirects, and environment-configured HTTP proxies are rejected or disabled.

All settings are listed in `.env.example`:

| Suffix after `TARGET_MODEL_` | Default | Meaning |
| --- | --- | --- |
| `TEMPERATURE`, `TOP_P` | `0`, `1` | Pinned generation parameters |
| `MAX_OUTPUT_TOKENS` | `1000` | Per-request output cap |
| `TOKEN_PARAMETER` | `max_tokens` | Alternative: `max_completion_tokens` |
| `JSON_MODE` | `true` | Request JSON-object responses |
| `TIMEOUT_SECONDS` | `30` | Provider deadline including concurrency wait |
| `MAX_INPUT_BYTES`, `MAX_RESPONSE_BYTES` | `65536`, `262144` | Message input and provider response caps |
| `MAX_REQUESTS` | `100` | Episode request cap, retained across reset |
| `MAX_TOTAL_TOKENS` | `200000` | Episode token budget |
| `MAX_COST` | `10` | Episode budget in the operator's pricing units |
| `INPUT_COST_PER_MILLION`, `OUTPUT_COST_PER_MILLION` | `0`, `0` | Operator-supplied rates |
| `MAX_CONCURRENCY` | `4` | Concurrent provider calls per supervisor process |

The endpoint/model must support the configured chat-completions parameters and the reference target's JSON decision schema. Both temperature and top-p are sent on each call. There is no automatic provider or model substitution.

## Pin, register, and run

From `backend/`, export a credential-free profile and render a model bundle with the real image ID printed by the reference build:

```bash
uv run adversarial-bundle build-reference --output var/bundles/finance-fixture.json
SERVICE_ROLE=capsule-supervisor uv run adversarial-bundle inference-profile --output var/bundles/target-inference-profile.json
uv run adversarial-bundle reference \
  --image 'sha256:<actual-built-image-id>' \
  --inference-profile var/bundles/target-inference-profile.json \
  --output var/bundles/finance-model.json
uv run adversarial-bundle validate var/bundles/finance-model.json
```

The profile includes model, provider type, endpoint hash, parameters, limits, prices, and any operator-declared revision. It contains neither the raw endpoint nor credentials. The immutable target manifest pins this profile. Provisioning fails if the supervisor configuration differs; export and register a new bundle after changing configuration. An unchanged scenario can be reused.

Register using the existing [target bundle workflow](TARGET_BUNDLES.md). For deployment, rebuild Blue and the supervisor from current source and start the updated stack with the configured supervisor. For an isolated local acceptance run:

```bash
docker build --target blue-gateway -t aml-inference-blue:local .
SERVICE_ROLE=capsule-supervisor OTEL_ENABLED=false uv run adversarial-bundle smoke var/bundles/finance-model.json \
  --docker --blue-image aml-inference-blue:local
```

This CLI hosts the trusted broker in the invoking process; use an endpoint reachable from that process. A deployed Compose supervisor has its own network namespace. The [cached-runtime build fallback](TARGET_BUNDLES.md#cached-runtime-fallback) remains available when registries cannot be reached.

The smoke requires successful legitimate model-driven tool use, reset, and containment, and reports observed attack consequences. A model may resist the known attack. `poc_model_gate: passed-configured-endpoint` reports the configured endpoint workflow; the operator must establish the endpoint's real-model provenance. Scripted providers remain wiring fixtures.

## Target protocol

The reference entrypoint is `python -m aml_reference_target --mode model`. It automatically uses `http://blue:8080/v1/inference/` and the injected capability. Optional `--model` asserts the pinned model name. Arbitrary model URLs are rejected.

Other targets POST `/v1/inference/chat/completions` to Blue with:

```text
Authorization: Bearer <BLUE_INFERENCE_CAPABILITY>
X-Episode-ID: <BLUE_EPISODE_ID>
X-Correlation-ID: <unique ID: 1–100 letters/digits/_/->
```

```json
{"messages":[{"role":"system","content":"Return a JSON object."},{"role":"user","content":"..."}]}
```

Messages accept `system`, `user`, and `assistant` roles. An optional `model` must equal the pinned model. Endpoint, credentials, generation overrides, and additional fields are rejected. Success returns `choices[0].message.content`. Budget rejection returns 429, timeout 504, and provider failure 502, with sanitized error codes. Identical input with the same correlation ID returns the same result; conflicting input is rejected. Provider calls are never automatically retried. Retrying completion delivery to Blue does not rerun inference.

## Accounting and reproducibility

Before dispatch, the broker reserves a conservative input/output token allowance and configured cost. Valid provider usage replaces the reservation. Missing usage, timeout, cancellation, malformed responses, and transport failures retain a conservative reservation because the provider may have performed work. These values are labeled `conservative_reservation`, not measured counts. Pre-dispatch rejection has zero usage. Actual provider tokenization and billing remain external; the operator must maintain prices. Zero rates are labeled `unpriced`, not proof of zero billing.

Blue permits four pending requests per episode, bounded by the request cap. An unclaimed request that times out cannot later dispatch. Relay failure closes inference and settles pending work with explicit failure records. Reset clears business effects and target conversation while retaining inference history, idempotency, and spend. Normal teardown drains pending inference before evidence generation and cancels the relay when destroying the capsule.

Private audit records include episode/request correlation, request hash, profile, status, token and cost sources, latency, and available provider request ID, resolved model, and system fingerprint. Missing provider resolution fields remain null. Reproducibility is `provider_revision_unavailable` unless an operator revision is supplied, in which case it is `operator_revision_declared`. Neither a fingerprint nor a mutable model alias is claimed to be an immutable provider revision.

The existing `ModelInvocation` and attribution tables store role `target`, stable invocation IDs, one campaign charge, episode links, and step links for completed actions. Episode API responses and evidence exports retain the credential-free audit. Private traces and credentials are not returned to the target. No schema migration is needed.

Abrupt supervisor/host loss can interrupt in-memory inference before audit persistence.
[Research recovery](RESEARCH_INTEGRATION.md#sessions-operations-and-outcomes) now
retires interrupted sessions and preserves indeterminate commands. It does not make
provider transactions durable, reconstruct missing provider usage or guarantee
provider-side cancellation. The external Firecracker runner is not implemented or
validated by this Docker relay.

## Validation

```bash
OTEL_ENABLED=false uv run pytest
uv run ruff check .
uv run pyright
uv build
AML_REFERENCE_BUNDLE=var/bundles/finance-fixture.json \
AML_REFERENCE_BLUE_IMAGE=aml-inference-blue:local OTEL_ENABLED=false \
uv run pytest tests/live/test_target_inference_docker.py --no-cov
```

The live Docker test starts an explicit scripted HTTP provider on the host, runs actual model-mode target and Blue containers, verifies simulated tool effects and reset, inspects credentials and network membership, probes blocked target egress, checks metering, and removes its resources. A separate operator-selected real-model run is required to close the real-model acceptance gate.
