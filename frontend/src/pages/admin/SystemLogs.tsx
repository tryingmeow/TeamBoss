import { useState, useEffect, useMemo } from 'react';
import type { OperationLog } from '../../api/client';
import { ChevronLeft, ChevronRight, Download, Search } from 'lucide-react';
import { formatDateSafe } from '../../lib/formatDate';
import {
  formatLogDetail,
  formatLogError,
  logActionLabel,
  logResultMeta,
  logTriggerLabel,
  teamStatusLabel,
} from '../../lib/logLabels';
import { csvField } from '../../lib/csv';
import { collectLogs, searchLogsPage } from '../../lib/logSearch';
import { cn } from '../../lib/utils';
import PageShell from '../../components/PageShell';
import { BUTTON, CARD, INPUT, PILL, TONE } from '../../components/ui';

const PER_PAGE = 50;
const EXPORT_MAX_ROWS = 1000;

function logToCsvRow(log: OperationLog): string {
  return [
    csvField(log.created_at ? formatDateSafe(log.created_at, 'yyyy-MM-dd HH:mm:ss', '-') : '-'),
    csvField(logResultMeta(log.result).label),
    csvField(logActionLabel(log.action)),
    csvField(log.action),
    csvField(log.team_name),
    csvField(log.team_id),
    csvField(log.team_owner_email),
    csvField(log.target_email),
    csvField(logTriggerLabel(log.trigger_type)),
    csvField(log.detail),
    csvField(log.error_message),
  ].join(',');
}

interface SystemLogsProps {
  embedded?: boolean;
  scope?: 'members';
  search?: string;
}

function shortTeamId(teamId: string | null): string {
  if (!teamId) return '';
  return teamId.length > 8 ? teamId.slice(0, 8) : teamId;
}

function teamDisplayName(log: OperationLog): string {
  if (log.team_name && log.team_remark) return `${log.team_name}（${log.team_remark}）`;
  if (log.team_name) return log.team_name;
  if (log.team_id) return `Team ${shortTeamId(log.team_id)}`;
  // Not tied to a Team: settings, logins, member remarks (kept per email across Teams), …
  return '全局';
}

export default function SystemLogs({ embedded = false, scope, search: externalSearch }: SystemLogsProps = {}) {
  const [logs, setLogs] = useState<OperationLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [retryTrigger, setRetryTrigger] = useState(0);
  const [page, setPage] = useState(1);
  const [localSearch, setLocalSearch] = useState('');
  const [totalPages, setTotalPages] = useState(1);
  const search = externalSearch ?? localSearch;

  // 输入防抖:每次按键都触发 GET /api/logs 太贵,300ms 内不再有新输入才真正拉取。
  const [debouncedSearch, setDebouncedSearch] = useState(search);
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(timer);
  }, [search]);

  // 把"搜索/scope 变了就回到第 1 页"放到渲染期间同步完成(而不是单独一个
  // effect),这样下面的抓取 effect 在同一次 commit 里就能看到最新的 page,
  // 不会先用旧 page 打一次注定被丢弃的请求。
  const [prevFetchKey, setPrevFetchKey] = useState({ scope, search: debouncedSearch });
  if (prevFetchKey.scope !== scope || prevFetchKey.search !== debouncedSearch) {
    setPrevFetchKey({ scope, search: debouncedSearch });
    setPage(1);
  }

  useEffect(() => {
    let cancelled = false;

    setLoading(true);
    setLogs([]);
    setLoadError('');
    // Matches the visible labels too: "移出" also finds every action labelled 移出.
    searchLogsPage({ scope, search: debouncedSearch }, page, PER_PAGE)
      .then(res => {
        if (cancelled) return;
        setLogs(res.logs);
        setTotalPages(res.totalPages);
      })
      .catch(error => {
        if (!cancelled) setLoadError(error instanceof Error ? error.message : '加载失败');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [page, scope, debouncedSearch, retryTrigger]);

  const [exporting, setExporting] = useState(false);
  const [exportMessage, setExportMessage] = useState<string | null>(null);

  const handleExportCsv = async () => {
    setExporting(true);
    setExportMessage(null);
    try {
      // Same matching as the list (labels included), so the file holds what the page shows.
      const { logs: rows, total } = await collectLogs({ scope, search: debouncedSearch }, EXPORT_MAX_ROWS);
      const truncated = total > rows.length;
      const header = ['时间', '状态', '操作', '操作代码', 'Team', 'Team ID', 'Team 负责人', '目标', '触发方', '详情', '错误信息']
        .map(csvField)
        .join(',');
      const csvBody = [header, ...rows.map(logToCsvRow)].join('\r\n');
      const csvContent = '﻿' + csvBody;

      const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
      const url = URL.createObjectURL(blob);
      const filename = `system-logs-${formatDateSafe(new Date(), 'yyyyMMdd-HHmmss')}.csv`;
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);

      if (truncated) {
        setExportMessage(`已导出 ${rows.length} 条记录，超过上限（最多 ${EXPORT_MAX_ROWS} 条），结果已截断。`);
      } else {
        setExportMessage(`已导出 ${rows.length} 条记录。`);
      }
    } catch (error) {
      console.error(error);
      setExportMessage('导出失败，请稍后重试。');
    } finally {
      setExporting(false);
    }
  };

  // Team names seen on this page, so error lists of team ids can be shown by name.
  const teamNames = useMemo(() => {
    const names = new Map<string, string>();
    for (const log of logs) {
      if (log.team_id && log.team_name) names.set(log.team_id, log.team_name);
    }
    return names;
  }, [logs]);

  const resultPill = (log: OperationLog) => {
    const meta = logResultMeta(log.result);
    return <span className={cn(PILL, TONE[meta.tone])}>{meta.label}</span>;
  };

  const teamCell = (log: OperationLog) => (
    <>
      {log.team_id ? (
        <div className="truncate font-medium text-gray-800 dark:text-gray-100" title={teamDisplayName(log)}>{teamDisplayName(log)}</div>
      ) : (
        <div className="truncate text-gray-400 dark:text-ink-500" title="不属于某个 Team">{teamDisplayName(log)}</div>
      )}
      {(log.team_owner_email || (log.team_status && log.team_status !== 'active')) && (
        <div className="mt-0.5 flex min-w-0 items-center gap-1.5 text-xs text-gray-400 dark:text-ink-500">
          {log.team_owner_email && <span className="truncate" title={log.team_owner_email}>{log.team_owner_email}</span>}
          {log.team_status && log.team_status !== 'active' && (
            <span className={cn(PILL, TONE.danger)}>{teamStatusLabel(log.team_status)}</span>
          )}
        </div>
      )}
    </>
  );

  const detailCell = (log: OperationLog) => {
    const detail = formatLogDetail(log.detail);
    return (
      <>
        {detail ? (
          <div className="line-clamp-2 text-gray-600 dark:text-ink-300" title={log.detail ?? undefined}>{detail}</div>
        ) : (
          <span className="text-gray-300 dark:text-ink-600">—</span>
        )}
        {log.error_message && (
          <div className="mt-1 line-clamp-3 break-words rounded-md bg-red-50 px-2 py-1 text-xs text-red-700 dark:bg-red-500/10 dark:text-red-300" title={log.error_message}>
            {formatLogError(log.error_message, (id) => teamNames.get(id))}
          </div>
        )}
      </>
    );
  };

  const time = (log: OperationLog) => (log.created_at ? formatDateSafe(log.created_at, 'MM-dd HH:mm:ss', '—') : '—');

  const statusRow = (text: string, tone = 'text-gray-500 dark:text-ink-400') => (
    <div className={`px-5 py-12 text-center text-sm ${tone}`}>{text}</div>
  );

  const body = (
    <div className={cn(CARD, 'overflow-hidden')}>
      {loadError && (
        <div role="alert" className="flex items-center gap-3 border-b border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 sm:px-5 dark:border-red-900/60 dark:bg-red-500/10 dark:text-red-300">
          <span className="min-w-0 flex-1">日志加载失败：{loadError}</span>
          <button type="button" disabled={loading} onClick={() => setRetryTrigger((v) => v + 1)} className="shrink-0 rounded-md border border-current px-3 py-1 text-sm disabled:opacity-50">重试</button>
        </div>
      )}

      {loading ? (
        statusRow('正在加载日志…')
      ) : loadError ? (
        statusRow('日志未能加载，请重试', 'text-red-600 dark:text-red-400')
      ) : logs.length === 0 ? (
        statusRow(search ? '没有匹配的日志' : '暂无日志')
      ) : (
        <>
          {/* Wide screens: table */}
          <table className="hidden w-full table-fixed text-left text-sm xl:table">
            <colgroup>
              <col className="w-[5.5rem]" />
              <col className="w-[13%]" />
              <col className="w-[19%]" />
              <col className="w-[17%]" />
              <col />
              <col className="w-[6.5rem]" />
              <col className="w-[8.5rem]" />
            </colgroup>
            <thead className="border-b border-gray-200 bg-gray-50 text-xs text-gray-500 dark:border-ink-800 dark:bg-ink-950/40 dark:text-ink-400">
              <tr>
                <th className="whitespace-nowrap px-4 py-3 font-medium">结果</th>
                <th className="whitespace-nowrap px-3 py-3 font-medium">操作</th>
                <th className="whitespace-nowrap px-3 py-3 font-medium">Team</th>
                <th className="whitespace-nowrap px-3 py-3 font-medium">目标</th>
                <th className="whitespace-nowrap px-3 py-3 font-medium">详情</th>
                <th className="whitespace-nowrap px-3 py-3 font-medium">触发方</th>
                <th className="whitespace-nowrap px-4 py-3 text-right font-medium">时间</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 dark:divide-ink-800">
              {logs.map((log, i) => (
                <tr key={log.id ?? i} className="align-top transition-colors hover:bg-gray-50 dark:hover:bg-ink-800/40">
                  <td className="px-4 py-3">{resultPill(log)}</td>
                  <td className="px-3 py-3 font-medium text-gray-800 dark:text-gray-100" title={log.action ?? undefined}>{logActionLabel(log.action)}</td>
                  <td className="min-w-0 px-3 py-3">{teamCell(log)}</td>
                  <td className="px-3 py-3">
                    <div className="truncate text-gray-700 dark:text-ink-200" title={log.target_email ?? undefined}>{log.target_email || '—'}</div>
                  </td>
                  <td className="px-3 py-3">{detailCell(log)}</td>
                  <td className="px-3 py-3">
                    <span className={cn(PILL, TONE.neutral)}>{logTriggerLabel(log.trigger_type)}</span>
                  </td>
                  <td className="whitespace-nowrap px-4 py-3 text-right tabular-nums text-gray-500 dark:text-ink-400">{time(log)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {/* Below 1280px: one block per entry */}
          <ul className="divide-y divide-gray-100 xl:hidden dark:divide-ink-800">
            {logs.map((log, i) => (
              <li key={log.id ?? i} className="space-y-1.5 px-4 py-3.5 text-sm">
                <div className="flex items-center gap-2">
                  {resultPill(log)}
                  <span className="min-w-0 flex-1 truncate font-medium text-gray-900 dark:text-gray-100" title={log.action ?? undefined}>{logActionLabel(log.action)}</span>
                  <span className="shrink-0 text-xs tabular-nums text-gray-400 dark:text-ink-500">{time(log)}</span>
                </div>
                <div className="min-w-0 text-[13px]">{teamCell(log)}</div>
                {log.target_email && (
                  <div className="truncate text-[13px] text-gray-600 dark:text-ink-300" title={log.target_email}>目标：{log.target_email}</div>
                )}
                {(log.detail || log.error_message) && <div className="text-[13px]">{detailCell(log)}</div>}
                <div className="text-xs text-gray-400 dark:text-ink-500">{logTriggerLabel(log.trigger_type)}</div>
              </li>
            ))}
          </ul>
        </>
      )}

      <div className="flex items-center justify-between gap-3 border-t border-gray-200 bg-gray-50/60 px-4 py-3 sm:px-5 dark:border-ink-800 dark:bg-ink-950/30">
        <button
          type="button"
          onClick={() => setPage(p => Math.max(1, p - 1))}
          disabled={loading || page === 1}
          className={cn(BUTTON.secondary, 'px-3 py-1.5')}
        >
          <ChevronLeft size={15} /> 上一页
        </button>
        <span className="text-sm tabular-nums text-gray-500 dark:text-ink-400">第 {page} / {totalPages} 页</span>
        <button
          type="button"
          onClick={() => setPage(p => p + 1)}
          disabled={loading || Boolean(loadError) || page >= totalPages}
          className={cn(BUTTON.secondary, 'px-3 py-1.5')}
        >
          下一页 <ChevronRight size={15} />
        </button>
      </div>
    </div>
  );

  if (embedded) return body;

  return (
    <PageShell
      title="系统日志"
      description="后台操作、定时任务和巡逻留下的记录，悬停可看原始内容。"
      actions={
        <div className="flex w-full flex-wrap items-center gap-2 md:w-auto md:flex-nowrap">
          <label className="relative min-w-0 flex-1 md:w-72 md:flex-none">
            <span className="sr-only">搜索日志</span>
            <Search size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 dark:text-ink-500" />
            <input
              type="search"
              value={search}
              onChange={(e) => setLocalSearch(e.target.value)}
              placeholder="搜索日志、Team 或邮箱…"
              className={cn(INPUT, 'pl-9')}
            />
          </label>
          <button type="button" onClick={handleExportCsv} disabled={exporting} className={BUTTON.secondary}>
            <Download size={16} />
            {exporting ? '导出中…' : '导出 CSV'}
          </button>
        </div>
      }
    >
      {exportMessage && <p className="-mt-2 mb-3 text-sm text-gray-500 dark:text-ink-400">{exportMessage}</p>}
      {body}
    </PageShell>
  );
}
