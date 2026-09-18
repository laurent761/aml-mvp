import { test, expect, navigate } from "./fixtures";

test("an unavailable API has an actionable error and refresh recovers", async ({ page, apiFailures }) => {
  apiFailures.expectHttp({ method: "GET", path: "/readyz", status: 503, reason: "Deliberately injected maintenance outage; refresh must recover" });
  await page.route("**/readyz", route => route.fulfill({ status: 503, json: { detail: "Maintenance fixture" } }));
  await page.goto("/");
  await expect(page.getByText("API offline", { exact: true }).first()).toHaveCount(1);
  await expect(page.getByRole("alert")).toContainText("Maintenance fixture");
  await page.unroute("**/readyz");
  await page.getByRole("button", { name: "Refresh data", exact: true }).click();
  await expect(page.getByText("API ready", { exact: true })).toHaveCount(1);
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("a partial refresh keeps prior records and exposes the failed data source", async ({ healthyPage: page, apiFailures }) => {
  await navigate(page, "Targets");
  await expect(page.getByRole("table").first()).toBeVisible();
  const before = await page.getByRole("row").count();
  expect(before).toBeGreaterThan(1);
  apiFailures.expectHttp({ method: "GET", path: "/v1/targets", status: 503, reason: "Deliberately fail one refresh; prior records must survive" });
  await page.route("**/v1/targets?*", route => route.fulfill({ status: 503, json: { detail: "Target store unavailable" } }));
  await page.getByRole("button", { name: "Refresh data", exact: true }).click();
  await expect(page.getByText("1 secondary data source could not be refreshed.")).toBeVisible();
  await expect(page.getByText("API ready", { exact: true })).toHaveCount(1);
  await expect(page.getByRole("row")).toHaveCount(before);
  await page.unroute("**/v1/targets?*");
  await page.getByRole("button", { name: "Refresh data", exact: true }).click();
  await expect(page.getByText("1 secondary data source could not be refreshed.")).toHaveCount(0);
});

test("a failed target submission preserves input and a retry creates exactly one record", async ({ healthyPage: page, request, apiFailures }, testInfo) => {
  const name = `Retry target ${testInfo.project.name}`;
  await navigate(page, "Targets");
  await page.getByRole("button", { name: "Register target", exact: true }).first().click();
  const dialog = page.getByRole("dialog", { name: "Register target" });
  await dialog.getByLabel("Display name").fill(name);
  apiFailures.expectHttp({ method: "POST", path: "/v1/targets", status: 503, reason: "Deliberately fail registration once; corrected retry must persist exactly one record" });
  await page.route("**/v1/targets", route => route.request().method() === "POST"
    ? route.fulfill({ status: 503, json: { detail: "Registration temporarily unavailable" } })
    : route.continue());
  await dialog.getByRole("button", { name: "Register target", exact: true }).click();
  await expect(page.getByText("Registration temporarily unavailable", { exact: true })).toBeVisible();
  await expect(dialog.getByLabel("Display name")).toHaveValue(name);
  await expect(dialog.getByRole("button", { name: "Register target", exact: true })).toBeEnabled();
  await page.unroute("**/v1/targets");
  await dialog.getByRole("button", { name: "Register target", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  const rows = await (await request.get("/v1/targets?limit=500")).json();
  expect(rows.filter((row: { name: string }) => row.name === name)).toHaveLength(1);
});

test("invalid credentials can be cleared to reconnect without persisting the secret", async ({ healthyPage: page, apiFailures }) => {
  await navigate(page, "System");
  const openConnection = async () => {
    if (!await page.getByRole("button", { name: "Connection settings", exact: true }).isVisible()) {
      await page.getByText("Advanced settings", { exact: true }).click();
    }
    await page.getByRole("button", { name: "Connection settings", exact: true }).click();
    return page.getByRole("dialog", { name: "Control API connection" });
  };
  let dialog = await openConnection();
  await dialog.getByLabel("Research access token").fill("invalid-test-token");
  for (const resource of ["overview", "targets", "target-versions", "attack-tasks", "campaigns", "findings", "red-experiment-configs", "strategies", "artifacts", "operational-events", "episodes"]) {
    apiFailures.expectHttp({ method: "GET", path: `/v1/${resource}`, status: 401, reason: "Deliberately invalid token; each workspace request must reject it once" });
  }
  // Connection changes remount the research panel. Its initial request can be
  // cancelled or repeat once as the workspace finishes refreshing.
  for (const resource of ["research-catalog", "research-health"]) {
    apiFailures.expectHttp({ method: "GET", path: `/v1/${resource}`, status: 401, min: 0, max: resource === "research-health" ? 2 : 1, reason: "Background view request during the invalid-token scenario, including connection remount" });
  }
  await dialog.getByRole("button", { name: "Save and reconnect" }).click();
  await expect(page.getByText(/secondary data sources? could not be refreshed/)).toBeVisible();
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain("invalid-test-token");
  dialog = await openConnection();
  await dialog.getByLabel("Research access token").fill("");
  const recoveredHealth = page.waitForResponse(response => new URL(response.url()).pathname === "/v1/research-health" && response.status() === 200);
  await dialog.getByRole("button", { name: "Save and reconnect" }).click();
  expect((await recoveredHealth).ok()).toBe(true);
  await expect(page.getByText(/secondary data sources? could not be refreshed/)).toHaveCount(0);
  await expect(page.getByText("API ready", { exact: true })).toHaveCount(1);
  const storage = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
  expect(storage).not.toContain("invalid-test-token");
  await page.reload();
  await expect(page.getByText("API ready", { exact: true })).toHaveCount(1);
});

test("registration disables duplicate submission while a real API response is pending", async ({ healthyPage: page, request }, testInfo) => {
  const name = `Pending target ${testInfo.project.name}`;
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  let submissions = 0;
  await page.route("**/v1/targets", async route => {
    if (route.request().method() === "POST") {
      submissions += 1;
      await pending;
    }
    await route.continue();
  });
  try {
    await navigate(page, "Targets");
    await page.getByRole("button", { name: "Register target", exact: true }).first().click();
    const dialog = page.getByRole("dialog", { name: "Register target" });
    await dialog.getByLabel("Display name").fill(name);
    await dialog.getByRole("button", { name: "Register target", exact: true }).dblclick();
    await expect(dialog.getByRole("button", { name: "Registering", exact: true })).toBeDisabled();
    await expect.poll(() => submissions).toBe(1);
    release();
    await expect(dialog).not.toBeVisible();
    const response = await request.get("/v1/targets?limit=500");
    expect(response.ok()).toBe(true);
    expect((await response.json()).filter((row: { name: string }) => row.name === name)).toHaveLength(1);
  } finally {
    release();
    await page.unrouteAll({ behavior: "wait" });
  }
});
