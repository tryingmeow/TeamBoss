import { type ReactNode, useEffect, useId, useRef, useState } from 'react';
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
import { AlertTriangle, Ban, Check, Copy, Loader2, Plus, RefreshCw, Ticket, X } from 'lucide-react';
import PageShell from '../../components/PageShell';
import PageLoading from '../../components/PageLoading';
import Toast from '../../components/Toast';
import { BUTTON, CARD, INPUT, PILL, TONE } from '../../components/ui';
import { cn } from '../../lib/utils';

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

const STATUSES = ['未使用', '已使用', '已过期', '已停用'] as const;
type TokenStatus = (typeof STATUSES)[number];

const STATUS_TONE: Record<TokenStatus, string> = {
  未使用: TONE.success,
  已使用: TONE.info,
  已过期: TONE.neutral,
  已停用: TONE.neutral,
};

function tokenStatus(token: AccessTokenListItem): TokenStatus {
  if (token.disabled) return '已停用';
  if (token.used_count > 0) return '已使用';
  const expiresAt = token.token_expires_at ? new Date(token.token_expires_at) : null;
  if (expiresAt && expiresAt <= new Date()) return '已过期';
  return '未使用';
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

const NEVER_WORDS = new Set(['never', 'none', 'null', 'infinite', 'infinity', 'forever', '永久', '∞']);

/** Reads a typed duration the way the backend accepts it ("45d", "12 h", "never"); null if it would be rejected. */
function readDuration(text: string): string | null {
  const value = text.trim().toLowerCase();
  if (NEVER_WORDS.has(value)) return durationLabel('never');
  const match = /^(\d+)\s*([mhd])$/.exec(value);
  if (!match || Number(match[1]) <= 0) return null;
  return durationLabel(`${Number(match[1])}${match[2]}`);
}

function redeemDeadline(dateStr: string | null): string {
  return dateStr ? formatDate(dateStr) : '永不过期';
}

const LABEL = 'mb-1.5 block text-sm font-medium text-gray-700 dark:text-ink-200';
const LABEL_HINT = 'font-normal text-gray-400 dark:text-ink-500';

interface DurationFieldProps {
  label: string;
  hint: string;
  presets: string[];
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
}

/**
 * Preset buttons plus a separate custom field. The field only holds what the admin typed, so a
 * preset never shows up there as a raw value like "30d".
 */
function DurationField({ label, hint, presets, value, onChange, placeholder }: DurationFieldProps) {
  const [custom, setCustom] = useState(() => (presets.includes(value) ? '' : value));
  const customId = useId();
  const typed = custom.trim();
  const reading = typed ? readDuration(typed) : null;

  return (
    <div>
      <div className={LABEL}>
        {label} <span className={LABEL_HINT}>· {hint}</span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {presets.map((preset) => (
          <button
            key={preset}
            type="button"
            aria-pressed={value === preset}
            onClick={() => {
              setCustom('');
              onChange(preset);
            }}
            className={cn(
              'h-9 whitespace-nowrap rounded-md px-3 text-xs font-medium transition-colors sm:h-7 sm:px-2.5',
              value === preset
                ? 'bg-blue-600 text-white'
                : 'bg-gray-100 text-gray-700 hover:bg-gray-200 dark:bg-ink-800 dark:text-ink-200 dark:hover:bg-ink-700',
            )}
          >
            {durationLabel(preset)}
          </button>
        ))}
      </div>
      <div className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] items-center gap-x-2 gap-y-1">
        <label htmlFor={customId} className="shrink-0 whitespace-nowrap text-xs text-gray-500 dark:text-ink-400">
          自定义
        </label>
        <input
          id={customId}
          value={custom}
          onChange={(e) => {
            setCustom(e.target.value);
            onChange(e.target.value);
          }}
          placeholder={placeholder}
          className={INPUT}
        />
        {typed && (
          <p className={cn('col-start-2 text-xs', reading ? 'text-gray-500 dark:text-ink-400' : 'text-amber-700 dark:text-amber-400')}>
            {reading ? `= ${reading}` : '格式：数字加 d（天）、h（小时）或 m（分钟），或 never（永不）'}
          </p>
        )}
      </div>
    </div>
  );
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
        ? `确认 ${item.email} 已经在「${item.team_name || item.team_id}」里？兑换码保持已使用，并补上授予时长（${durationLabel(item.grant_expires_in)}）。`
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

  const disableButton = (token: AccessTokenListItem) =>
    !token.disabled && (
      <button
        onClick={() => handleDisable(token.id)}
        title="停用"
        aria-label={`停用兑换码 ${token.token_prefix}`}
        className={cn(BUTTON.icon, 'text-red-600 hover:bg-red-50 hover:text-red-700 dark:text-red-400 dark:hover:bg-red-500/10 dark:hover:text-red-300')}
      >
        <Ban className="size-4" />
      </button>
    );

  const statusCounts = STATUSES.map((status) => ({
    status,
    count: tokens.filter((t) => tokenStatus(t) === status).length,
  }));

  let list: ReactNode;
  if (loading && tokens.length === 0) {
    list = <PageLoading />;
  } else if (error) {
    list = (
      <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-300">
        <p>{error}</p>
        <button onClick={loadTokens} className={cn(BUTTON.secondary, 'mt-3')}>
          重新加载
        </button>
      </div>
    );
  } else if (tokens.length === 0) {
    list = (
      <div className={cn(CARD, 'flex flex-col items-center px-6 py-14 text-center')}>
        <div className="mb-3 flex size-10 items-center justify-center rounded-full bg-blue-50 text-blue-600 dark:bg-blue-500/15 dark:text-blue-400">
          <Ticket className="size-5" />
        </div>
        <p className="font-medium text-gray-900 dark:text-gray-100">还没有兑换码</p>
        <p className="mt-1 max-w-sm text-sm leading-6 text-gray-500 dark:text-ink-400">
          点击上方「生成兑换码」创建一个。用户在
          <a href="/" target="_blank" rel="noreferrer" className="mx-0.5 text-blue-600 hover:underline dark:text-blue-400">
            自助页
          </a>
          输入兑换码，即可自行加入或续期 Team。
        </p>
      </div>
    );
  } else {
    list = (
      <section className={cn(CARD, 'overflow-hidden')}>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-gray-200 px-4 py-3 dark:border-ink-800">
          <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">
            全部兑换码
            <span className="ml-1.5 text-sm font-normal tabular-nums text-gray-500 dark:text-ink-400">{tokens.length}</span>
          </h2>
          <div className="flex flex-wrap gap-1.5">
            {statusCounts.map(({ status, count }) => (
              <span key={status} className={cn(PILL, STATUS_TONE[status])}>
                {status} <span className="tabular-nums">{count}</span>
              </span>
            ))}
          </div>
        </div>

        <ul className="divide-y divide-gray-200 md:hidden dark:divide-ink-800">
          {tokens.map((token) => {
            const status = tokenStatus(token);
            return (
              <li key={token.id} className="px-4 py-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="flex min-w-0 items-center gap-2">
                    <code className="truncate rounded bg-gray-100 px-1.5 py-0.5 font-mono text-xs text-gray-800 dark:bg-ink-800 dark:text-ink-200">
                      {token.token_prefix}…
                    </code>
                    <span className={cn(PILL, STATUS_TONE[status])}>{status}</span>
                  </div>
                  {disableButton(token)}
                </div>
                {token.note && (
                  <p className="mt-0.5 truncate text-sm text-gray-700 dark:text-ink-200" title={token.note}>
                    {token.note}
                  </p>
                )}
                <dl className="mt-1.5 grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-gray-800 dark:text-ink-200">
                  {[
                    ['授予', durationLabel(token.grant_expires_in)],
                    ['截止', redeemDeadline(token.token_expires_at)],
                    ['创建', formatDate(token.created_at)],
                    ['兑换', formatDate(token.last_used_at)],
                  ].map(([term, value]) => (
                    <div key={term} className="flex min-w-0 gap-1.5">
                      <dt className="shrink-0 text-gray-500 dark:text-ink-400">{term}</dt>
                      <dd className="min-w-0 tabular-nums">{value}</dd>
                    </div>
                  ))}
                </dl>
              </li>
            );
          })}
        </ul>

        <div className="hidden overflow-x-auto md:block">
          <table className="w-full min-w-[52rem] text-sm">
            <thead className="bg-gray-50 text-left text-xs font-medium text-gray-500 dark:bg-ink-950/40 dark:text-ink-400">
              <tr className="[&>th]:whitespace-nowrap [&>th]:px-4 [&>th]:py-2.5 [&>th]:font-medium">
                <th>兑换码</th>
                <th>授予时长</th>
                <th>状态</th>
                <th>备注</th>
                <th>兑换截止</th>
                <th>创建时间</th>
                <th>兑换时间</th>
                <th className="text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 dark:divide-ink-800">
              {tokens.map((token) => {
                const status = tokenStatus(token);
                return (
                  <tr key={token.id} className="transition-colors hover:bg-gray-50 dark:hover:bg-ink-800/40">
                    <td className="whitespace-nowrap px-4 py-2.5">
                      <code className="rounded bg-gray-100 px-1.5 py-0.5 font-mono text-xs text-gray-800 dark:bg-ink-800 dark:text-ink-200">
                        {token.token_prefix}…
                      </code>
                    </td>
                    <td className="whitespace-nowrap px-4 py-2.5 text-gray-700 dark:text-ink-200">
                      {durationLabel(token.grant_expires_in)}
                    </td>
                    <td className="px-4 py-2.5">
                      <span className={cn(PILL, STATUS_TONE[status])}>{status}</span>
                    </td>
                    <td className="max-w-[16rem] px-4 py-2.5 text-gray-700 dark:text-ink-200">
                      {token.note ? (
                        <span className="block truncate" title={token.note}>{token.note}</span>
                      ) : (
                        <span className="text-gray-400 dark:text-ink-500">—</span>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2.5 text-xs tabular-nums text-gray-600 dark:text-ink-300">
                      {redeemDeadline(token.token_expires_at)}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2.5 text-xs tabular-nums text-gray-600 dark:text-ink-300">
                      {formatDate(token.created_at)}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2.5 text-xs tabular-nums text-gray-600 dark:text-ink-300">
                      {formatDate(token.last_used_at)}
                    </td>
                    <td className="px-4 py-1 text-right">{disableButton(token)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>
    );
  }

  return (
    <PageShell
      title="兑换码"
      description="一次性兑换码：用户在自助页输入后，自行加入或续期 Team。"
      actions={
        <>
          <button
            onClick={() => setShowCreate((v) => !v)}
            aria-expanded={showCreate}
            className={BUTTON.primary}
          >
            <Plus className="size-4" />
            生成兑换码
          </button>
          <button type="button" onClick={loadTokens} disabled={loading} className={BUTTON.secondary}>
            <RefreshCw className={cn('size-4', loading && 'animate-spin')} />
            刷新
          </button>
        </>
      }
    >
      <div className="space-y-6">
        {showCreate && (
          <section className={cn(CARD, 'p-4 sm:p-5')}>
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">生成兑换码</h2>
                <p className="mt-0.5 text-sm text-gray-500 dark:text-ink-400">
                  每个兑换码只能兑换一次，完整兑换码只在生成后显示一次。
                </p>
              </div>
              <button
                onClick={() => {
                  setShowCreate(false);
                  setCreated(null);
                }}
                aria-label="关闭"
                className={cn(BUTTON.icon, '-mr-2 -mt-1.5')}
              >
                <X className="size-4" />
              </button>
            </div>

            <div className="mt-4 grid gap-4 sm:grid-cols-2">
              <DurationField
                label="授予时长"
                hint="兑换后可用多久"
                presets={GRANT_PRESETS}
                value={grant}
                onChange={setGrant}
                placeholder="如 45d、12h"
              />
              <DurationField
                label="兑换有效期"
                hint="过期未兑换即作废"
                presets={TTL_PRESETS}
                value={ttl}
                onChange={setTtl}
                placeholder="如 3d，留空按 7 天"
              />
            </div>

            <label className="mt-4 block">
              <span className={LABEL}>
                备注 <span className={LABEL_HINT}>· 仅内部可见</span>
              </span>
              <input
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="给谁的 / 什么用途"
                className={INPUT}
              />
            </label>

            <button onClick={handleCreate} disabled={creating} className={cn(BUTTON.primary, 'mt-4')}>
              {creating ? <Loader2 className="size-4 animate-spin" /> : <Plus className="size-4" />}
              {creating ? '生成中…' : '生成'}
            </button>

            {created && (
              <div className="mt-4 rounded-lg border border-emerald-200 bg-emerald-50 p-3 dark:border-emerald-500/30 dark:bg-emerald-500/10">
                <div className="text-sm font-medium text-emerald-800 dark:text-emerald-300">
                  已生成，请立即复制
                </div>
                <div className="mt-0.5 text-xs text-emerald-700 dark:text-emerald-300/80">
                  授予 {durationLabel(created.grant_expires_in)} ·{' '}
                  {created.token_expires_at ? `${formatDate(created.token_expires_at)} 前有效` : '永不过期'}
                </div>
                <div className="mt-2 flex items-center gap-2">
                  <code className="min-w-0 flex-1 select-all break-all rounded-lg border border-emerald-200 bg-white px-3 py-2 font-mono text-sm text-gray-900 dark:border-emerald-500/20 dark:bg-ink-950 dark:text-gray-100">
                    {created.token}
                  </code>
                  <button
                    onClick={handleCopyToken}
                    title="复制完整兑换码"
                    aria-label="复制完整兑换码"
                    className="inline-flex size-9 shrink-0 items-center justify-center rounded-lg bg-emerald-600 text-white transition-colors hover:bg-emerald-700"
                  >
                    {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
                  </button>
                </div>
              </div>
            )}
          </section>
        )}

        {pending.length > 0 && (
          <section className={cn(CARD, 'overflow-hidden border-amber-300 dark:border-amber-500/40')}>
            <div className="border-b border-amber-200 bg-amber-50 px-4 py-3 dark:border-amber-500/20 dark:bg-amber-500/10">
              <h2 className="flex items-center gap-2 text-base font-semibold text-amber-900 dark:text-amber-200">
                <AlertTriangle className="size-4 shrink-0" />
                待确认的兑换
                <span className="tabular-nums">{pending.length}</span>
              </h2>
              <p className="mt-1 text-sm text-amber-800 dark:text-amber-300/80">
                兑换时邀请结果未能确认，兑换码已锁定。请核对该邮箱在 Team 里的真实状态后再处理。
              </p>
            </div>
            <ul className="divide-y divide-gray-200 dark:divide-ink-800">
              {pending.map((item) => (
                <li key={item.id} className="flex flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center">
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium text-gray-900 dark:text-gray-100" title={item.email}>
                      {item.email}
                    </div>
                    <div className="mt-0.5 text-xs text-gray-600 dark:text-ink-300">
                      {item.team_name || item.team_id} · 授予 {durationLabel(item.grant_expires_in)} ·{' '}
                      <code className="font-mono">{item.token_prefix}…</code> · {formatDate(item.created_at)}
                    </div>
                    <div className="mt-0.5 text-xs text-gray-500 dark:text-ink-400">
                      最新成员快照：{item.seen_in_cached_snapshot ? '已找到' : '未找到'}
                      {item.cache_updated_at ? `（${formatDate(item.cache_updated_at)}）` : ''}
                      {item.error_message ? ` · ${item.error_message}` : ''}
                    </div>
                  </div>
                  <div className="flex shrink-0 flex-wrap gap-2">
                    <button
                      disabled={resolving === item.id}
                      onClick={() => handleResolve(item, 'success')}
                      className={BUTTON.primary}
                    >
                      确认成功
                    </button>
                    <button
                      disabled={resolving === item.id}
                      onClick={() => handleResolve(item, 'released')}
                      className={BUTTON.secondary}
                    >
                      确认失败并退码
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </section>
        )}

        {list}
      </div>

      {toasts.length > 0 && (
        <div className="fixed bottom-4 right-4 z-[100] flex w-[min(24rem,calc(100vw-2rem))] flex-col gap-2">
          {toasts.map((toast) => (
            <Toast key={toast.id} text={toast.text} type={toast.type} />
          ))}
        </div>
      )}
    </PageShell>
  );
}
