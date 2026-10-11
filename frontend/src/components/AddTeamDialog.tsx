import { useState, useEffect } from 'react';
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
const STEP_BADGE = 'grid size-7 shrink-0 place-items-center rounded-full bg-blue-600 text-sm font-semibold text-white';

export default function AddTeamDialog({ open, onOpenChange, onSuccess, team = null }: AddTeamDialogProps) {
  const [json, setJson] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState(false);
  const [proxies, setProxies] = useState<Proxy[]>([]);
  const [selectedProxyId, setSelectedProxyId] = useState<number | null>(null);

  const copySessionUrl = async () => {
    try {
      await navigator.clipboard.writeText(SESSION_URL);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  useEffect(() => {
    if (open) {
      setJson('');
      setError('');
      setSelectedProxyId(team?.proxy_id ?? null);
      setCopied(false);
      void copySessionUrl();
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
    <DialogFrame
      open={open}
      onOpenChange={onOpenChange}
      size="xl"
      title={team ? `重新导入 ${team.name}` : '添加 Team'}
      footer={
        <>
          <button type="button" onClick={() => onOpenChange(false)} className={BUTTON.secondary}>取消</button>
          <button type="button" onClick={handleSubmit} disabled={loading} className={BUTTON.primary}>
            {loading && <LoadingSpinner size={14} />}
            {loading ? '处理中…' : (team ? '重新导入' : '添加')}
          </button>
        </>
      }
    >
          <ol className="space-y-5 text-base leading-7 text-gray-800 dark:text-gray-100">
            <li className="flex gap-3">
              <span className={STEP_BADGE}>1</span>
              <div className="min-w-0 flex-1">
                <p>登录 ChatGPT Owner 账号，打开</p>
                <div className="mt-2.5 flex items-center gap-1 rounded-lg bg-blue-50 py-1.5 pl-4 pr-1.5 ring-1 ring-inset ring-blue-100 dark:bg-blue-500/10 dark:ring-blue-500/20">
                  <a
                    href={SESSION_URL}
                    target="_blank"
                    rel="noreferrer"
                    onClick={() => void copySessionUrl()}
                    className="min-w-0 flex-1 break-all font-mono text-sm text-blue-700 hover:underline dark:text-blue-300"
                  >
                    {SESSION_URL}
                  </a>
                  {copied && (
                    <span className="flex shrink-0 items-center gap-1 px-1.5 text-sm font-medium text-emerald-600 dark:text-emerald-400">
                      <Check size={16} />已复制
                    </span>
                  )}
                  <button
                    type="button"
                    onClick={() => void copySessionUrl()}
                    className={cn(BUTTON.icon, 'text-blue-600 hover:bg-blue-100 hover:text-blue-800 dark:text-blue-300 dark:hover:bg-blue-500/20 dark:hover:text-blue-200')}
                    title="复制链接"
                    aria-label="复制链接"
                  >
                    <Copy size={17} />
                  </button>
                  <a
                    href={SESSION_URL}
                    target="_blank"
                    rel="noreferrer"
                    className={cn(BUTTON.icon, 'text-blue-600 hover:bg-blue-100 hover:text-blue-800 dark:text-blue-300 dark:hover:bg-blue-500/20 dark:hover:text-blue-200')}
                    title="打开链接"
                    aria-label="打开链接"
                  >
                    <ExternalLink size={17} />
                  </a>
                </div>
              </div>
            </li>
            <li className="flex gap-3">
              <span className={STEP_BADGE}>2</span>
              <p>全选复制展示的 Session</p>
            </li>
            <li className="flex gap-3">
              <span className={STEP_BADGE}>3</span>
              <div className="min-w-0 flex-1">
                <p>粘贴</p>
                <textarea
                  value={json}
                  onChange={(e) => setJson(e.target.value)}
                  placeholder="请输入"
                  rows={8}
                  aria-label="Session JSON"
                  className={cn(INPUT, 'mt-2.5 resize-none px-4 py-3 font-mono sm:text-sm')}
                />
              </div>
            </li>
          </ol>

          <label className="mt-6 flex items-center gap-3 border-t border-gray-100 pt-5 dark:border-ink-800">
            <Globe size={18} className="shrink-0 text-gray-400 dark:text-ink-500" />
            <span className="shrink-0 text-base text-gray-800 dark:text-gray-100">连接方式</span>
            <select
              value={selectedProxyId ?? ''}
              onChange={(e) => setSelectedProxyId(e.target.value === '' ? null : Number(e.target.value))}
              className={INPUT}
            >
              <option value="">直连</option>
              {proxies.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          </label>

          {error && <p role="alert" className="mt-4 text-sm text-red-600 dark:text-red-400">{error}</p>}
    </DialogFrame>
  );
}
