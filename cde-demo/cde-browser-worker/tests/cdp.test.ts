import { describe, expect, it, vi } from "vitest";

import { CriCdpFactory, type CriBackend } from "../src/cdp.js";

function backendFixture(): {
  backend: CriBackend;
  client: {
    Page: {
      enable: ReturnType<typeof vi.fn>;
      navigate: ReturnType<typeof vi.fn>;
      loadEventFired: ReturnType<typeof vi.fn>;
      captureScreenshot: ReturnType<typeof vi.fn>;
    };
    Runtime: {
      enable: ReturnType<typeof vi.fn>;
      evaluate: ReturnType<typeof vi.fn>;
    };
    Network: { enable: ReturnType<typeof vi.fn> };
    close: ReturnType<typeof vi.fn>;
  };
} {
  const client = {
    Page: {
      enable: vi.fn().mockResolvedValue(undefined),
      navigate: vi.fn().mockResolvedValue({}),
      loadEventFired: vi.fn().mockResolvedValue(undefined),
      captureScreenshot: vi
        .fn()
        .mockResolvedValue({ data: Buffer.from("png").toString("base64") }),
    },
    Runtime: {
      enable: vi.fn().mockResolvedValue(undefined),
      evaluate: vi.fn().mockResolvedValue({ result: { value: { ready: true } } }),
    },
    Network: { enable: vi.fn().mockResolvedValue(undefined) },
    close: vi.fn().mockResolvedValue(undefined),
  };
  return {
    client,
    backend: {
      newTarget: vi.fn().mockResolvedValue({ id: "target-1" }),
      closeTarget: vi.fn().mockResolvedValue(undefined),
      connect: vi.fn().mockResolvedValue(client),
    },
  };
}

describe("CriCdpFactory", () => {
  it("creates a fresh target and enables the narrow domains", async () => {
    const { backend, client } = backendFixture();
    const factory = new CriCdpFactory(9223, backend);

    const session = await factory.createSession("job-1");

    expect(session.sessionId).toMatch(/^cde-session-/);
    expect(backend.newTarget).toHaveBeenCalledWith(9223, "about:blank");
    expect(client.Page.enable).toHaveBeenCalledOnce();
    expect(client.Runtime.enable).toHaveBeenCalledOnce();
    expect(client.Network.enable).toHaveBeenCalledWith({});
  });

  it("navigates, evaluates by value and captures PNG bytes", async () => {
    const { backend, client } = backendFixture();
    const session = await new CriCdpFactory(9223, backend).createSession("job-1");

    await session.navigate("https://example.com", 100);
    const value = await session.evaluate<{ ready: boolean }>("({ ready: true })");
    const screenshot = await session.captureScreenshot();

    expect(client.Page.navigate).toHaveBeenCalledWith({ url: "https://example.com" });
    expect(client.Runtime.evaluate).toHaveBeenCalledWith({
      expression: "({ ready: true })",
      returnByValue: true,
      awaitPromise: true,
    });
    expect(value).toEqual({ ready: true });
    expect(screenshot.toString()).toBe("png");
  });

  it("closes only its own client and target", async () => {
    const { backend, client } = backendFixture();
    const session = await new CriCdpFactory(9223, backend).createSession("job-1");

    await session.close();

    expect(client.close).toHaveBeenCalledOnce();
    expect(backend.closeTarget).toHaveBeenCalledWith(9223, "target-1");
  });
});
