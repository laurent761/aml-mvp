import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { createViteTestServer, root } from "./helpers.mjs";

const vite = await createViteTestServer();

test("renders the complete operator navigation and containment posture", async () => {
  const { default: ControlPlatform } = await vite.ssrLoadModule(
    "/app/control-platform.tsx",
  );
  const html = renderToStaticMarkup(React.createElement(ControlPlatform));

  for (const label of [
    "Ask AML",
    "Adversarial Overview",
    "Targets",
    "Attack Campaigns",
    "Verified Exploits",
    "Experiments",
    "Live Attack Lab",
    "Trajectories",
    "Learning",
    "Evidence",
    "System",
  ]) {
    assert.match(html, new RegExp(`>${label}<`));
  }
  assert.doesNotMatch(html, />Defense policies<|>Policies<|>Hardening</);
  assert.match(html, /Skip to content/);
  assert.match(html, /Containment boundary/);
  assert.match(html, /Loading platform data/);
});

test("preserves the silver and slate palette and layered page background", async () => {
  const css = await readFile(path.join(root, "app/globals.css"), "utf8");
  for (const color of [
    "#F8F8F6",
    "#F2F4F4",
    "#EAECEE",
    "#FFFFFF",
    "#F3F4F5",
    "#ECECEA",
    "#171718",
    "#111112",
    "#202022",
    "#181819",
    "#646467",
  ]) {
    assert.match(css.toUpperCase(), new RegExp(color.toUpperCase()));
  }
  assert.match(css, /circle at 86% 5%/);
  assert.match(css, /rgba\(49, 107, 139, 0\.13\)/);
  assert.match(css, /circle at 16% 90%/);
  assert.match(css, /rgba\(68, 79, 95, 0\.10\)/);
  assert.match(css, /linear-gradient\(145deg, #F8F8F6 0%, #F2F4F4 50%, #EAECEE 100%\)/i);
});

test("API client normalizes origins and surfaces structured failures", async (t) => {
  const { ApiError, apiRequest, apiUrl, normalizeApiBase } =
    await vite.ssrLoadModule("/lib/api-client.ts");

  assert.equal(normalizeApiBase(" https://api.example/// "), "https://api.example");
  assert.equal(normalizeApiBase("/"), "");
  assert.equal(apiUrl("https://api.example/", "v1/campaigns"), "https://api.example/v1/campaigns");

  t.mock.method(globalThis, "fetch", async () =>
    Response.json({ detail: "budget exceeded" }, { status: 409 }),
  );
  await assert.rejects(
    () => apiRequest("", "/v1/campaigns"),
    (error) => error instanceof ApiError && error.status === 409 && error.detail === "budget exceeded",
  );
});

test("attack workspace cannot fetch or launch defensive workflows", async () => {
  const source = await readFile(path.join(root, "app/control-platform.tsx"), "utf8");
  assert.doesNotMatch(source, /\/v1\/policy-versions|\/v1\/hardening-runs|\$\{finding\.finding_id\}\/hardening/);
  assert.doesNotMatch(source, /function PolicyDialog|function PoliciesView|Defense policies|Harden finding|Benign regression/);
  assert.match(source, /search_nearby_bypasses: nearby, reproduction_only: true/);
});


test("documentation citations render untrusted content as text and merge repeated source buttons", async () => {
  const { CitedParagraph } = await vite.ssrLoadModule("/app/guide-chat.tsx");
  const attack = '<script>alert("untrusted")</script>';
  const html = renderToStaticMarkup(React.createElement(CitedParagraph, {
    paragraph: { text: attack, support: [{ source: 1, quote: attack }, { source: 1, quote: "Another exact quote" }] },
    sources: [{ number: 1, references: [{ heading: "Reset / Lifecycle", path: "guide.html" }] }],
    onSource() {},
  }));
  assert.doesNotMatch(html, /<script>|href=/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /Supporting quotes/);
  assert.equal((html.match(/<button/g) || []).length, 1);
  assert.match(html, /Inspect source 1: Reset \/ Lifecycle/);
});

test("documentation composer waits for backend status before accepting questions", async () => {
  const { default: GuideChat } = await vite.ssrLoadModule("/app/guide-chat.tsx");
  const html = renderToStaticMarkup(React.createElement(GuideChat, { apiBase: "" }));
  assert.match(html, /Connecting to documentation/);
  assert.match(html, /textarea[^>]*disabled/);
  assert.match(html, /Source evidence/);
  assert.doesNotMatch(html, /type="password"|sk-/);
});
