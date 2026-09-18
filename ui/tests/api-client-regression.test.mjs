import assert from "node:assert/strict";
import { once } from "node:events";
import { createServer } from "node:http";
import { afterEach, test } from "node:test";

import { createViteTestServer } from "./helpers.mjs";

const vite = await createViteTestServer();
const { ApiError, apiRequest, setResearchToken } = await vite.ssrLoadModule("/lib/api-client.ts");

afterEach(() => setResearchToken("", ""));

function captureRequests(t, response = () => Response.json({ items: [] })) {
  const requests = [];
  t.mock.method(globalThis, "fetch", async (url, init) => {
    requests.push({ url, ...init });
    return response();
  });
  return requests;
}

test("same-origin credentials use the explicit research header and never browser authorization", async (t) => {
  const requests = captureRequests(t);
  setResearchToken(" / ", "  research-secret  ");

  await apiRequest("", "/v1/research-sessions");

  assert.equal(requests[0].url, "/v1/research-sessions");
  assert.equal(requests[0].headers.get("x-aml-research-token"), "research-secret");
  assert.equal(requests[0].headers.get("authorization"), null);
  assert.equal(requests[0].headers.get("x-aml-api-version"), "aml.research.v1");
});

test("direct credentials are normalized, isolated by selected API, and removed on API switches", async (t) => {
  const requests = captureRequests(t);
  setResearchToken(" https://first.example/// ", " first-secret ");
  await apiRequest("https://first.example/", "/v1/research-sessions");
  await apiRequest("https://second.example", "/v1/research-sessions");
  await apiRequest("", "/v1/research-sessions");
  setResearchToken("https://second.example", "second-secret");
  await apiRequest("https://first.example", "/v1/research-sessions");
  await apiRequest("https://second.example", "/v1/research-sessions");

  assert.deepEqual(requests.map(({ headers }) => headers.get("authorization")), [
    "Bearer first-secret", null, null, null, "Bearer second-secret",
  ]);
  assert.ok(requests.every(({ headers }) => !headers.has("x-aml-research-token")));
});

test("clearing a credential removes it from subsequent calls", async (t) => {
  const requests = captureRequests(t);
  setResearchToken("", "old-secret");
  await apiRequest("", "/v1/research-sessions");
  setResearchToken("", " \t ");
  await apiRequest("", "/v1/research-sessions");

  assert.equal(requests[0].headers.get("x-aml-research-token"), "old-secret");
  assert.equal(requests[1].headers.get("x-aml-research-token"), null);
});

test("JSON mutations preserve caller inputs, idempotency and abort signal while disabling caching", async (t) => {
  const requests = captureRequests(t, () => Response.json({ id: "session-created" }, { status: 201 }));
  const controller = new AbortController();
  const headers = new Headers({ "Idempotency-Key": "session-create-1", "X-AML-API-Version": "obsolete" });
  const body = JSON.stringify({ mode: "external", limits: { max_steps: 5 } });
  const init = Object.freeze({ method: "POST", body, headers, signal: controller.signal, cache: "force-cache" });

  const result = await apiRequest("https://api.example", "v1/research-sessions", init);

  assert.deepEqual(result, { id: "session-created" });
  assert.equal(requests[0].url, "https://api.example/v1/research-sessions");
  assert.equal(requests[0].method, "POST");
  assert.equal(requests[0].body, body);
  assert.equal(requests[0].signal, controller.signal);
  assert.equal(requests[0].cache, "no-store");
  assert.equal(requests[0].headers.get("idempotency-key"), "session-create-1");
  assert.equal(requests[0].headers.get("content-type"), "application/json");
  assert.equal(requests[0].headers.get("x-aml-api-version"), "aml.research.v1");
  assert.notEqual(requests[0].headers, headers);
  assert.equal(headers.has("content-type"), false, "caller-owned headers must not be mutated");
  assert.equal(headers.get("x-aml-api-version"), "obsolete");
});

test("explicit content types survive and bodyless reads do not acquire a JSON content type", async (t) => {
  const requests = captureRequests(t);
  const bytes = new Uint8Array([0, 255, 128, 13, 10]);
  await apiRequest("", "/v1/artifacts", { method: "PUT", body: bytes, headers: { "content-type": "application/octet-stream" } });
  await apiRequest("", "/v1/artifacts");

  assert.equal(requests[0].body, bytes);
  assert.equal(requests[0].headers.get("content-type"), "application/octet-stream");
  assert.equal(requests[1].headers.get("content-type"), null);
});

test("successful no-content responses do not attempt JSON parsing", async (t) => {
  captureRequests(t, () => new Response(null, { status: 204 }));
  assert.equal(await apiRequest("", "/v1/research-sessions/session-1", { method: "DELETE" }), undefined);
});

const errorCases = [
  { name: "validation arrays", status: 422, body: { detail: [{ loc: ["body", "max_steps"], msg: "Must be positive", type: "greater_than" }] }, expected: '[{"loc":["body","max_steps"],"msg":"Must be positive","type":"greater_than"}]' },
  { name: "structured operation conflicts", status: 409, body: { detail: { code: "operation_in_progress", operation_id: "op-1" } }, expected: '{"code":"operation_in_progress","operation_id":"op-1"}' },
  { name: "JSON without detail", status: 401, statusText: "Unauthorized", body: { message: "credentials rejected" }, expected: "401 Unauthorized" },
  { name: "null error payload", status: 502, statusText: "Bad Gateway", body: null, expected: "502 Bad Gateway" },
  { name: "HTML gateway errors", status: 503, statusText: "Service Unavailable", raw: "<html>Unavailable</html>", expected: "503 Service Unavailable" },
  { name: "empty error bodies", status: 504, statusText: "Gateway Timeout", raw: "", expected: "504 Gateway Timeout" },
];

for (const scenario of errorCases) {
  test(`API failures retain status and useful detail for ${scenario.name}`, async (t) => {
    captureRequests(t, () => new Response(scenario.raw ?? JSON.stringify(scenario.body), { status: scenario.status, statusText: scenario.statusText }));
    await assert.rejects(apiRequest("", "/v1/research-sessions"), error => {
      assert.ok(error instanceof ApiError);
      assert.ok(error instanceof Error);
      assert.equal(error.name, "ApiError");
      assert.equal(error.status, scenario.status);
      assert.equal(error.message, scenario.expected);
      assert.equal(error.detail, scenario.expected);
      return true;
    });
  });
}

test("network errors retain their identity without inventing an HTTP status or retrying mutations", async (t) => {
  const failure = new TypeError("connection reset");
  const fetchMock = t.mock.method(globalThis, "fetch", async () => { throw failure; });

  await assert.rejects(apiRequest("", "/v1/research-sessions", { method: "POST", body: "{}" }), error => error === failure);
  assert.equal(fetchMock.mock.callCount(), 1);
});

test("malformed successful responses fail instead of masquerading as empty data", async (t) => {
  captureRequests(t, () => new Response('{"items":', { status: 200 }));
  await assert.rejects(apiRequest("", "/v1/research-sessions"), { name: "SyntaxError" });
});

test("aborting an in-flight request cancels real HTTP work with the original abort reason", { timeout: 10_000 }, async (t) => {
  let received;
  const requestReceived = new Promise(resolve => { received = resolve; });
  const upstream = createServer((_request, response) => {
    received();
    response.on("error", () => {});
    // Deliberately leave the response pending; the test drives cancellation without sleeps.
  });
  t.after(async () => {
    const closed = new Promise(resolve => upstream.close(resolve));
    upstream.closeAllConnections();
    await closed;
  });
  upstream.listen(0, "127.0.0.1");
  await once(upstream, "listening");

  const controller = new AbortController();
  const reason = new DOMException("API origin changed", "AbortError");
  const pending = apiRequest(`http://127.0.0.1:${upstream.address().port}`, "/v1/research-sessions", { signal: controller.signal });
  const rejection = assert.rejects(pending, error => error === reason);
  await requestReceived;
  controller.abort(reason);
  await rejection;
});
