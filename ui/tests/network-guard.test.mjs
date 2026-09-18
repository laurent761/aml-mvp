import assert from "node:assert/strict";
import test from "node:test";
import { createViteTestServer } from "./helpers.mjs";

const vite = await createViteTestServer();
const { NetworkGuard } = await vite.ssrLoadModule("/e2e/network-guard.ts");

for (const status of [400, 401, 404, 422, 429, 500, 503]) {
  test(`browser guard rejects an undeclared HTTP ${status} even without a JavaScript exception`, () => {
    const guard = new NetworkGuard();
    guard.recordHttp({ method: "GET", path: "/v1/targets", status });
    assert.deepEqual(guard.violations(), [`Unexpected HTTP ${status}: GET /v1/targets`]);
  });
}

test("a declared failure is bounded and stays visible with its reason", () => {
  const guard = new NetworkGuard();
  const failure = { method: "POST", path: "/v1/targets", status: 503 };
  guard.expectHttp({ ...failure, reason: "Injected outage; recovery is asserted" });
  guard.recordHttp(failure);
  assert.deepEqual(guard.violations(), []);
  assert.deepEqual(guard.report().responses, [{ ...failure, expected: true, reason: "Injected outage; recovery is asserted" }]);
  guard.recordHttp(failure);
  assert.deepEqual(guard.violations(), ["Unexpected HTTP 503: POST /v1/targets"]);
});

test("an expected outage cannot excuse another method, endpoint or status", () => {
  const guard = new NetworkGuard();
  guard.expectHttp({ method: "GET", path: "/v1/targets", status: 503, reason: "Injected outage" });
  for (const failure of [
    { method: "POST", path: "/v1/targets", status: 503 },
    { method: "GET", path: "/v1/campaigns", status: 503 },
    { method: "GET", path: "/v1/targets", status: 500 },
  ]) guard.recordHttp(failure);
  assert.equal(guard.violations().length, 4); // Three unrelated errors plus missing intended outage.
  assert.equal(guard.report().responses.every(item => !item.expected), true);
});

test("a negative test fails if its declared error never happens", () => {
  const guard = new NetworkGuard();
  guard.expectHttp({ method: "POST", path: "/v1/attack-tasks", status: 422, reason: "Invalid budget" });
  guard.recordHttp({ method: "POST", path: "/v1/attack-tasks", status: 201 });
  assert.match(guard.violations()[0], /occurred 0 times; expected 1–1/);
});

test("declaring an expectation later cannot hide an already observed failure", () => {
  const guard = new NetworkGuard();
  const failure = { method: "GET", path: "/readyz", status: 503 };
  guard.recordHttp(failure);
  guard.expectHttp({ ...failure, reason: "Too late" });
  assert.equal(guard.violations().length, 2);
  assert.equal(guard.report().responses[0].expected, false);
});

test("bounded polling allowances must be met and cannot grow without limit", () => {
  const guard = new NetworkGuard();
  const failure = { method: "GET", path: "/v1/research-health", status: 401 };
  guard.expectHttp({ ...failure, min: 1, max: 2, reason: "Invalid token while polling" });
  guard.recordHttp(failure);
  guard.recordHttp(failure);
  assert.deepEqual(guard.violations(), []);
  guard.recordHttp(failure);
  assert.equal(guard.violations().length, 1);
});

test("transport failures and browser exceptions fail while client cancellations remain separately reported", () => {
  const guard = new NetworkGuard();
  for (const error of ["net::ERR_ABORTED", "NS_BINDING_ABORTED", "cancelled"]) {
    guard.recordTransport({ method: "GET", path: "/v1/targets", error });
  }
  assert.deepEqual(guard.violations(), []);
  assert.equal(guard.report().cancellations.length, 3);
  for (const error of ["net::ERR_CONNECTION_REFUSED", "net::ERR_FAILED", "Timeout"]) {
    guard.recordTransport({ method: "GET", path: "/v1/targets", error });
  }
  guard.pageErrors.push("Cannot read properties of undefined");
  assert.equal(guard.violations().length, 4);
});

test("expectations require reasons and finite nonnegative occurrence bounds", () => {
  for (const options of [{ reason: "" }, { reason: "outage", min: -1 }, { reason: "outage", max: Infinity }, { reason: "outage", min: 2, max: 1 }]) {
    const guard = new NetworkGuard();
    assert.throws(() => guard.expectHttp({ method: "GET", path: "/readyz", status: 503, ...options }));
  }
});
