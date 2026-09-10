// Generate runtime declarations for the inline Vite Cloudflare configuration.
// This temporary configuration is only used by `wrangler types`.
import { mkdir, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
await mkdir(".wrangler", { recursive: true });
await writeFile(".wrangler/typecheck.json", JSON.stringify({
  name: "aml-typecheck", compatibility_date: "2026-09-10",
  compatibility_flags: ["nodejs_compat"],
  d1_databases: [{ binding: "DB", database_name: "typecheck", database_id: "00000000-0000-4000-8000-000000000000" }],
}));
for (const args of [["wrangler", "types", "worker-configuration.d.ts", "--config", ".wrangler/typecheck.json"], ["tsc", "--noEmit"]]) {
  const result = spawnSync("npx", ["--no-install", ...args], { stdio: "inherit" });
  if (result.status !== 0) process.exit(result.status ?? 1);
}
