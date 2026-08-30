import { readFile } from "node:fs/promises";
import { isAbsolute } from "node:path";

import YAML from "yaml";
import { z } from "zod";

export const WorkerConfigSchema = z
  .object({
    enabled: z.boolean(),
    bind_host: z.literal("127.0.0.1"),
    bind_port: z.number().int().min(1).max(65_535),
    chrome_executable: z
      .string()
      .min(1)
      .refine(isAbsolute, "chrome_executable must be an absolute path"),
    remote_debugging_port: z.number().int().min(1).max(65_535),
    profile_dir: z.string().min(1),
    profile_generation: z.string().min(1).max(256),
    headless: z.literal(false),
    challenge_timeout_seconds: z.number().positive(),
    navigation_timeout_seconds: z.number().positive(),
    inter_request_delay_seconds: z.number().nonnegative(),
    session_idle_timeout_seconds: z.number().positive(),
    detail_cache_ttl_seconds: z.number().int().min(60).max(3_600),
    max_queued_requests: z.number().int().min(0).max(2),
    detail_concurrency: z.number().int().min(1).max(4),
    detail_launch_stagger_seconds: z.number().nonnegative().max(5),
    max_resources_per_request: z.number().int().min(1).max(10),
    max_pages: z.number().int().min(1).max(100),
    max_response_bytes: z.number().int().positive(),
    capture_failure_screenshots: z.boolean(),
    max_screenshot_bytes: z.number().int().positive(),
  })
  .strict();

export type WorkerConfig = z.infer<typeof WorkerConfigSchema>;

export async function loadWorkerConfig(path: string): Promise<WorkerConfig> {
  const content = await readFile(path, "utf8");
  return WorkerConfigSchema.parse(YAML.parse(content));
}
