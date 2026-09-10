# INF-03 — Explicit intervention delivery

The finance reference scenario is now version **1.1.0** and supports three explicitly declared surfaces. New bundles opt in with `manifest.intervention_protocol: "aml.intervention.v1"`. Existing immutable 1.0.0 scenarios and legacy target manifests remain readable and runnable; legacy targets do not claim verified delivery receipts.

## Supported payloads

| Surface | Payload | Reference delivery point | Limit |
| --- | --- | --- | --- |
| `user_message` | `{"text":"..."}` | Next conversational input, slot `conversation` | 8,192 bytes |
| `uploaded_document` | `{"document_name":"invoice-note.txt","content":"..."}` | Named slot `invoice-attachment`, before the next workflow's first model turn | 16,384 bytes |
| `simulated_tool_result` | `{"slot_id":"invoice-content","content":"..."}` | First matching `documents` / `file.read` response for `file_id=invoice-001`; replace only `result.file.content` | 16,384 bytes |

Limits count the UTF-8 encoding of JSON with sorted keys, compact separators, and unescaped Unicode. Required string values cannot be blank. Extra fields and non-string content are rejected. Document names and slot IDs are opaque names of up to 128 characters, beginning with a letter or digit and containing only letters, digits, `_`, `.`, or `-`. They are not filesystem paths.

The public scenario catalog exposes the payload schemas, limits, delivery points, and slot definitions. Supported slot definitions are:

- Document: `kind=document`, `slot_id`, `document_name`, and `workflow_point=before_first_model_turn`.
- Tool response: `kind=tool_result`, `slot_id`, `destination_alias`, `operation`, nonempty `arguments_match`, `occurrence` (1–30), and `result_path` inside the public result dictionary.

The gateway and target share the small `aml_target_protocol` package. Registration validates each surface against the supported schema and ensures tool slots use declared destination operations. Changes to names, selectors, occurrences, paths, limits, or other intervention semantics require a new scenario version.

## Delivery behavior

Blue validates the action before invoking the target. It creates a delivery ID and a plan bound to the requested surface, slot, point, and content hash. The attacker cannot supply this plan through the action payload.

For a message, the reference target appends only the requested conversational text. For an uploaded document, it consumes a named document envelope at the declared workflow point and retains the content in its in-memory document collection. Uploading does not write or replace any virtual file, alter a tool response, or select an arbitrary target filesystem path. Reset clears the document collection and conversation.

For a tool-result action, Blue holds the replacement. The target receives only the slot ID and a workflow trigger; replacement text is absent from the conversational input. Target tool requests carry `X-Intervention-ID` for their current invocation. Blue checks that ID, destination, operation, argument selector, and occurrence. The substitution is applied only if the selected public result leaf exists, is a string, and the simulated operation succeeded. Missing, denied, or incompatible results leave the slot unapplied.

The actual virtual operation and private verifier processing happen first. Blue copies the public result, replaces only its declared leaf, and returns that copy to the target. The original effect, virtual result, private events, identity, and verifier state remain authoritative. Replacement content can contain text resembling JSON or verifier events, but it remains a string in public data. It is never interpreted as trusted evidence.

The overlay is confined to one invocation and one matching response. Repeated identical tool requests use existing effect idempotency and do not consume a second occurrence. A tool correlation ID reused with a different intervention ID conflicts instead of returning an altered response to another action. Stale IDs, unrelated tools, other documents, and later responses do not receive the replacement. The same behavior applies to MCP calls, MCP JSON-RPC, and both HTTP tool facades.

## Receipts and errors

`PublicObservation.delivery_receipt` contains Blue's receipt. The target's supplied receipt fields are ignored. A matching target acknowledgement confirms message/document consumption under the target contract; a gateway-recorded tool substitution confirms the tool-response delivery boundary. These are delivery facts, not proof that a model followed an instruction or that a forbidden outcome occurred.

| `status` | Meaning |
| --- | --- |
| `applied` | Exact content was applied to the recorded slot with delivery evidence |
| `unsupported_surface` | This scenario did not declare the requested surface |
| `malformed_action` | Invalid action envelope, payload, encoding, or size |
| `unavailable_slot` | Unknown document/slot, or the selected tool response was not observed in this invocation |
| `target_rejected` | The target explicitly rejected the action or violated the target contract |
| `delivery_unknown` | A transport/response failure or missing/mismatched acknowledgement prevented confirmation |

Only `applied` has `applied: true`, `delivered_content`, and `content_sha256`. Receipts also include request/action correlation, `payload_sha256`, requested slot, actual affected slot, delivery point, and evidence type. Tool substitutions include the matching effect ID and tool correlation ID. Successful delivery remains a delivery fact even if later model/tool execution fails; execution errors are reported separately.

The gateway's authenticated private trace includes `intervention_events`, with a monotonically increasing sequence and an `intervention_after` cursor. Receipt history survives world resets within the current episode; pending overlays do not. There is a hard bound of 1,000 receipts per gateway episode. Reset clears conversation, documents, and virtual effects using the existing lifecycle.

Normal orchestrated steps persist the receipt in the existing step observation. Episode API responses and evidence exports retain it without a database migration. Replay and novelty compare receipt content/status/slot facts while excluding run-specific receipt, episode, action, effect, and tool correlation IDs. Differences in actual public tool results still count as replay differences.

To verify a receipt from a persisted step:

```python
import hashlib
import json

action = step["red_action"]
receipt = step["public_observation"]["delivery_receipt"]
payload = json.dumps(action["payload"], ensure_ascii=False,
                     sort_keys=True, separators=(",", ":")).encode("utf-8")
assert hashlib.sha256(payload).hexdigest() == receipt["payload_sha256"]
assert action["action_id"] == receipt["action_id"]
if receipt["applied"]:
    delivered = receipt["delivered_content"].encode("utf-8")
    assert hashlib.sha256(delivered).hexdigest() == receipt["content_sha256"]
```

For a tool receipt, its effect ID links to the separately stored original virtual effect/result. These hashes verify content consistency; receipt authority comes from the trusted gateway and persisted evidence, not a target-generated assertion about verifier success.

## Target integration

Opt-in targets accept `intervention_surfaces` on reset and a `delivery` plan on invoke. Message/document handling must validate the plan, consume the content at the declared point, and return an `intervention_ack` containing the plan's `delivery_id`, `channel`, `slot_id`, `delivery_point`, and `content_sha256`. Acknowledgement is produced after input consumption, even if subsequent model execution fails.

For tool-result interventions, the invoke payload contains only `slot_id`; the target must never expect replacement content there. It starts the configured legitimate workflow and tags its tool requests with the supplied delivery ID. Blue generates the delivery receipt when the matching response is substituted. Targets must not manufacture private effect or verifier events.

The shared public package is included in target builds alongside the reference agent. It contains no scenario ground truth, verifier implementation, or provider credentials.

## Build and validate

Use the existing [target bundle](TARGET_BUNDLES.md) and [target inference](TARGET_INFERENCE.md) workflows. Rendering the reference now produces a 1.1.0 scenario with all three surfaces. For cached local dependencies:

```bash
uv run adversarial-bundle build-reference --cached-runtime-image blue-gateway:local \
  --output var/bundles/finance-reference.json
docker build --pull=false -f targets/reference/Dockerfile.blue-cached \
  --build-arg CACHED_RUNTIME_IMAGE=blue-gateway:local -t aml-inf03-blue:local .
AML_REFERENCE_BUNDLE=var/bundles/finance-reference.json \
AML_REFERENCE_BLUE_IMAGE=aml-inf03-blue:local OTEL_ENABLED=false uv run pytest
```

The live inference test uses an explicitly scripted HTTP provider outside the internal capsule network and exercises all three surfaces through the actual model-mode target. It verifies receipts, virtual tool consequences, reset, provider credential isolation, blocked outbound connectivity, and usage accounting. This is delivery/inference wiring evidence; it is not a real-model behavior result.

Research session APIs, action-level idempotency/recovery, and durable handling of abruptly interrupted workers remain INF-04. This phase does not open capsule network access or add arbitrary intervention surfaces.
