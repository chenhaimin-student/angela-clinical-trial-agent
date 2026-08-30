import { CriCdpFactory } from "./cdp.js";
import { CdeResourceFetcher } from "./cde-fetcher.js";
import { ChromeController } from "./chrome.js";
import { loadWorkerConfig } from "./config.js";
import { buildServer } from "./server.js";
import { BrowserWorkerService } from "./service.js";

async function main(): Promise<void> {
  const configPath = process.env.ANGELA_BROWSER_WORKER_CONFIG?.trim();
  if (!configPath) {
    throw new Error("ANGELA_BROWSER_WORKER_CONFIG is required");
  }
  const config = await loadWorkerConfig(configPath);
  if (!config.enabled) {
    throw new Error("CDE Browser Worker is disabled by configuration");
  }

  const chrome = new ChromeController(config);
  const chromeHealth = await chrome.ensureReady();
  const fetcher = new CdeResourceFetcher(config);
  const service = new BrowserWorkerService(config, {
    cdpFactory: new CriCdpFactory(config.remote_debugging_port),
    fetchResource: (resource, session) => fetcher.fetch(resource, session),
    chromeVersion: chromeHealth.chromeVersion,
    delay: (milliseconds) =>
      new Promise((resolvePromise) => {
        setTimeout(resolvePromise, milliseconds);
      }),
  });
  const server = buildServer({
    service,
    getHealth: async () => {
      const current = await chrome.currentHealth();
      return {
        status: current === null ? "degraded" : "ready",
        browser_ready: current !== null,
        chrome_version: current?.chromeVersion ?? null,
        profile_generation: chromeHealth.profileGeneration,
        active_request_id: service.activeRequestId,
      };
    },
  });

  const shutdown = async (): Promise<void> => {
    await server.close();
    await chrome.stop();
  };
  process.once("SIGINT", () => void shutdown());
  process.once("SIGTERM", () => void shutdown());

  await server.listen({ host: config.bind_host, port: config.bind_port });
}

main().catch((error: unknown) => {
  const message = error instanceof Error ? error.message : String(error);
  process.stderr.write(`${message}\n`);
  process.exitCode = 1;
});
