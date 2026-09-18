import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { once } from "node:events";
import { createServer } from "node:http";
import test from "node:test";

import { createViteTestServer } from "./helpers.mjs";

const vite = await createViteTestServer();
const { proxyBackendRequest } = await vite.ssrLoadModule("/lib/backend-proxy.ts");

async function listen(t, handler) {
  const server = createServer(handler);
  t.after(async () => {
    const closed = new Promise(resolve => server.close(resolve));
    server.closeAllConnections();
    await closed;
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return `http://127.0.0.1:${server.address().port}`;
}

test("binary artifacts round-trip through a real backend with exact bytes and integrity metadata", { timeout: 10_000 }, async (t) => {
  const payload = Buffer.from([0, 255, 128, 13, 10, 0, 65, 66, 67, 240, 159, 147, 132]);
  const sha256 = createHash("sha256").update(payload).digest("hex");
  let incoming;
  const origin = await listen(t, (request, response) => {
    incoming = { method: request.method, url: request.url, headers: request.headers };
    response.writeHead(200, {
      "content-type": "application/octet-stream",
      "content-disposition": 'attachment; filename="evidence.bin"',
      "content-length": payload.length,
      "cache-control": "private, no-store",
      etag: '"artifact-v1"',
      "x-content-sha256": sha256,
      "x-artifact-sha256": sha256,
      "set-cookie": "backend_session=private; HttpOnly",
      "x-internal-debug": "private-backend-metadata",
      "access-control-allow-origin": "*",
    });
    response.write(payload.subarray(0, 5));
    response.end(payload.subarray(5));
  });

  const response = await proxyBackendRequest(new Request("https://ui.example:8443/v1/artifacts/file%2Fname?download=1&label=a%2Bb&label=%E6%97%A5", { headers: { "x-forwarded-host": "attacker.example", "x-forwarded-proto": "http" } }), `${origin}/ignored-prefix?ignored=1`);

  assert.equal(incoming.method, "GET");
  assert.equal(incoming.url, "/v1/artifacts/file%2Fname?download=1&label=a%2Bb&label=%E6%97%A5");
  assert.equal(incoming.headers["x-forwarded-host"], "ui.example:8443");
  assert.equal(incoming.headers["x-forwarded-proto"], "https");
  assert.equal(response.status, 200);
  assert.deepEqual(Buffer.from(await response.arrayBuffer()), payload);
  assert.deepEqual(Object.fromEntries(response.headers), {
    "cache-control": "private, no-store",
    "content-disposition": 'attachment; filename="evidence.bin"',
    "content-length": String(payload.length),
    "content-type": "application/octet-stream",
    etag: '"artifact-v1"',
    "x-artifact-sha256": sha256,
    "x-content-sha256": sha256,
  });
});

test("a real backend receives unmodified binary mutations and no ambient identity headers", { timeout: 10_000 }, async (t) => {
  const payload = Buffer.from([0, 10, 13, 128, 255, 0]);
  let incoming;
  const origin = await listen(t, (request, response) => {
    const chunks = [];
    request.on("data", chunk => chunks.push(chunk));
    request.on("end", () => {
      incoming = { method: request.method, headers: request.headers, body: Buffer.concat(chunks) };
      response.writeHead(204);
      response.end();
    });
  });
  const response = await proxyBackendRequest(new Request("https://ui.example/v1/artifacts/blob", {
    method: "PUT", body: payload,
    headers: {
      "content-type": "application/octet-stream",
      "idempotency-key": "artifact-write-1",
      "x-aml-api-version": "aml.research.v1",
      authorization: "Bearer browser-session",
      cookie: "sid=browser-session",
      "x-aml-research-token": "explicit-research-token",
      "oai-authenticated-user-email": "private@example.com",
      "cf-access-jwt-assertion": "private-access-token",
      "x-forwarded-for": "private-browser-address",
    },
  }), origin);

  assert.equal(response.status, 204);
  assert.equal(await response.text(), "");
  assert.equal(incoming.method, "PUT");
  assert.deepEqual(incoming.body, payload);
  assert.equal(incoming.headers.authorization, "Bearer explicit-research-token");
  assert.equal(incoming.headers["content-type"], "application/octet-stream");
  assert.equal(incoming.headers["idempotency-key"], "artifact-write-1");
  assert.equal(incoming.headers["x-aml-api-version"], "aml.research.v1");
  for (const name of ["cookie", "x-aml-research-token", "oai-authenticated-user-email", "cf-access-jwt-assertion", "x-forwarded-for"]) {
    assert.equal(incoming.headers[name], undefined, `${name} must not cross the backend boundary`);
  }
});

for (const status of [400, 401, 403, 404, 409, 422, 429, 500, 503]) {
  test(`backend HTTP ${status} remains distinguishable from a proxy connection failure`, async () => {
    const payload = { detail: { code: "backend_error", status } };
    const response = await proxyBackendRequest(new Request("https://ui.example/v1/research-sessions"), "https://backend.example", async () => Response.json(payload, { status, headers: { "set-cookie": "private=1" } }));

    assert.equal(response.status, status);
    assert.deepEqual(await response.json(), payload);
    assert.equal(response.headers.get("set-cookie"), null);
  });
}

for (const status of [301, 303, 307, 308]) {
  test(`redirect ${status} cannot move an authenticated mutation to another origin`, async () => {
    let calls = 0;
    const response = await proxyBackendRequest(new Request("https://ui.example/v1/research-sessions", { method: "POST", body: "{}", headers: { "x-aml-research-token": "research-secret" } }), "https://backend.example", async request => {
      calls++;
      assert.equal(request.redirect, "manual");
      assert.equal(request.url, "https://backend.example/v1/research-sessions");
      return new Response(null, { status, headers: { location: "https://other.example/collect" } });
    });

    assert.equal(calls, 1);
    assert.equal(response.status, 502);
    assert.equal(response.headers.get("location"), null);
    assert.deepEqual(await response.json(), { detail: "Backend redirects are not followed by the UI proxy" });
  });
}

test("transport failures return a stable gateway error without leaking connection details", async () => {
  const response = await proxyBackendRequest(new Request("https://ui.example/v1/campaigns"), "https://backend.example", async () => { throw new Error("connect ECONNREFUSED internal-host:1234; secret=private"); });

  assert.equal(response.status, 502);
  assert.deepEqual(await response.json(), { detail: "The configured backend could not be reached" });
});

test("unreadable request bodies fail before any upstream mutation is attempted", async () => {
  const request = new Request("https://ui.example/v1/campaigns", { method: "POST", body: "{}" });
  await request.text();
  let calls = 0;
  const response = await proxyBackendRequest(request, "https://backend.example", async () => { calls++; return Response.json({}); });

  assert.equal(calls, 0);
  assert.equal(response.status, 502);
  assert.deepEqual(await response.json(), { detail: "The configured backend could not be reached" });
});

test("HEAD requests forward metadata without introducing a body", async () => {
  const response = await proxyBackendRequest(new Request("https://ui.example/v1/artifacts/blob", { method: "HEAD" }), "https://backend.example", async request => {
    assert.equal(request.method, "HEAD");
    assert.equal(request.body, null);
    return new Response(null, { status: 200, headers: { "content-length": "1234", "content-type": "application/octet-stream", etag: '"digest"' } });
  });

  assert.equal(await response.text(), "");
  assert.equal(response.headers.get("content-length"), "1234");
  assert.equal(response.headers.get("etag"), '"digest"');
});

test("invalid backend configuration never performs a network request", async () => {
  let calls = 0;
  for (const origin of ["   ", "not a url", "javascript:alert(1)", "ftp://backend.example"]) {
    const response = await proxyBackendRequest(new Request("https://ui.example/readyz"), origin, async () => { calls++; return Response.json({}); });
    assert.equal(response.status, 503, origin);
    assert.equal(typeof (await response.json()).detail, "string");
  }
  assert.equal(calls, 0);
});
