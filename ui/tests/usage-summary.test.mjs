import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createViteTestServer } from "./helpers.mjs";
const vite = await createViteTestServer();
const { UsagePanel, combineUsage, AttackerModeNotice } = await vite.ssrLoadModule("/app/usage-summary.tsx");
const base = { execution_kind: "none", real_calls: 0, simulated_calls: 0, unknown_calls: 0, runtime_tokens: 0, provider_tokens: 0, estimated_tokens: 0, simulated_tokens: 0, unknown_tokens: 0, priced_cost: 0, unpriced_calls: 0 };
const render = usage => renderToStaticMarkup(React.createElement(UsagePanel, { usage }));
test("simulation has an explicit visual treatment and never claims billed model tokens", () => {
  const html = render({ ...base, execution_kind: "simulated", simulated_calls: 10, simulated_tokens: 5840 });
  assert.match(html, /usage-badge--simulated/);
  assert.match(html, /Simulation/);
  assert.match(html, /simulated token units/);
  assert.match(html, /No model charges/);
  assert.doesNotMatch(html, /Real model calls|\$0/);
});
test("real calls with missing prices do not show a free dollar amount", () => {
  const html = render({ ...base, execution_kind: "real", real_calls: 1, provider_tokens: 300, unpriced_calls: 1 });
  assert.match(html, /Real model calls/);
  assert.match(html, /provider-reported tokens/);
  assert.match(html, /Pricing not configured/);
  assert.doesNotMatch(html, /\$0|Simulation/);
});
test("estimates and simulation stay separate in mixed aggregates", () => {
  const u = combineUsage([
    { usage_summary: { ...base, execution_kind: "simulated", simulated_calls: 1, simulated_tokens: 50 } },
    { usage_summary: { ...base, execution_kind: "real", real_calls: 1, estimated_tokens: 200, priced_cost: .1 } },
  ]);
  assert.equal(u.execution_kind, "mixed");
  const html = render(u);
  assert.match(html, /Mixed activity/); assert.match(html, /200 estimated tokens/); assert.match(html, /50 simulated token units/);
  assert.match(html, /estimated cost/);
});
test("legacy missing metadata and no calls do not masquerade as simulations", () => {
  assert.match(render(undefined), /Usage source unknown/);
  assert.match(render(base), /No model calls recorded/);
  assert.equal(combineUsage([{ tokens_used: 45 }]).unknown_tokens, 45);
});
test("partial pricing is explicit and launch preview distinguishes heuristic from model", () => {
  assert.match(render({ ...base, execution_kind: "real", real_calls: 2, unpriced_calls: 1, priced_cost: 1 }), /pricing incomplete/);
  const notice = provider => renderToStaticMarkup(React.createElement(AttackerModeNotice, { provider }));
  assert.match(notice("heuristic"), /Simulated attacker/);
  assert.match(notice("hosted_openai_compatible"), /Real model attacker/);
  assert.match(notice(undefined), /not yet confirmed/);
});

test("connected-runtime counts are not presented as provider billing counts", () => {
  const html = render({ ...base, execution_kind: "real", real_calls: 1, runtime_tokens: 50, unpriced_calls: 1 });
  assert.match(html, /50 runtime-reported tokens/);
  assert.doesNotMatch(html, /provider-reported tokens/);
});
