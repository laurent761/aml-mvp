import assert from "node:assert/strict";
import test from "node:test";


import { createViteTestServer } from "./helpers.mjs";

const vite = await createViteTestServer();

const { isBackendPath, proxyBackendRequest, resolveBackendApiUrl } = await vite.ssrLoadModule(
  "/lib/backend-proxy.ts",
);

test("recognizes only control API paths", () => {
  assert.equal(isBackendPath("/readyz"), true);
  assert.equal(isBackendPath("/healthz"), true);
  assert.equal(isBackendPath("/v1/campaigns"), true);
  assert.equal(isBackendPath("/v10/campaigns"), false);
  assert.equal(isBackendPath("/"), false);
});

test("fails closed when the backend binding is absent or invalid", async () => {
  const request = new Request("https://ui.example/readyz");
  assert.equal((await proxyBackendRequest(request, undefined)).status, 503);
  assert.equal((await proxyBackendRequest(request, "file:///tmp/backend")).status, 503);
});

test("prefers a Worker binding and falls back to the local production environment", () => {
  assert.equal(
    resolveBackendApiUrl("https://worker.example", "http://127.0.0.1:8000"),
    "https://worker.example",
  );
  assert.equal(
    resolveBackendApiUrl("", "http://127.0.0.1:8000"),
    "http://127.0.0.1:8000",
  );
  assert.equal(resolveBackendApiUrl(undefined, undefined), undefined);
});

test("forwards path, query, method, body, and only allowed headers", async () => {
  let captured;
  const request = new Request("https://ui.example/v1/campaigns?limit=10", {
    method: "POST",
    headers: {
      accept: "application/json",
      authorization: "Bearer must-not-leak",
      cookie: "session=must-not-leak",
      "content-type": "application/json",
      "idempotency-key": "stable-key",
      "oai-authenticated-user-email": "must-not-leak@example.com",
    },
    body: JSON.stringify({ search_mode: "adaptive" }),
  });
  const response = await proxyBackendRequest(
    request,
    "https://backend.example",
    async (upstreamRequest) => {
      captured = {
        url: upstreamRequest.url,
        method: upstreamRequest.method,
        headers: Object.fromEntries(upstreamRequest.headers.entries()),
        body: await upstreamRequest.text(),
        redirect: upstreamRequest.redirect,
      };
      return Response.json({ accepted: true }, {
        status: 202,
        headers: {
          etag: "abc123",
          "set-cookie": "backend-session=must-not-reach-browser",
          "x-artifact-sha256": "deadbeef",
        },
      });
    },
  );

  assert.equal(response.status, 202);
  assert.deepEqual(captured, {
    url: "https://backend.example/v1/campaigns?limit=10",
    method: "POST",
    headers: {
      accept: "application/json",
      "content-type": "application/json",
      "idempotency-key": "stable-key",
      "x-forwarded-host": "ui.example",
      "x-forwarded-proto": "https",
    },
    body: '{"search_mode":"adaptive"}',
    redirect: "manual",
  });
  assert.equal(response.headers.get("etag"), "abc123");
  assert.equal(response.headers.get("x-artifact-sha256"), "deadbeef");
  assert.equal(response.headers.get("set-cookie"), null);
});

test("does not follow backend redirects", async () => {
  const response = await proxyBackendRequest(
    new Request("https://ui.example/v1/findings"),
    "https://backend.example",
    async () => new Response(null, { status: 302, headers: { location: "https://other.example" } }),
  );
  assert.equal(response.status, 502);
  assert.deepEqual(await response.json(), {
    detail: "Backend redirects are not followed by the UI proxy",
  });
});
