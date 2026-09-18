import AxeBuilder from "@axe-core/playwright";
import { test, expect, navigate } from "./fixtures";

test("every workspace route supports navigation, deep links, and reload", async ({ healthyPage: page }) => {
  const routes = [
    ["Targets", "targets"], ["Attack Campaigns", "campaigns"],
    ["Experiments", "experiments"], ["Live Attack Lab", "lab"],
    ["Trajectories", "trajectories"], ["Verified Exploits", "findings"],
    ["Learning", "learning"], ["Evidence", "evidence"], ["System", "system"],
    ["Quickstart tour", "tour"], ["Ask AML", "guide"],
    ["Adversarial Overview", "overview"],
  ];
  for (const [label, route] of routes) {
    await navigate(page, label);
    await expect(page).toHaveURL(new RegExp(`#/${route}$`));
    await expect(page.locator('[aria-current="page"]')).toHaveText(label);
  }
  await page.goto("/#/targets");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Targets");
  await page.reload();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Targets");
  await page.goto("/#/unknown-route");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Adversarial Overview");
});

test("browser history restores views and navigation clears the previous filter", async ({ healthyPage: page }) => {
  await navigate(page, "Targets");
  await page.getByRole("textbox", { name: "Filter current view" }).fill("no-such-target");
  await navigate(page, "Experiments");
  await expect(page.getByRole("textbox", { name: "Filter current view" })).toHaveValue("");
  await page.goBack();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Targets");
  await page.goForward();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Experiments");
});

test("keyboard skip link focuses the current workspace without changing views", async ({ page }) => {
  await page.goto("/#/targets");
  await expect(page.getByText("API ready", { exact: true })).toHaveCount(1);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Targets");
  // Test Enter activation independently of each browser's tab-to-links preference.
  await page.getByRole("link", { name: "Skip to content" }).focus();
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("main")).toBeFocused();
  await expect(page).toHaveURL(/#\/targets$/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Targets");
});

test("overview and registry fit the viewport without horizontal page overflow", async ({ healthyPage: page }) => {
  for (const view of ["Adversarial Overview", "Targets"]) {
    await navigate(page, view);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
  }
});

test("registering a target persists through the proxy and reload, then supports inspection and filtering", async ({ healthyPage: page }, testInfo) => {
  const name = `Browser target ${testInfo.project.name} ${testInfo.testId}`;
  await navigate(page, "Targets");
  await page.getByRole("button", { name: "Register target", exact: true }).first().click();
  const dialog = page.getByRole("dialog", { name: "Register target" });
  await dialog.getByLabel("Display name").fill("   ");
  await expect(dialog.getByRole("button", { name: "Register target", exact: true })).toBeDisabled();
  await dialog.getByLabel("Display name").fill(name);
  const saved = page.waitForResponse(response => response.url().endsWith("/v1/targets") && response.request().method() === "POST");
  await dialog.getByRole("button", { name: "Register target", exact: true }).click();
  expect((await saved).status()).toBe(201);
  await expect(dialog).not.toBeVisible();
  await page.reload();
  await page.getByRole("textbox", { name: "Filter current view" }).fill(name);
  const row = page.getByRole("row").filter({ hasText: name });
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("No version");
  await row.getByRole("button").click();
  const inspector = page.getByRole("dialog", { name });
  await expect(inspector.locator("pre")).toContainText(`"name": "${name}"`);
  await page.keyboard.press("Escape");
  await expect(inspector).not.toBeVisible();
  await page.getByRole("textbox", { name: "Filter current view" }).fill("no-target-matches-this-query");
  await expect(row).toHaveCount(0);
});

test("cancel and Escape close target dialogs without creating records", async ({ healthyPage: page }) => {
  await navigate(page, "Targets");
  const mutations: string[] = [];
  page.on("request", request => { if (request.method() === "POST") mutations.push(request.url()); });
  const trigger = page.getByRole("button", { name: "Register target", exact: true }).first();
  await trigger.click();
  await page.getByLabel("Display name").fill("Must not be saved");
  await page.getByRole("dialog").getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page.getByRole("dialog")).not.toBeVisible();
  await trigger.click();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).not.toBeVisible();
  expect(mutations).toEqual([]);
});

test("untrusted target names stay text when rendered and inspected", async ({ healthyPage: page, request }, testInfo) => {
  const name = `<img src=x onerror="window.__amlXss=true"> ${testInfo.project.name}`;
  const created = await request.post("/v1/targets", { data: { name } });
  expect(created.status()).toBe(201);
  await page.reload();
  await navigate(page, "Targets");
  await page.getByRole("textbox", { name: "Filter current view" }).fill(name);
  await page.getByRole("button", { name, exact: false }).click();
  await expect(page.getByRole("dialog").locator("pre")).toContainText("onerror");
  expect(await page.evaluate(() => "__amlXss" in window)).toBe(false);
  await expect(page.locator('img[src="x"]')).toHaveCount(0);
});

test("malformed manifest is rejected without a request and the editor can recover", async ({ healthyPage: page }) => {
  await navigate(page, "Targets");
  await page.getByRole("button", { name: "Add version", exact: true }).first().click();
  const dialog = page.getByRole("dialog", { name: "Add immutable target version" });
  const editor = dialog.getByLabel("TargetManifest JSON");
  const original = await editor.inputValue();
  const mutations: string[] = [];
  page.on("request", request => { if (request.method() === "POST") mutations.push(request.url()); });
  await editor.fill("{ invalid json");
  await dialog.getByRole("button", { name: "Add version", exact: true }).click();
  await expect(page.locator("[data-sonner-toast]")).toBeVisible();
  await expect(dialog).toBeVisible();
  expect(mutations).toEqual([]);
  await editor.fill(original);
  await expect(dialog.getByRole("button", { name: "Add version", exact: true })).toBeEnabled();
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
});

test("overview and target registration meet WCAG A/AA accessibility checks", async ({ healthyPage: page }) => {
  for (const dialog of [false, true]) {
    if (dialog) {
      await navigate(page, "Targets");
      await page.getByRole("button", { name: "Register target", exact: true }).first().click();
      const content = page.getByRole("dialog", { name: "Register target" });
      await expect(content).toBeVisible();
      await content.evaluate(element => Promise.all(element.getAnimations({ subtree: true }).map(animation => animation.finished)));
    }
    const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
    expect(results.violations.map(({ id, nodes }) => ({ id, targets: nodes.map(node => node.target) }))).toEqual([]);
  }
});
