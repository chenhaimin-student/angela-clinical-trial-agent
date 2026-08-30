import { randomUUID } from "node:crypto";

import CDP from "chrome-remote-interface";

export interface CdpSessionPort {
  readonly sessionId: string;
  navigate(url: string, timeoutMs: number): Promise<void>;
  evaluate<T>(expression: string): Promise<T>;
  captureScreenshot(): Promise<Buffer>;
  close(): Promise<void>;
}

interface CriClientPort {
  Page: {
    enable(): Promise<unknown>;
    navigate(params: { url: string }): Promise<unknown>;
    loadEventFired(): Promise<unknown>;
    captureScreenshot(params: { format: "png" }): Promise<{ data: string }>;
  };
  Runtime: {
    enable(): Promise<unknown>;
    evaluate(params: {
      expression: string;
      returnByValue: true;
      awaitPromise: true;
    }): Promise<{
      result: { value?: unknown };
      exceptionDetails?: { text?: string };
    }>;
  };
  Network: {
    enable(params: Record<string, never>): Promise<unknown>;
  };
  close(): Promise<void>;
}

export interface CriBackend {
  newTarget(port: number, url: string): Promise<{ id: string }>;
  closeTarget(port: number, targetId: string): Promise<void>;
  connect(port: number, targetId: string): Promise<CriClientPort>;
}

const DEFAULT_BACKEND: CriBackend = {
  async newTarget(port, url) {
    return CDP.New({ host: "127.0.0.1", port, url });
  },
  async closeTarget(port, targetId) {
    await CDP.Close({ host: "127.0.0.1", port, id: targetId });
  },
  async connect(port, targetId) {
    return CDP({ host: "127.0.0.1", port, target: targetId });
  },
};

function withTimeout<T>(promise: Promise<T>, timeoutMs: number): Promise<T> {
  return new Promise<T>((resolvePromise, rejectPromise) => {
    const timeout = setTimeout(() => {
      rejectPromise(new Error(`CDP operation timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    promise.then(
      (value) => {
        clearTimeout(timeout);
        resolvePromise(value);
      },
      (error: unknown) => {
        clearTimeout(timeout);
        rejectPromise(error);
      },
    );
  });
}

class CriCdpSession implements CdpSessionPort {
  private closed = false;

  constructor(
    public readonly sessionId: string,
    private readonly port: number,
    private readonly targetId: string,
    private readonly client: CriClientPort,
    private readonly backend: CriBackend,
  ) {}

  async navigate(url: string, timeoutMs: number): Promise<void> {
    const loaded = this.client.Page.loadEventFired();
    await this.client.Page.navigate({ url });
    await withTimeout(loaded, timeoutMs);
  }

  async evaluate<T>(expression: string): Promise<T> {
    const response = await this.client.Runtime.evaluate({
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
    if (response.exceptionDetails !== undefined) {
      throw new Error(
        response.exceptionDetails.text ?? "CDP evaluation failed",
      );
    }
    return response.result.value as T;
  }

  async captureScreenshot(): Promise<Buffer> {
    const response = await this.client.Page.captureScreenshot({ format: "png" });
    return Buffer.from(response.data, "base64");
  }

  async close(): Promise<void> {
    if (this.closed) {
      return;
    }
    this.closed = true;
    try {
      await this.client.close();
    } finally {
      await this.backend.closeTarget(this.port, this.targetId);
    }
  }
}

export class CriCdpFactory {
  constructor(
    private readonly port: number,
    private readonly backend: CriBackend = DEFAULT_BACKEND,
  ) {}

  async createSession(_jobId: string): Promise<CdpSessionPort> {
    const target = await this.backend.newTarget(this.port, "about:blank");
    const client = await this.backend.connect(this.port, target.id);
    try {
      await Promise.all([
        client.Page.enable(),
        client.Runtime.enable(),
        client.Network.enable({}),
      ]);
    } catch (error) {
      await client.close();
      await this.backend.closeTarget(this.port, target.id);
      throw error;
    }
    return new CriCdpSession(
      `cde-session-${randomUUID()}`,
      this.port,
      target.id,
      client,
      this.backend,
    );
  }
}
