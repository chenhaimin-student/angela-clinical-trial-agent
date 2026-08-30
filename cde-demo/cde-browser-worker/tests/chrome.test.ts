import { EventEmitter } from "node:events";

import { describe, expect, it, vi } from "vitest";

import {
  BrowserRuntimeError,
  ChromeController,
  type ChromeProcess,
} from "../src/chrome.js";
import type { WorkerConfig } from "../src/config.js";

const CONFIG: WorkerConfig = {
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
  navigation_timeout_seconds: 0.05,
  inter_request_delay_seconds: 0,
  session_idle_timeout_seconds: 300,
  max_resources_per_request: 10,
  max_pages: 100,
  max_response_bytes: 10_485_760,
  capture_failure_screenshots: false,
  max_screenshot_bytes: 2_097_152,
};

class FakeProcess extends EventEmitter implements ChromeProcess {
  exitCode: number | null = null;
  kill = vi.fn(() => true);
}

describe("ChromeController", () => {
  it("launches normal Chrome with a dedicated profile and no automation flags", async () => {
    const process = new FakeProcess();
    const spawnChrome = vi.fn(() => process);
    const probeVersion = vi
      .fn()
      .mockRejectedValueOnce(new Error("not ready"))
      .mockResolvedValue({ Browser: "Chrome/150.0.7871.128" });
    const controller = new ChromeController(CONFIG, {
      spawnChrome,
      probeVersion,
      sleep: vi.fn().mockResolvedValue(undefined),
    });

    const health = await controller.ensureReady();

    expect(health.chromeVersion).toBe("150.0.7871.128");
    expect(spawnChrome).toHaveBeenCalledOnce();
    const [executable, args] = spawnChrome.mock.calls[0] ?? [];
    expect(executable).toBe(CONFIG.chrome_executable);
    expect(args).toContain("--remote-debugging-port=9223");
    expect(args).toContain("--remote-debugging-address=127.0.0.1");
    expect(args).toEqual(
      expect.arrayContaining([expect.stringContaining("--user-data-dir=")]),
    );
    expect(args).not.toContain("--headless");
    expect(args).not.toContain("--enable-automation");
  });

  it("reuses an already ready Chrome endpoint", async () => {
    const spawnChrome = vi.fn();
    const controller = new ChromeController(CONFIG, {
      spawnChrome,
      probeVersion: vi
        .fn()
        .mockResolvedValue({ Browser: "Chrome/150.0.7871.128" }),
      sleep: vi.fn().mockResolvedValue(undefined),
    });

    await controller.ensureReady();

    expect(spawnChrome).not.toHaveBeenCalled();
  });

  it("reports chrome_start_failed when the process exits before CDP is ready", async () => {
    const process = new FakeProcess();
    const controller = new ChromeController(CONFIG, {
      spawnChrome: vi.fn(() => {
        process.exitCode = 1;
        return process;
      }),
      probeVersion: vi.fn().mockRejectedValue(new Error("not ready")),
      sleep: vi.fn().mockResolvedValue(undefined),
    });

    await expect(controller.ensureReady()).rejects.toEqual(
      new BrowserRuntimeError(
        "chrome_start_failed",
        "Chrome exited before the CDP endpoint became ready",
      ),
    );
  });

  it("stops only the process it launched", async () => {
    const process = new FakeProcess();
    const controller = new ChromeController(CONFIG, {
      spawnChrome: vi.fn(() => process),
      probeVersion: vi
        .fn()
        .mockRejectedValueOnce(new Error("not ready"))
        .mockResolvedValue({ Browser: "Chrome/150.0.7871.128" }),
      sleep: vi.fn().mockResolvedValue(undefined),
    });
    await controller.ensureReady();

    await controller.stop();

    expect(process.kill).toHaveBeenCalledWith("SIGTERM");
  });

  it("reports unavailable when the CDP endpoint stops responding", async () => {
    const probeVersion = vi
      .fn()
      .mockResolvedValueOnce({ Browser: "Chrome/150.0.7871.128" })
      .mockRejectedValueOnce(new Error("offline"));
    const controller = new ChromeController(CONFIG, {
      spawnChrome: vi.fn(),
      probeVersion,
      sleep: vi.fn().mockResolvedValue(undefined),
    });
    await controller.ensureReady();

    expect(await controller.currentHealth()).toBeNull();
  });
});
