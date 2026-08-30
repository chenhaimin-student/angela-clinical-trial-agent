import { describe, expect, it, vi } from "vitest";

import type {
  CdeFetchRequest,
  CdeFetchResponse,
  WorkerHealth,
} from "../src/contracts.js";
import { buildServer } from "../src/server.js";
import { WorkerBusyError } from "../src/service.js";

const HEALTH: WorkerHealth = {
  status: "ready",
  browser_ready: true,
  chrome_version: "150.0.7871.128",
  profile_generation: "profile-v1",
  active_request_id: null,
};

const RESPONSE: CdeFetchResponse = {
  request_id: "request-1",
  browser_session_id: "session-1",
  chrome_version: "150.0.7871.128",
  profile_generation: "profile-v1",
  results: [],
};

const REQUEST: CdeFetchRequest = {
  request_id: "request-1",
  job_id: "job-1",
  resources: [
    { kind: "result_page", query_term: "示例申办方", page: 1 },
  ],
};

describe("Browser Worker server", () => {
  it("returns health without secrets", async () => {
    const server = buildServer({
      service: { fetch: vi.fn(), shutdown: vi.fn() },
      getHealth: vi.fn(() => HEALTH),
    });

    const response = await server.inject({ method: "GET", url: "/health" });

    expect(response.statusCode).toBe(200);
    expect(response.json()).toEqual(HEALTH);
    expect(response.body).not.toContain("cookie");
    expect(response.body).not.toContain("job-1");
  });

  it("rejects invalid or arbitrary fetch payloads", async () => {
    const server = buildServer({
      service: { fetch: vi.fn(), shutdown: vi.fn() },
      getHealth: vi.fn(() => HEALTH),
    });

    const response = await server.inject({
      method: "POST",
      url: "/v1/cde/fetch",
      payload: {
        request_id: "request-1",
        job_id: "job-1",
        resources: [{ kind: "url", url: "https://example.com" }],
      },
    });

    expect(response.statusCode).toBe(400);
  });

  it("returns a typed fetch response", async () => {
    const fetch = vi.fn().mockResolvedValue(RESPONSE);
    const server = buildServer({
      service: { fetch, shutdown: vi.fn() },
      getHealth: vi.fn(() => HEALTH),
    });

    const response = await server.inject({
      method: "POST",
      url: "/v1/cde/fetch",
      payload: REQUEST,
    });

    expect(response.statusCode).toBe(200);
    expect(response.json()).toEqual(RESPONSE);
    expect(fetch).toHaveBeenCalledWith(REQUEST);
  });

  it("maps a busy service to HTTP 409", async () => {
    const server = buildServer({
      service: {
        fetch: vi.fn().mockRejectedValue(new WorkerBusyError()),
        shutdown: vi.fn(),
      },
      getHealth: vi.fn(() => HEALTH),
    });

    const response = await server.inject({
      method: "POST",
      url: "/v1/cde/fetch",
      payload: REQUEST,
    });

    expect(response.statusCode).toBe(409);
    expect(response.json()).toMatchObject({ code: "browser_worker_busy" });
  });

  it("does not expose arbitrary URL routes", async () => {
    const server = buildServer({
      service: { fetch: vi.fn(), shutdown: vi.fn() },
      getHealth: vi.fn(() => HEALTH),
    });

    const response = await server.inject({
      method: "GET",
      url: "/v1/cde/fetch?url=https://example.com",
    });

    expect(response.statusCode).toBe(404);
  });
});
