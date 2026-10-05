/**
 * Log search that also finds rows by their displayed labels.
 *
 * "Raw text OR any code whose label matches" is one GET /api/logs: every term goes in as a
 * repeated `q`, the matching action codes as `q_action`, and the server ORs them and paginates.
 */
import { fetchLogs, type OperationLog } from '../api/client';
import { logCodesMatchingLabel } from './logLabels';

export interface LogSearchParams {
  scope?: 'members';
  search: string;
}

export interface LogWindow {
  logs: OperationLog[];
  /** Exact number of matching rows. */
  total: number;
}

function searchQuery(search: string): { q?: string[]; q_action?: string[] } {
  const text = search.trim();
  if (!text) return {};
  const { actions, values } = logCodesMatchingLabel(text);
  return { q: [text, ...values], q_action: actions };
}

/** One page of the log list: a single request. */
export async function searchLogsPage(
  { scope, search }: LogSearchParams,
  page: number,
  perPage: number,
): Promise<LogWindow & { totalPages: number }> {
  const res = await fetchLogs({ ...searchQuery(search), scope, page, per_page: perPage });
  return { logs: res.logs, total: res.total, totalPages: Math.max(1, res.total_pages || 1) };
}

/** The newest `limit` matching rows (server max 1000), for export: a single request. */
export async function collectLogs({ scope, search }: LogSearchParams, limit: number): Promise<LogWindow> {
  const res = await fetchLogs({ ...searchQuery(search), scope, page: 1, per_page: limit });
  return { logs: res.logs, total: res.total };
}
