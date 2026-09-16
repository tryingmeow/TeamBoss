import { useState, useEffect, useRef } from 'react';
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
import { AlertTriangle, CheckCircle2, Clock, Copy, Plus, Send, Settings, Shield, Trash2, Users } from 'lucide-react';
import * as Dialog from '@radix-ui/react-dialog';
import Toast from '../../components/Toast';
import Switch from '../../components/Switch';
import { formatDateSafe } from '../../lib/formatDate';

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
 * 「本轮刷新成功的队」是真实行为：巡逻只处理同步刚刷新过的队，
 * 同步失败或已挂起的队这一轮完全不碰。
 */
function patrolRuleText(intervalMinutes: number): string {
  return `每 ${intervalMinutes} 分钟自动巡逻，仅清理未受保护车队中外部新增的超额成员。`;
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
      showToast(`演练完成: ${result.would_kick} 人会被踢`);
      await loadStatus();
    } catch (err) {
      console.error(err);
      showToast('空跑失败', 'error');
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
    const next = new Set(selectedTeams);
    if (pendingExempt.willExempt) next.add(pendingExempt.team.team_id);
    else next.delete(pendingExempt.team.team_id);
    try {
      setSavingExempt(true);
      await updatePatrolSettings({ exempt_team_ids: Array.from(next) });
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
      <div className="p-8 text-center">
        <div className="text-gray-500 dark:text-slate-400">加载中...</div>
      </div>
    );
  }

  if (!status) {
    return (
      <div className="p-8 text-center">
        <div className="text-gray-500 dark:text-slate-400">无法加载巡逻状态</div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header with status */}
      <div className="bg-white dark:bg-slate-900 border border-gray-200 dark:border-slate-800 rounded-xl p-6">
        <div className="flex items-start justify-between mb-4">
          <div>
            <h2 className="text-lg font-semibold text-gray-900 dark:text-slate-100 flex items-center gap-2">
              <Shield className="w-5 h-5 text-indigo-400" />
              巡逻自动踢人
            </h2>
          </div>
        </div>

        <div className="space-y-4">
          {/* Single safe activation / disable entry */}
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between p-4 bg-gray-50 dark:bg-slate-800/50 rounded-lg border border-gray-200 dark:border-slate-700">
            <div className="flex-1">
              <div className="font-medium text-gray-800 dark:text-slate-200">
                巡逻自动踢人：
                {status.kick_enabled ? (
                  <span className="text-rose-500 dark:text-rose-400">开启</span>
                ) : (
                  <span className="text-gray-500 dark:text-slate-400">关闭</span>
                )}
              </div>
              <div className="text-sm text-gray-500 dark:text-slate-400 mt-1 leading-6">
                {patrolRuleText(status.sync_interval_minutes)}
                {status.baseline_at && (
                  <span className="block text-xs mt-1">
                    上次保护：{formatDateSafe(status.baseline_at, 'MM-dd HH:mm:ss')}
                  </span>
                )}
              </div>
            </div>
            <Switch
              checked={status.kick_enabled}
              onChange={handleToggleKickEnabled}
              disabled={activating}
              aria-label="巡逻自动踢人"
            />
          </div>

          {/* Dry Run Button */}
          <div className="flex items-center justify-between p-4 bg-gray-50 dark:bg-slate-800/50 rounded-lg border border-gray-200 dark:border-slate-700">
            <div className="flex-1">
              <div className="font-medium text-gray-800 dark:text-slate-200">演练空跑</div>
              <div className="text-sm text-gray-500 dark:text-slate-400 mt-1">
                按当前规则预览将被清理的违规成员（不会真正执行）。
              </div>
            </div>
            <button
              onClick={handleRunDryRun}
              disabled={running}
              className="flex shrink-0 items-center gap-2 px-4 py-2 rounded-lg border border-gray-300 dark:border-slate-600 text-gray-700 dark:text-slate-200 font-medium hover:bg-gray-50 dark:hover:bg-slate-800 transition-colors disabled:opacity-60"
            >
              {running ? '运行中…' : '演练空跑'}
            </button>
          </div>
        </div>
      </div>

      {/* Teams: colored border = status, click to toggle exemption (with confirm) */}
      <div className="bg-white dark:bg-slate-900 border border-gray-200 dark:border-slate-800 rounded-xl p-6">
        <div className="mb-3 flex items-center justify-between gap-3">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-slate-100 flex items-center gap-2">
            <Shield className="w-5 h-5 text-indigo-400" />
            车队豁免
          </h3>
          <span className="shrink-0 text-xs text-gray-400 dark:text-slate-500">
            受保护 {status.teams.filter((t) => t.codex_enabled || selectedTeams.has(t.team_id)).length} / {status.teams.length}
          </span>
        </div>
        <div className="mb-4 flex items-center gap-2 rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-sm text-indigo-700 dark:border-indigo-500/20 dark:bg-indigo-500/10 dark:text-indigo-300">
          点击车队切换保护状态。带有 🛡️ 标记的车队超员时不自动清理。
        </div>
        <div className="flex flex-wrap gap-2">
          {status.teams.map((team) => {
            const exempt = selectedTeams.has(team.team_id);
            const isProtected = team.codex_enabled || exempt;
            const cls = isProtected
              ? 'border-emerald-500 text-emerald-700 hover:bg-emerald-500/10 dark:text-emerald-300'
              : team.risk === 'over'
                ? 'border-rose-500 text-rose-600 hover:bg-rose-500/10 dark:text-rose-300'
                : team.risk === 'watch'
                  ? 'border-amber-500 text-amber-600 hover:bg-amber-500/10 dark:text-amber-300'
                  : 'border-gray-300 text-gray-600 hover:bg-gray-500/5 dark:border-slate-600 dark:text-slate-300';
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
                onClick={() => requestToggleExempt(team)}
                title={`${team.team_id.slice(0, 8)}… · ${state}`}
                className={`inline-flex items-center gap-1 rounded-full border-2 bg-transparent px-3 py-1.5 text-sm font-medium transition-colors disabled:cursor-default ${cls}`}
              >
                {isProtected && <span aria-hidden>🛡️</span>}
                {team.name}
              </button>
            );
          })}
        </div>
        <div className="mt-4 flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-400 dark:text-slate-500">
          <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-full border-2 border-emerald-500" />已豁免 / Codex</span>
          <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-full border-2 border-amber-500" />观察</span>
          <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-full border-2 border-rose-500" />超员风险</span>
        </div>
      </div>

      {/* Confirm Dialog */}
      <Dialog.Root open={showKickConfirm} onOpenChange={setShowKickConfirm}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50" />
          <Dialog.Content className="fixed left-[50%] top-[50%] translate-x-[-50%] translate-y-[-50%] w-full max-w-md bg-white dark:bg-slate-900 border border-gray-200 dark:border-slate-800 rounded-2xl p-6 z-50 shadow-2xl">
            <Dialog.Title className="text-lg font-semibold text-gray-900 dark:text-slate-100 flex items-center gap-2 mb-2">
              <AlertTriangle className="w-5 h-5 text-amber-500" />
              确认豁免现有成员并开启
            </Dialog.Title>
            <Dialog.Description className="text-gray-500 dark:text-slate-400 mb-6 text-sm leading-6">
              请先确保当前各 Team 的成员干净且全部受信任。
              <br />
              <br />
              确认后会实时刷新全部成员，将当前成员和邀请统一列为受保护对象，再开启自动踢人。
              {patrolRuleText(status.sync_interval_minutes)}
            </Dialog.Description>
            <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
              <Dialog.Close asChild>
                <button className="px-4 py-2 rounded-lg bg-gray-100 dark:bg-slate-800 text-gray-700 dark:text-slate-300 hover:bg-gray-200 dark:hover:bg-slate-700 font-medium transition-colors">
                  取消
                </button>
              </Dialog.Close>
              <button
                onClick={handleActivatePatrol}
                disabled={activating}
                className="px-4 py-2 rounded-lg bg-indigo-500 hover:bg-indigo-600 text-white font-medium transition-colors disabled:opacity-60"
              >
                {activating ? '正在刷新并开启...' : '确认豁免并开启'}
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      {/* Exempt toggle confirm */}
      <Dialog.Root open={!!pendingExempt} onOpenChange={(o) => !o && setPendingExempt(null)}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50" />
          <Dialog.Content className="fixed left-[50%] top-[50%] translate-x-[-50%] translate-y-[-50%] w-full max-w-md bg-white dark:bg-slate-900 border border-gray-200 dark:border-slate-800 rounded-2xl p-6 z-50 shadow-2xl">
            <Dialog.Title className="text-lg font-semibold text-gray-900 dark:text-slate-100 mb-2">
              {pendingExempt?.willExempt ? '加入豁免名单' : '移出豁免名单'}
            </Dialog.Title>
            <Dialog.Description className="text-gray-500 dark:text-slate-400 mb-6 text-sm leading-6">
              {pendingExempt?.willExempt
                ? `把「${pendingExempt?.team.name}」加入豁免后，即使超员也不会被自动踢人。`
                : `把「${pendingExempt?.team.name}」移出豁免后，其超员成员可能被自动踢人。`}
            </Dialog.Description>
            <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
              <button
                onClick={() => setPendingExempt(null)}
                className="px-4 py-2 rounded-lg bg-gray-100 dark:bg-slate-800 text-gray-700 dark:text-slate-300 hover:bg-gray-200 dark:hover:bg-slate-700 font-medium transition-colors"
              >
                取消
              </button>
              <button
                onClick={confirmToggleExempt}
                disabled={savingExempt}
                className="px-4 py-2 rounded-lg bg-indigo-500 hover:bg-indigo-600 text-white font-medium transition-colors disabled:opacity-60"
              >
                {savingExempt ? '保存中…' : '确认'}
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      {/* Toasts */}
      {toasts.length > 0 && (
        <div className="fixed right-5 top-20 z-[100] flex w-[min(22rem,calc(100vw-2rem))] flex-col gap-2">
          {toasts.map((toast) => (
            <Toast key={toast.id} text={toast.text} type={toast.type} />
          ))}
        </div>
      )}
    </div>
  );
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
      showToast(next ? '已开启自动刷新摘要' : '已关闭自动刷新摘要');
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
      <div className="p-8 text-center">
        <div className="text-gray-500 dark:text-slate-400">加载中...</div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Config Section */}
      <div className="bg-white dark:bg-slate-900 border border-gray-200 dark:border-slate-800 rounded-xl p-6">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-slate-100 flex items-center gap-2 mb-4">
          <Settings className="w-5 h-5 text-indigo-400" />
          机器人配置
        </h2>

        <div className="space-y-4">
          {/* Enable Toggle */}
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between p-4 bg-gray-50 dark:bg-slate-800/50 rounded-lg border border-gray-200 dark:border-slate-700">
            <div>
              <div className="font-medium text-gray-800 dark:text-slate-200">启用状态</div>
              <div className="text-sm text-gray-500 dark:text-slate-400 mt-1">
                {config?.enabled ? '已启用 TG 机器人' : '已禁用 TG 机器人'}
              </div>
            </div>
            <Switch
              checked={!!config?.enabled}
              onChange={handleToggleEnabled}
              aria-label="启用 TG 机器人"
            />
          </div>

          {/* Bot Info */}
          {config?.bot_username && (
            <div className="p-4 bg-emerald-50 dark:bg-emerald-500/10 rounded-lg border border-emerald-200 dark:border-emerald-500/20">
              <div className="flex items-center gap-2 text-emerald-600 dark:text-emerald-400">
                <CheckCircle2 className="w-4 h-4" />
                <span className="font-medium">机器人: @{config.bot_username}</span>
              </div>
            </div>
          )}

          <div className="p-4 bg-gray-50 dark:bg-slate-800/50 rounded-lg border border-gray-200 dark:border-slate-700 space-y-4">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <div className="font-medium text-gray-800 dark:text-slate-200 flex items-center gap-2">
                  <Clock className="w-4 h-4 text-indigo-400" />
                  自动刷新摘要
                </div>
                <div className="text-sm text-gray-500 dark:text-slate-400 mt-1">
                  每次完整自动同步后按间隔推送车队、在线、席位和超员统计。
                </div>
              </div>
              <Switch
                checked={!!config?.summary_enabled}
                onChange={handleToggleSummary}
                disabled={!config?.enabled}
                aria-label="自动刷新摘要"
              />
            </div>

            <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
              <label className="flex-1 text-sm font-medium text-gray-800 dark:text-slate-200">
                最短推送间隔（分钟）
                <input
                  type="number"
                  min={5}
                  max={1440}
                  step={5}
                  value={summaryInterval}
                  onChange={(event) => setSummaryInterval(Number(event.target.value))}
                  className="mt-2 w-full px-3 py-2 bg-white dark:bg-slate-900 border border-gray-300 dark:border-slate-700 rounded-lg text-sm text-gray-800 dark:text-slate-200 focus:outline-none focus:ring-2 focus:ring-indigo-500"
                />
              </label>
              <button
                onClick={handleSaveSummaryInterval}
                disabled={summarySaving}
                className="px-4 py-2 rounded-lg border border-gray-300 dark:border-slate-600 text-gray-700 dark:text-slate-200 font-medium hover:bg-gray-50 dark:hover:bg-slate-800 transition-colors disabled:opacity-60"
              >
                {summarySaving ? '保存中…' : '保存间隔'}
              </button>
              <button
                onClick={handleSendSummary}
                disabled={!config?.enabled || summarySending}
                className="flex items-center justify-center gap-2 px-4 py-2 rounded-lg bg-indigo-500 hover:bg-indigo-600 text-white font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <Send className="w-4 h-4" />
                {summarySending ? '发送中…' : '立即发送'}
              </button>
            </div>

            <div className="text-xs text-gray-500 dark:text-slate-400 flex flex-wrap gap-x-4 gap-y-1">
              <span>轮询线程: {config?.polling ? '运行中' : '未运行'}</span>
              <span>
                上次摘要: {config?.summary_last_sent_at
                  ? formatDateSafe(config.summary_last_sent_at, 'MM-dd HH:mm:ss')
                  : '尚未发送'}
              </span>
            </div>
          </div>

          {/* Token Input */}
          <div className="space-y-2">
            <label className="block text-sm font-medium text-gray-800 dark:text-slate-200">
              机器人 Token
            </label>
            <div className="flex flex-col gap-2 sm:flex-row">
              <input
                type="password"
                value={tokenInput}
                onChange={(e) => setTokenInput(e.target.value)}
                placeholder="输入或替换 Token..."
                className="flex-1 px-3 py-2 bg-white dark:bg-slate-900 border border-gray-300 dark:border-slate-700 rounded-lg text-sm text-gray-800 dark:text-slate-200 placeholder:text-gray-400 dark:placeholder:text-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500"
              />
              <button
                onClick={handleSaveToken}
                disabled={tokenSaving}
                className="shrink-0 whitespace-nowrap px-4 py-2 rounded-lg bg-indigo-500 hover:bg-indigo-600 text-white font-medium transition-colors disabled:opacity-60"
              >
                {tokenSaving ? '保存中...' : '保存'}
              </button>
            </div>
            {config?.token_set && (
              <div className="text-xs text-gray-500 dark:text-slate-400">
                ✓ Token 已设置（出于安全考虑，已设置的 Token 不会显示）
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Pairing Codes Section */}
      <div className="bg-white dark:bg-slate-900 border border-gray-200 dark:border-slate-800 rounded-xl p-6">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-slate-100 flex items-center gap-2 mb-4">
          <Plus className="w-5 h-5 text-indigo-400" />
          管理员配对码
        </h2>

        <p className="mb-4 text-sm leading-6 text-gray-500 dark:text-slate-400">
          此处生成的配对码拥有完整后台权限，仅发给管理员。成员配对码请在“用户管理”中按邮箱生成。
        </p>

        <div className="space-y-4 mb-6">
          <div>
            <label className="block text-sm font-medium text-gray-800 dark:text-slate-200 mb-2">
              备注（可选）
            </label>
            <input
              type="text"
              value={codeNote}
              onChange={(e) => setCodeNote(e.target.value)}
              placeholder="如：Jack 的管理员配对码"
              className="w-full px-3 py-2 bg-white dark:bg-slate-900 border border-gray-300 dark:border-slate-700 rounded-lg text-sm text-gray-800 dark:text-slate-200 placeholder:text-gray-400 dark:placeholder:text-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500"
            />
          </div>
          <button
            onClick={handleCreateCode}
            disabled={codeGenerating}
            className="w-full px-4 py-2 rounded-lg bg-indigo-500 hover:bg-indigo-600 text-white font-medium transition-colors disabled:opacity-60 flex items-center justify-center gap-2"
          >
            <Plus className="w-4 h-4" />
            {codeGenerating ? '生成中…' : '生成管理员配对码'}
          </button>
        </div>

        {/* New Code Display */}
        {showNewCode && (
          <div className="mb-6 p-4 bg-emerald-50 dark:bg-emerald-500/10 rounded-lg border border-emerald-200 dark:border-emerald-500/20">
            <div className="text-sm font-medium text-emerald-600 dark:text-emerald-400 mb-2">
              管理员配对码已生成（仅显示一次）
            </div>
            <div className="flex items-center gap-2">
              <code className="flex-1 px-3 py-2 bg-white dark:bg-slate-900 rounded font-mono text-lg font-bold text-gray-800 dark:text-slate-200 break-all">
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
                className="p-2 rounded-lg bg-emerald-500 hover:bg-emerald-600 text-white transition-colors"
              >
                <Copy className="w-4 h-4" />
              </button>
            </div>
            <div className="text-xs text-emerald-600 dark:text-emerald-400 mt-2">
              • 一次性使用<br />
              • 24 小时后过期
            </div>
          </div>
        )}

        {/* Codes List */}
        <div className="space-y-2">
          <div className="text-sm font-medium text-gray-800 dark:text-slate-200">已生成的码</div>
          {codes.length === 0 ? (
            <div className="text-sm text-gray-500 dark:text-slate-400 py-4 text-center">
              暂无配对码
            </div>
          ) : (
            <div className="space-y-2">
              {codes.map((code) => (
                <div key={code.id} className="flex items-center justify-between p-3 bg-gray-50 dark:bg-slate-800/50 rounded-lg border border-gray-200 dark:border-slate-700">
                  <div className="flex-1 min-w-0">
                    <div className="font-mono text-sm text-gray-800 dark:text-slate-200 break-all">
                      {code.code}
                    </div>
                    <div className="text-xs text-gray-500 dark:text-slate-400 mt-1">
                      {code.note && <span>{code.note} • </span>}
                      管理员 •{' '}
                      {code.used_by_chat_id ? (
                        <span className="text-emerald-600 dark:text-emerald-400">
                          已用 ({formatDateSafe(code.used_at, 'MM-dd HH:mm')})
                        </span>
                      ) : (
                        <span>过期: {formatDateSafe(code.expires_at, 'MM-dd HH:mm')}</span>
                      )}
                    </div>
                  </div>
                  {!code.used_by_chat_id && (
                    <button
                      onClick={() => handleDeleteCode(code.id)}
                      className="p-2 rounded-lg text-gray-400 dark:text-slate-500 hover:bg-rose-500/10 hover:text-rose-400 transition-colors ml-2"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Users Section */}
      <div className="bg-white dark:bg-slate-900 border border-gray-200 dark:border-slate-800 rounded-xl overflow-hidden">
        <div className="p-6 border-b border-gray-200 dark:border-slate-800">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-slate-100 flex items-center gap-2">
            <Users className="w-5 h-5 text-indigo-400" />
            已配对管理员
          </h2>
        </div>
        <div className="divide-y divide-gray-200 dark:divide-slate-800 md:hidden">
          {users.length === 0 ? (
            <div className="px-6 py-8 text-center text-sm text-gray-500 dark:text-slate-400">
              暂无已配对管理员
            </div>
          ) : (
            users.map((user) => (
              <div key={user.id} className="space-y-4 p-6">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="truncate font-medium text-gray-800 dark:text-slate-200">
                      {user.username}
                    </div>
                    <div className="mt-1 font-mono text-xs text-gray-500 dark:text-slate-400">
                      Chat ID: {user.chat_id}
                    </div>
                  </div>
                  <span className={`shrink-0 rounded-full px-2 py-1 text-xs font-medium ${
                    user.disabled
                      ? 'bg-gray-100 text-gray-500 dark:bg-slate-800 dark:text-slate-400'
                      : 'bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-400'
                  }`}>
                    {user.disabled ? '已停用' : '已启用'}
                  </span>
                </div>
                <div className="text-xs text-gray-500 dark:text-slate-400">
                  配对时间：{formatDateSafe(user.paired_at, 'MM-dd HH:mm:ss')}
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={() => handleUpdateUserDisabled(user.id, !user.disabled)}
                    className="flex-1 rounded-lg bg-gray-100 px-3 py-2 text-xs font-medium text-gray-700 transition-colors hover:bg-gray-200 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
                  >
                    {user.disabled ? '重新启用' : '停用管理员'}
                  </button>
                  <button
                    onClick={() => handleDeleteUser(user.id)}
                    aria-label={`移除管理员 ${user.username}`}
                    className="rounded-lg border border-gray-200 p-2 text-gray-400 transition-colors hover:border-rose-200 hover:bg-rose-500/10 hover:text-rose-400 dark:border-slate-700 dark:text-slate-500"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              </div>
            ))
          )}
        </div>
        <div className="hidden overflow-x-auto md:block">
          <table className="w-full text-left text-sm text-gray-700 dark:text-slate-300">
            <thead className="bg-gray-50 dark:bg-slate-950/50 text-gray-500 dark:text-slate-400">
              <tr>
                <th className="px-6 py-4 font-medium">用户名</th>
                <th className="px-6 py-4 font-medium">Chat ID</th>
                <th className="px-6 py-4 font-medium">配对时间</th>
                <th className="px-6 py-4 font-medium text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 dark:divide-slate-800/50">
              {users.length === 0 ? (
                <tr>
                  <td colSpan={4} className="px-6 py-8 text-center text-gray-500 dark:text-slate-400">
                    暂无已配对管理员
                  </td>
                </tr>
              ) : (
                users.map((user) => (
                  <tr key={user.id} className="hover:bg-gray-50 dark:hover:bg-slate-800/20">
                    <td className="px-6 py-4 font-medium text-gray-800 dark:text-slate-200">
                      {user.username}
                      {user.disabled && <span className="text-xs ml-2 text-gray-400">(已停用)</span>}
                    </td>
                    <td className="px-6 py-4 font-mono text-xs text-gray-500 dark:text-slate-400">
                      {user.chat_id}
                    </td>
                    <td className="px-6 py-4 text-xs text-gray-500 dark:text-slate-400">
                      {formatDateSafe(user.paired_at, 'MM-dd HH:mm:ss')}
                    </td>
                    <td className="px-6 py-4 text-right space-x-2">
                      <button
                        onClick={() => handleUpdateUserDisabled(user.id, !user.disabled)}
                        className="px-3 py-1 rounded text-xs font-medium bg-gray-100 dark:bg-slate-800 text-gray-700 dark:text-slate-300 hover:bg-gray-200 dark:hover:bg-slate-700 transition-colors"
                      >
                        {user.disabled ? '启用' : '停用'}
                      </button>
                      <button
                        onClick={() => handleDeleteUser(user.id)}
                        aria-label={`移除管理员 ${user.username}`}
                        className="p-1.5 rounded text-gray-400 dark:text-slate-500 hover:bg-rose-500/10 hover:text-rose-400 transition-colors"
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Toasts */}
      {toasts.length > 0 && (
        <div className="fixed right-5 top-20 z-[100] flex w-[min(22rem,calc(100vw-2rem))] flex-col gap-2">
          {toasts.map((toast) => (
            <Toast key={toast.id} text={toast.text} type={toast.type} />
          ))}
        </div>
      )}
    </div>
  );
}

export default function TgPatrol() {
  const [activeTab, setActiveTab] = useState<'patrol' | 'bot'>('patrol');

  return (
    <div className="p-8 max-w-7xl mx-auto space-y-8 animate-in fade-in duration-500">
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-slate-100 mb-1">
          TG 机器人 & 巡逻
        </h1>
        <p className="text-gray-500 dark:text-slate-400 text-sm">
          管理巡逻自动踢人和 Telegram 机器人配置。
        </p>
      </div>

      <div className="flex gap-4 border-b border-gray-200 dark:border-slate-800 pb-px">
        <button
          onClick={() => setActiveTab('patrol')}
          className={`pb-3 px-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
            activeTab === 'patrol'
              ? 'border-indigo-500 text-indigo-400'
              : 'border-transparent text-gray-500 dark:text-slate-400 hover:text-gray-700 dark:hover:text-slate-300'
          }`}
        >
          巡逻自动踢人
        </button>
        <button
          onClick={() => setActiveTab('bot')}
          className={`pb-3 px-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
            activeTab === 'bot'
              ? 'border-indigo-500 text-indigo-400'
              : 'border-transparent text-gray-500 dark:text-slate-400 hover:text-gray-700 dark:hover:text-slate-300'
          }`}
        >
          TG 机器人
        </button>
      </div>

      <div className="pt-2">
        {activeTab === 'patrol' ? <PatrolSection /> : <TgBotSection />}
      </div>
    </div>
  );
}
