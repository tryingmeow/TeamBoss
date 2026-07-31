import { useState, useEffect } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { X, ClipboardCheck, Globe } from 'lucide-react';
import { addTeam, fetchProxies, reimportTeam, type Proxy } from '../api/client';
import type { Team } from '../types';
import LoadingSpinner from './LoadingSpinner';

interface AddTeamDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess: () => void;
  team?: Team | null;
}

const SESSION_URL = 'https://chatgpt.com/api/auth/session';

export default function AddTeamDialog({ open, onOpenChange, onSuccess, team = null }: AddTeamDialogProps) {
  const [json, setJson] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState(false);
  const [proxies, setProxies] = useState<Proxy[]>([]);
  const [selectedProxyId, setSelectedProxyId] = useState<number | null>(null);

  useEffect(() => {
    if (open) {
      setJson('');
      setError('');
      setSelectedProxyId(team?.proxy_id ?? null);
      navigator.clipboard.writeText(SESSION_URL).then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      }).catch(() => {});
      fetchProxies().then(setProxies).catch(() => {});
    }
  }, [open, team]);

  const handleSubmit = async () => {
    if (!json.trim()) {
      setError('请粘贴 session JSON');
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(json);
    } catch {
      setError('JSON 格式无效');
      return;
    }
    setError('');
    setLoading(true);
    try {
      if (team) {
        await reimportTeam(team.id, parsed, selectedProxyId);
      } else {
        await addTeam(parsed, selectedProxyId);
      }
      onSuccess();
      onOpenChange(false);
      setJson('');
      setSelectedProxyId(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : (team ? '重新导入失败' : '添加失败'));
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/60 z-50" />
        <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-full max-w-lg rounded-xl bg-white dark:bg-[#1a1d27] border border-gray-200 dark:border-[#2a2d3a] p-6 shadow-2xl">
          <Dialog.Title className="text-lg font-bold text-gray-900 dark:text-gray-100">
            {team ? `重新导入 ${team.name}` : '添加 Team'}
          </Dialog.Title>

          <div className="mt-4 space-y-3">
            {team && (
              <div className="rounded-lg border border-blue-200 bg-blue-50 px-3 py-2 text-xs text-blue-700 dark:border-blue-900/60 dark:bg-blue-950/30 dark:text-blue-300">
                只更新当前 Team 的 Session，保留备注、成员期限和管理记录。系统会拒绝属于其他 Team 的 Session。
              </div>
            )}
            <div className="flex items-center gap-2 text-sm">
              <ClipboardCheck size={16} className={copied ? 'text-green-400' : 'text-gray-400'} />
              <span className={copied ? 'text-green-400' : 'text-gray-400'}>
                {copied ? '链接已复制!' : '链接将自动复制'}
              </span>
            </div>

            <div className="px-3 py-2 bg-gray-50 dark:bg-[#0f1117] rounded-lg border border-gray-200 dark:border-[#2a2d3a]">
              <code className="text-xs text-blue-500 dark:text-blue-400 break-all">{SESSION_URL}</code>
            </div>

            <p className="text-xs text-gray-500 dark:text-gray-400">
              请登录 owner 账号，在浏览器中打开上方链接，复制返回的 JSON 粘贴到下方
            </p>

            <textarea
              value={json}
              onChange={(e) => setJson(e.target.value)}
              placeholder="粘贴 session JSON ..."
              rows={8}
              className="w-full px-3 py-2 bg-gray-50 dark:bg-[#0f1117] border border-gray-200 dark:border-[#2a2d3a] rounded-lg text-sm text-gray-900 dark:text-gray-200 placeholder:text-gray-400 dark:placeholder:text-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500 resize-none font-mono transition-all"
            />

            {/* Proxy selector */}
            <div className="flex items-center gap-2">
              <Globe size={14} className="text-gray-400 shrink-0" />
              <select
                value={selectedProxyId ?? ''}
                onChange={(e) => setSelectedProxyId(e.target.value === '' ? null : Number(e.target.value))}
                className="flex-1 px-3 py-1.5 bg-gray-50 dark:bg-[#0f1117] border border-gray-200 dark:border-[#2a2d3a] rounded-lg text-sm text-gray-700 dark:text-gray-300 focus:outline-none focus:ring-2 focus:ring-blue-500/50"
              >
                <option value="">直连</option>
                {proxies.map((p) => (
                  <option key={p.id} value={p.id}>{p.name}</option>
                ))}
              </select>
            </div>

            {error && <p className="text-sm text-red-500 dark:text-red-400">{error}</p>}
          </div>

          <div className="mt-6 flex justify-end gap-3">
            <Dialog.Close asChild>
              <button className="px-4 py-2 rounded-lg text-sm font-medium text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-[#2a2d3a] hover:bg-gray-200 dark:hover:bg-[#3a3d4a] transition-colors">
                取消
              </button>
            </Dialog.Close>
            <button
              onClick={handleSubmit}
              disabled={loading}
              className="px-4 py-2 rounded-lg text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 shadow-md shadow-blue-500/20 transition-all disabled:opacity-50 flex items-center gap-2"
            >
              {loading && <LoadingSpinner size={14} />}
              {loading ? '处理中...' : (team ? '确认重新导入' : '确认添加')}
            </button>
          </div>

          <Dialog.Close asChild>
            <button className="absolute top-4 right-4 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 transition-colors p-1 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800">
              <X size={16} />
            </button>
          </Dialog.Close>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
