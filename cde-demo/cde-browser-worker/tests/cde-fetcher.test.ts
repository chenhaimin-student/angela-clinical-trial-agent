import { readFileSync } from "node:fs";
import { join } from "node:path";
import { runInNewContext } from "node:vm";

import { describe, expect, it, vi } from "vitest";

import type { CdpSessionPort } from "../src/cdp.js";
import type { WorkerConfig } from "../src/config.js";
import {
  buildCdeProbeExpression,
  CdeResourceFetcher,
  type CdePageProbe,
} from "../src/cde-fetcher.js";
import type { CdeResource } from "../src/contracts.js";

const FICTIONAL_QUERY = "示例申办方";
const RESULT_HTML = `<html><body><table><tr><td>${FICTIONAL_QUERY}</td></tr></table></body></html>`;
const DETAIL_HTML = `<html><body><div>CTR20260001</div><div id="collapseTwo">虚构公开试验详情</div></body></html>`;
const RSS_XML = readFileSync(
  join(import.meta.dirname, "fixtures", "cde-rss-page-1.xml"),
  "utf8",
);

const CONFIG: WorkerConfig = {
  enabled: true,
  bind_host: "127.0.0.1",
  bind_port: 8765,
  chrome_executable: "/path/to/chrome",
  remote_debugging_port: 9223,
  profile_dir: ".runtime/chrome-profile",
  profile_generation: "profile-v1",
  headless: false,
  challenge_timeout_seconds: 0.002,
  navigation_timeout_seconds: 1,
  inter_request_delay_seconds: 0,
  session_idle_timeout_seconds: 300,
  max_resources_per_request: 10,
  max_pages: 100,
  max_response_bytes: 10_485_760,
  capture_failure_screenshots: false,
  max_screenshot_bytes: 2_097_152,
};

const RESULT_RESOURCE: CdeResource = {
  kind: "result_page",
  query_term: FICTIONAL_QUERY,
  page: 1,
};

function probe(overrides: Partial<CdePageProbe> = {}): CdePageProbe {
  return {
    ready: false,
    challenge: false,
    captcha: false,
    login: false,
    invalid: false,
    content: null,
    ...overrides,
  };
}

function fakeSession(states: CdePageProbe[]): CdpSessionPort {
  let index = 0;
  return {
    sessionId: "session-1",
    navigate: vi.fn().mockResolvedValue(undefined),
    evaluate: vi.fn(async () => {
      const value = states[Math.min(index, states.length - 1)];
      index += 1;
      return value;
    }),
    captureScreenshot: vi.fn().mockResolvedValue(Buffer.from("png")),
    close: vi.fn().mockResolvedValue(undefined),
  };
}

describe("CdeResourceFetcher", () => {
  it("accepts an empty-condition catalog page", () => {
    const expression = buildCdeProbeExpression({ kind: "catalog_page", page: 1 });
    const page = runInNewContext(expression, {
      URL,
      location: {
        href: "https://www.chinadrugtrials.org.cn/clinicaltrials.searchlist.dhtml?currentpage=1",
      },
      document: {
        body: { innerText: "当前第 1 页，共 500 页，共 9999 条记录" },
        documentElement: {
          outerHTML: RESULT_HTML,
          textContent: "当前第 1 页，共 500 页，共 9999 条记录",
        },
        querySelector: (selector: string) => (selector === "table" ? {} : null),
        querySelectorAll: () => [{ value: "" }],
      },
    }) as CdePageProbe;
    expect(page).toMatchObject({ ready: true, invalid: false });
  });

  it("identifies a result page from its URL and populated query form input", () => {
    const expression = buildCdeProbeExpression(RESULT_RESOURCE);
    const page = runInNewContext(expression, {
      URL,
      location: {
        href: `https://www.chinadrugtrials.org.cn/clinicaltrials.searchlist.dhtml?keywords=${encodeURIComponent(FICTIONAL_QUERY)}&currentpage=1`,
      },
      document: {
        body: { innerText: "当前第 1 页，共 36 页，共 718 条记录" },
        documentElement: {
          outerHTML: RESULT_HTML,
          textContent: "当前第 1 页，共 36 页，共 718 条记录",
        },
        querySelector: (selector: string) => (selector === "table" ? {} : null),
        querySelectorAll: (selector: string) =>
          selector === 'input[name="keywords"]'
            ? [{ value: "" }, { value: FICTIONAL_QUERY }]
            : [],
      },
    }) as CdePageProbe;

    expect(page).toMatchObject({ ready: true, invalid: false, content: RESULT_HTML });
  });

  it("waits through the challenge until the result table and totals appear", async () => {
    let evaluationCount = 0;
    const session = fakeSession([]);
    session.evaluate = vi.fn(async () => {
      evaluationCount += 1;
      if (evaluationCount === 1) {
        await new Promise((resolvePromise) => setTimeout(resolvePromise, 5));
        return probe({ challenge: true });
      }
      return probe({ ready: true, content: RESULT_HTML });
    });
    const fetcher = new CdeResourceFetcher(
      { ...CONFIG, challenge_timeout_seconds: 0.001 },
      { sleep: vi.fn().mockResolvedValue(undefined), nowMs: () => 0 },
    );

    const result = await fetcher.fetch(RESULT_RESOURCE, session);

    expect(result.status).toBe("succeeded");
    expect(result.challenge_completed).toBe(true);
    expect(result.content).toContain(FICTIONAL_QUERY);
    expect(session.evaluate).toHaveBeenCalledWith(
      expect.stringContaining(JSON.stringify(FICTIONAL_QUERY)),
    );
  });

  it("returns challenge_timeout without source content", async () => {
    const result = await new CdeResourceFetcher(CONFIG, {
      sleep: vi.fn().mockResolvedValue(undefined),
    }).fetch(RESULT_RESOURCE, fakeSession([probe({ challenge: true })]));

    expect(result).toMatchObject({
      status: "failed",
      error_code: "challenge_timeout",
      content: null,
    });
  });

  it("classifies captcha before returning page content", async () => {
    const result = await new CdeResourceFetcher(CONFIG).fetch(
      RESULT_RESOURCE,
      fakeSession([probe({ captcha: true, content: "captcha" })]),
    );

    expect(result).toMatchObject({
      status: "failed",
      error_code: "captcha_detected",
      content: null,
    });
  });

  it("returns complete RSS XML", async () => {
    const result = await new CdeResourceFetcher(CONFIG).fetch(
      { kind: "rss_page", query_term: FICTIONAL_QUERY, page: 1 },
      fakeSession([probe({ ready: true, content: RSS_XML })]),
    );

    expect(result.status).toBe("succeeded");
    expect(result.content_type).toBe("application/rss+xml");
    expect(result.content).toContain("CTR20262582");
  });

  it("returns complete synthetic detail HTML", async () => {
    const result = await new CdeResourceFetcher(CONFIG).fetch(
      { kind: "detail", detail_id: "00000000000000000000000000000001" },
      fakeSession([probe({ ready: true, content: DETAIL_HTML })]),
    );

    expect(result.status).toBe("succeeded");
    expect(result.content).toContain('id="collapseTwo"');
    expect(result.content).toContain("虚构公开试验详情");
  });

  it("accepts a valid detail page whose applicant name is empty", () => {
    const expression = buildCdeProbeExpression({
      kind: "detail",
      detail_id: "00000000000000000000000000000002",
    });
    const page = runInNewContext(expression, {
      document: {
        body: { innerText: "登记号 CTR20260002 申请人名称" },
        documentElement: {
          outerHTML: '<html><div id="collapseTwo"></div></html>',
          textContent: "登记号 CTR20260002 申请人名称",
        },
        querySelector: (selector: string) => (selector === "#collapseTwo" ? {} : null),
      },
    }) as CdePageProbe;

    expect(page).toMatchObject({ ready: true, invalid: false });
  });

  it("rejects oversized content without truncating it", async () => {
    const result = await new CdeResourceFetcher({
      ...CONFIG,
      max_response_bytes: 4,
    }).fetch(RESULT_RESOURCE, fakeSession([probe({ ready: true, content: RESULT_HTML })]));

    expect(result).toMatchObject({
      status: "failed",
      error_code: "cde_result_page_invalid",
      content: null,
    });
  });

  it("attaches one bounded PNG diagnostic without replacing the source error", async () => {
    const result = await new CdeResourceFetcher({
      ...CONFIG,
      capture_failure_screenshots: true,
    }).fetch(RESULT_RESOURCE, fakeSession([probe({ captcha: true })]));

    expect(result.error_code).toBe("captcha_detected");
    expect(result.diagnostic_content_type).toBe("image/png");
    expect(result.diagnostic_screenshot_base64).toBe(
      Buffer.from("png").toString("base64"),
    );
  });
});
