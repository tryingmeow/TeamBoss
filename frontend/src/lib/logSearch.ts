/**
 * Log search that also finds rows by their displayed labels.
 *
 * GET /api/logs takes one `q` (substring of the stored codes and text) and one exact
 * `action`, so "raw text OR any action whose label matches" is several requests: the plain
 * `q`, plus one per matching code. Each is ordered newest first, so the newest N merged
 * rows are always among the newest N rows of each request.
 */
import { fetchLogs, type OperationLog } from '../api/client';
import { logCodesMatchingLabel } from './logLabels';

type LogQuery = { q?: string; action?: string };

export interface LogSearchParams {
  scope?: 'members';
  search: string;
}

export interface LogWindow {
  logs: OperationLog[];
  /** Exact when every request was read to the end; otherwise an upper bound (overlaps between requests are counted twice). */
  total: number;
}

const API_MAX_PER_PAGE = 200;
const MAX_PARALLEL = 6;

export function logSearchQueries(search: string): LogQuery[] {
  const text = search.trim();
  if (!text) return [{}];
  const { actions, values } = logCodesMatchingLabel(text);
  return [{ q: text }, ...actions.map((action) => ({ action })), ...values.map((q) => ({ q }))];
}

async function newestRows(
  query: LogQuery,
  scope: LogSearchParams['scope'],
  count: number,
): Promise<{ rows: OperationLog[]; total: number }> {
  const perPage = Math.min(API_MAX_PER_PAGE, count);
  const rows: OperationLog[] = [];
  let total = 0;
  for (let page = 1; rows.length < count; page++) {
    const res = await fetchLogs({ ...query, scope, page, per_page: perPage });
    total = res.total;
    rows.push(...res.logs);
    if (res.logs.length < perPage || rows.length >= total) break;
  }
  return { rows: rows.slice(0, count), total };
}

async function mapLimited<T, R>(items: T[], limit: number, fn: (item: T) => Promise<R>): Promise<R[]> {
  const results = new Array<R>(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const index = next++;
      results[index] = await fn(items[index]);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return results;
}

function timeOf(log: OperationLog): number {
  const ms = log.created_at ? Date.parse(log.created_at) : NaN;
  return Number.isNaN(ms) ? 0 : ms;
}

/** Rows [offset, offset + limit) of the merged, newest-first result. */
async function logWindow({ scope, search }: LogSearchParams, offset: number, limit: number): Promise<LogWindow> {
  const queries = logSearchQueries(search);
  const need = offset + limit;
  const parts = await mapLimited(queries, MAX_PARALLEL, (query) => newestRows(query, scope, need));

  const byId = new Map<number, OperationLog>();
  for (const part of parts) for (const row of part.rows) byId.set(row.id, row);
  const merged = [...byId.values()].sort((a, b) => timeOf(b) - timeOf(a) || b.id - a.id);
  const unread = parts.reduce((sum, part) => sum + Math.max(0, part.total - part.rows.length), 0);

  return { logs: merged.slice(offset, need), total: merged.length + unread };
}

/** One page of the log list. Without label matches this is a single plain request. */
export async function searchLogsPage(
  params: LogSearchParams,
  page: number,
  perPage: number,
): Promise<LogWindow & { totalPages: number }> {
  if (logSearchQueries(params.search).length === 1) {
    const res = await fetchLogs({ scope: params.scope, q: params.search, page, per_page: perPage });
    return { logs: res.logs, total: res.total, totalPages: Math.max(1, res.total_pages || 1) };
  }
  const window = await logWindow(params, (page - 1) * perPage, perPage);
  return { ...window, totalPages: Math.max(1, Math.ceil(window.total / perPage)) };
}

/** The newest `limit` matching rows, for export. */
export function collectLogs(params: LogSearchParams, limit: number): Promise<LogWindow> {
  return logWindow(params, 0, limit);
}
