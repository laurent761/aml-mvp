import { after } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

export const root = fileURLToPath(new URL("..", import.meta.url));

export async function createViteTestServer() {
  const server = await createServer({
    appType: "custom",
    configFile: false,
    root,
    resolve: { alias: { "@": root } },
    server: { middlewareMode: true },
  });
  after(() => server.close());
  return server;
}
