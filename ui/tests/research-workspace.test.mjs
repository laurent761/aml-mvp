import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { createViteTestServer } from "./helpers.mjs";

const vite = await createViteTestServer();
const { ResearchSessionTable, ResearchTrajectory } = await vite.ssrLoadModule("/app/research-workspace.tsx");
const { proxyBackendRequest } = await vite.ssrLoadModule("/lib/backend-proxy.ts");

test("SDK-created sessions show control mode, progress and interruption accurately", () => {
  const html = renderToStaticMarkup(React.createElement(ResearchSessionTable, {
    sessions: [{ id: "session_abc", run_id: "run_abc", state: "interrupted", episode_id: "episode_1", step_index: 2,
      stop_reason: "worker_interrupted", configuration: { mode: "external", limits: { max_steps: 12 } } }], onSelect() {},
  }));
  assert.match(html, /external/);
  assert.match(html, /2 \/ 12 steps/);
  assert.match(html, /worker interrupted/);
  assert.match(html, /run_abc/);
  assert.doesNotMatch(html, /NaN|\[object Object\]/);
});

test("trajectory keeps verified outcome, delivery receipt and usage distinct", () => {
  const html = renderToStaticMarkup(React.createElement(ResearchTrajectory, { operations: [{ id: "op_1", kind: "step", status: "completed", input: {}, result: {
    public_observation: { target_response: "<script>untrusted target text</script>", delivery_receipt: { status: "applied", slot_id: "invoice-content", delivery_point: "tool_response" } },
    outcome: { terminal_success: true, termination_reason: "forbidden_state", truncation_reason: null, execution_status: "ok" },
    usage: { model_tokens: 42, cost: .02, latency_ms: 30 },
  } }] }));
  assert.match(html, /Forbidden state reached/);
  assert.match(html, /invoice-content/);
  assert.match(html, /42 model tokens/);
  assert.doesNotMatch(html, /<script>untrusted/);
});

test("proxy maps only an explicit research token to backend authorization", async () => {
  await proxyBackendRequest(new Request("https://ui.example/v1/research-sessions", { headers: {
    "authorization": "Bearer unrelated-browser-token", "x-aml-research-token": "research-account-token",
    "x-aml-api-version": "aml.research.v1",
  } }), "https://backend.example", async request => {
    assert.equal(request.headers.get("authorization"), "Bearer research-account-token");
    assert.equal(request.headers.get("x-aml-research-token"), null);
    assert.equal(request.headers.get("x-aml-api-version"), "aml.research.v1");
    return Response.json({ items: [] });
  });
});
