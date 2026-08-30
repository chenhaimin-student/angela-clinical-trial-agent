import type { ChildProcess } from "node:child_process";
import { spawn } from "node:child_process";
import { resolve } from "node:path";

import type { WorkerConfig } from "./config.js";

export type BrowserRuntimeErrorCode =
  | "chrome_start_failed"
  | "browser_session_interrupted";

export class BrowserRuntimeError extends Error {
  constructor(
    public readonly code: BrowserRuntimeErrorCode,
    message: string,
  ) {
    super(message);
    this.name = "BrowserRuntimeError";
  }
}

export interface ChromeProcess {
  exitCode: number | null;
  kill(signal?: NodeJS.Signals): boolean;
}

export interface ChromeVersionResponse {
  Browser: string;
}

export interface ChromeHealth {
  chromeVersion: string;
  profileGeneration: string;
}

export interface ChromeControllerDependencies {
  spawnChrome: (
    executable: string,
    args: string[],
  ) => ChromeProcess;
  probeVersion: (port: number) => Promise<ChromeVersionResponse>;
  sleep: (milliseconds: number) => Promise<void>;
}

async function probeVersion(port: number): Promise<ChromeVersionResponse> {
  const response = await fetch(`http://127.0.0.1:${port}/json/version`);
  if (!response.ok) {
    throw new Error(`CDP version endpoint returned HTTP ${response.status}`);
  }
  return (await response.json()) as ChromeVersionResponse;
}

function spawnChrome(executable: string, args: string[]): ChildProcess {
  return spawn(executable, args, {
    detached: false,
    stdio: "ignore",
  });
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolvePromise) => {
    setTimeout(resolvePromise, milliseconds);
  });
}

function normalizeChromeVersion(value: string): string {
  return value.replace(/^(?:Google )?Chrome\//, "");
}

export class ChromeController {
  private ownedProcess: ChromeProcess | null = null;

  private readonly dependencies: ChromeControllerDependencies;

  constructor(
    private readonly config: WorkerConfig,
    dependencies: Partial<ChromeControllerDependencies> = {},
  ) {
    this.dependencies = {
      spawnChrome,
      probeVersion,
      sleep,
      ...dependencies,
    };
  }

  async ensureReady(): Promise<ChromeHealth> {
    const existing = await this.tryProbe();
    if (existing !== null) {
      return this.health(existing);
    }

    const args = [
      `--remote-debugging-port=${this.config.remote_debugging_port}`,
      "--remote-debugging-address=127.0.0.1",
      `--user-data-dir=${resolve(this.config.profile_dir)}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--new-window",
      "about:blank",
    ];
    this.ownedProcess = this.dependencies.spawnChrome(
      this.config.chrome_executable,
      args,
    );

    const deadline =
      Date.now() + this.config.navigation_timeout_seconds * 1_000;
    while (Date.now() <= deadline) {
      if (this.ownedProcess.exitCode !== null) {
        throw new BrowserRuntimeError(
          "chrome_start_failed",
          "Chrome exited before the CDP endpoint became ready",
        );
      }
      const version = await this.tryProbe();
      if (version !== null) {
        return this.health(version);
      }
      await this.dependencies.sleep(100);
    }

    throw new BrowserRuntimeError(
      "chrome_start_failed",
      "Chrome did not expose a CDP endpoint before the startup timeout",
    );
  }

  async stop(): Promise<void> {
    if (this.ownedProcess === null) {
      return;
    }
    if (this.ownedProcess.exitCode === null) {
      this.ownedProcess.kill("SIGTERM");
    }
    this.ownedProcess = null;
  }

  async currentHealth(): Promise<ChromeHealth | null> {
    const version = await this.tryProbe();
    return version === null ? null : this.health(version);
  }

  private async tryProbe(): Promise<ChromeVersionResponse | null> {
    try {
      return await this.dependencies.probeVersion(
        this.config.remote_debugging_port,
      );
    } catch {
      return null;
    }
  }

  private health(version: ChromeVersionResponse): ChromeHealth {
    return {
      chromeVersion: normalizeChromeVersion(version.Browser),
      profileGeneration: this.config.profile_generation,
    };
  }
}
