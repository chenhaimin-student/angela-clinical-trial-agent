import Fastify, { type FastifyInstance } from "fastify";

import {
  CdeFetchRequestSchema,
  CdeFetchResponseSchema,
  WorkerHealthSchema,
  type CdeFetchRequest,
  type CdeFetchResponse,
  type WorkerHealth,
} from "./contracts.js";
import { WorkerBusyError } from "./service.js";

export interface CdeFetchServicePort {
  fetch(request: CdeFetchRequest): Promise<CdeFetchResponse>;
  shutdown(): Promise<void> | void;
}

interface ServerDependencies {
  service: CdeFetchServicePort;
  getHealth: () => WorkerHealth | Promise<WorkerHealth>;
}

export function buildServer(dependencies: ServerDependencies): FastifyInstance {
  const server = Fastify({ logger: false });

  server.get("/health", async (_request, reply) => {
    const health = WorkerHealthSchema.parse(await dependencies.getHealth());
    return reply.send(health);
  });

  server.post("/v1/cde/fetch", async (request, reply) => {
    const parsed = CdeFetchRequestSchema.safeParse(request.body);
    if (!parsed.success) {
      return reply.code(400).send({
        code: "invalid_request",
        message: "CDE fetch request failed validation",
      });
    }
    try {
      const response = CdeFetchResponseSchema.parse(
        await dependencies.service.fetch(parsed.data),
      );
      return reply.send(response);
    } catch (error) {
      if (error instanceof WorkerBusyError) {
        return reply.code(error.statusCode).send({
          code: error.code,
          message: error.message,
        });
      }
      throw error;
    }
  });

  server.addHook("onClose", async () => {
    await dependencies.service.shutdown();
  });

  return server;
}
