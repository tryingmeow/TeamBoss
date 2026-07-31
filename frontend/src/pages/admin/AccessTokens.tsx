import { useState, useEffect } from 'react';
import { listAccessTokens, disableAccessToken } from '../../api/client';
import type { AccessTokenListItem } from '../../api/client';
import { Trash2, RotateCw } from 'lucide-react';
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

export default function AccessTokens() {
  const [tokens, setTokens] = useState<AccessTokenListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toasts, setToasts] = useState<ToastMessage[]>([]);
  const [nextToastId, setNextToastId] = useState(0);

  const showToast = (text: string, type: 'success' | 'error' = 'success') => {
    const id = nextToastId;
    setNextToastId((prev) => prev + 1);
    setToasts((prev) => [...prev, { id, text, type }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 5000);
  };

  const loadTokens = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await listAccessTokens();
      setTokens(data);
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
          <p className="text-sm text-gray-600 dark:text-slate-400 mt-1">生成与管理成员使用的一次性兑换码</p>
        </div>
        <button
          onClick={loadTokens}
          className="inline-flex items-center gap-2 px-4 py-2 bg-indigo-500 hover:bg-indigo-600 text-white rounded-lg font-medium transition-colors"
        >
          <RotateCw className="w-4 h-4" />
          刷新
        </button>
      </div>

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

      <div className="p-4 bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg text-blue-900 dark:text-blue-300 text-sm">
        <p className="font-medium mb-2">💡 如何使用</p>
        <ul className="space-y-1 text-xs">
          <li>• 在 Telegram 中向机器人发送 <code className="bg-blue-100 dark:bg-blue-900/50 px-1 rounded">/token 30</code> 生成有效期 30 天的兑换码</li>
          <li>• 机器人会返回完整的 Code，复制后转发给成员使用</li>
          <li>• 成员在自助页面兑换 Code 后会自动加入车队或续期</li>
          <li>• 可以在此页面查看 Code 的使用状态和停用已发布的 Code</li>
        </ul>
      </div>

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
