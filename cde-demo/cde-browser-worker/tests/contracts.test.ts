import { describe, expect, it } from "vitest";

import {
  CdeFetchRequestSchema,
  CdeFetchResponseSchema,
} from "../src/contracts.js";

describe("CDE worker contracts", () => {
  it("accepts only bounded typed resources", () => {
    const parsed = CdeFetchRequestSchema.parse({
      request_id: "cde-2-job:rss-1",
      job_id: "cde-2-job",
      resources: [{ kind: "rss_page", query_term: "示例申办方", page: 1 }],
    });

    expect(parsed.resources).toHaveLength(1);
    expect(() =>
      CdeFetchRequestSchema.parse({
        request_id: "x",
        job_id: "x",
        resources: [{ kind: "url", url: "https://example.com" }],
      }),
    ).toThrow();
  });

  it("accepts catalog pages beyond the old 100-page test bound", () => {
    const parsed = CdeFetchRequestSchema.parse({
      request_id: "catalog-101",
      job_id: "catalog",
      resources: [{ kind: "catalog_page", page: 101 }],
    });
    expect(parsed.resources[0]).toEqual({ kind: "catalog_page", page: 101 });
  });

  it("accepts the official optional _p detail-id suffix", () => {
    const parsed = CdeFetchRequestSchema.parse({
      request_id: "detail-p",
      job_id: "detail",
      resources: [
        { kind: "detail", detail_id: "2457661b7c934cbbb45a6bfe91a97cfc_p" },
      ],
    });
    expect(parsed.resources[0]).toEqual({
      kind: "detail",
      detail_id: "2457661b7c934cbbb45a6bfe91a97cfc_p",
    });
  });

  it("accepts the official legacy numeric detail-id format", () => {
    const parsed = CdeFetchRequestSchema.parse({
      request_id: "legacy-detail",
      job_id: "detail",
      resources: [{ kind: "detail", detail_id: "2128_p" }],
    });
    expect(parsed.resources[0]).toEqual({ kind: "detail", detail_id: "2128_p" });
  });

  it("forbids more than ten resources", () => {
    expect(() =>
      CdeFetchRequestSchema.parse({
        request_id: "x",
        job_id: "x",
        resources: Array.from({ length: 11 }, (_, index) => ({
          kind: "rss_page",
          query_term: "示例申办方",
          page: index + 1,
        })),
      }),
    ).toThrow();
  });

  it("validates a successful response without arbitrary metadata", () => {
    const parsed = CdeFetchResponseSchema.parse({
      request_id: "x",
      browser_session_id: "session-1",
      chrome_version: "150.0.7871.128",
      profile_generation: "profile-v1",
      results: [
        {
          kind: "result_page",
          status: "succeeded",
          query_term: "示例申办方",
          page: 1,
          detail_id: null,
          source_url:
            "https://www.chinadrugtrials.org.cn/clinicaltrials.searchlist.dhtml?keywords=x&currentpage=1",
          content_type: "text/html",
          content: "<html></html>",
          retrieved_at: "2026-07-19T17:00:00+08:00",
          challenge_completed: true,
          error_code: null,
          error_message: null,
          diagnostic_screenshot_base64: null,
          diagnostic_content_type: null,
        },
      ],
    });

    expect(parsed.results[0]?.challenge_completed).toBe(true);
  });

  it("rejects arbitrary response fields", () => {
    expect(() =>
      CdeFetchResponseSchema.parse({
        request_id: "x",
        browser_session_id: "session-1",
        chrome_version: "150.0.7871.128",
        profile_generation: "profile-v1",
        cookie: "secret",
        results: [],
      }),
    ).toThrow();
  });
});
