import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { WorkerConfigSchema, loadWorkerConfig } from "../src/config.js";

const VALID_CONFIG = {
  enabled: true,
  bind_host: "127.0.0.1",
  bind_port: 8765,
  chrome_executable:
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  remote_debugging_port: 9223,
  profile_dir: "data/chrome-profile",
  profile_generation: "profile-v1",
  headless: false,
  challenge_timeout_seconds: 20,
  navigation_timeout_seconds: 60,
  inter_request_delay_seconds: 1.5,
  session_idle_timeout_seconds: 300,
  detail_cache_ttl_seconds: 600,
  max_queued_requests: 1,
  detail_concurrency: 1,
  detail_launch_stagger_seconds: 1.5,
  max_resources_per_request: 10,
  max_pages: 100,
  max_response_bytes: 10_485_760,
  capture_failure_screenshots: false,
  max_screenshot_bytes: 2_097_152,
};

describe("Worker configuration", () => {
  it("accepts the normal Chrome loopback configuration", () => {
    const parsed = WorkerConfigSchema.parse(VALID_CONFIG);
    expect(parsed.headless).toBe(false);
    expect(parsed.bind_host).toBe("127.0.0.1");
  });

  it("rejects headless Chrome", () => {
    expect(() =>
      WorkerConfigSchema.parse({ ...VALID_CONFIG, headless: true }),
    ).toThrow();
  });

  it("rejects non-loopback binding", () => {
    expect(() =>
      WorkerConfigSchema.parse({ ...VALID_CONFIG, bind_host: "0.0.0.0" }),
    ).toThrow();
  });

  it("rejects a relative Chrome executable", () => {
    expect(() =>
      WorkerConfigSchema.parse({
        ...VALID_CONFIG,
        chrome_executable: "Google Chrome",
      }),
    ).toThrow();
  });

  it("loads a strict YAML file", async () => {
    const directory = await mkdtemp(join(tmpdir(), "cde-worker-config-"));
    const path = join(directory, "worker.yaml");
    await writeFile(
      path,
      [
        "enabled: true",
        "bind_host: 127.0.0.1",
        "bind_port: 8765",
        "chrome_executable: /Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "remote_debugging_port: 9223",
        "profile_dir: data/chrome-profile",
        "profile_generation: profile-v1",
        "headless: false",
        "challenge_timeout_seconds: 20",
        "navigation_timeout_seconds: 60",
        "inter_request_delay_seconds: 1.5",
        "session_idle_timeout_seconds: 300",
        "detail_cache_ttl_seconds: 600",
        "max_queued_requests: 1",
        "detail_concurrency: 1",
        "detail_launch_stagger_seconds: 1.5",
        "max_resources_per_request: 10",
        "max_pages: 100",
        "max_response_bytes: 10485760",
        "capture_failure_screenshots: false",
        "max_screenshot_bytes: 2097152",
      ].join("\n"),
      "utf8",
    );

    const loaded = await loadWorkerConfig(path);
    expect(loaded.profile_generation).toBe("profile-v1");
  });
});
