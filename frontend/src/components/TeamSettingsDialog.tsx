import { useEffect, useMemo, useState } from 'react';
import { ArrowLeftRight, ChevronDown, Loader2 } from 'lucide-react';
import type { OveragePolicy, Team, TeamWorkspaceSettings, WorkspaceDefaultSeatType } from '../types';
import {
  checkProxy,
  fetchProxies,
  fetchTeamWorkspaceSettings,
  updateTeamDefaultSeatType,
  updateTeamOveragePolicy,
  updateTeamProxy,
  type Proxy,
} from '../api/client';
import { OVERAGE_POLICY_OPTIONS, SEAT_STYLE, formatSeatTypeLabel, parseOveragePolicy } from '../lib/seatType';
import ConfirmDialog from './ConfirmDialog';
import DialogFrame from './DialogFrame';
import { cn } from '../lib/utils';

interface TeamSettingsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  teamId: string;
  /** Shown in the description so the operator can tell which Team this dialog belongs to. */
  teamName?: string;
  ownerEmail?: string;
  currentProxyId: number | null;
  /** The Team's overage policy as last loaded. */
  overagePolicy?: OveragePolicy | null;
  initialSettings?: TeamWorkspaceSettings | null;
  onChanged?: (settings: TeamWorkspaceSettings) => void;
  onProxyChanged?: (proxyId: number | null) => void;
  /** The server's Team after the overage policy changed. */
  onTeamUpdated?: (team: Team) => void;
}

/** The workspace default is ChatGPT or Codex only; anything else upstream reports reads as ChatGPT. */
function normalizeWorkspaceSeatType(value: string | null | undefined): WorkspaceDefaultSeatType {
  return value === 'usage_based' ? 'usage_based' : 'default';
}

export default function TeamSettingsDialog({
  open,
  onOpenChange,
  teamId,
  teamName,
  ownerEmail,
  currentProxyId,
  overagePolicy,
  initialSettings,
  onChanged,
  onProxyChanged,
  onTeamUpdated,
}: TeamSettingsDialogProps) {
  const [settings, setSettings] = useState<TeamWorkspaceSettings | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [error, setError] = useState('');

  const [proxies, setProxies] = useState<Proxy[]>([]);
  const [selectedProxyId, setSelectedProxyId] = useState<number | null>(currentProxyId);
  const [savingProxy, setSavingProxy] = useState(false);

  const [policy, setPolicy] = useState<OveragePolicy>(() => parseOveragePolicy(overagePolicy));
  const [savingPolicy, setSavingPolicy] = useState<OveragePolicy | null>(null);
  const [policyError, setPolicyError] = useState('');

  useEffect(() => {
    if (!open) return;
    setPolicy(parseOveragePolicy(overagePolicy));
    setPolicyError('');
    // 只在打开时同步；打开期间父组件刷新不该把管理员刚点的选项拍回去。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, teamId]);

  const handlePolicyChange = async (next: OveragePolicy) => {
    if (next === policy || savingPolicy) return;
    const previous = policy;
    setPolicy(next);
    setSavingPolicy(next);
    setPolicyError('');
    try {
      const updated = await updateTeamOveragePolicy(teamId, next);
      setPolicy(parseOveragePolicy(updated.overage_policy ?? next));
      onTeamUpdated?.(updated);
    } catch (err) {
      setPolicy(previous);
      setPolicyError(err instanceof Error ? err.message : '保存失败');
    } finally {
      setSavingPolicy(null);
    }
  };

  useEffect(() => {
    if (!open) return;

    let cancelled = false;
    if (initialSettings) {
      setSettings({ ...initialSettings, default_seat_type: normalizeWorkspaceSeatType(initialSettings.default_seat_type) });
    }
    setLoading(true);
    setError('');
    setSelectedProxyId(currentProxyId);

    fetchTeamWorkspaceSettings(teamId)
      .then((data) => {
        if (!cancelled) setSettings({ ...data, default_seat_type: normalizeWorkspaceSeatType(data.default_seat_type) });
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : '加载失败');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    fetchProxies()
      .then(async (list) => {
        if (cancelled) return;
        setProxies(list);
        if (currentProxyId == null) return;
        const current = list.find((p) => p.id === currentProxyId);
        if (!current || current.status !== 'unknown') return;
        try {
          const result = await checkProxy(currentProxyId);
          if (!cancelled) {
            setProxies((prev) =>
              prev.map((p) =>
                p.id === currentProxyId
                  ? { ...p, status: result.status, last_check_at: result.last_check_at }
                  : p,
              ),
            );
          }
        } catch {
          if (!cancelled) {
            setProxies((prev) => prev.map((p) => (p.id === currentProxyId ? { ...p, status: 'error' } : p)));
          }
        }
      })
      .catch(() => {});

    return () => {
      cancelled = true;
    };
    // initialSettings 特意不放进依赖:它是 TeamCard 的 workspaceSettings state,
    // 每次 syncTeam(含结算轮询的每一次 tick)都会给出一个内容可能完全相同的新
    // 对象引用。放进依赖会导致弹窗打开期间只要父组件刷新就整段重新拉取,
    // 把"查询中..."重新拍回来。只在 open/teamId/currentProxyId 变化时重新拉取,
    // 弹窗刚打开那一次仍然用 initialSettings 做首屏内容(如果有的话)。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, teamId, currentProxyId]);

  const currentSeat = useMemo<WorkspaceDefaultSeatType>(
    () => normalizeWorkspaceSeatType(settings?.default_seat_type),
    [settings]
  );
  const nextSeat: WorkspaceDefaultSeatType = currentSeat === 'usage_based' ? 'default' : 'usage_based';

  const handleConfirmSwitch = async () => {
    setSaving(true);
    setError('');
    try {
      const updated = await updateTeamDefaultSeatType(teamId, nextSeat);
      const normalized = { ...updated, default_seat_type: normalizeWorkspaceSeatType(updated.default_seat_type) };
      setSettings(normalized);
      setConfirmOpen(false);
      onChanged?.(normalized);
    } catch (err) {
      setError(err instanceof Error ? err.message : '切换失败');
    } finally {
      setSaving(false);
    }
  };

  const applyProxyCheckResult = (proxyId: number, status: string, lastCheckAt?: string) => {
    setProxies((prev) =>
      prev.map((p) =>
        p.id === proxyId ? { ...p, status, ...(lastCheckAt ? { last_check_at: lastCheckAt } : {}) } : p,
      ),
    );
  };

  const handleProxyChange = async (newId: number | null) => {
    setSelectedProxyId(newId);
    setSavingProxy(true);
    setError('');
    try {
      await updateTeamProxy(teamId, newId);
      onProxyChanged?.(newId);
      if (newId != null) {
        try {
          const result = await checkProxy(newId);
          applyProxyCheckResult(newId, result.status, result.last_check_at);
        } catch {
          applyProxyCheckResult(newId, 'error');
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '切换失败');
      setSelectedProxyId(currentProxyId);
    } finally {
      setSavingProxy(false);
    }
  };

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen) setConfirmOpen(false);
    onOpenChange(nextOpen);
  };

  const proxyStatusDot = (p: Proxy) => {
    if (p.status === 'ok') return 'bg-emerald-500';
    if (p.status === 'error') return 'bg-red-400';
    return 'bg-gray-400';
  };

  const selectedProxy = proxies.find((p) => p.id === selectedProxyId);

  return (
    <>
      <DialogFrame
        open={open}
        onOpenChange={handleOpenChange}
        title="Team 设置"
        description={
          (teamName || ownerEmail) && (
            <span className="flex min-w-0 flex-wrap items-baseline gap-x-2">
              {teamName && <span className="font-medium text-gray-900 dark:text-gray-100">{teamName}</span>}
              {ownerEmail && (
                <span className="min-w-0 truncate text-gray-500 dark:text-ink-400" title={ownerEmail}>{ownerEmail}</span>
              )}
            </span>
          )
        }
        size="md"
      >
        <div className="divide-y divide-gray-100 rounded-lg border border-gray-200 dark:divide-ink-800 dark:border-ink-800">
          <fieldset className="p-3" disabled={savingPolicy !== null}>
            <legend className="sr-only">超员策略</legend>
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="text-sm font-medium text-gray-900 dark:text-gray-100" aria-hidden>超员策略</div>
                <div className="mt-0.5 text-xs text-gray-500 dark:text-ink-400">ChatGPT / Premium 席位满了时，加人或切换怎么办</div>
              </div>
              {savingPolicy && <Loader2 size={14} className="shrink-0 animate-spin text-gray-400" aria-label="保存中" />}
            </div>
            <div className="mt-2.5 grid gap-1.5">
              {OVERAGE_POLICY_OPTIONS.map((option) => (
                <label
                  key={option.value}
                  className={cn(
                    'flex cursor-pointer items-start gap-2.5 rounded-lg border px-3 py-2 transition-colors',
                    policy === option.value
                      ? 'border-blue-500 bg-blue-50/70 dark:border-blue-400/60 dark:bg-blue-500/10'
                      : 'border-gray-200 hover:bg-gray-50 dark:border-ink-800 dark:hover:bg-ink-850',
                    savingPolicy !== null && 'cursor-wait',
                  )}
                >
                  <input
                    type="radio"
                    name={`overage-policy-${teamId}`}
                    value={option.value}
                    checked={policy === option.value}
                    onChange={() => void handlePolicyChange(option.value)}
                    className="mt-0.5 size-4 shrink-0 accent-blue-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500/50 dark:accent-blue-500"
                  />
                  <span className="min-w-0">
                    <span className="block text-sm font-medium text-gray-900 dark:text-gray-100">{option.label}</span>
                    <span className="block text-xs leading-5 text-gray-500 dark:text-ink-400">{option.hint}</span>
                  </span>
                </label>
              ))}
            </div>
            {policyError && <p role="alert" className="mt-2 text-sm text-red-600 dark:text-red-400">{policyError}</p>}
          </fieldset>

          <div className="flex items-center justify-between gap-3 p-3">
            <div className="min-w-0">
              <div className="text-sm font-medium text-gray-900 dark:text-gray-100">默认邀请席位</div>
              <div className="mt-0.5 text-xs text-gray-500 dark:text-ink-400">成员邀请默认使用的席位</div>
            </div>
            <button
              type="button"
              disabled={loading || !settings}
              onClick={() => setConfirmOpen(true)}
              className={cn(
                'inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border px-3 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50',
                SEAT_STYLE[currentSeat].surface,
                SEAT_STYLE[currentSeat].text,
                'hover:brightness-95 dark:hover:brightness-125',
              )}
              aria-label={`默认邀请席位：${formatSeatTypeLabel(currentSeat)}，点击切换`}
              title="切换"
            >
              {loading ? (
                <>
                  <Loader2 size={14} className="animate-spin" />
                  查询中…
                </>
              ) : (
                <>
                  {formatSeatTypeLabel(currentSeat)}
                  <ArrowLeftRight size={13} className="opacity-70" />
                </>
              )}
            </button>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 p-3">
            <div className="min-w-0 flex-1 basis-40">
              <label htmlFor="team-proxy" className="text-sm font-medium text-gray-900 dark:text-gray-100">代理</label>
              <div className="mt-0.5 text-xs text-gray-500 dark:text-ink-400">访问 ChatGPT 时使用的网络</div>
            </div>
            <div className="relative w-full sm:w-44">
              <span className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2">
                {savingProxy ? (
                  <Loader2 size={12} className="animate-spin text-gray-400" />
                ) : (
                  <span
                    className={cn(
                      'block size-2 rounded-full',
                      selectedProxyId == null ? 'bg-emerald-500' : proxyStatusDot(selectedProxy ?? ({ status: 'unknown' } as Proxy)),
                    )}
                  />
                )}
              </span>
              <select
                id="team-proxy"
                value={selectedProxyId ?? ''}
                onChange={(e) => {
                  const val = e.target.value;
                  handleProxyChange(val === '' ? null : Number(val));
                }}
                disabled={savingProxy}
                className="h-9 w-full cursor-pointer appearance-none truncate rounded-lg border border-gray-200 bg-white pl-7 pr-8 text-sm text-gray-700 transition-colors hover:border-gray-300 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/30 disabled:opacity-50 dark:border-ink-800 dark:bg-ink-950 dark:text-gray-200 dark:hover:border-ink-700"
              >
                <option value="">直连</option>
                {proxies.map((p) => (
                  <option key={p.id} value={p.id}>{p.name}</option>
                ))}
              </select>
              <ChevronDown size={14} className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 dark:text-ink-500" />
            </div>
          </div>
        </div>

        {error && <p className="mt-3 text-sm text-red-600 dark:text-red-400">{error}</p>}
      </DialogFrame>

      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="切换默认邀请席位"
        message={`成员邀请的默认席位将从 ${formatSeatTypeLabel(currentSeat)} 改为 ${formatSeatTypeLabel(nextSeat)}。`}
        confirmLabel="切换"
        loading={saving}
        onConfirm={handleConfirmSwitch}
      />
    </>
  );
}
