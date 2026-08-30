import type { CdpSessionPort } from "./cdp.js";
import type { WorkerConfig } from "./config.js";
import type {
  CdeFetchRequest,
  CdeFetchResponse,
  CdeFetchResult,
  CdeResource,
} from "./contracts.js";

interface CdpFactoryPort {
  createSession(jobId: string): Promise<CdpSessionPort>;
}

interface BrowserWorkerServiceDependencies {
  cdpFactory: CdpFactoryPort;
  fetchResource: (
    resource: CdeResource,
    session: CdpSessionPort,
  ) => Promise<CdeFetchResult>;
  chromeVersion: string;
  delay?: (milliseconds: number) => Promise<void>;
  nowMs?: () => number;
}

interface ResolvedBrowserWorkerServiceDependencies
  extends Omit<BrowserWorkerServiceDependencies, "delay" | "nowMs"> {
  delay: (milliseconds: number) => Promise<void>;
  nowMs: () => number;
}

interface SessionLease {
  jobId: string;
  sessions: CdpSessionPort[];
}

interface PendingRequest {
  request: CdeFetchRequest;
  resolve: (response: CdeFetchResponse) => void;
  reject: (error: Error) => void;
}

interface CachedDetail {
  expiresAt: number;
  result: CdeFetchResult;
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolvePromise) => {
    setTimeout(resolvePromise, milliseconds);
  });
}

export class WorkerBusyError extends Error {
  readonly code = "browser_worker_busy";
  readonly statusCode = 409;

  constructor() {
    super("The CDE Browser Worker is already processing another request");
    this.name = "WorkerBusyError";
  }
}

export class BrowserWorkerService {
  private activeRequest: string | null = null;
  private readonly pendingRequests: PendingRequest[] = [];
  private readonly detailCache = new Map<string, CachedDetail>();
  private nextDetailLaunchAt = 0;
  private lease: SessionLease | null = null;
  private idleTimer: NodeJS.Timeout | null = null;
  private readonly dependencies: ResolvedBrowserWorkerServiceDependencies;

  constructor(
    private readonly config: WorkerConfig,
    dependencies: BrowserWorkerServiceDependencies,
  ) {
    this.dependencies = {
      ...dependencies,
      delay: dependencies.delay ?? delay,
      nowMs: dependencies.nowMs ?? Date.now,
    };
  }

  get activeRequestId(): string | null {
    return this.activeRequest;
  }

  async fetch(request: CdeFetchRequest): Promise<CdeFetchResponse> {
    if (this.activeRequest === null) {
      return this.fetchExclusive(request);
    }
    if (this.pendingRequests.length >= this.config.max_queued_requests) {
      throw new WorkerBusyError();
    }
    return new Promise<CdeFetchResponse>((resolve, reject) => {
      this.pendingRequests.push({ request, resolve, reject });
    });
  }

  private async fetchExclusive(request: CdeFetchRequest): Promise<CdeFetchResponse> {
    this.activeRequest = request.request_id;
    this.clearIdleTimer();
    try {
      const session = await this.sessionFor(request.job_id);
      const results =
        this.config.detail_concurrency > 1 &&
        request.resources.every((resource) => resource.kind === "detail")
          ? await this.fetchDetailResources(request.job_id, request.resources)
          : await this.fetchSequentialResources(request.resources, session);
      return {
        request_id: request.request_id,
        browser_session_id: session.sessionId,
        chrome_version: this.dependencies.chromeVersion,
        profile_generation: this.config.profile_generation,
        results,
      };
    } finally {
      this.activeRequest = null;
      this.scheduleIdleClose();
      this.startNextQueuedRequest();
    }
  }

  async shutdown(): Promise<void> {
    this.clearIdleTimer();
    while (this.pendingRequests.length > 0) {
      this.pendingRequests.shift()?.reject(
        new Error("CDE Browser Worker is shutting down"),
      );
    }
    await this.closeLease();
  }

  private startNextQueuedRequest(): void {
    const next = this.pendingRequests.shift();
    if (next === undefined) {
      return;
    }
    void this.fetchExclusive(next.request).then(next.resolve, next.reject);
  }

  private cachedDetail(resource: CdeResource): CdeFetchResult | null {
    if (resource.kind !== "detail") {
      return null;
    }
    const cached = this.detailCache.get(resource.detail_id);
    if (cached === undefined) {
      return null;
    }
    if (cached.expiresAt <= this.dependencies.nowMs()) {
      this.detailCache.delete(resource.detail_id);
      return null;
    }
    return { ...cached.result };
  }

  private cacheDetail(resource: CdeResource, result: CdeFetchResult): void {
    if (resource.kind !== "detail" || result.status !== "succeeded") {
      return;
    }
    this.detailCache.set(resource.detail_id, {
      expiresAt: this.dependencies.nowMs() + this.config.detail_cache_ttl_seconds * 1_000,
      result: { ...result },
    });
  }

  private async fetchSequentialResources(
    resources: CdeResource[],
    session: CdpSessionPort,
  ): Promise<CdeFetchResult[]> {
    const results: CdeFetchResult[] = [];
    let networkFetchCount = 0;
    for (const resource of resources) {
      const cached = this.cachedDetail(resource);
      if (cached !== null) {
        results.push(cached);
        continue;
      }
      if (networkFetchCount > 0 && this.config.inter_request_delay_seconds > 0) {
        await this.dependencies.delay(this.config.inter_request_delay_seconds * 1_000);
      }
      const result = await this.dependencies.fetchResource(resource, session);
      networkFetchCount += 1;
      results.push(result);
      this.cacheDetail(resource, result);
      if (result.error_code === "browser_session_interrupted") {
        await this.closeLease();
        break;
      }
    }
    return results;
  }

  private async fetchDetailResources(
    jobId: string,
    resources: CdeResource[],
  ): Promise<CdeFetchResult[]> {
    const sessions = await this.detailSessionsFor(jobId, this.config.detail_concurrency);
    const results: Array<CdeFetchResult | undefined> = new Array(resources.length);
    let nextIndex = 0;
    let interrupted = false;

    const runLane = async (session: CdpSessionPort): Promise<void> => {
      while (!interrupted) {
        const index = nextIndex;
        nextIndex += 1;
        if (index >= resources.length) {
          return;
        }
        const resource = resources[index];
        if (resource === undefined) {
          return;
        }
        const cached = this.cachedDetail(resource);
        if (cached !== null) {
          results[index] = cached;
          continue;
        }
        await this.waitForDetailLaunchSlot();
        const result = await this.dependencies.fetchResource(resource, session);
        results[index] = result;
        this.cacheDetail(resource, result);
        if (result.error_code === "browser_session_interrupted") {
          interrupted = true;
          await this.closeLease();
        }
      }
    };

    await Promise.all(sessions.map((session) => runLane(session)));
    return results.filter((result): result is CdeFetchResult => result !== undefined);
  }

  private async waitForDetailLaunchSlot(): Promise<void> {
    const now = this.dependencies.nowMs();
    const scheduled = Math.max(now, this.nextDetailLaunchAt);
    this.nextDetailLaunchAt =
      scheduled + this.config.detail_launch_stagger_seconds * 1_000;
    if (scheduled > now) {
      await this.dependencies.delay(scheduled - now);
    }
  }

  private async sessionFor(jobId: string): Promise<CdpSessionPort> {
    if (this.lease !== null && this.lease.jobId !== jobId) {
      await this.closeLease();
    }
    if (this.lease === null) {
      this.lease = {
        jobId,
        sessions: [await this.dependencies.cdpFactory.createSession(jobId)],
      };
    }
    return this.lease.sessions[0] as CdpSessionPort;
  }

  private async detailSessionsFor(jobId: string, count: number): Promise<CdpSessionPort[]> {
    await this.sessionFor(jobId);
    const lease = this.lease;
    if (lease === null) {
      throw new Error("CDE Browser Worker session could not be created");
    }
    while (lease.sessions.length < count) {
      lease.sessions.push(await this.dependencies.cdpFactory.createSession(jobId));
    }
    return lease.sessions.slice(0, count);
  }

  private scheduleIdleClose(): void {
    if (this.lease === null) {
      return;
    }
    this.clearIdleTimer();
    this.idleTimer = setTimeout(() => {
      void this.closeLease();
    }, this.config.session_idle_timeout_seconds * 1_000);
  }

  private clearIdleTimer(): void {
    if (this.idleTimer !== null) {
      clearTimeout(this.idleTimer);
      this.idleTimer = null;
    }
  }

  private async closeLease(): Promise<void> {
    const lease = this.lease;
    this.lease = null;
    this.clearIdleTimer();
    if (lease !== null) {
      await Promise.all(lease.sessions.map((session) => session.close()));
    }
  }
}
