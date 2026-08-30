import type { CdpSessionPort } from "./cdp.js";
import { buildCdeResourceUrl } from "./cde-resources.js";
import type { WorkerConfig } from "./config.js";
import type {
  CdeFetchResult,
  CdeResource,
} from "./contracts.js";

type CdeResourceErrorCode = NonNullable<CdeFetchResult["error_code"]>;

export interface CdePageProbe {
  ready: boolean;
  challenge: boolean;
  captcha: boolean;
  login: boolean;
  invalid: boolean;
  content: string | null;
}

interface FetcherDependencies {
  sleep: (milliseconds: number) => Promise<void>;
  now: () => Date;
  nowMs: () => number;
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolvePromise) => {
    setTimeout(resolvePromise, milliseconds);
  });
}

export function buildCdeProbeExpression(resource: CdeResource): string {
  const common = `
    const text = document.body?.innerText || document.documentElement?.textContent || "";
    const captcha = /验证码|人机验证/.test(text);
    const login = /请登录后访问|登录后继续/.test(text);
    const challenge = /正在进行安全验证|安全验证中|Just a moment/i.test(text) && !captcha;
  `;
  if (resource.kind === "result_page" || resource.kind === "catalog_page") {
    const isCatalog = resource.kind === "catalog_page";
    const queryTerm = JSON.stringify(
      resource.kind === "catalog_page" ? "" : resource.query_term,
    );
    return `(() => {
      ${common}
      const table = document.querySelector("table");
      const totals = text.match(/当前第\\s*(\\d+)\\s*页，共\\s*(\\d+)\\s*页，共\\s*(\\d+)\\s*条记录/);
      const expectedQuery = ${queryTerm};
      const urlQuery = new URL(location.href).searchParams.get("keywords") || "";
      const formQueryMatches = Array.from(document.querySelectorAll('input[name="keywords"]')).some((input) => {
        return (input.value || "").trim() === expectedQuery;
      });
      const queryMatches = ${isCatalog}
        ? urlQuery.trim() === ""
        : urlQuery.trim() === expectedQuery && formQueryMatches;
      const ready = Boolean(table && totals && queryMatches);
      return {
        ready,
        captcha,
        login,
        challenge,
        invalid: Boolean(table && totals && !queryMatches),
        content: ready ? document.documentElement.outerHTML : null,
      };
    })()`;
  }
  if (resource.kind === "rss_page") {
    return `(() => {
      ${common}
      const rss = document.querySelector("rss");
      const items = rss ? Array.from(rss.querySelectorAll("item")) : [];
      const malformed = items.some((item) => {
        const guid = item.querySelector("guid")?.textContent?.trim() || "";
        const link = item.querySelector("link")?.textContent?.trim() || "";
        return !guid.startsWith("CTR") || !link;
      });
      const ready = Boolean(rss && !malformed);
      return {
        ready,
        captcha,
        login,
        challenge,
        invalid: Boolean(rss && malformed),
        content: ready ? rss.outerHTML : null,
      };
    })()`;
  }
  return `(() => {
    ${common}
    const registration = text.match(/CTR\\d{8}/)?.[0] || null;
    const collapse = document.querySelector("#collapseTwo");
    // Some valid CDE records expose an empty applicant-name cell. Treat the
    // stable registration number and detail container as the page contract;
    // field completeness belongs to parsing/quality reporting, not readiness.
    const ready = Boolean(registration && collapse);
    return {
      ready,
      captcha,
      login,
      challenge,
      invalid: false,
      content: ready ? document.documentElement.outerHTML : null,
    };
  })()`;
}

function invalidCode(resource: CdeResource): CdeResourceErrorCode {
  switch (resource.kind) {
    case "catalog_page":
    case "result_page":
      return "cde_result_page_invalid";
    case "rss_page":
      return "cde_rss_invalid";
    case "detail":
      return "cde_detail_invalid";
  }
}

function contentType(resource: CdeResource): CdeFetchResult["content_type"] {
  return resource.kind === "rss_page" ? "application/rss+xml" : "text/html";
}

function resourceIdentity(resource: CdeResource): Pick<
  CdeFetchResult,
  "kind" | "query_term" | "page" | "detail_id"
> {
  if (resource.kind === "detail") {
    return {
      kind: resource.kind,
      query_term: null,
      page: null,
      detail_id: resource.detail_id,
    };
  }
  if (resource.kind === "catalog_page") {
    return {
      kind: resource.kind,
      query_term: null,
      page: resource.page,
      detail_id: null,
    };
  }
  return {
    kind: resource.kind,
    query_term: resource.query_term,
    page: resource.page,
    detail_id: null,
  };
}

export class CdeResourceFetcher {
  private readonly dependencies: FetcherDependencies;

  constructor(
    private readonly config: WorkerConfig,
    dependencies: Partial<FetcherDependencies> = {},
  ) {
    this.dependencies = {
      sleep,
      now: () => new Date(),
      nowMs: () => Date.now(),
      ...dependencies,
    };
  }

  async fetch(
    resource: CdeResource,
    session: CdpSessionPort,
  ): Promise<CdeFetchResult> {
    const sourceUrl = buildCdeResourceUrl(resource);
    try {
      await session.navigate(
        sourceUrl,
        this.config.navigation_timeout_seconds * 1_000,
      );
    } catch (error) {
      return this.failure(
        resource,
        session,
        sourceUrl,
        "browser_session_interrupted",
        error instanceof Error ? error.message : "Browser navigation failed",
      );
    }

    const deadline =
      this.dependencies.nowMs() + this.config.challenge_timeout_seconds * 1_000;
    let sawChallenge = false;
    while (this.dependencies.nowMs() <= deadline) {
      let state: CdePageProbe;
      try {
        state = await session.evaluate<CdePageProbe>(buildCdeProbeExpression(resource));
      } catch (error) {
        return this.failure(
          resource,
          session,
          sourceUrl,
          "browser_session_interrupted",
          error instanceof Error ? error.message : "Browser evaluation failed",
        );
      }
      sawChallenge ||= state.challenge;
      if (state.captcha) {
        return this.failure(
          resource,
          session,
          sourceUrl,
          "captcha_detected",
          "CDE displayed a CAPTCHA or human verification page",
        );
      }
      if (state.login) {
        return this.failure(
          resource,
          session,
          sourceUrl,
          "cde_login_required",
          "CDE requires a login before the resource can be accessed",
        );
      }
      if (state.invalid) {
        return this.failure(
          resource,
          session,
          sourceUrl,
          invalidCode(resource),
          "CDE returned a resource that failed its readiness contract",
        );
      }
      if (state.ready && state.content !== null) {
        if (Buffer.byteLength(state.content, "utf8") > this.config.max_response_bytes) {
          return this.failure(
            resource,
            session,
            sourceUrl,
            invalidCode(resource),
            "CDE resource exceeds the configured response-size limit",
          );
        }
        return {
          ...resourceIdentity(resource),
          status: "succeeded",
          source_url: sourceUrl,
          content_type: contentType(resource),
          content: state.content,
          retrieved_at: this.dependencies.now().toISOString(),
          challenge_completed: true,
          error_code: null,
          error_message: null,
          diagnostic_screenshot_base64: null,
          diagnostic_content_type: null,
        };
      }
      await this.dependencies.sleep(100);
    }

    return this.failure(
      resource,
      session,
      sourceUrl,
      sawChallenge ? "challenge_timeout" : invalidCode(resource),
      sawChallenge
        ? "CDE JavaScript challenge did not complete before the timeout"
        : "CDE resource did not satisfy its readiness contract before the timeout",
    );
  }

  private async failure(
    resource: CdeResource,
    session: CdpSessionPort,
    sourceUrl: string,
    errorCode: CdeResourceErrorCode,
    errorMessage: string,
  ): Promise<CdeFetchResult> {
    let screenshot: string | null = null;
    let screenshotType: "image/png" | null = null;
    if (this.config.capture_failure_screenshots) {
      try {
        const content = await session.captureScreenshot();
        if (content.byteLength <= this.config.max_screenshot_bytes) {
          screenshot = content.toString("base64");
          screenshotType = "image/png";
        }
      } catch {
        screenshot = null;
        screenshotType = null;
      }
    }
    return {
      ...resourceIdentity(resource),
      status: "failed",
      source_url: sourceUrl,
      content_type: null,
      content: null,
      retrieved_at: this.dependencies.now().toISOString(),
      challenge_completed: false,
      error_code: errorCode,
      error_message: errorMessage,
      diagnostic_screenshot_base64: screenshot,
      diagnostic_content_type: screenshotType,
    };
  }
}
