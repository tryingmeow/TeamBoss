import { useState, useEffect, useRef } from 'react';
import {
  listAccessTokens,
  createAccessToken,
  disableAccessToken,
  listPendingConfirmations,
  resolvePendingConfirmation,
} from '../../api/client';
import type {
  AccessTokenListItem,
  AccessTokenResponse,
  PendingConfirmationItem,
} from '../../api/client';
import { Trash2, RotateCw, AlertTriangle, Plus, Copy, Check, X } from 'lucide-react';
import Toast from '../../components/Toast';

interface ToastMessage {
  id: number;
  text: string;
  type: 'success' | 'error';
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return '—';
  try {
    const d = new Date(dateStr);
    return d.toLocaleString('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return dateStr;
  }
}

function formatStatus(token: AccessTokenListItem): { label: string; badge: string } {
  if (token.disabled) {
    return { label: '已停用', badge: 'bg-red-100 dark:bg-red-900/30 text-red-800 dark:text-red-200' };
  }
  if (token.used_count > 0) {
    return { label: '已使用', badge: 'bg-blue-100 dark:bg-blue-900/30 text-blue-800 dark:text-blue-200' };
  }
  const expiresAt = token.token_expires_at ? new Date(token.token_expires_at) : null;
  if (expiresAt && expiresAt <= new Date()) {
    return { label: '已过期', badge: 'bg-gray-100 dark:bg-slate-700 text-gray-800 dark:text-slate-300' };
  }
  return { label: '未使用', badge: 'bg-emerald-100 dark:bg-emerald-900/30 text-emerald-800 dark:text-emerald-200' };
}

function statusLabel(token: AccessTokenListItem): string {
  return formatStatus(token).label;
}

const GRANT_PRESETS = ['7d', '30d', '90d', '360d', 'never'];
const TTL_PRESETS = ['1d', '7d', '30d', 'never'];

function durationLabel(value: string): string {
  if (value === 'never') return '永不';
  const amount = value.slice(0, -1);
  const unit = value.slice(-1);
  if (unit === 'd') return `${amount} 天`;
  if (unit === 'h') return `${amount} 小时`;
  if (unit === 'm') return `${amount} 分钟`;
  return value;
}

export default function AccessTokens() {
  const [tokens, setTokens] = useState<AccessTokenListItem[]>([]);
  const [pending, setPending] = useState<PendingConfirmationItem[]>([]);
  const [resolving, setResolving] = useState<number | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [grant, setGrant] = useState('30d');
  const [ttl, setTtl] = useState('7d');
  const [note, setNote] = useState('');
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<AccessTokenResponse | null>(null);
  const [copied, setCopied] = useState(false);
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toasts, setToasts] = useState<ToastMessage[]>([]);
  const toastIdRef = useRef(0);
  const toastTimersRef = useRef<Set<ReturnType<typeof setTimeout>>>(new Set());

  useEffect(() => () => {
    if (copyTimer.current) clearTimeout(copyTimer.current);
    toastTimersRef.current.forEach((timer) => clearTimeout(timer));
    toastTimersRef.current.clear();
  }, []);

  const showToast = (text: string, type: 'success' | 'error' = 'success') => {
    const id = ++toastIdRef.current;
    setToasts((prev) => [...prev, { id, text, type }]);
    const timer = setTimeout(() => {
      toastTimersRef.current.delete(timer);
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 5000);
    toastTimersRef.current.add(timer);
  };

  const loadTokens = async () => {
    setLoading(true);
    setError(null);
    try {
      const [data, stuck] = await Promise.all([
        listAccessTokens(),
        listPendingConfirmations(),
      ]);
      setTokens(data);
      setPending(stuck);
    } catch (err) {
      const message = err instanceof Error ? err.message : '加载兑换码列表失败';
      setError(message);
      showToast(message, 'error');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadTokens();
  }, []);

  const handleCreate = async () => {
    const grantValue = grant.trim();
    const ttlValue = ttl.trim();
    if (!grantValue) {
      showToast('请填写授予时长', 'error');
      return;
    }
    setCreating(true);
    try {
      const token = await createAccessToken({
        grant_expires_in: grantValue,
        token_ttl: ttlValue || '7d',
        note: note.trim() || undefined,
      });
      setCreated(token);
      setCopied(false);
      setNote('');
      await loadTokens();
    } catch (err) {
      const message = err instanceof Error ? err.message : '生成兑换码失败';
      showToast(message, 'error');
    } finally {
      setCreating(false);
    }
  };

  const handleCopyToken = async () => {
    if (!created) return;
    try {
      await navigator.clipboard.writeText(created.token);
      setCopied(true);
      if (copyTimer.current) clearTimeout(copyTimer.current);
      copyTimer.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      showToast('复制失败，请手动选中复制', 'error');
    }
  };

  const handleResolve = async (item: PendingConfirmationItem, outcome: 'success' | 'released') => {
    const question =
      outcome === 'success'
        ? `确认 ${item.email} 已经在「${item.team_name || item.team_id}」里？兑换码保持已使用，并补上 ${item.grant_expires_in} 时长。`
        : `确认 ${item.email} 在「${item.team_name || item.team_id}」里既没有成员也没有邀请？兑换码将退回未使用。`;
    if (!confirm(question)) return;
    setResolving(item.id);
    try {
      await resolvePendingConfirmation(item.id, outcome);
      showToast(outcome === 'success' ? '已确认成功并补齐时长' : '已退回兑换码');
      await loadTokens();
    } catch (err) {
      const message = err instanceof Error ? err.message : '处理失败';
      showToast(message, 'error');
    } finally {
      setResolving(null);
    }
  };

  const handleDisable = async (tokenId: number) => {
    if (!confirm('确认停用该兑换码吗？')) return;
    try {
      await disableAccessToken(tokenId);
      showToast('兑换码已停用');
      await loadTokens();
    } catch (err) {
      const message = err instanceof Error ? err.message : '停用兑换码失败';
      showToast(message, 'error');
    }
  };

  if (loading) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-slate-100">一次性兑换码</h1>
        <div className="flex items-center justify-center h-96 bg-white dark:bg-slate-900 rounded-lg border border-gray-300 dark:border-slate-700">
          <div className="text-center">
            <div className="inline-flex items-center justify-center w-12 h-12 rounded-full bg-indigo-100 dark:bg-indigo-900/30 mb-3">
              <RotateCw className="w-6 h-6 text-indigo-500 animate-spin" />
            </div>
            <p className="text-gray-600 dark:text-slate-400">加载中...</p>
          </div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-slate-100">一次性兑换码</h1>
        <div className="p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg text-red-700 dark:text-red-300">
          <p>{error}</p>
          <button
            onClick={loadTokens}
            className="mt-3 px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg text-sm font-medium transition-colors"
          >
            重新加载
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-slate-100">一次性兑换码</h1>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowCreate((v) => !v)}
            className="inline-flex items-center gap-2 px-4 py-2 bg-indigo-500 hover:bg-indigo-600 text-white rounded-lg font-medium transition-colors"
          >
            <Plus className="w-4 h-4" />
            生成兑换码
          </button>
          <button
            onClick={loadTokens}
            title="刷新"
            className="inline-flex items-center gap-2 px-3 py-2 border border-gray-300 dark:border-slate-600 text-gray-700 dark:text-slate-300 hover:bg-gray-50 dark:hover:bg-slate-800 rounded-lg font-medium transition-colors"
          >
            <RotateCw className="w-4 h-4" />
          </button>
        </div>
      </div>

      {showCreate && (
        <div className="bg-white dark:bg-slate-900 rounded-lg border border-gray-300 dark:border-slate-700 p-4 space-y-4">
          <div className="flex items-start justify-between">
            <div>
              <div className="text-sm font-medium text-gray-900 dark:text-slate-100">生成一个兑换码</div>
              <p className="text-xs text-gray-600 dark:text-slate-400 mt-0.5">
                兑换成功即失效。生成后仅显示一次。
              </p>
            </div>
            <button
              onClick={() => {
                setShowCreate(false);
                setCreated(null);
              }}
              className="p-1 text-gray-500 dark:text-slate-400 hover:bg-gray-100 dark:hover:bg-slate-800 rounded transition-colors"
            >
              <X className="w-4 h-4" />
            </button>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="block text-xs font-medium text-gray-700 dark:text-slate-300 mb-1.5">
                授予时长
              </label>
              <div className="flex flex-wrap gap-1.5 mb-2">
                {GRANT_PRESETS.map((preset) => (
                  <button
                    key={preset}
                    onClick={() => setGrant(preset)}
                    className={`px-2.5 py-1 rounded-md text-xs font-medium transition-colors ${
                      grant === preset
                        ? 'bg-indigo-500 text-white'
                        : 'bg-gray-100 dark:bg-slate-800 text-gray-700 dark:text-slate-300 hover:bg-gray-200 dark:hover:bg-slate-700'
                    }`}
                  >
                    {durationLabel(preset)}
                  </button>
                ))}
              </div>
              <input
                value={grant}
                onChange={(e) => setGrant(e.target.value)}
                placeholder="或自定义，如 45d / 12h / never"
                className="w-full px-3 py-2 text-sm rounded-lg border border-gray-300 dark:border-slate-600 bg-white dark:bg-slate-800 text-gray-900 dark:text-slate-100 placeholder-gray-400 dark:placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500"
              />
            </div>

            <div>
              <label className="block text-xs font-medium text-gray-700 dark:text-slate-300 mb-1.5">
                兑换有效期
              </label>
              <div className="flex flex-wrap gap-1.5 mb-2">
                {TTL_PRESETS.map((preset) => (
                  <button
                    key={preset}
                    onClick={() => setTtl(preset)}
                    className={`px-2.5 py-1 rounded-md text-xs font-medium transition-colors ${
                      ttl === preset
                        ? 'bg-indigo-500 text-white'
                        : 'bg-gray-100 dark:bg-slate-800 text-gray-700 dark:text-slate-300 hover:bg-gray-200 dark:hover:bg-slate-700'
                    }`}
                  >
                    {durationLabel(preset)}
                  </button>
                ))}
              </div>
              <input
                value={ttl}
                onChange={(e) => setTtl(e.target.value)}
                placeholder="默认 7d"
                className="w-full px-3 py-2 text-sm rounded-lg border border-gray-300 dark:border-slate-600 bg-white dark:bg-slate-800 text-gray-900 dark:text-slate-100 placeholder-gray-400 dark:placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500"
              />
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-700 dark:text-slate-300 mb-1.5">
              备注（仅内部可见）
            </label>
            <input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="给谁的 / 什么用途"
              className="w-full px-3 py-2 text-sm rounded-lg border border-gray-300 dark:border-slate-600 bg-white dark:bg-slate-800 text-gray-900 dark:text-slate-100 placeholder-gray-400 dark:placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500"
            />
          </div>

          <button
            onClick={handleCreate}
            disabled={creating}
            className="inline-flex items-center gap-2 px-4 py-2 bg-indigo-500 hover:bg-indigo-600 disabled:opacity-50 text-white rounded-lg text-sm font-medium transition-colors"
          >
            {creating ? <RotateCw className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
            {creating ? '生成中...' : '生成'}
          </button>

          {created && (
            <div className="rounded-lg border border-emerald-300 dark:border-emerald-700 bg-emerald-50 dark:bg-emerald-900/20 p-3">
              <div className="text-xs text-emerald-900 dark:text-emerald-200 mb-2">
                授予 {durationLabel(created.grant_expires_in)} ·{' '}
                {created.token_expires_at
                  ? `${formatDate(created.token_expires_at)} 前有效`
                  : 'Code 永不过期'}
              </div>
              <div className="flex items-center gap-2">
                <code className="flex-1 min-w-0 px-3 py-2 bg-white dark:bg-slate-900 border border-emerald-200 dark:border-emerald-800 rounded-lg font-mono text-sm text-gray-900 dark:text-slate-100 break-all select-all">
                  {created.token}
                </code>
                <button
                  onClick={handleCopyToken}
                  title="复制完整 Code"
                  className="shrink-0 p-2 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white transition-colors"
                >
                  {copied ? <Check className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {pending.length > 0 && (
        <div className="bg-white dark:bg-slate-900 rounded-lg border border-amber-300 dark:border-amber-700 overflow-hidden">
          <div className="px-4 py-3 bg-amber-50 dark:bg-amber-900/20 border-b border-amber-200 dark:border-amber-800">
            <div className="flex items-center gap-2 text-amber-900 dark:text-amber-200 font-medium">
              <AlertTriangle className="w-4 h-4" />
              待确认的兑换（{pending.length}）
            </div>
            <p className="text-xs text-amber-800 dark:text-amber-300/80 mt-1">
              邀请状态未确认，兑换码已锁定。请核对真实状态后手动确认。
            </p>
          </div>
          <div className="divide-y divide-gray-200 dark:divide-slate-700">
            {pending.map((item) => (
              <div key={item.id} className="px-4 py-3 flex flex-wrap items-center gap-3">
                <div className="min-w-0 flex-1">
                  <div className="text-sm text-gray-900 dark:text-slate-100 font-medium truncate">
                    {item.email}
                  </div>
                  <div className="text-xs text-gray-600 dark:text-slate-400 mt-0.5">
                    {item.team_name || item.team_id} · {item.grant_expires_in} ·{' '}
                    <code className="font-mono">{item.token_prefix}</code> · {formatDate(item.created_at)}
                  </div>
                  <div className="text-xs text-gray-500 dark:text-slate-500 mt-0.5">
                    最新快照：{item.seen_in_cached_snapshot ? '已找到' : '未找到'}
                    {item.cache_updated_at ? `（${formatDate(item.cache_updated_at)}）` : ''}
                    {item.error_message ? ` · ${item.error_message}` : ''}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    disabled={resolving === item.id}
                    onClick={() => handleResolve(item, 'success')}
                    className="px-3 py-1.5 text-xs font-medium rounded-lg bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 text-white transition-colors"
                  >
                    确认成功
                  </button>
                  <button
                    disabled={resolving === item.id}
                    onClick={() => handleResolve(item, 'released')}
                    className="px-3 py-1.5 text-xs font-medium rounded-lg border border-gray-300 dark:border-slate-600 text-gray-700 dark:text-slate-300 hover:bg-gray-50 dark:hover:bg-slate-800 disabled:opacity-50 transition-colors"
                  >
                    确认失败并退码
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {tokens.length === 0 ? (
        <div className="flex items-center justify-center h-64 bg-white dark:bg-slate-900 rounded-lg border border-gray-300 dark:border-slate-700">
          <div className="text-center">
            <p className="text-gray-600 dark:text-slate-400">暂无兑换码</p>
            <p className="text-sm text-gray-500 dark:text-slate-500 mt-1">
              在 Telegram 中使用 /token &lt;天数&gt; 生成新兑换码
            </p>
          </div>
        </div>
      ) : (
        <div className="bg-white dark:bg-slate-900 rounded-lg border border-gray-300 dark:border-slate-700 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 dark:bg-slate-800 border-b border-gray-200 dark:border-slate-700">
                <tr>
                  <th className="px-4 py-3 text-left font-medium text-gray-800 dark:text-slate-200">Code 前缀</th>
                  <th className="px-4 py-3 text-left font-medium text-gray-800 dark:text-slate-200">授予时长</th>
                  <th className="px-4 py-3 text-left font-medium text-gray-800 dark:text-slate-200">状态</th>
                  <th className="px-4 py-3 text-left font-medium text-gray-800 dark:text-slate-200">Code 有效期</th>
                  <th className="px-4 py-3 text-left font-medium text-gray-800 dark:text-slate-200">创建时间</th>
                  <th className="px-4 py-3 text-left font-medium text-gray-800 dark:text-slate-200">最后使用</th>
                  <th className="px-4 py-3 text-center font-medium text-gray-800 dark:text-slate-200">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200 dark:divide-slate-700">
                {tokens.map((token) => {
                  const { label, badge } = formatStatus(token);
                  return (
                    <tr
                      key={token.id}
                      className="hover:bg-gray-50 dark:hover:bg-slate-800/50 transition-colors"
                    >
                      <td className="px-4 py-3">
                        <code className="px-2 py-1 bg-gray-100 dark:bg-slate-800 rounded text-gray-800 dark:text-slate-200 font-mono text-xs">
                          {token.token_prefix}
                        </code>
                      </td>
                      <td className="px-4 py-3 text-gray-700 dark:text-slate-300">
                        {token.grant_expires_in === 'never' ? '永不' : token.grant_expires_in}
                      </td>
                      <td className="px-4 py-3">
                        <span className={`px-2 py-1 rounded-full text-xs font-medium ${badge}`}>
                          {label}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-gray-700 dark:text-slate-300 text-xs">
                        {formatDate(token.token_expires_at)}
                      </td>
                      <td className="px-4 py-3 text-gray-700 dark:text-slate-300 text-xs">
                        {formatDate(token.created_at)}
                      </td>
                      <td className="px-4 py-3 text-gray-700 dark:text-slate-300 text-xs">
                        {token.last_used_at ? formatDate(token.last_used_at) : '—'}
                      </td>
                      <td className="px-4 py-3 text-center">
                        <div className="inline-flex items-center gap-2">
                          {!token.disabled && (
                            <button
                              onClick={() => handleDisable(token.id)}
                              title="停用此 Code"
                              className="p-1.5 text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 rounded transition-colors"
                            >
                              <Trash2 className="w-4 h-4" />
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}


      <div className="flex gap-2 text-xs text-gray-600 dark:text-slate-400">
        <div className="px-3 py-2 bg-gray-100 dark:bg-slate-800 rounded">
          共 {tokens.length} 个 Code
        </div>
        <div className="px-3 py-2 bg-gray-100 dark:bg-slate-800 rounded">
          未使用：{tokens.filter((t) => statusLabel(t) === '未使用').length}
        </div>
        <div className="px-3 py-2 bg-gray-100 dark:bg-slate-800 rounded">
          已使用：{tokens.filter((t) => statusLabel(t) === '已使用').length}
        </div>
        <div className="px-3 py-2 bg-gray-100 dark:bg-slate-800 rounded">
          已停用：{tokens.filter((t) => statusLabel(t) === '已停用').length}
        </div>
      </div>

      {toasts.map((toast) => (
        <Toast key={toast.id} text={toast.text} type={toast.type} />
      ))}
    </div>
  );
}
