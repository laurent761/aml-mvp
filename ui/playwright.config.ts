import { defineConfig, devices } from "@playwright/test";

const uiPort = Number(process.env.AML_E2E_UI_PORT ?? 4173);
const apiPort = Number(process.env.AML_E2E_API_PORT ?? 8173);

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: [
    ["list"],
    ["html", { outputFolder: "playwright-report", open: "never" }],
    ["junit", { outputFile: "test-results/browser-junit.xml" }],
  ],
  use: {
    baseURL: `http://127.0.0.1:${uiPort}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "firefox", use: { ...devices["Desktop Firefox"] } },
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
    { name: "mobile-chromium", use: { ...devices["Pixel 7"] } },
  ],
  webServer: [
    {
      command: `uv run --project ../backend python e2e/server.py --port ${apiPort}`,
      url: `http://127.0.0.1:${apiPort}/readyz`,
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: `npm run start -- --hostname 127.0.0.1 --port ${uiPort}`,
      url: `http://127.0.0.1:${uiPort}`,
      env: { BACKEND_API_URL: `http://127.0.0.1:${apiPort}` },
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
