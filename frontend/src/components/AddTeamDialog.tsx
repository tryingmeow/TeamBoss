import { useState, useEffect, useRef } from 'react';
import { Globe, Copy, Check, ExternalLink } from 'lucide-react';
import { addTeam, fetchProxies, reimportTeam, type Proxy } from '../api/client';
import type { Team } from '../types';
import DialogFrame from './DialogFrame';
import LoadingSpinner from './LoadingSpinner';
import { BUTTON, INPUT } from './ui';
import { cn } from '../lib/utils';

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
  const copyResetTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [proxies, setProxies] = useState<Proxy[]>([]);
  const [selectedProxyId, setSelectedProxyId] = useState<number | null>(null);

  useEffect(() => {
    if (open) {
      setJson('');
      setError('');
      setSelectedProxyId(team?.proxy_id ?? null);
      setCopied(false);
      fetchProxies().then(setProxies).catch(() => {});
    }

    return () => {
      if (copyResetTimer.current) clearTimeout(copyResetTimer.current);
    };
  }, [open, team]);

  const copySessionUrl = async () => {
    try {
      await navigator.clipboard.writeText(SESSION_URL);
      setCopied(true);
      if (copyResetTimer.current) clearTimeout(copyResetTimer.current);
      copyResetTimer.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  };

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
    <DialogFrame
      open={open}
      onOpenChange={onOpenChange}
      size="lg"
      title={team ? `重新导入 ${team.name}` : '添加 Team'}
      description={team
        ? '只更新这个 Team 的 Session，备注、成员到期和管理记录都会保留。属于其他 Team 的 Session 会被拒绝。'
        : '用工作区 owner 账号的 Session 接入一个 ChatGPT Team。'}
      footer={
        <>
          <button type="button" onClick={() => onOpenChange(false)} className={BUTTON.secondary}>取消</button>
          <button type="button" onClick={handleSubmit} disabled={loading} className={BUTTON.primary}>
            {loading && <LoadingSpinner size={14} />}
            {loading ? '处理中…' : (team ? '确认重新导入' : '确认添加')}
          </button>
        </>
      }
    >
          <ol className="space-y-2 text-sm text-gray-600 dark:text-ink-300">
            <li className="flex gap-2.5">
              <span className="mt-0.5 grid size-5 shrink-0 place-items-center rounded-full bg-gray-100 text-xs font-semibold text-gray-500 dark:bg-ink-800 dark:text-ink-300">1</span>
              <span>在浏览器里用 owner 账号登录 chatgpt.com</span>
            </li>
            <li className="flex gap-2.5">
              <span className="mt-0.5 grid size-5 shrink-0 place-items-center rounded-full bg-gray-100 text-xs font-semibold text-gray-500 dark:bg-ink-800 dark:text-ink-300">2</span>
              <span className="min-w-0 flex-1">
                打开这个链接，复制页面显示的全部 JSON
                <span className="mt-1.5 flex items-center gap-1 rounded-lg border border-gray-200 bg-gray-50 py-1 pl-3 pr-1 dark:border-ink-800 dark:bg-ink-950">
                  <code className="min-w-0 flex-1 break-all text-xs text-blue-600 dark:text-blue-400">{SESSION_URL}</code>
                  <button
                    type="button"
                    onClick={() => void copySessionUrl()}
                    className={cn(
                      'shrink-0 rounded-md p-1.5 transition-colors',
                      copied
                        ? 'text-emerald-600 dark:text-emerald-400'
                        : 'text-gray-400 hover:bg-gray-200 hover:text-gray-700 dark:text-ink-400 dark:hover:bg-ink-800 dark:hover:text-gray-200'
                    )}
                    title={copied ? '已复制' : '复制链接'}
                    aria-label={copied ? '已复制' : '复制 Session 链接'}
                  >
                    {copied ? <Check size={15} /> : <Copy size={15} />}
                  </button>
                  <a
                    href={SESSION_URL}
                    target="_blank"
                    rel="noreferrer"
                    className="shrink-0 rounded-md p-1.5 text-gray-400 transition-colors hover:bg-gray-200 hover:text-gray-700 dark:text-ink-400 dark:hover:bg-ink-800 dark:hover:text-gray-200"
                    title="在新标签页打开"
                    aria-label="在新标签页打开 Session 链接"
                  >
                    <ExternalLink size={15} />
                  </a>
                </span>
              </span>
            </li>
            <li className="flex gap-2.5">
              <span className="mt-0.5 grid size-5 shrink-0 place-items-center rounded-full bg-gray-100 text-xs font-semibold text-gray-500 dark:bg-ink-800 dark:text-ink-300">3</span>
              <span>粘贴到下面</span>
            </li>
          </ol>

          <textarea
            value={json}
            onChange={(e) => setJson(e.target.value)}
            placeholder="粘贴 Session JSON …"
            rows={7}
            aria-label="Session JSON"
            className={cn(INPUT, 'mt-3 resize-none font-mono text-base sm:text-xs')}
          />

          <label className="mt-3 flex items-center gap-2">
            <Globe size={15} className="shrink-0 text-gray-400 dark:text-ink-500" />
            <span className="shrink-0 text-sm text-gray-600 dark:text-ink-300">连接方式</span>
            <select
              value={selectedProxyId ?? ''}
              onChange={(e) => setSelectedProxyId(e.target.value === '' ? null : Number(e.target.value))}
              className={cn(INPUT, 'py-1.5')}
            >
              <option value="">直连</option>
              {proxies.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          </label>

          {error && <p role="alert" className="mt-3 text-sm text-red-600 dark:text-red-400">{error}</p>}
    </DialogFrame>
  );
}
