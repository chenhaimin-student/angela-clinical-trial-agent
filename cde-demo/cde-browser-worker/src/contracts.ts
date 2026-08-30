import { z } from "zod";

const ResultPageResourceSchema = z
  .object({
    kind: z.literal("result_page"),
    query_term: z.string().trim().min(1).max(256),
    page: z.number().int().min(1).max(100),
  })
  .strict();

const CatalogPageResourceSchema = z
  .object({
    kind: z.literal("catalog_page"),
    page: z.number().int().min(1).max(10_000),
  })
  .strict();

const RssPageResourceSchema = z
  .object({
    kind: z.literal("rss_page"),
    query_term: z.string().trim().min(1).max(256),
    page: z.number().int().min(1).max(100),
  })
  .strict();

const DetailResourceSchema = z
  .object({
    kind: z.literal("detail"),
    detail_id: z.string().regex(/^(?:[a-f0-9]{32}(?:_p)?|[0-9]{1,20}_p)$/i),
  })
  .strict();

export const CdeResourceSchema = z.discriminatedUnion("kind", [
  CatalogPageResourceSchema,
  ResultPageResourceSchema,
  RssPageResourceSchema,
  DetailResourceSchema,
]);

export const CdeFetchRequestSchema = z
  .object({
    request_id: z.string().min(1).max(256),
    job_id: z.string().min(1).max(256),
    resources: z.array(CdeResourceSchema).min(1).max(10),
  })
  .strict();

export const CdeResourceErrorCodeSchema = z.enum([
  "challenge_timeout",
  "captcha_detected",
  "cde_login_required",
  "cde_result_page_invalid",
  "cde_rss_invalid",
  "cde_detail_invalid",
  "browser_session_interrupted",
]);

export const CdeFetchResultSchema = z
  .object({
    kind: z.enum(["catalog_page", "result_page", "rss_page", "detail"]),
    status: z.enum(["succeeded", "failed"]),
    query_term: z.string().min(1).max(256).nullable(),
    page: z.number().int().min(1).max(10_000).nullable(),
    detail_id: z.string().regex(/^(?:[a-f0-9]{32}(?:_p)?|[0-9]{1,20}_p)$/i).nullable(),
    source_url: z.string().url(),
    content_type: z.enum(["text/html", "application/rss+xml"]).nullable(),
    content: z.string().nullable(),
    retrieved_at: z.string().datetime({ offset: true }),
    challenge_completed: z.boolean(),
    error_code: CdeResourceErrorCodeSchema.nullable(),
    error_message: z.string().max(2_000).nullable(),
    diagnostic_screenshot_base64: z.string().base64().nullable(),
    diagnostic_content_type: z.literal("image/png").nullable(),
  })
  .strict();

export const CdeFetchResponseSchema = z
  .object({
    request_id: z.string().min(1).max(256),
    browser_session_id: z.string().min(1).max(256),
    chrome_version: z.string().min(1).max(256),
    profile_generation: z.string().min(1).max(256),
    results: z.array(CdeFetchResultSchema).max(10),
  })
  .strict();

export const WorkerHealthSchema = z
  .object({
    status: z.enum(["ready", "degraded"]),
    browser_ready: z.boolean(),
    chrome_version: z.string().min(1).max(256).nullable(),
    profile_generation: z.string().min(1).max(256),
    active_request_id: z.string().min(1).max(256).nullable(),
  })
  .strict();

export type CdeResource = z.infer<typeof CdeResourceSchema>;
export type CdeFetchRequest = z.infer<typeof CdeFetchRequestSchema>;
export type CdeFetchResult = z.infer<typeof CdeFetchResultSchema>;
export type CdeFetchResponse = z.infer<typeof CdeFetchResponseSchema>;
export type WorkerHealth = z.infer<typeof WorkerHealthSchema>;
