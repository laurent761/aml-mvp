import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createViteTestServer } from "./helpers.mjs";
const vite = await createViteTestServer();
const { ExecutionProblem, TargetReadiness } = await vite.ssrLoadModule("/app/target-readiness.tsx");

test("a missing image with no actions is an execution blocker, never a defense result", () => {
  const html = renderToStaticMarkup(React.createElement(ExecutionProblem, {
    error: "docker: No such image: sha256:old", noActions: true, apiBase: "", versionId: "v1", onSelectVersion() {},
  }));
  assert.match(html, /Could not start — no security result/);
  assert.match(html, /No attacks ran/);
  assert.match(html, /Select another version/);
  assert.match(html, /Recorded error details/);
  assert.match(html, /Recovery does not change this historical result/);
  assert.doesNotMatch(html, /successful defense|No forbidden state observed/);
});

test("an interrupted run with actions retains partial evidence without claiming nothing ran", () => {
  const html = renderToStaticMarkup(React.createElement(ExecutionProblem, { error: "timeout", noActions: false, apiBase: "" }));
  assert.match(html, /Execution interrupted/);
  assert.match(html, /Any completed evidence remains available/);
  assert.doesNotMatch(html, /No attacks ran/);
});

test("readiness begins as a pending check, never registered-as-ready", () => {
  const html = renderToStaticMarkup(React.createElement(TargetReadiness, { apiBase: "", versionId: "v1", autoPrepare: true }));
  assert.match(html, /Preparing target/);
  assert.match(html, /aria-busy="true"/);
  assert.doesNotMatch(html, /Ready to start/);
});
