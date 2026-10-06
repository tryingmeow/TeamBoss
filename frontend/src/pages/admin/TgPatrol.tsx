import { type ReactNode, useState, useEffect, useRef } from 'react';
import {
  fetchPatrolStatus,
  updatePatrolSettings,
  runPatrol,
  activatePatrol,
  fetchTgConfig,
  updateTgConfig,
  fetchTgUsers,
  updateTgUser,
  deleteTgUser,
  fetchTgCodes,
  createTgCode,
  deleteTgCode,
  sendTgSummary,
} from '../../api/client';
import { AlertTriangle, Copy, Plus, Send, ShieldCheck, Trash2 } from 'lucide-react';
import * as Dialog from '@radix-ui/react-dialog';
import PageShell from '../../components/PageShell';
import PageLoading from '../../components/PageLoading';
import SegmentedTabs from '../../components/SegmentedTabs';
import Toast from '../../components/Toast';
import Switch from '../../components/Switch';
import { BUTTON, CARD, INPUT, PILL, TONE } from '../../components/ui';
import { formatDateSafe } from '../../lib/formatDate';
import { cn } from '../../lib/utils';

interface ToastMessage {
  id: number;
  text: string;
  type: 'success' | 'error';
}

interface PatrolStatus {
  kick_enabled: boolean;
  baseline_at: string | null;
  sync_interval_minutes: number;
  exempt_team_ids: string[];
  teams: Array<{
    team_id: string;
    name: string;
    codex_enabled: boolean;
    seats_entitled: number;
    active_chatgpt: number;
    over_by: number;
    risk: 'ok' | 'watch' | 'over';
    detected_over: Array<{
      email: string;
      user_id: string;
      seat_type: string;
      first_seen_at: string | null;
    }>;
  }>;
}

interface TgConfig {
  enabled: boolean;
  token_set: boolean;
  bot_username: string | null;
  polling: boolean;
  summary_enabled: boolean;
  summary_interval_minutes: number;
  summary_last_sent_at: string | null;
}

interface TgUser {
  id: number;
  chat_id: string;
  username: string;
  note: string;
  disabled: boolean;
  paired_at: string;
}

interface TgCode {
  id: number;
  code: string;
  note: string;
  expires_at: string;
  used_by_chat_id: string | null;
  used_at: string | null;
  disabled: boolean;
  created_at: string;
}

type ToastType = 'success' | 'error';

/**
 * 踢人规则只写一遍，卡片和确认弹窗共用——两处各写一份会各自漂移。
 * 巡逻只处理同步刚刷新成功的 Team，同步失败或已挂起的 Team 这一轮完全不碰。
 */
function patrolRuleText(intervalMinutes: number): string {
  return `每 ${intervalMinutes} 分钟巡逻一次，只处理未豁免的 Team：ChatGPT 席位超出时，移除绕过 TeamBoss 新加入的成员，最多移除超出的人数；绕过 TeamBoss 占用 Premium 席位的成员，不管是否超出都会移除（每个 Premium 席位都按月扣费）。`;
}

const DIALOG_OVERLAY = 'fixed inset-0 z-50 bg-black/60 backdrop-blur-sm';
const DIALOG_CONTENT =
  'fixed left-1/2 top-1/2 z-50 max-h-[calc(100dvh-2rem)] w-[calc(100vw-2rem)] max-w-md -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-xl border border-gray-200 bg-white p-6 shadow-2xl dark:border-ink-800 dark:bg-ink-900';
const DIALOG_TITLE = 'text-lg font-semibold text-gray-900 dark:text-gray-100';
const DIALOG_TEXT = 'mt-2 text-sm leading-6 text-gray-600 dark:text-ink-300';
const DIALOG_ACTIONS = 'mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end';
const MUTED = 'text-gray-500 dark:text-ink-400';

function SectionHeader({ title, description, aside }: { title: string; description?: ReactNode; aside?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1">
      <div className="min-w-0 flex-1 basis-64">
        <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">{title}</h2>
        {description && <p className={cn('mt-1 text-sm leading-6', MUTED)}>{description}</p>}
      </div>
      {aside}
    </div>
  );
}

/** One setting: label + explanation on the left, its control on the right (wraps below on narrow screens). */
function SettingRow({ title, description, control }: { title: ReactNode; description: ReactNode; control: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 py-4 first:pt-0 last:pb-0">
      <div className="min-w-0 flex-1 basis-64">
        <div className="flex flex-wrap items-center gap-2 text-sm font-medium text-gray-900 dark:text-gray-100">{title}</div>
        <div className={cn('mt-1 text-sm leading-6', MUTED)}>{description}</div>
      </div>
      {control}
    </div>
  );
}

function Toasts({ toasts }: { toasts: ToastMessage[] }) {
  if (toasts.length === 0) return null;
  return (
    <div className="fixed bottom-4 right-4 z-[100] flex w-[min(24rem,calc(100vw-2rem))] flex-col gap-2">
      {toasts.map((toast) => (
        <Toast key={toast.id} text={toast.text} type={toast.type} />
      ))}
    </div>
  );
}

function PatrolSection() {
  const [status, setStatus] = useState<PatrolStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [activating, setActivating] = useState(false);
  const [showKickConfirm, setShowKickConfirm] = useState(false);
  const [toasts, setToasts] = useState<ToastMessage[]>([]);
  const toastIdRef = useRef(0);
  const toastTimersRef = useRef<Set<ReturnType<typeof setTimeout>>>(new Set());
  const [selectedTeams, setSelectedTeams] = useState<Set<string>>(new Set());
  const [pendingExempt, setPendingExempt] = useState<{ team: PatrolStatus['teams'][number]; willExempt: boolean } | null>(null);
  const [savingExempt, setSavingExempt] = useState(false);

  const showToast = (text: string, type: ToastType = 'success') => {
    const id = ++toastIdRef.current;
    setToasts((prev) => [...prev, { id, text, type }]);
    const timer = setTimeout(() => {
      toastTimersRef.current.delete(timer);
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 3500);
    toastTimersRef.current.add(timer);
  };

  useEffect(() => () => {
    toastTimersRef.current.forEach((timer) => clearTimeout(timer));
    toastTimersRef.current.clear();
  }, []);

  useEffect(() => {
    loadStatus();
  }, []);

  const loadStatus = async () => {
    try {
      setLoading(true);
      const data = await fetchPatrolStatus();
      setStatus(data);
      setSelectedTeams(new Set(data.exempt_team_ids));
    } catch (err) {
      console.error(err);
      showToast('加载巡逻状态失败', 'error');
    } finally {
      setLoading(false);
    }
  };

  const handleToggleKickEnabled = async () => {
    if (status && !status.kick_enabled) {
      setShowKickConfirm(true);
      return;
    }
    try {
      await updatePatrolSettings({
        kick_enabled: false,
      });
      await loadStatus();
      showToast('巡逻自动踢人已关闭');
    } catch (err) {
      console.error(err);
      showToast('更新踢人设置失败', 'error');
    }
  };

  const handleActivatePatrol = async () => {
    try {
      setActivating(true);
      const result = await activatePatrol();
      setShowKickConfirm(false);
      await loadStatus();
      showToast(`已保护现有成员并开启自动踢人（保护 ${result.grandfathered + result.backfilled} 人）`);
    } catch (err) {
      console.error(err);
      showToast(err instanceof Error ? err.message : '开启自动踢人失败', 'error');
    } finally {
      setActivating(false);
    }
  };

  const handleRunDryRun = async () => {
    try {
      setRunning(true);
      const result = await runPatrol({ dry_run: true });
      showToast(`演练完成：${result.would_kick} 人会被踢`);
      await loadStatus();
    } catch (err) {
      console.error(err);
      showToast('演练失败', 'error');
    } finally {
      setRunning(false);
    }
  };

  const requestToggleExempt = (team: PatrolStatus['teams'][number]) => {
    if (team.codex_enabled) return;
    setPendingExempt({ team, willExempt: !selectedTeams.has(team.team_id) });
  };

  const confirmToggleExempt = async () => {
    if (!pendingExempt) return;
    try {
      setSavingExempt(true);
      // The PATCH replaces the whole list. Apply this one toggle to the server's current
      // list, not to the one this page loaded, so a tab left open cannot silently drop an
      // exemption added elsewhere (which would expose that Team to auto-kick) or revive one.
      const fresh = await fetchPatrolStatus();
      const next = new Set(fresh.exempt_team_ids);
      if (pendingExempt.willExempt) next.add(pendingExempt.team.team_id);
      else next.delete(pendingExempt.team.team_id);
      const unchanged =
        next.size === fresh.exempt_team_ids.length && fresh.exempt_team_ids.every((id) => next.has(id));
      if (!unchanged) await updatePatrolSettings({ exempt_team_ids: Array.from(next) });
      setStatus({ ...fresh, exempt_team_ids: Array.from(next) });
      setSelectedTeams(next);
      showToast(pendingExempt.willExempt ? '已加入豁免' : '已移出豁免');
      setPendingExempt(null);
    } catch (err) {
      console.error(err);
      showToast('更新豁免失败', 'error');
    } finally {
      setSavingExempt(false);
    }
  };

  if (loading) {
    return (
      <>
        <PageLoading />
        <Toasts toasts={toasts} />
      </>
    );
  }

  if (!status) {
    return (
      <>
        <div className={cn(CARD, 'px-6 py-14 text-center text-sm', MUTED)}>无法加载巡逻状态</div>
        <Toasts toasts={toasts} />
      </>
    );
  }

  const exemptCount = status.teams.filter((t) => t.codex_enabled || selectedTeams.has(t.team_id)).length;

  return (
    <div className="space-y-6">
      <section className={cn(CARD, 'p-4 sm:p-6')}>
        <SectionHeader title="巡逻自动踢人" description="防止有人绕过 TeamBoss 直接往 Team 里加人、占用付费席位。" />
        <div className="mt-5 divide-y divide-gray-100 dark:divide-ink-800">
          <SettingRow
            title={
              <>
                自动踢人
                <span className={cn(PILL, status.kick_enabled ? TONE.warning : TONE.neutral)}>
                  {status.kick_enabled ? '已开启' : '已关闭'}
                </span>
              </>
            }
            description={
              <>
                {patrolRuleText(status.sync_interval_minutes)}
                {status.baseline_at && (
                  <span className="mt-1 block text-xs">
                    上次保护现有成员：{formatDateSafe(status.baseline_at, 'MM-dd HH:mm:ss')}
                  </span>
                )}
              </>
            }
            control={
              <Switch
                checked={status.kick_enabled}
                onChange={handleToggleKickEnabled}
                disabled={activating}
                aria-label="巡逻自动踢人"
              />
            }
          />
          <SettingRow
            title="演练空跑"
            description="按当前规则预览会被踢的人数，不会真正踢人。"
            control={
              <button onClick={handleRunDryRun} disabled={running} className={BUTTON.secondary}>
                {running ? '运行中…' : '演练空跑'}
              </button>
            }
          />
        </div>
      </section>

      <section className={cn(CARD, 'p-4 sm:p-6')}>
        <SectionHeader
          title="Team 豁免"
          description="点击 Team 切换豁免。豁免的 Team 巡逻不踢人（超员和外部 Premium 成员都不踢），发现外部人员占用 Premium 席位时只发 TG 提醒；开启 Codex 的 Team 自动豁免。"
          aside={
            <span className="shrink-0 whitespace-nowrap pt-0.5 text-xs tabular-nums text-gray-500 dark:text-ink-400">
              已豁免 {exemptCount} / {status.teams.length}
            </span>
          }
        />
        <div className="mt-4 flex flex-wrap gap-2">
          {status.teams.map((team) => {
            const exempt = selectedTeams.has(team.team_id);
            const isProtected = team.codex_enabled || exempt;
            const cls = isProtected
              ? 'border-emerald-500 text-emerald-700 hover:bg-emerald-500/10 dark:text-emerald-300'
              : team.risk === 'over'
                ? 'border-red-500 text-red-600 hover:bg-red-500/10 dark:text-red-300'
                : team.risk === 'watch'
                  ? 'border-amber-500 text-amber-700 hover:bg-amber-500/10 dark:text-amber-300'
                  : 'border-gray-300 text-gray-700 hover:bg-gray-100 dark:border-ink-700 dark:text-ink-200 dark:hover:bg-ink-800';
            const state = team.codex_enabled
              ? 'Codex 自动豁免'
              : `席位 ${team.active_chatgpt}/${team.seats_entitled}${team.over_by > 0 ? ` · 超额 +${team.over_by}` : ''} · ${
                  exempt ? '已豁免' : team.risk === 'over' ? '超员风险' : team.risk === 'watch' ? '观察' : '正常'
                }`;
            return (
              <button
                key={team.team_id}
                type="button"
                disabled={team.codex_enabled}
                aria-pressed={isProtected}
                onClick={() => requestToggleExempt(team)}
                title={`${team.team_id.slice(0, 8)}… · ${state}`}
                className={cn(
                  'inline-flex min-h-9 max-w-full items-center gap-1.5 rounded-full border-2 bg-transparent px-3 py-1 text-sm font-medium transition-colors disabled:cursor-default disabled:hover:bg-transparent',
                  cls,
                )}
              >
                {isProtected && <ShieldCheck className="size-3.5 shrink-0" aria-hidden />}
                <span className="truncate">{team.name}</span>
                <span className="shrink-0 whitespace-nowrap text-xs font-normal tabular-nums opacity-75">
                  {team.codex_enabled ? 'Codex' : `${team.active_chatgpt}/${team.seats_entitled}`}
                </span>
              </button>
            );
          })}
        </div>
        <div className="mt-4 flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-500 dark:text-ink-400">
          <span className="inline-flex items-center gap-1.5 whitespace-nowrap"><span className="size-2.5 rounded-full border-2 border-emerald-500" />已豁免 / Codex</span>
          <span className="inline-flex items-center gap-1.5 whitespace-nowrap"><span className="size-2.5 rounded-full border-2 border-gray-300 dark:border-ink-600" />正常</span>
          <span className="inline-flex items-center gap-1.5 whitespace-nowrap"><span className="size-2.5 rounded-full border-2 border-amber-500" />观察</span>
          <span className="inline-flex items-center gap-1.5 whitespace-nowrap"><span className="size-2.5 rounded-full border-2 border-red-500" />超员风险</span>
          <span className="whitespace-nowrap">数字 = 在用 ChatGPT 席位 / 总席位</span>
        </div>
      </section>

      <Dialog.Root open={showKickConfirm} onOpenChange={setShowKickConfirm}>
        <Dialog.Portal>
          <Dialog.Overlay className={DIALOG_OVERLAY} />
          <Dialog.Content className={DIALOG_CONTENT}>
            <Dialog.Title className={cn(DIALOG_TITLE, 'flex items-center gap-2')}>
              <AlertTriangle className="size-5 shrink-0 text-amber-500" />
              保护现有成员并开启自动踢人
            </Dialog.Title>
            <Dialog.Description className={DIALOG_TEXT}>
              请先确认各 Team 现在的成员都是你认可的。
              <br />
              <br />
              确认后会实时刷新全部成员，把当前成员和邀请都列为受保护，再开启自动踢人。
              {patrolRuleText(status.sync_interval_minutes)}
            </Dialog.Description>
            <div className={DIALOG_ACTIONS}>
              <Dialog.Close asChild>
                <button className={BUTTON.secondary}>取消</button>
              </Dialog.Close>
              <button onClick={handleActivatePatrol} disabled={activating} className={BUTTON.primary}>
                {activating ? '正在刷新并开启…' : '确认并开启'}
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      <Dialog.Root open={!!pendingExempt} onOpenChange={(o) => !o && setPendingExempt(null)}>
        <Dialog.Portal>
          <Dialog.Overlay className={DIALOG_OVERLAY} />
          <Dialog.Content className={DIALOG_CONTENT}>
            <Dialog.Title className={DIALOG_TITLE}>
              {pendingExempt?.willExempt ? '加入豁免' : '移出豁免'}
            </Dialog.Title>
            <Dialog.Description className={DIALOG_TEXT}>
              {pendingExempt?.willExempt
                ? `把「${pendingExempt?.team.name}」加入豁免后，巡逻不会在这个 Team 踢人：超员和外部 Premium 成员都不踢，只发 TG 提醒。`
                : `把「${pendingExempt?.team.name}」移出豁免后，超员时新加入的外部成员、以及绕过 TeamBoss 占用 Premium 席位的成员可能被自动踢人。`}
            </Dialog.Description>
            <div className={DIALOG_ACTIONS}>
              <button onClick={() => setPendingExempt(null)} className={BUTTON.secondary}>
                取消
              </button>
              <button onClick={confirmToggleExempt} disabled={savingExempt} className={BUTTON.primary}>
                {savingExempt ? '保存中…' : '确认'}
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      <Toasts toasts={toasts} />
    </div>
  );
}

function pairingCodeState(code: TgCode): { label: string; tone: string; when: string } {
  if (code.used_by_chat_id) {
    return { label: '已使用', tone: TONE.info, when: `${formatDateSafe(code.used_at, 'MM-dd HH:mm')} 使用` };
  }
  if (code.disabled) {
    return { label: '已吊销', tone: TONE.neutral, when: '' };
  }
  const expiresAt = new Date(code.expires_at);
  const expired = !Number.isNaN(expiresAt.getTime()) && expiresAt <= new Date();
  return {
    label: expired ? '已过期' : '未使用',
    tone: expired ? TONE.neutral : TONE.success,
    when: `${formatDateSafe(code.expires_at, 'MM-dd HH:mm')} ${expired ? '已过期' : '过期'}`,
  };
}

function TgBotSection() {
  const [config, setConfig] = useState<TgConfig | null>(null);
  const [users, setUsers] = useState<TgUser[]>([]);
  const [codes, setCodes] = useState<TgCode[]>([]);
  const [loading, setLoading] = useState(true);
  const [toasts, setToasts] = useState<ToastMessage[]>([]);
  const toastIdRef = useRef(0);
  const toastTimersRef = useRef<Set<ReturnType<typeof setTimeout>>>(new Set());
  const [tokenInput, setTokenInput] = useState('');
  const [tokenSaving, setTokenSaving] = useState(false);
  const [codeGenerating, setCodeGenerating] = useState(false);
  const [codeNote, setCodeNote] = useState('');
  const [showNewCode, setShowNewCode] = useState<string | null>(null);
  const [summaryInterval, setSummaryInterval] = useState(15);
  const [summarySaving, setSummarySaving] = useState(false);
  const [summarySending, setSummarySending] = useState(false);

  const showToast = (text: string, type: ToastType = 'success') => {
    const id = ++toastIdRef.current;
    setToasts((prev) => [...prev, { id, text, type }]);
    const timer = setTimeout(() => {
      toastTimersRef.current.delete(timer);
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 3500);
    toastTimersRef.current.add(timer);
  };

  useEffect(() => () => {
    toastTimersRef.current.forEach((timer) => clearTimeout(timer));
    toastTimersRef.current.clear();
  }, []);

  useEffect(() => {
    loadData();
  }, []);

  const loadData = async () => {
    try {
      setLoading(true);
      const [configRes, usersRes, codesRes] = await Promise.all([
        fetchTgConfig(),
        fetchTgUsers(),
        fetchTgCodes(),
      ]);
      setConfig(configRes);
      setSummaryInterval(configRes.summary_interval_minutes || 15);
      setUsers(usersRes.users);
      setCodes(codesRes.codes);
    } catch (err) {
      console.error(err);
      showToast('加载 TG 机器人配置失败', 'error');
    } finally {
      setLoading(false);
    }
  };

  const handleToggleEnabled = async () => {
    if (!config) return;
    try {
      await updateTgConfig({ enabled: !config.enabled });
      setConfig({ ...config, enabled: !config.enabled });
      showToast(config.enabled ? '已禁用 TG 机器人' : '已启用 TG 机器人');
    } catch (err) {
      console.error(err);
      showToast('更新机器人状态失败', 'error');
    }
  };

  const handleSaveToken = async () => {
    if (!tokenInput.trim()) {
      showToast('Token 不能为空', 'error');
      return;
    }
    try {
      setTokenSaving(true);
      const result = await updateTgConfig({ token: tokenInput });
      setTokenInput('');
      if (result.bot_changed && result.admin_pairing_code) {
        setShowNewCode(result.admin_pairing_code);
        await loadData();
        showToast('已切换到新机器人，请使用新配对码重新绑定管理员');
      } else {
        const refreshed = await fetchTgConfig();
        setConfig(refreshed);
        showToast('同一机器人 Token 已更新，原绑定已保留');
      }
    } catch (err) {
      console.error(err);
      showToast('保存 Token 失败', 'error');
    } finally {
      setTokenSaving(false);
    }
  };

  const handleToggleSummary = async () => {
    if (!config) return;
    const next = !config.summary_enabled;
    try {
      await updateTgConfig({ summary_enabled: next });
      setConfig({ ...config, summary_enabled: next });
      showToast(next ? '已开启定时摘要' : '已关闭定时摘要');
    } catch (err) {
      console.error(err);
      showToast('更新摘要开关失败', 'error');
    }
  };

  const handleSaveSummaryInterval = async () => {
    if (!Number.isInteger(summaryInterval) || summaryInterval < 5 || summaryInterval > 1440) {
      showToast('摘要间隔必须是 5–1440 分钟', 'error');
      return;
    }
    try {
      setSummarySaving(true);
      await updateTgConfig({ summary_interval_minutes: summaryInterval });
      setConfig((prev) => prev ? { ...prev, summary_interval_minutes: summaryInterval } : null);
      showToast('摘要间隔已保存');
    } catch (err) {
      console.error(err);
      showToast('保存摘要间隔失败', 'error');
    } finally {
      setSummarySaving(false);
    }
  };

  const handleSendSummary = async () => {
    try {
      setSummarySending(true);
      const result = await sendTgSummary();
      setConfig((prev) => prev ? { ...prev, summary_last_sent_at: result.sent_at } : null);
      showToast(`摘要已发送给 ${result.sent} 位管理员`);
    } catch (err) {
      console.error(err);
      showToast('摘要发送失败', 'error');
    } finally {
      setSummarySending(false);
    }
  };

  const handleCreateCode = async () => {
    try {
      setCodeGenerating(true);
      const result = await createTgCode({ note: codeNote || undefined });
      setShowNewCode(result.code);
      setCodes((prev) => [result, ...prev]);
      setCodeNote('');
      showToast('管理员配对码已生成');
    } catch (err) {
      console.error(err);
      showToast('生成配对码失败', 'error');
    } finally {
      setCodeGenerating(false);
    }
  };

  const handleDeleteCode = async (id: number) => {
    try {
      await deleteTgCode(id);
      setCodes((prev) => prev.filter((c) => c.id !== id));
      showToast('配对码已吊销');
    } catch (err) {
      console.error(err);
      showToast('吊销配对码失败', 'error');
    }
  };

  const handleUpdateUserDisabled = async (id: number, disabled: boolean) => {
    try {
      await updateTgUser(id, { disabled });
      setUsers((prev) => prev.map((u) => (u.id === id ? { ...u, disabled } : u)));
      showToast(disabled ? '用户已停用' : '用户已启用');
    } catch (err) {
      console.error(err);
      showToast('更新用户状态失败', 'error');
    }
  };

  const handleDeleteUser = async (id: number) => {
    if (confirm('确定要移除这个管理员吗？')) {
      try {
        await deleteTgUser(id);
        setUsers((prev) => prev.filter((u) => u.id !== id));
        showToast('管理员已移除');
      } catch (err) {
        console.error(err);
        showToast('移除管理员失败', 'error');
      }
    }
  };

  if (loading) {
    return (
      <>
        <PageLoading />
        <Toasts toasts={toasts} />
      </>
    );
  }

  const deleteIconButton = 'hover:bg-red-50 hover:text-red-600 dark:hover:bg-red-500/10 dark:hover:text-red-400';
  const botIdentity = config?.bot_username
    ? `@${config.bot_username}`
    : config?.token_set
      ? 'Token 已设置'
      : '还没有设置 Token';

  const userActions = (user: TgUser) => (
    <>
      <button onClick={() => handleUpdateUserDisabled(user.id, !user.disabled)} className={BUTTON.secondary}>
        {user.disabled ? '启用' : '停用'}
      </button>
      <button
        onClick={() => handleDeleteUser(user.id)}
        title="移除"
        aria-label={`移除管理员 ${user.username}`}
        className={cn(BUTTON.icon, deleteIconButton)}
      >
        <Trash2 className="size-4" />
      </button>
    </>
  );

  return (
    <div className="space-y-6">
      <section className={cn(CARD, 'p-4 sm:p-6')}>
        <SectionHeader
          title="Telegram 机器人"
          description="启用后，配对过的管理员可以在 Telegram 里查询 Team、邀请或移除成员、生成兑换码，并接收提醒。"
        />
        <div className="mt-5 divide-y divide-gray-100 dark:divide-ink-800">
          <SettingRow
            title={
              <>
                启用机器人
                <span className={cn(PILL, config?.enabled ? TONE.success : TONE.neutral)}>
                  {config?.enabled ? '已启用' : '已禁用'}
                </span>
              </>
            }
            description={
              <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span className="min-w-0 break-all">{botIdentity}</span>
                <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
                  <span className={cn('size-1.5 rounded-full', config?.polling ? 'bg-emerald-500' : 'bg-gray-400 dark:bg-ink-500')} />
                  {config?.polling ? '在线' : '离线'}
                </span>
              </span>
            }
            control={
              <Switch checked={!!config?.enabled} onChange={handleToggleEnabled} aria-label="启用 TG 机器人" />
            }
          />
          <div className="py-4 last:pb-0">
            <label htmlFor="tg-bot-token" className="text-sm font-medium text-gray-900 dark:text-gray-100">
              机器人 Token
            </label>
            <p className={cn('mt-1 text-sm leading-6', MUTED)}>
              在 Telegram 里找 @BotFather 创建机器人即可获得。
              {config?.token_set && '已设置的 Token 不会显示，填写新 Token 即替换。'}
            </p>
            <div className="mt-2 flex flex-col gap-2 sm:flex-row">
              <input
                id="tg-bot-token"
                type="password"
                autoComplete="off"
                value={tokenInput}
                onChange={(e) => setTokenInput(e.target.value)}
                placeholder={config?.token_set ? '输入新 Token 以替换' : '粘贴机器人 Token'}
                className={cn(INPUT, 'sm:flex-1')}
              />
              <button onClick={handleSaveToken} disabled={tokenSaving} className={BUTTON.primary}>
                {tokenSaving ? '保存中…' : '保存 Token'}
              </button>
            </div>
          </div>
        </div>
      </section>

      <section className={cn(CARD, 'p-4 sm:p-6')}>
        <SettingRow
          title={<h2 className="text-base font-semibold">定时摘要</h2>}
          description={
            config?.enabled
              ? '每次自动同步后，把 Team、在线、席位和超员统计推送给管理员；两次推送至少间隔下方设定的分钟数。'
              : '需先启用机器人。开启后，每次自动同步后把 Team、在线、席位和超员统计推送给管理员。'
          }
          control={
            <Switch
              checked={!!config?.summary_enabled}
              onChange={handleToggleSummary}
              disabled={!config?.enabled}
              aria-label="定时摘要"
            />
          }
        />
        <div className="mt-4 flex flex-wrap items-end gap-2">
          <label className="w-full sm:w-48">
            <span className="mb-1.5 block text-sm font-medium text-gray-700 dark:text-ink-200">最短推送间隔（分钟）</span>
            <input
              type="number"
              inputMode="numeric"
              min={5}
              max={1440}
              step={5}
              value={summaryInterval}
              onChange={(event) => setSummaryInterval(Number(event.target.value))}
              className={INPUT}
            />
          </label>
          <button onClick={handleSaveSummaryInterval} disabled={summarySaving} className={BUTTON.secondary}>
            {summarySaving ? '保存中…' : '保存间隔'}
          </button>
          <button onClick={handleSendSummary} disabled={!config?.enabled || summarySending} className={BUTTON.primary}>
            <Send className="size-4" />
            {summarySending ? '发送中…' : '立即发送'}
          </button>
        </div>
        <p className={cn('mt-3 text-xs', MUTED)}>
          上次发送：
          {config?.summary_last_sent_at ? formatDateSafe(config.summary_last_sent_at, 'MM-dd HH:mm:ss') : '尚未发送'}
        </p>
      </section>

      <section className={cn(CARD, 'p-4 sm:p-6')}>
        <SectionHeader
          title="管理员配对码"
          description={
            <>
              配对码拥有完整后台权限，只发给管理员。对方私聊机器人发送 <code className="font-mono text-gray-700 dark:text-ink-200">/pair &lt;配对码&gt;</code> 即可绑定。成员的配对码请在「用户管理」里按邮箱生成。
            </>
          }
        />
        <div className="mt-4 flex flex-col gap-2 sm:flex-row">
          <input
            type="text"
            value={codeNote}
            onChange={(e) => setCodeNote(e.target.value)}
            placeholder="备注（可选），如：Jack 的手机"
            aria-label="配对码备注（可选）"
            className={cn(INPUT, 'sm:flex-1')}
          />
          <button onClick={handleCreateCode} disabled={codeGenerating} className={BUTTON.primary}>
            <Plus className="size-4" />
            {codeGenerating ? '生成中…' : '生成配对码'}
          </button>
        </div>

        {showNewCode && (
          <div className="mt-4 rounded-lg border border-emerald-200 bg-emerald-50 p-3 dark:border-emerald-500/30 dark:bg-emerald-500/10">
            <div className="text-sm font-medium text-emerald-800 dark:text-emerald-300">管理员配对码已生成，只显示这一次</div>
            <div className="mt-0.5 text-xs text-emerald-700 dark:text-emerald-300/80">一次性使用，24 小时后过期。</div>
            <div className="mt-2 flex items-center gap-2">
              <code className="min-w-0 flex-1 select-all break-all rounded-lg border border-emerald-200 bg-white px-3 py-2 font-mono text-base font-semibold text-gray-900 dark:border-emerald-500/20 dark:bg-ink-950 dark:text-gray-100">
                {showNewCode}
              </code>
              <button
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(showNewCode);
                    showToast('已复制到剪贴板');
                  } catch {
                    showToast('复制失败，请手动选中复制', 'error');
                  }
                }}
                title="复制配对码"
                aria-label="复制配对码"
                className="inline-flex size-9 shrink-0 items-center justify-center rounded-lg bg-emerald-600 text-white transition-colors hover:bg-emerald-700"
              >
                <Copy className="size-4" />
              </button>
            </div>
          </div>
        )}

        <h3 className="mt-6 text-sm font-medium text-gray-900 dark:text-gray-100">已生成的配对码</h3>
        {codes.length === 0 ? (
          <p className={cn('mt-2 text-sm', MUTED)}>暂无配对码</p>
        ) : (
          <ul className="mt-2 divide-y divide-gray-200 rounded-lg border border-gray-200 dark:divide-ink-800 dark:border-ink-800">
            {codes.map((code) => {
              const state = pairingCodeState(code);
              return (
                <li key={code.id} className="flex items-center gap-3 px-3 py-2.5">
                  <div className="min-w-0 flex-1">
                    <div className="break-all font-mono text-sm text-gray-900 dark:text-gray-100">{code.code}</div>
                    <div className={cn('mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs', MUTED)}>
                      <span className={cn(PILL, state.tone)}>{state.label}</span>
                      {state.when && <span className="whitespace-nowrap">{state.when}</span>}
                      {code.note && (
                        <span className="min-w-0 truncate" title={code.note}>
                          {code.note}
                        </span>
                      )}
                    </div>
                  </div>
                  {!code.used_by_chat_id && (
                    <button
                      onClick={() => handleDeleteCode(code.id)}
                      title="吊销"
                      aria-label={`吊销配对码 ${code.code}`}
                      className={cn(BUTTON.icon, deleteIconButton)}
                    >
                      <Trash2 className="size-4" />
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section className={cn(CARD, 'overflow-hidden')}>
        <div className="border-b border-gray-200 px-4 py-4 sm:px-6 dark:border-ink-800">
          <SectionHeader
            title="已配对管理员"
            aside={<span className="pt-0.5 text-xs tabular-nums text-gray-500 dark:text-ink-400">{users.length} 人</span>}
          />
        </div>
        {users.length === 0 ? (
          <p className={cn('px-6 py-10 text-center text-sm', MUTED)}>暂无已配对管理员</p>
        ) : (
          <>
            <ul className="divide-y divide-gray-200 md:hidden dark:divide-ink-800">
              {users.map((user) => (
                <li key={user.id} className="flex items-center gap-3 px-4 py-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex min-w-0 items-center gap-2">
                      <span className="truncate font-medium text-gray-900 dark:text-gray-100" title={user.username}>
                        {user.username}
                      </span>
                      {user.disabled && <span className={cn(PILL, TONE.neutral)}>已停用</span>}
                    </div>
                    <div className={cn('mt-0.5 truncate text-xs', MUTED)}>
                      Chat ID <span className="font-mono">{user.chat_id}</span>
                    </div>
                    <div className={cn('mt-0.5 text-xs', MUTED)}>
                      {formatDateSafe(user.paired_at, 'MM-dd HH:mm:ss')} 配对
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-1">{userActions(user)}</div>
                </li>
              ))}
            </ul>
            <div className="hidden overflow-x-auto md:block">
              <table className="w-full text-left text-sm">
                <thead className="bg-gray-50 text-xs text-gray-500 dark:bg-ink-950/40 dark:text-ink-400">
                  <tr className="[&>th]:whitespace-nowrap [&>th]:px-6 [&>th]:py-2.5 [&>th]:font-medium">
                    <th>用户名</th>
                    <th>Chat ID</th>
                    <th>配对时间</th>
                    <th className="text-right">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-200 dark:divide-ink-800">
                  {users.map((user) => (
                    <tr key={user.id} className="transition-colors hover:bg-gray-50 dark:hover:bg-ink-800/40">
                      <td className="px-6 py-2.5">
                        <div className="flex items-center gap-2">
                          <span className="font-medium text-gray-900 dark:text-gray-100">{user.username}</span>
                          {user.disabled && <span className={cn(PILL, TONE.neutral)}>已停用</span>}
                        </div>
                      </td>
                      <td className="whitespace-nowrap px-6 py-2.5 font-mono text-xs text-gray-600 dark:text-ink-300">
                        {user.chat_id}
                      </td>
                      <td className="whitespace-nowrap px-6 py-2.5 text-xs tabular-nums text-gray-600 dark:text-ink-300">
                        {formatDateSafe(user.paired_at, 'MM-dd HH:mm:ss')}
                      </td>
                      <td className="px-6 py-2">
                        <div className="flex items-center justify-end gap-1">{userActions(user)}</div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </section>

      <Toasts toasts={toasts} />
    </div>
  );
}

type TabValue = 'patrol' | 'bot';

const TABS: { value: TabValue; label: string }[] = [
  { value: 'patrol', label: '巡逻' },
  { value: 'bot', label: 'TG 机器人' },
];

export default function TgPatrol() {
  const [activeTab, setActiveTab] = useState<TabValue>('patrol');

  return (
    <PageShell
      title="TG 与巡逻"
      description="巡逻自动清理超出席位的成员；Telegram 机器人用于远程管理和推送摘要。"
    >
      <div className="mb-6">
        <SegmentedTabs value={activeTab} onChange={setActiveTab} options={TABS} ariaLabel="TG 与巡逻视图" />
      </div>

      {activeTab === 'patrol' ? <PatrolSection /> : <TgBotSection />}
    </PageShell>
  );
}
