import { useState, useRef, useEffect, type FormEvent } from 'react';
import { UserPlus, Trash2, KeyRound, DollarSign, CreditCard, Globe, Mail, Users, Zap, Calendar, ChevronDown, RefreshCw, Settings, Pencil, X, Loader2, CircleAlert, Copy, Check } from 'lucide-react';
import * as Dialog from '@radix-ui/react-dialog';
import * as Tooltip from '@radix-ui/react-tooltip';
import type { Team, TeamWorkspaceSettings, MembersData, ShowToast } from '../types';
import MemberPanel from './MemberPanel';
import ConfirmDialog from './ConfirmDialog';
import AddMemberDialog from './AddMemberDialog';
import TeamSettingsDialog from './TeamSettingsDialog';
import { useMembers } from '../hooks/useMembers';
import { deleteTeam, syncTeam, updateTeamRemark } from '../api/client';
import { activeChatGptSeats } from '../lib/seatCapacity';
import { formatSeatTypeLabel } from '../lib/seatType';

interface TeamCardProps {
  team: Team;
  onDelete: (id: string) => void;
  onReimport: (team: Team) => void;
  onTeamSynced: (team: Team) => void;
  onSyncSucceeded: (team: Team) => void;
  syncError?: string;
  showToast: ShowToast;
}

function formatShortDate(dateStr: string | null): string {
  if (!dateStr) return '—';
  const d = new Date(dateStr);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

function formatAmount(total: number): string {
  return Number.isInteger(total) ? String(total) : String(Math.round(total * 100) / 100);
}

function discountedMonthlyTotal(team: Team): number | null {
  if (typeof team.monthly_total === 'number') return team.monthly_total;
  if (team.price_per_seat === null) return null;
  return Math.max(0, (team.price_per_seat * team.seats_entitled) - (team.discount_amount || 0));
}

function monthlySubtotal(team: Team): number | null {
  if (typeof team.monthly_subtotal === 'number') return team.monthly_subtotal;
  if (team.price_per_seat === null) return null;
  return team.price_per_seat * team.seats_entitled;
}

function remainingPromoMonths(team: Team, now = new Date()): number | null {
  const totalMonths = team.discount_duration_num_periods;
  if (!team.discount_expires_at) return totalMonths;

  const expiresAt = new Date(team.discount_expires_at);
  if (Number.isNaN(expiresAt.getTime())) return totalMonths;
  if (expiresAt.getTime() <= now.getTime()) return 0;

  let months = (expiresAt.getUTCFullYear() - now.getUTCFullYear()) * 12
    + expiresAt.getUTCMonth() - now.getUTCMonth();
  const expiryRemainder = [
    expiresAt.getUTCDate(),
    expiresAt.getUTCHours(),
    expiresAt.getUTCMinutes(),
    expiresAt.getUTCSeconds(),
    expiresAt.getUTCMilliseconds(),
  ];
  const nowRemainder = [
    now.getUTCDate(),
    now.getUTCHours(),
    now.getUTCMinutes(),
    now.getUTCSeconds(),
    now.getUTCMilliseconds(),
  ];
  let expiresEarlierInMonth = false;
  for (let index = 0; index < expiryRemainder.length; index += 1) {
    if (expiryRemainder[index] === nowRemainder[index]) continue;
    expiresEarlierInMonth = expiryRemainder[index] < nowRemainder[index];
    break;
  }
  if (expiresEarlierInMonth) months -= 1;

  const remaining = Math.max(0, months);
  return totalMonths ? Math.min(totalMonths, remaining) : remaining;
}

function promoLabel(team: Team): string {
  const remainingCharges = remainingPromoMonths(team);
  if (remainingCharges !== null) return `还剩 ${remainingCharges} 次折扣`;
  return '优惠中';
}

function moneySuffix(team: Team): string {
  return team.billing_symbol || team.billing_currency;
}

function shortTeamId(teamId: string): string {
  return teamId.length > 8 ? teamId.slice(0, 8) : teamId;
}

// 成员+待接受邀请的指纹，用来判断"操作是否已在 ChatGPT 侧生效"。
// 除增删外，席位和到期时间也会被编辑；这些字段必须参与比较，否则前端
// 会误以为操作一直未生效并持续强制同步。
function membersSignature(data: MembersData | null): string {
  if (!data) return '';
  const members = data.members
    .map((m) => `${m.id}:${m.seat_type}:${m.expires_at ?? ''}`)
    .sort();
  const pending = data.pending_invites
    .map((i) => `${i.id}:${i.email}:${i.seat_type}:${i.expires_at ?? ''}`)
    .sort();
  return `m:${members.join(',')}|p:${pending.join(',')}`;
}

/** 把"从什么时候开始坏的"说成人话：已持续 3 小时 / 已持续 2 天。 */
function brokenForLabel(since: string | null | undefined): string {
  if (!since) return '';
  const startedAt = new Date(since).getTime();
  if (Number.isNaN(startedAt)) return '';
  const minutes = Math.floor((Date.now() - startedAt) / 60000);
  if (minutes < 1) return '刚刚开始';
  if (minutes < 60) return `已持续 ${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `已持续 ${hours} 小时`;
  return `已持续 ${Math.floor(hours / 24)} 天`;
}

export default function TeamCard({
  team,
  onDelete,
  onReimport,
  onTeamSynced,
  onSyncSucceeded,
  syncError,
  showToast,
}: TeamCardProps) {
  const [expanded, setExpanded] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [addMemberOpen, setAddMemberOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [defaultSeatInfoOpen, setDefaultSeatInfoOpen] = useState(false);
  const [remarkOpen, setRemarkOpen] = useState(false);
  const [remarkDraft, setRemarkDraft] = useState(team.remark ?? '');
  const [remarkError, setRemarkError] = useState('');
  const [savingRemark, setSavingRemark] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [ownerEmailCopied, setOwnerEmailCopied] = useState(false);
  const ownerEmailCopyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [workspaceSettings, setWorkspaceSettings] = useState<TeamWorkspaceSettings | null>(null);
  const { data: membersData, loading: membersLoading, error: membersError, refresh: refreshMembers, setData: setMembersData } = useMembers(
    expanded ? team.id : null
  );

  const isAuthExpired = team.status === 'token_expired';
  // 会话还能应答，但它交回的 token 已被上游吊销。表现和 Session 失效一样是"用不
  // 了"，但原因和处理方式不同，所以文案分开写。
  const isAuthRejected = team.auth_state === 'rejected';
  const authBlocked = isAuthExpired || isAuthRejected;
  const authBlockedSince = brokenForLabel(team.auth_state_since);
  const isSubscriptionExpired = team.subscription_status === 'expired';
  const isSubscriptionStale = team.subscription_status === 'stale';
  const isNonRenewing = team.subscription_status === 'nonrenewing';
  const isWarning = (isNonRenewing || (team.days_remaining !== null && team.days_remaining <= 3))
    && !authBlocked
    && !isSubscriptionExpired;
  const activeGptSeats = activeChatGptSeats(team);
  const monthlyTotal = discountedMonthlyTotal(team);
  const subtotal = monthlySubtotal(team);
  const [openingAddMember, setOpeningAddMember] = useState(false);
  const defaultSeatLabel = team.default_seat_type
    ? formatSeatTypeLabel(team.default_seat_type)
    : '—';

  const handleDelete = async () => {
    setDeleting(true);
    try {
      await deleteTeam(team.id);
      onDelete(team.id); // onDelete 里会弹"已删除"的成功 toast
      setConfirmDelete(false);
    } catch (err) {
      // 保留确认弹窗：删除失败时这张卡片必须还在，不能看起来像删除成功了。
      showToast(err instanceof Error ? err.message : '删除 Team 失败', 'error');
    } finally {
      setDeleting(false);
    }
  };

  const handleReimportInstead = () => {
    setConfirmDelete(false);
    onReimport(team);
  };

  const handleCopyOwnerEmail = async () => {
    try {
      await navigator.clipboard.writeText(team.owner_email);
      setOwnerEmailCopied(true);
      if (ownerEmailCopyTimer.current) clearTimeout(ownerEmailCopyTimer.current);
      ownerEmailCopyTimer.current = setTimeout(() => setOwnerEmailCopied(false), 2000);
    } catch {
      showToast('复制邮箱失败', 'error');
    }
  };

  const handleSyncTeam = async (force: boolean) => {
    if (syncing) return;
    setSyncing(true);
    try {
      const result = await syncTeam(team.id, force);
      setMembersData(result.members);
      setWorkspaceSettings(result.workspace_settings);
      onSyncSucceeded(result.team);
    } catch (err) {
      await refreshMembers(false);
      const errorMsg = err instanceof Error ? err.message : '同步失败';
      showToast(`同步失败，当前显示的是缓存数据${errorMsg ? '：' + errorMsg : ''}`, 'error');
    } finally {
      setSyncing(false);
    }
  };

  // ── 操作后自动刷新:先静默给 ChatGPT 生效时间,再阶梯轮询,检测到变化即停 ──
  const [settling, setSettling] = useState(false);
  const settleTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const settleStart = useRef(0);
  const settleBaseline = useRef('');

  const stopSettle = () => {
    if (settleTimer.current) {
      clearTimeout(settleTimer.current);
      settleTimer.current = null;
    }
    setSettling(false);
  };

  useEffect(() => () => {
    if (settleTimer.current) clearTimeout(settleTimer.current);
    if (ownerEmailCopyTimer.current) clearTimeout(ownerEmailCopyTimer.current);
  }, []);

  useEffect(() => {
    if (membersError) {
      showToast(`成员列表刷新失败：${membersError}`, 'error');
    }
  }, [membersError, showToast]);

  const scheduleSettlePoll = () => {
    const elapsed = Date.now() - settleStart.current;
    let delay: number;
    if (elapsed < 30_000) delay = 30_000 - elapsed;   // 头 30s 静默,不打扰 ChatGPT
    else if (elapsed < 180_000) delay = 5_000;        // 30–180s:每 5s 拉一次
    else if (elapsed < 360_000) delay = 20_000;       // 180–360s:每 20s 拉一次
    else { stopSettle(); return; }                    // 360s 封顶,停

    settleTimer.current = setTimeout(async () => {
      try {
        const result = await syncTeam(team.id, true);
        setMembersData(result.members);
        setWorkspaceSettings(result.workspace_settings);
        onSyncSucceeded(result.team);
        if (membersSignature(result.members) !== settleBaseline.current) {
          stopSettle();   // 变化已在 ChatGPT 侧生效,收工
          return;
        }
      } catch { /* 网络抖动忽略,继续下一档 */ }
      scheduleSettlePoll();
    }, delay);
  };

  // 邀请 / 踢人 / 撤邀请后调用:记下操作前的成员指纹,启动阶梯轮询。
  const startMemberSettle = () => {
    settleBaseline.current = membersSignature(membersData);
    settleStart.current = Date.now();
    if (settleTimer.current) clearTimeout(settleTimer.current);
    setSettling(true);
    scheduleSettlePoll();
  };

  const handleToggleExpanded = () => {
    const nextExpanded = !expanded;
    setExpanded(nextExpanded);
    if (nextExpanded) {
      void handleSyncTeam(false);
    }
  };

  const handleForceRefresh = async () => {
    if (!expanded) setExpanded(true);
    await handleSyncTeam(true);
  };

  const handleOpenAddMember = async () => {
    if (openingAddMember) return;
    setOpeningAddMember(true);
    try {
      await handleSyncTeam(false);
      setAddMemberOpen(true);
    } finally {
      setOpeningAddMember(false);
    }
  };

  const handleOpenSettings = async () => {
    await handleSyncTeam(false);
    setSettingsOpen(true);
  };

  const handleOpenRemark = () => {
    setRemarkDraft(team.remark ?? '');
    setRemarkError('');
    setRemarkOpen(true);
  };

  const handleRemarkOpenChange = (open: boolean) => {
    if (savingRemark) return;
    if (open) {
      setRemarkDraft(team.remark ?? '');
      setRemarkError('');
    }
    setRemarkOpen(open);
  };

  const handleSaveRemark = async (event: FormEvent) => {
    event.preventDefault();
    const nextRemark = remarkDraft.trim();
    if (nextRemark.length > 80) {
      setRemarkError('备注不能超过 80 个字符');
      return;
    }

    setSavingRemark(true);
    setRemarkError('');
    try {
      const updated = await updateTeamRemark(team.id, nextRemark);
      onTeamSynced({
        ...updated,
        cached_member_emails: updated.cached_member_emails ?? team.cached_member_emails ?? [],
      });
      setRemarkOpen(false);
    } catch (err) {
      setRemarkError(err instanceof Error ? err.message : '保存失败');
    } finally {
      setSavingRemark(false);
    }
  };

  const statusDotClass = syncError
    ? 'bg-red-500 animate-pulse'
    : authBlocked
    ? 'bg-gray-400'
    : isSubscriptionExpired
      ? 'bg-red-500'
    : isWarning
      ? 'bg-yellow-400 animate-pulse'
      : 'bg-emerald-500';

  const borderClass = syncError
    ? 'border-red-500 ring-1 ring-red-500/40 dark:border-red-500 dark:ring-red-500/30'
    : isSubscriptionExpired
    ? 'border-red-400/60 dark:border-red-500/60'
    : isWarning ? 'border-yellow-400/50 dark:border-yellow-500/50' : 'border-gray-200 dark:border-[#2a2d3a]';

  const hoverBorderClass = syncError
    ? 'hover:border-red-500 dark:hover:border-red-500'
    : 'hover:border-blue-300 dark:hover:border-[#3a3d4a]';

  return (
    <div className="relative">
      <div
        className={`relative bg-white dark:bg-[#1a1d27] rounded-2xl border ${borderClass} ${hoverBorderClass} overflow-hidden transition-all duration-300 hover:shadow-lg group`}
      >
        {authBlocked && (
          <div className="absolute inset-0 bg-white/85 dark:bg-black/75 backdrop-blur-sm flex flex-col items-center justify-center gap-3 z-10 px-5 text-center">
            <div className="w-full space-y-1.5">
              <div className="text-xs font-semibold text-red-500 dark:text-red-400">
                {isAuthExpired ? 'Session 失效' : '授权已被吊销'}
              </div>
              <div className="text-xs text-gray-600 dark:text-gray-400">
                需重新导入 session
                {!isAuthExpired && authBlockedSince && ` · ${authBlockedSince}`}
              </div>
              <div className="break-words text-base font-bold text-gray-900 dark:text-gray-100">
                {team.name}
                {team.remark && (
                  <span className="font-semibold text-gray-500 dark:text-gray-400">（{team.remark}）</span>
                )}
              </div>
              <div className="inline-flex max-w-full items-center justify-center gap-1.5 text-xs text-gray-600 dark:text-gray-400">
                <span className="break-all">{team.owner_email}</span>
                <button
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation();
                    void handleCopyOwnerEmail();
                  }}
                  className={`shrink-0 rounded-md p-1 transition-colors ${ownerEmailCopied
                    ? 'text-emerald-500 dark:text-emerald-400'
                    : 'text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-200'
                  }`}
                  title="复制邮箱"
                  aria-label={`复制 ${team.owner_email}`}
                >
                  {ownerEmailCopied ? <Check size={14} /> : <Copy size={14} />}
                </button>
              </div>
              <div className="inline-flex rounded bg-gray-100 px-2 py-0.5 font-mono text-xs text-gray-500 dark:bg-gray-800 dark:text-gray-400" title={team.id}>
                ID {shortTeamId(team.id)}
              </div>
            </div>
            <button
              onClick={(e) => { e.stopPropagation(); onReimport(team); }}
              className="flex items-center gap-2 px-5 py-2.5 bg-blue-600 hover:bg-blue-700 text-white rounded-xl text-sm font-semibold transition-all transform hover:scale-105 shadow-md"
            >
              <KeyRound size={16} /> 重新导入
            </button>
            <button
              onClick={(e) => { e.stopPropagation(); setConfirmDelete(true); }}
              className="flex items-center gap-2 px-5 py-2.5 bg-red-500 hover:bg-red-600 text-white rounded-xl text-sm font-semibold transition-all transform hover:scale-105 shadow-md"
            >
              <Trash2 size={16} /> 删除
            </button>
          </div>
        )}

        <div
          className="p-5 cursor-pointer"
          onClick={handleToggleExpanded}
        >
          {/* Header */}
          <div className="flex items-start justify-between mb-4">
            <div className="flex flex-col gap-1 w-full overflow-hidden">
              <div className="flex items-center gap-2 min-w-0">
                <span className={`w-2.5 h-2.5 rounded-full shadow-sm ${statusDotClass}`} />
                {syncError && (
                  <span
                    className="shrink-0 rounded bg-red-100 px-1.5 py-0.5 text-[10px] font-semibold text-red-700 dark:bg-red-500/20 dark:text-red-300"
                    title={`刷新失败：${syncError}`}
                  >
                    刷新失败
                  </span>
                )}
                <div className="flex items-center gap-1.5 min-w-0">
                  <h3 className="flex min-w-0 items-baseline overflow-hidden pr-0.5 font-bold text-gray-900 dark:text-gray-100 text-base">
                    <span className="truncate">{team.name}</span>
                    {team.remark && (
                      <span
                        className="ml-1 max-w-[9rem] shrink-0 truncate text-sm font-semibold text-gray-500 dark:text-gray-400"
                        title={team.remark}
                      >
                        （{team.remark}）
                      </span>
                    )}
                  </h3>
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); handleOpenRemark(); }}
                    className="inline-flex shrink-0 items-center gap-1 rounded-md px-1.5 py-1 text-xs font-medium text-gray-400 transition-colors hover:bg-gray-100 hover:text-blue-500 dark:text-gray-500 dark:hover:bg-gray-800 dark:hover:text-blue-400"
                    aria-label="编辑备注，不修改 Workspace 名称"
                    title="编辑备注，不修改 Workspace 名称"
                  >
                    <Pencil size={12} />
                    <span>备注</span>
                  </button>
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400 pl-4">
                <span className="flex min-w-0 items-center gap-1.5">
                  <Mail size={12} className="shrink-0" />
                  <span className="truncate">{team.owner_email}</span>
                </span>
                {team.proxy_id && (
                  <span className="shrink-0 text-blue-400 dark:text-blue-500" title="代理">
                    <Globe size={11} />
                  </span>
                )}
                <span className={`flex shrink-0 items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] font-medium ${team.is_codex_enabled ? 'bg-purple-100 text-purple-600 dark:bg-purple-500/20 dark:text-purple-400' : 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'}`} title={`Codex ${team.is_codex_enabled ? 'ON' : 'OFF'}`}>
                  <Zap size={10} /> {team.is_codex_enabled ? 'Codex ON' : 'Codex OFF'}
                </span>
                <span
                  className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${
                    team.default_seat_type === 'usage_based'
                      ? 'bg-purple-100 text-purple-600 dark:bg-purple-500/20 dark:text-purple-400'
                      : team.default_seat_type === 'default'
                        ? 'bg-blue-100 text-blue-600 dark:bg-blue-500/20 dark:text-blue-400'
                        : 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'
                  }`}
                  title={`默认邀请席位：${defaultSeatLabel}`}
                >
                  Default: {defaultSeatLabel}
                </span>
                <button
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation();
                    setDefaultSeatInfoOpen(true);
                  }}
                  className="inline-flex shrink-0 rounded-full text-gray-400 transition hover:text-gray-500 focus:outline-none focus:ring-2 focus:ring-gray-400/40 dark:text-gray-500 dark:hover:text-gray-400"
                  aria-label="查看默认邀请席位说明"
                  title="默认邀请席位说明"
                >
                  <CircleAlert size={14} />
                </button>
              </div>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              <button
                onClick={(e) => { e.stopPropagation(); void handleForceRefresh(); }}
                disabled={syncing}
                className="text-gray-400 hover:text-emerald-500 dark:text-gray-500 dark:hover:text-emerald-400 transition-all p-1.5 hover:bg-emerald-50 dark:hover:bg-emerald-500/10 rounded-lg disabled:opacity-50"
                aria-label="强制刷新 Team"
                title="强制刷新 Team"
              >
                <RefreshCw size={16} className={syncing ? 'animate-spin' : ''} />
              </button>
              <button
                onClick={(e) => { e.stopPropagation(); void handleOpenSettings(); }}
                className="text-gray-400 hover:text-blue-500 dark:text-gray-500 dark:hover:text-blue-400 transition-all p-1.5 hover:bg-blue-50 dark:hover:bg-blue-500/10 rounded-lg"
                aria-label="Team 设置"
                title="设置"
              >
                <Settings size={16} />
              </button>
              <button
                onClick={(e) => { e.stopPropagation(); setConfirmDelete(true); }}
                className="text-gray-400 hover:text-red-500 dark:text-gray-500 dark:hover:text-red-400 opacity-0 group-hover:opacity-100 transition-all p-1.5 hover:bg-red-50 dark:hover:bg-red-500/10 rounded-lg"
              >
                <Trash2 size={16} />
              </button>
            </div>
          </div>

          {/* Metrics */}
          <div className={team.is_codex_enabled && team.codex_count > 0 ? "grid grid-cols-2 gap-3 mb-5" : "grid grid-cols-1 gap-3 mb-5"}>
            <div className="bg-blue-50/50 dark:bg-blue-900/10 p-3 rounded-xl border border-blue-100/50 dark:border-blue-800/30 flex flex-col items-center justify-center transition-colors group-hover:bg-blue-50 dark:group-hover:bg-blue-900/20">
              <span className="text-xs font-medium text-blue-600 dark:text-blue-400 mb-1.5 flex items-center gap-1.5">
                <Users size={12} className="opacity-80"/> ChatGPT
              </span>
              <div className="flex items-baseline gap-1">
                <span className="text-xl font-bold text-gray-900 dark:text-gray-100">{activeGptSeats}</span>
                <span className="text-sm font-medium text-gray-400 dark:text-gray-500">/ {team.seats_entitled}</span>
              </div>
            </div>
            {team.is_codex_enabled && team.codex_count > 0 && (
              <div className="bg-purple-50/50 dark:bg-purple-900/10 p-3 rounded-xl border border-purple-100/50 dark:border-purple-800/30 flex flex-col items-center justify-center transition-colors group-hover:bg-purple-50 dark:group-hover:bg-purple-900/20">
                <span className="text-xs font-medium text-purple-600 dark:text-purple-400 mb-1.5 flex items-center gap-1.5">
                  <Zap size={12} className="opacity-80"/> Codex
                </span>
                <div className="flex items-baseline gap-1">
                  <span className="text-xl font-bold text-gray-900 dark:text-gray-100">{team.codex_count}</span>
                  <span className="text-sm font-medium text-gray-400 dark:text-gray-500">人</span>
                </div>
              </div>
            )}
          </div>

          {/* Footer Info */}
          <div className="space-y-3">
            <div className="flex items-center justify-between text-sm">
              <div className="flex items-center gap-1.5 text-gray-500 dark:text-gray-400">
                <Calendar size={14} />
                <span>
                  {formatShortDate(team.active_start)} - {formatShortDate(team.active_until)}
                </span>
                {team.days_remaining !== null && (
                  <span className={`ml-1 px-1.5 py-0.5 rounded text-xs font-semibold ${
                    isWarning ? 'bg-yellow-100 text-yellow-700 dark:bg-yellow-500/20 dark:text-yellow-400' 
                    : 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300'
                  }`}>
                    {isSubscriptionExpired ? '已到期' : isSubscriptionStale ? '未同步' : `${team.days_remaining}d`}
                  </span>
                )}
              </div>
              <button
                onClick={(e) => { e.stopPropagation(); void handleOpenAddMember(); }}
                disabled={openingAddMember || isSubscriptionExpired}
                title={isSubscriptionExpired ? '订阅已到期，不能添加成员' : undefined}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-blue-50 hover:bg-blue-100 dark:bg-blue-600/20 text-blue-600 dark:text-blue-400 dark:hover:bg-blue-600/30 rounded-lg text-xs font-semibold transition-colors disabled:cursor-wait disabled:opacity-70"
              >
                {openingAddMember ? (
                  <Loader2 size={14} className="animate-spin" />
                ) : (
                  <UserPlus size={14} />
                )}
                {openingAddMember ? '加载中' : '添加成员'}
              </button>
            </div>

            <div className="flex items-center justify-between text-xs text-gray-500 dark:text-gray-500 pt-3 border-t border-gray-100 dark:border-gray-800">
              <div className="flex items-center gap-1.5">
                {monthlyTotal !== null && subtotal !== null ? (
                  <>
                    <span title={team.discount_amount ? `原价 ${formatAmount(subtotal)}, 优惠 -${formatAmount(team.discount_amount)}` : undefined}>
                      {moneySuffix(team)} {formatAmount(monthlyTotal)}/m
                    </span>
                    {team.discount_amount > 0 && (
                      <>
                        <span className="text-sky-500 dark:text-sky-400">
                          (-{formatAmount(team.discount_amount)})
                        </span>
                        <span className="inline-flex items-center rounded-full bg-sky-50 px-2 py-0.5 text-[10px] font-semibold text-sky-600 dark:bg-sky-500/10 dark:text-sky-300 border border-sky-200/60 dark:border-sky-500/20">
                          {promoLabel(team)}
                        </span>
                      </>
                    )}
                  </>
                ) : team.billing_period === null ? (
                  <span className="text-gray-400 dark:text-gray-600">计费周期未知</span>
                ) : (
                  <span className="text-gray-400 dark:text-gray-600">年付，月费暂不计算</span>
                )}
              </div>
              <ChevronDown
                size={14}
                className={`text-gray-400 dark:text-gray-500 transition-transform duration-300 group-hover:text-blue-500 dark:group-hover:text-blue-400 shrink-0 ${
                  expanded ? 'rotate-180' : ''
                }`}
                aria-hidden
              />
              <div className="flex items-center gap-3">
                <span className="flex items-center gap-1">
                  <DollarSign size={12} /> {team.balance}
                </span>
                {team.card_last4 && (
                  <Tooltip.Provider delayDuration={200}>
                    <Tooltip.Root>
                      <Tooltip.Trigger asChild>
                        <span className="flex items-center gap-1 cursor-default bg-gray-100 dark:bg-gray-800 px-1.5 py-0.5 rounded">
                          <CreditCard size={12} /> {team.card_last4}
                        </span>
                      </Tooltip.Trigger>
                      <Tooltip.Portal>
                        <Tooltip.Content
                          className="bg-white dark:bg-[#2a2d3a] text-gray-900 dark:text-gray-200 text-xs px-2 py-1 rounded shadow-lg z-50 border border-gray-200 dark:border-transparent"
                          sideOffset={5}
                        >
                          {team.card_brand || 'Card'}
                        </Tooltip.Content>
                      </Tooltip.Portal>
                    </Tooltip.Root>
                  </Tooltip.Provider>
                )}
                <span className={
                  isSubscriptionExpired
                    ? 'text-red-500 dark:text-red-400'
                    : isNonRenewing
                      ? 'text-amber-500 dark:text-amber-400'
                      : 'text-gray-400 dark:text-gray-500'
                }>
                  {isSubscriptionExpired ? '订阅已到期' : isSubscriptionStale ? '数据未同步' : isNonRenewing ? '到期不续费' : '正常续费'}
                </span>
              </div>
            </div>
          </div>
        </div>

        <div
          className="overflow-hidden transition-all duration-300 ease-in-out bg-gray-50/50 dark:bg-transparent"
          style={{ maxHeight: expanded ? '600px' : '0px' }}
        >
          <div className="border-t border-gray-100 dark:border-[#2a2d3a] px-2 pb-3">
            <MemberPanel
              teamId={team.id}
              data={membersData}
              loading={membersLoading || syncing}
              settling={settling}
              isCodexEnabled={team.is_codex_enabled}
              onRefresh={startMemberSettle}
              showToast={showToast}
            />
          </div>
        </div>
      </div>

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title="处理 Team"
        message={`“${team.name}”可能只需要更新 Session。重新导入会保留备注、成员期限和管理记录；仍然删除会清除本地管理数据，但不会取消订阅或移除 OpenAI 团队里的成员。`}
        secondaryLabel="重新导入"
        onSecondary={handleReimportInstead}
        confirmLabel="仍然删除"
        destructive
        loading={deleting}
        onConfirm={handleDelete}
      />

      <Dialog.Root open={defaultSeatInfoOpen} onOpenChange={setDefaultSeatInfoOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60" />
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-[calc(100vw-2rem)] max-w-md -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-gray-200 bg-white p-6 shadow-2xl dark:border-[#2a2d3a] dark:bg-[#1a1d27]">
            <Dialog.Title className="flex items-center gap-2 text-lg font-bold text-gray-900 dark:text-gray-100">
              <CircleAlert size={18} className="text-gray-400 dark:text-gray-500" />
              默认邀请席位
            </Dialog.Title>
            <Dialog.Description className="mt-2 text-sm leading-6 text-gray-500 dark:text-gray-400">
              空间邀请新成员时使用的默认席位类型。
            </Dialog.Description>

            <div className="mt-5 rounded-xl border border-gray-100 bg-gray-50 p-4 dark:border-[#2a2d3a] dark:bg-[#0f1117]">
              <div className="flex items-center justify-between gap-3">
                <span className="text-sm text-gray-500 dark:text-gray-400">当前默认席位</span>
                <span className={`rounded-lg px-2.5 py-1 text-xs font-semibold ${
                  team.default_seat_type === 'usage_based'
                    ? 'bg-purple-100 text-purple-700 dark:bg-purple-500/20 dark:text-purple-300'
                    : team.default_seat_type === 'default'
                      ? 'bg-blue-100 text-blue-700 dark:bg-blue-500/20 dark:text-blue-300'
                      : 'bg-gray-200 text-gray-600 dark:bg-gray-800 dark:text-gray-300'
                }`}>
                  {defaultSeatLabel}
                </span>
              </div>
            </div>

            <div className="mt-5 space-y-3 text-sm leading-6 text-gray-600 dark:text-gray-300">
              <p><span className="font-semibold text-gray-900 dark:text-gray-100">管理员：</span>邀请时可以覆盖默认值并选择其他席位，但仍受 Workspace 权限和官方策略限制。</p>
              <p><span className="font-semibold text-gray-900 dark:text-gray-100">普通成员：</span>不能覆盖邀请席位，邀请会跟随 Workspace 的默认席位类型。</p>
              <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800 dark:bg-amber-500/10 dark:text-amber-300">
                默认设为 Codex 通常不会占用 GPT 席位，可能降低意外新增 GPT 计费席位的风险；实际权限、计费和可用性以 OpenAI 实时规则为准。（即：将默认席位设为 Codex 可防止超拉人，策略可能根据官方调整，以实测为准）
              </p>
            </div>

            <div className="mt-6 flex justify-end">
              <Dialog.Close asChild>
                <button className="rounded-lg bg-gray-900 px-4 py-2 text-sm font-semibold text-white transition hover:bg-gray-700 dark:bg-gray-100 dark:text-gray-900 dark:hover:bg-white">
                  知道了
                </button>
              </Dialog.Close>
            </div>
            <Dialog.Close asChild>
              <button
                className="absolute right-4 top-4 rounded-md p-1 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-200"
                aria-label="关闭默认邀请席位说明"
              >
                <X size={16} />
              </button>
            </Dialog.Close>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      <Dialog.Root open={remarkOpen} onOpenChange={handleRemarkOpenChange}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-black/60 z-50" />
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-[calc(100vw-2rem)] max-w-sm -translate-x-1/2 -translate-y-1/2 rounded-xl border border-gray-200 bg-white p-6 shadow-2xl dark:border-[#2a2d3a] dark:bg-[#1a1d27]">
            <Dialog.Title className="flex items-center gap-2 text-lg font-bold text-gray-900 dark:text-gray-100">
              <Pencil size={17} />
              Team 备注
            </Dialog.Title>
            <form className="mt-5 space-y-4" onSubmit={handleSaveRemark}>
              <div>
                <label className="mb-2 block text-sm font-medium text-gray-700 dark:text-gray-300">
                  {team.name}
                </label>
                <input
                  value={remarkDraft}
                  onChange={(e) => {
                    setRemarkDraft(e.target.value);
                    if (remarkError) setRemarkError('');
                  }}
                  maxLength={80}
                  autoFocus
                  placeholder="备注，例如：主力 / 备用"
                  className="w-full rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-sm text-gray-900 placeholder:text-gray-400 transition-all focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/50 dark:border-[#2a2d3a] dark:bg-[#0f1117] dark:text-gray-200 dark:placeholder:text-gray-600"
                />
                <div className="mt-1 flex items-center justify-between text-xs text-gray-400 dark:text-gray-500">
                  <span>留空则清除备注</span>
                  <span>{remarkDraft.trim().length}/80</span>
                </div>
              </div>

              {remarkError && <p className="text-sm text-red-500 dark:text-red-400">{remarkError}</p>}

              <div className="flex justify-end gap-3 pt-1">
                <Dialog.Close asChild>
                  <button
                    type="button"
                    disabled={savingRemark}
                    className="rounded-lg bg-gray-100 px-4 py-2 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-200 disabled:opacity-50 dark:bg-[#2a2d3a] dark:text-gray-300 dark:hover:bg-[#3a3d4a]"
                  >
                    取消
                  </button>
                </Dialog.Close>
                <button
                  type="submit"
                  disabled={savingRemark}
                  className="flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white shadow-md shadow-blue-500/20 transition-all hover:bg-blue-700 disabled:opacity-50"
                >
                  {savingRemark && <Loader2 size={14} className="animate-spin" />}
                  {savingRemark ? '保存中...' : '保存'}
                </button>
              </div>
            </form>
            <Dialog.Close asChild>
              <button
                disabled={savingRemark}
                className="absolute right-4 top-4 rounded-md p-1 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600 disabled:opacity-50 dark:hover:bg-gray-800 dark:hover:text-gray-200"
              >
                <X size={16} />
              </button>
            </Dialog.Close>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      <AddMemberDialog
        open={addMemberOpen}
        onOpenChange={setAddMemberOpen}
        teamId={team.id}
        onSuccess={startMemberSettle}
      />

      <TeamSettingsDialog
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        teamId={team.id}
        currentProxyId={team.proxy_id}
        initialSettings={workspaceSettings}
        onChanged={(settings) => {
          setWorkspaceSettings(settings);
          onTeamSynced({ ...team, default_seat_type: settings.default_seat_type });
        }}
        onProxyChanged={(proxyId) => onTeamSynced({ ...team, proxy_id: proxyId })}
      />
    </div>
  );
}
