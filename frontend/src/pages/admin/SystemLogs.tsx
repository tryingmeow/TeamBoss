import { useState, useEffect } from 'react';
import { fetchLogs as fetchLogsApi, type OperationLog } from '../../api/client';
import { Activity, CheckCircle2, AlertCircle, Info, Clock, Search, Download } from 'lucide-react';
import { formatDateSafe } from '../../lib/formatDate';

const EXPORT_PER_PAGE = 50;
const EXPORT_MAX_PAGES = 20;
const EXPORT_MAX_ROWS = EXPORT_PER_PAGE * EXPORT_MAX_PAGES;

function csvField(value: string | number | null | undefined): string {
  const s = value === null || value === undefined ? '' : String(value);
  return `"${s.replace(/"/g, '""')}"`;
}

function logToCsvRow(log: OperationLog): string {
  return [
    csvField(log.created_at ? formatDateSafe(log.created_at, 'yyyy-MM-dd HH:mm:ss', '-') : '-'),
    csvField(log.result),
    csvField(log.action),
    csvField(log.team_name),
    csvField(log.team_id),
    csvField(log.team_owner_email),
    csvField(log.target_email),
    csvField(log.trigger_type),
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
  return '全局任务';
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
    fetchLogsApi({ page, per_page: 50, q: debouncedSearch, scope })
      .then(res => {
        if (cancelled) return;
        setLogs(res.logs);
        setTotalPages(Math.max(1, res.total_pages || 1));
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
      const first = await fetchLogsApi({ page: 1, per_page: EXPORT_PER_PAGE, q: debouncedSearch, scope });
      const allLogs: OperationLog[] = [...first.logs];
      const totalPagesForExport = Math.max(1, first.total_pages || 1);
      const pagesToFetch = Math.min(totalPagesForExport, EXPORT_MAX_PAGES);
      const truncated = totalPagesForExport > EXPORT_MAX_PAGES;

      for (let p = 2; p <= pagesToFetch; p++) {
        const res = await fetchLogsApi({ page: p, per_page: EXPORT_PER_PAGE, q: debouncedSearch, scope });
        allLogs.push(...res.logs);
      }

      const rows = allLogs.slice(0, EXPORT_MAX_ROWS);
      const header = ['时间', '状态', '操作', 'Team', 'Team ID', 'Team 负责人', '目标', '触发方', '详情', '错误信息']
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

  const getStatusIcon = (result: string | null) => {
    switch (result?.toLowerCase()) {
      case 'success':
        return <CheckCircle2 className="w-5 h-5 text-emerald-400" />;
      case 'error':
      case 'failed':
        return <AlertCircle className="w-5 h-5 text-rose-400" />;
      default:
        return <Info className="w-5 h-5 text-blue-400" />;
    }
  };

  return (
    <div className={embedded ? 'animate-in fade-in duration-300' : 'p-8 max-w-7xl mx-auto space-y-8 animate-in fade-in duration-500'}>
      {!embedded && (
        <div className="flex justify-between items-end">
          <div>
            <h1 className="text-2xl font-bold text-gray-900 dark:text-slate-100 mb-1 flex items-center gap-2">
              <Activity className="w-6 h-6 text-indigo-400" />
              系统日志
            </h1>
          </div>
          <div className="flex items-center gap-3">
            <div className="relative">
              <Search className="w-4 h-4 text-gray-400 dark:text-slate-500 absolute left-3 top-1/2 -translate-y-1/2" />
              <input
                type="text"
                value={search}
                onChange={(e) => setLocalSearch(e.target.value)}
                placeholder="搜索日志、Team 或邮箱..."
                className="pl-9 pr-4 py-2 bg-white dark:bg-slate-900 border border-gray-200 dark:border-slate-800 rounded-lg text-sm text-gray-800 dark:text-slate-200 focus:outline-none focus:border-indigo-500 transition-colors"
              />
            </div>
            <button
              type="button"
              onClick={handleExportCsv}
              disabled={exporting}
              className="flex items-center gap-2 px-4 py-2 bg-white dark:bg-slate-900 border border-gray-200 dark:border-slate-800 hover:bg-gray-50 dark:hover:bg-slate-800 rounded-lg text-sm text-gray-700 dark:text-slate-300 hover:text-indigo-600 dark:hover:text-indigo-400 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Download className="w-4 h-4" />
              {exporting ? '导出中...' : '导出 CSV'}
            </button>
          </div>
        </div>
      )}
      {!embedded && exportMessage && (
        <div className="text-sm text-gray-500 dark:text-slate-400 -mt-4">{exportMessage}</div>
      )}

      <div className="bg-white dark:bg-slate-900 border border-gray-200 dark:border-slate-800 rounded-xl overflow-hidden">
        {loadError && (
          <div role="alert" className="flex items-center gap-3 border-b border-red-200 bg-red-50 px-6 py-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">
            <span>日志加载失败：{loadError}</span>
            <button type="button" disabled={loading} onClick={() => setRetryTrigger((v) => v + 1)} className="ml-auto shrink-0 rounded border border-current px-3 py-1 disabled:opacity-50">重试</button>
          </div>
        )}
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm text-gray-700 dark:text-slate-300">
            <thead className="bg-gray-50 dark:bg-slate-950/50 text-gray-500 dark:text-slate-400">
              <tr>
                <th className="px-6 py-4 font-medium w-16">状态</th>
                <th className="px-6 py-4 font-medium">操作</th>
                <th className="px-6 py-4 font-medium">Team</th>
                <th className="px-6 py-4 font-medium">目标</th>
                <th className="px-6 py-4 font-medium">触发方</th>
                <th className="px-6 py-4 font-medium">详情</th>
                <th className="px-6 py-4 font-medium text-right">时间</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 dark:divide-slate-800/50">
              {loading ? (
                <tr><td colSpan={7} className="px-6 py-8 text-center text-gray-500 dark:text-slate-400">正在加载日志...</td></tr>
              ) : loadError ? (
                <tr><td colSpan={7} className="px-6 py-8 text-center text-red-600 dark:text-red-400">日志未能加载，请重试</td></tr>
              ) : logs.length === 0 ? (
                <tr><td colSpan={7} className="px-6 py-8 text-center text-gray-400 dark:text-slate-500">暂无日志</td></tr>
              ) : (
                logs.map((log, i) => (
                  <tr key={i} className="transition-colors hover:bg-gray-100 dark:hover:bg-slate-800 dark:bg-slate-800/20 group">
                    <td className="px-6 py-4">
                      {getStatusIcon(log.result)}
                    </td>
                    <td className="px-6 py-4">
                      <div className="font-medium text-gray-800 dark:text-slate-200">{log.action || '-'}</div>
                    </td>
                    <td className="px-6 py-4 min-w-56">
                      <div className="font-medium text-gray-800 dark:text-slate-200">{teamDisplayName(log)}</div>
                      <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-gray-400 dark:text-slate-500">
                        {log.team_owner_email && <span className="max-w-48 truncate">{log.team_owner_email}</span>}
                        {log.team_id && (
                          <span className="rounded bg-gray-100 dark:bg-slate-800 px-1.5 py-0.5 font-mono" title={log.team_id}>
                            {shortTeamId(log.team_id)}
                          </span>
                        )}
                        {log.team_status && (
                          <span className="rounded bg-gray-100 dark:bg-slate-800 px-1.5 py-0.5">
                            {log.team_status}
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="px-6 py-4">
                      <div className="text-gray-700 dark:text-slate-300">{log.target_email || '-'}</div>
                    </td>
                    <td className="px-6 py-4">
                      <span className="px-2.5 py-1 bg-gray-100 dark:bg-slate-800 rounded-md text-xs font-mono text-gray-500 dark:text-slate-400">
                        {log.trigger_type || 'system'}
                      </span>
                    </td>
                    <td className="px-6 py-4">
                      <div className="text-gray-700 dark:text-slate-300 line-clamp-1 group-hover:line-clamp-none transition-all">
                        {log.detail || '-'}
                      </div>
                      {log.error_message && (
                        <div className="text-rose-400 text-xs mt-1 bg-rose-500/10 px-2 py-1 rounded">
                          {log.error_message}
                        </div>
                      )}
                    </td>
                    <td className="px-6 py-4 text-right text-gray-500 dark:text-slate-400">
                      <div className="flex items-center justify-end gap-1.5 whitespace-nowrap">
                        <Clock className="w-3.5 h-3.5" />
                        {log.created_at ? formatDateSafe(log.created_at, 'MM-dd HH:mm:ss', '-') : '-'}
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
        <div className="px-6 py-4 border-t border-gray-200 dark:border-slate-800 flex items-center justify-between bg-gray-50 dark:bg-slate-950/30">
          <button 
            onClick={() => setPage(p => Math.max(1, p - 1))}
            disabled={loading || page === 1}
            className="px-3 py-1.5 text-sm bg-gray-100 dark:bg-slate-800 hover:bg-gray-200 dark:bg-slate-700 text-gray-700 dark:text-slate-300 rounded disabled:opacity-50"
          >
            上一页
          </button>
          <span className="text-sm text-gray-400 dark:text-slate-500">第 {page} 页</span>
          <button 
            onClick={() => setPage(p => p + 1)}
            disabled={loading || Boolean(loadError) || page >= totalPages}
            className="px-3 py-1.5 text-sm bg-gray-100 dark:bg-slate-800 hover:bg-gray-200 dark:bg-slate-700 text-gray-700 dark:text-slate-300 rounded disabled:opacity-50"
          >
            下一页
          </button>
        </div>
      </div>
    </div>
  );
}
