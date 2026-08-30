import { describe, expect, it, vi } from "vitest";

import type { CdpSessionPort } from "../src/cdp.js";
import type { WorkerConfig } from "../src/config.js";
import type {
  CdeFetchRequest,
  CdeFetchResult,
  CdeResource,
} from "../src/contracts.js";
import {
  BrowserWorkerService,
  WorkerBusyError,
} from "../src/service.js";

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
  navigation_timeout_seconds: 60,
  inter_request_delay_seconds: 0,
  session_idle_timeout_seconds: 300,
  detail_cache_ttl_seconds: 600,
  max_queued_requests: 1,
  max_resources_per_request: 10,
  max_pages: 100,
  max_response_bytes: 10_485_760,
  capture_failure_screenshots: false,
  max_screenshot_bytes: 2_097_152,
};

const RESOURCE: CdeResource = {
  kind: "result_page",
  query_term: "示例申办方",
  page: 1,
};

function request(jobId = "job-1", requestId = "request-1"): CdeFetchRequest {
  return { job_id: jobId, request_id: requestId, resources: [RESOURCE] };
}

function success(resource: CdeResource): CdeFetchResult {
  return {
    kind: resource.kind,
    status: "succeeded",
    query_term: resource.kind === "detail" ? null : resource.query_term,
    page: resource.kind === "detail" ? null : resource.page,
    detail_id: resource.kind === "detail" ? resource.detail_id : null,
    source_url: "https://www.chinadrugtrials.org.cn/example",
    content_type: "text/html",
    content: "<html></html>",
    retrieved_at: "2026-07-19T17:00:00.000Z",
    challenge_completed: true,
    error_code: null,
    error_message: null,
    diagnostic_screenshot_base64: null,
    diagnostic_content_type: null,
  };
}

function session(id: string): CdpSessionPort {
  return {
    sessionId: id,
    navigate: vi.fn().mockResolvedValue(undefined),
    evaluate: vi.fn(),
    captureScreenshot: vi.fn(),
    close: vi.fn().mockResolvedValue(undefined),
  };
}

describe("BrowserWorkerService", () => {
  it("queues one request while another request is active", async () => {
    const browserSession = session("session-1");
    let release: (() => void) | undefined;
    let calls = 0;
    const fetchResource = vi.fn(() => {
      calls += 1;
      if (calls > 1) {
        return Promise.resolve(success(RESOURCE));
      }
      return new Promise<CdeFetchResult>((resolve) => {
        release = () => resolve(success(RESOURCE));
      });
    });
    const service = new BrowserWorkerService(CONFIG, {
      cdpFactory: { createSession: vi.fn().mockResolvedValue(browserSession) },
      fetchResource,
      chromeVersion: "150.0.7871.128",
      delay: vi.fn().mockResolvedValue(undefined),
    });

    const first = service.fetch(request());
    await vi.waitFor(() => expect(fetchResource).toHaveBeenCalledOnce());

    const second = service.fetch(request("job-2", "request-2"));
    expect(fetchResource).toHaveBeenCalledOnce();
    release?.();
    await first;
    await second;
    expect(fetchResource).toHaveBeenCalledTimes(2);
  });

  it("rejects requests beyond the small queue capacity", async () => {
    const browserSession = session("session-1");
    let release: (() => void) | undefined;
    let calls = 0;
    const fetchResource = vi.fn(() => {
      calls += 1;
      if (calls > 1) {
        return Promise.resolve(success(RESOURCE));
      }
      return new Promise<CdeFetchResult>((resolve) => {
        release = () => resolve(success(RESOURCE));
      });
    });
    const service = new BrowserWorkerService(CONFIG, {
      cdpFactory: { createSession: vi.fn().mockResolvedValue(browserSession) },
      fetchResource,
      chromeVersion: "150.0.7871.128",
      delay: vi.fn().mockResolvedValue(undefined),
    });

    const first = service.fetch(request());
    await vi.waitFor(() => expect(fetchResource).toHaveBeenCalledOnce());
    const queued = service.fetch(request("job-2", "request-2"));
    await expect(service.fetch(request("job-3", "request-3"))).rejects.toEqual(
      new WorkerBusyError(),
    );
    release?.();
    await first;
    await queued;
  });

  it("reuses one browser session across requests for the same job", async () => {
    const browserSession = session("session-1");
    const createSession = vi.fn().mockResolvedValue(browserSession);
    const service = new BrowserWorkerService(CONFIG, {
      cdpFactory: { createSession },
      fetchResource: vi.fn(async (resource) => success(resource)),
      chromeVersion: "150.0.7871.128",
      delay: vi.fn().mockResolvedValue(undefined),
    });

    const first = await service.fetch(request("job-1", "request-1"));
    const second = await service.fetch(request("job-1", "request-2"));

    expect(createSession).toHaveBeenCalledOnce();
    expect(first.browser_session_id).toBe("session-1");
    expect(second.browser_session_id).toBe("session-1");
    expect(browserSession.close).not.toHaveBeenCalled();
  });

  it("closes the leased session when a different job starts", async () => {
    const firstSession = session("session-1");
    const secondSession = session("session-2");
    const createSession = vi
      .fn()
      .mockResolvedValueOnce(firstSession)
      .mockResolvedValueOnce(secondSession);
    const service = new BrowserWorkerService(CONFIG, {
      cdpFactory: { createSession },
      fetchResource: vi.fn(async (resource) => success(resource)),
      chromeVersion: "150.0.7871.128",
      delay: vi.fn().mockResolvedValue(undefined),
    });

    await service.fetch(request("job-1", "request-1"));
    await service.fetch(request("job-2", "request-2"));

    expect(firstSession.close).toHaveBeenCalledOnce();
    expect(createSession).toHaveBeenCalledTimes(2);
  });

  it("closes an idle leased session after the configured timeout", async () => {
    vi.useFakeTimers();
    const browserSession = session("session-1");
    const service = new BrowserWorkerService(CONFIG, {
      cdpFactory: { createSession: vi.fn().mockResolvedValue(browserSession) },
      fetchResource: vi.fn(async (resource) => success(resource)),
      chromeVersion: "150.0.7871.128",
      delay: vi.fn().mockResolvedValue(undefined),
    });

    await service.fetch(request());
    await vi.advanceTimersByTimeAsync(300_000);

    expect(browserSession.close).toHaveBeenCalledOnce();
    vi.useRealTimers();
  });

  it("reuses a successful public detail response within the configured TTL", async () => {
    const browserSession = session("session-1");
    const detail: CdeResource = {
      kind: "detail",
      detail_id: "8f9910139a404b9aad6812850049d012",
    };
    const fetchResource = vi.fn(async (resource) => success(resource));
    const service = new BrowserWorkerService(CONFIG, {
      cdpFactory: { createSession: vi.fn().mockResolvedValue(browserSession) },
      fetchResource,
      chromeVersion: "150.0.7871.128",
      delay: vi.fn().mockResolvedValue(undefined),
      nowMs: () => 1_000,
    });

    await service.fetch({ job_id: "job-1", request_id: "request-1", resources: [detail] });
    await service.fetch({ job_id: "job-2", request_id: "request-2", resources: [detail] });

    expect(fetchResource).toHaveBeenCalledOnce();
  });

  it("uses at most two independent sessions for a two-lane detail batch", async () => {
    const firstSession = session("session-1");
    const secondSession = session("session-2");
    const createSession = vi
      .fn()
      .mockResolvedValueOnce(firstSession)
      .mockResolvedValueOnce(secondSession);
    let active = 0;
    let peakActive = 0;
    const fetchResource = vi.fn(async (resource: CdeResource) => {
      active += 1;
      peakActive = Math.max(peakActive, active);
      await new Promise<void>((resolve) => setTimeout(resolve, 10));
      active -= 1;
      return success(resource);
    });
    const detailResources: CdeResource[] = ["a", "b", "c", "d"].map((character) => ({
      kind: "detail",
      detail_id: character.repeat(32),
    }));
    const service = new BrowserWorkerService(
      { ...CONFIG, detail_concurrency: 2, detail_launch_stagger_seconds: 0 },
      {
        cdpFactory: { createSession },
        fetchResource,
        chromeVersion: "150.0.7871.128",
        delay: vi.fn().mockResolvedValue(undefined),
      },
    );

    const response = await service.fetch({
      job_id: "detail-job",
      request_id: "detail-request",
      resources: detailResources,
    });

    expect(createSession).toHaveBeenCalledTimes(2);
    expect(peakActive).toBe(2);
    expect(response.results.map((result) => result.detail_id)).toEqual(
      detailResources.map((resource) => resource.detail_id),
    );
  });

  it("keeps detail pages sequential when the configured lane count is one", async () => {
    const browserSession = session("session-1");
    let active = 0;
    let peakActive = 0;
    const fetchResource = vi.fn(async (resource: CdeResource) => {
      active += 1;
      peakActive = Math.max(peakActive, active);
      await new Promise<void>((resolve) => setTimeout(resolve, 5));
      active -= 1;
      return success(resource);
    });
    const service = new BrowserWorkerService(CONFIG, {
      cdpFactory: { createSession: vi.fn().mockResolvedValue(browserSession) },
      fetchResource,
      chromeVersion: "150.0.7871.128",
      delay: vi.fn().mockResolvedValue(undefined),
    });

    await service.fetch({
      job_id: "detail-job",
      request_id: "detail-request",
      resources: [
        { kind: "detail", detail_id: "a".repeat(32) },
        { kind: "detail", detail_id: "b".repeat(32) },
      ],
    });

    expect(peakActive).toBe(1);
  });
});
