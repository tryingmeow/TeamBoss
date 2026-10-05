/**
 * Path-pattern matching for the demo routes. Patterns are literal segments
 * plus `:name` params; the first route whose method and pattern both match
 * wins, so literal routes (`/api/teams/sync`) are listed before param routes.
 */
import type { DemoDb } from './db';
import { fail, type DemoRequest, type DemoResponse, type DemoRoute } from './http';
import { accessTokenRoutes } from './routes/accessTokens';
import { financeRoutes } from './routes/finance';
import { patrolTgRoutes } from './routes/patrolTg';
import { systemRoutes } from './routes/system';
import { teamRoutes } from './routes/teams';
import { userRoutes } from './routes/users';

const ROUTES: DemoRoute[] = [
  ...systemRoutes,
  ...teamRoutes,
  ...userRoutes,
  ...financeRoutes,
  ...patrolTgRoutes,
  ...accessTokenRoutes,
];

function matchPattern(pattern: string, path: string): Record<string, string> | null {
  const patternParts = pattern.split('/').filter(Boolean);
  const pathParts = path.split('/').filter(Boolean);
  if (patternParts.length !== pathParts.length) return null;
  const params: Record<string, string> = {};
  for (let i = 0; i < patternParts.length; i += 1) {
    const expected = patternParts[i];
    const actual = pathParts[i];
    if (expected.startsWith(':')) {
      try {
        params[expected.slice(1)] = decodeURIComponent(actual);
      } catch {
        params[expected.slice(1)] = actual;
      }
    } else if (expected !== actual) {
      return null;
    }
  }
  return params;
}

export function dispatch(db: DemoDb, request: DemoRequest): DemoResponse {
  let pathMatched = false;
  for (const route of ROUTES) {
    const params = matchPattern(route.pattern, request.path);
    if (!params) continue;
    pathMatched = true;
    if (route.method !== request.method) continue;
    return route.handler({ ...request, db, params });
  }
  return pathMatched ? fail(405, 'Method Not Allowed') : fail(404, 'Not Found');
}
