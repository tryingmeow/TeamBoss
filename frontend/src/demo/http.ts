/**
 * Tiny request/response vocabulary for the demo API.
 *
 * Handlers never build `Response` objects themselves: they return a
 * `DemoResponse`, and `index.ts` turns it into a real `Response`. That keeps
 * every handler a plain synchronous function over the in-memory state.
 */
import type { DemoDb } from './db';

export interface DemoRequest {
  method: string;
  path: string;
  query: URLSearchParams;
  body: unknown;
}

export interface DemoContext extends DemoRequest {
  db: DemoDb;
  params: Record<string, string>;
}

export interface DemoResponse {
  status: number;
  body?: unknown;
}

export type DemoHandler = (ctx: DemoContext) => DemoResponse;

export interface DemoRoute {
  method: string;
  /** Path pattern such as `/api/teams/:teamId/members/:userId/seat`. */
  pattern: string;
  handler: DemoHandler;
}

export function ok(body: unknown = { status: 'ok' }): DemoResponse {
  return { status: 200, body };
}

export function noContent(): DemoResponse {
  return { status: 204 };
}

/** FastAPI-style error body: `{ "detail": "..." }` (or a structured detail). */
export function fail(status: number, detail: unknown): DemoResponse {
  return { status, body: { detail } };
}

/** Reads a JSON object body defensively; anything else becomes `{}`. */
export function bodyObject(ctx: DemoContext): Record<string, unknown> {
  const body = ctx.body;
  return body && typeof body === 'object' && !Array.isArray(body) ? (body as Record<string, unknown>) : {};
}

export function bodyString(ctx: DemoContext, key: string): string {
  const value = bodyObject(ctx)[key];
  return typeof value === 'string' ? value : '';
}

export function queryFlag(ctx: DemoContext, key: string): boolean {
  return ctx.query.get(key) === 'true';
}
