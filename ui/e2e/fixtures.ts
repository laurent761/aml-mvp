import { test as base, expect, type Page } from "@playwright/test";
import { NetworkGuard } from "./network-guard";

export const test = base.extend<{ healthyPage: Page; apiFailures: NetworkGuard }>({
  apiFailures: [async ({ context }, provide, testInfo) => {
    const guard = new NetworkGuard();
    context.on("response", response => {
      guard.recordHttp({ method: response.request().method(), path: new URL(response.url()).pathname, status: response.status() });
    });
    context.on("requestfailed", request => {
      guard.recordTransport({ method: request.method(), path: new URL(request.url()).pathname, error: request.failure()?.errorText ?? "Unknown transport failure" });
    });
    context.on("weberror", error => guard.pageErrors.push(error.error().message));
    await provide(guard);
    await testInfo.attach("api-audit", { body: JSON.stringify(guard.report(), null, 2), contentType: "application/json" });
    expect(guard.violations(), "Unexpected browser/API failures (see api-audit attachment)").toEqual([]);
  }, { auto: true }],
  healthyPage: async ({ page }, provide) => {
    await page.goto("/");
    await expect(page.getByText("API ready", { exact: true })).toHaveCount(1);
    await expect(page.getByText(/secondary data sources? could not be refreshed/)).toHaveCount(0);
    await provide(page);
  },
});

export { expect };

export async function navigate(page: Page, label: string) {
  const navigation = page.getByRole("complementary", { name: "Primary navigation" });
  const open = page.getByRole("button", { name: "Open navigation", exact: true });
  if (await open.isVisible()) await open.click();
  await navigation.getByRole("button", { name: label, exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: label, exact: true })).toBeVisible();
}
