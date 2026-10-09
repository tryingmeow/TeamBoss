import { useState, useRef, useEffect, type FormEvent, type ReactNode } from 'react';
import * as Tooltip from '@radix-ui/react-tooltip';
import { UserPlus, Trash2, KeyRound, CreditCard, Globe, Mail, Users, Zap, ChevronDown, RefreshCw, Settings, Pencil, Loader2, CircleAlert, Copy, Check, Gem, Ban, Receipt } from 'lucide-react';
import type { MembersData, SeatType, ShowToast, Team, TeamWorkspaceSettings } from '../types';
import MemberPanel from './MemberPanel';
import ConfirmDialog from './ConfirmDialog';
import DialogFrame from './DialogFrame';
import AddMemberDialog from './AddMemberDialog';
import TeamSettingsDialog from './TeamSettingsDialog';
import TeamBillingDialog from './TeamBillingDialog';
import SeatBetaBadge from './BetaBadge';
import { useMembers } from '../hooks/useMembers';
import { deleteTeam, syncTeam, updateTeamRemark, TeamAuthRejectedError } from '../api/client';
import { billedSeatSummary, chatgptPaidSeats, premiumSeatUsage, teamPendingCounts } from '../lib/seatCapacity';
import { OVERAGE_POLICY_OPTIONS, SEAT_STYLE, SEAT_TYPES, formatSeatTypeLabel, parseOveragePolicy } from '../lib/seatType';
import { formatBeijingDateTime, formatDateSafe } from '../lib/formatDate';
import { formatCredit, formatMoney } from '../lib/money';
import { BUTTON, INPUT, PILL, TONE } from './ui';

interface TeamCardProps {
  team: Team;
  onDelete: (id: string) => void;
  onReimport: (team: Team) => void;
  onTeamSynced: (team: Team) => void;
  onSyncSucceeded: (team: Team) => void;
  syncError?: string;
  showToast: ShowToast;
}

/** Not a status: the policy is a setting, so it gets an outline instead of a status color. */
const POLICY_CHIP =
  'bg-white text-gray-700 ring-1 ring-inset ring-gray-300 dark:bg-transparent dark:text-ink-200 dark:ring-ink-600';

/**
 * One billed seat type on the card: in use / paid, plus the free seats the add and switch
 * dialogs decide with (pending invites hold seats too, so they are counted and shown).
 */
function BilledSeatBlock({
  seatType,
  icon,
  summary,
  className = '',
}: {
  seatType: SeatType;
  icon: ReactNode;
  summary: ReturnType<typeof billedSeatSummary>;
  className?: string;
}) {
  const style = SEAT_STYLE[seatType];
  const label = SEAT_TYPES[seatType].label;
  // 待接受邀请也占席位：在用 + 待接受超过已付就是超出，和添加弹窗、席位菜单同一套说法。
  const { inUse, paid, pending, free, over: overBy, unknown } = summary;
  const over = overBy > 0;
  const usedFill = paid > 0 ? Math.min(100, (inUse / paid) * 100) : inUse > 0 ? 100 : 0;
  const heldFill = paid > 0 ? Math.min(100, ((inUse + pending) / paid) * 100) : 0;
  const status = over
    ? { text: `超出 ${overBy}`, className: 'font-medium text-red-600 dark:text-red-400' }
    : unknown
      ? { text: '空位未知', className: 'text-gray-600 dark:text-ink-300' }
      : free > 0
        ? { text: `空 ${free}`, className: 'text-gray-600 dark:text-ink-300' }
        : { text: '已满', className: 'font-medium text-gray-700 dark:text-ink-200' };
  const title = [
    `${label} 在用 ${inUse} 个，已付 ${paid} 个`,
    pending > 0 ? `待接受 ${pending} 个` : '',
    over ? `超出 ${overBy} 个` : unknown ? '空位未知' : `空位 ${free} 个`,
  ].filter(Boolean).join('，');
  return (
    <div className={`rounded-xl px-3.5 py-3 ${style.surface} ${className}`} title={title}>
      {/* A Beta badge takes the place of the icon and 「席位」 so the row still fits a half-width
          block; where it doesn't, the status wraps to the right of the next line. */}
      <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-0.5 text-xs">
        <span className={`flex min-w-0 items-center gap-1.5 whitespace-nowrap font-medium ${style.text}`}>
          {SEAT_TYPES[seatType].beta ? (
            <>
              {label}
              <SeatBetaBadge seatType={seatType} className="-ml-0.5" />
            </>
          ) : (
            <>
              {icon} {label}<span className="hidden sm:inline"> 席位</span>
            </>
          )}
        </span>
        <span className={`ml-auto shrink-0 whitespace-nowrap ${status.className}`}>{status.text}</span>
      </div>
      <div className="mt-1 flex items-baseline gap-1">
        <span className={`text-xl font-semibold tabular-nums ${over ? 'text-red-600 dark:text-red-400' : style.text}`}>{inUse}</span>
        <span className="text-sm tabular-nums text-gray-500 dark:text-ink-400">/ {paid}</span>
        <span className="ml-auto whitespace-nowrap text-[11px] text-gray-500 dark:text-ink-400">在用 / 已付</span>
      </div>
      <div className={`relative mt-2 h-1 overflow-hidden rounded-full ${style.track}`} aria-hidden="true">
        {heldFill > usedFill && (
          <div className={`absolute inset-y-0 left-0 rounded-full opacity-40 ${over ? 'bg-red-500' : style.solid}`} style={{ width: `${heldFill}%` }} />
        )}
        <div className={`relative h-full rounded-full ${over ? 'bg-red-500' : style.solid}`} style={{ width: `${usedFill}%` }} />
      </div>
      {pending > 0 && (
        <div className="mt-1.5 text-[11px] leading-4 text-gray-600 dark:text-ink-300">待接受 {pending} 个，也占席位</div>
      )}
    </div>
  );
}

/** Tooltip for the 「续费前可减 K 席」 chip: per billed type, then the renewal time and what to do. */
function renewalIdleTitle(idle: NonNullable<Team['renewal_idle_seats']>): string {
  const lines = idle.lines.map((line) => {
    const paid = line.renewing === line.paid ? `已付 ${line.paid}` : `已付 ${line.paid} · 续费 ${line.renewing}`;
    return `${formatSeatTypeLabel(line.seat_type)}：${paid} · 在用 ${line.in_use} · 待接受 ${line.pending} · 空闲 ${line.idle}`;
  });
  return [
    ...lines,
    `续费时间：${formatBeijingDateTime(idle.renews_at)}`,
    '续费时空闲席位照样扣费。不需要的话，在 ChatGPT 后台「管理席位」里减少，下个计费周期生效。',
  ].join('\n');
}

function formatShortDate(value: string | Date | null): string {
  if (!value) return '—';
  const d = new Date(value);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

const CARD_BRANDS: Record<string, string> = {
  visa: 'Visa',
  mastercard: 'Mastercard',
  amex: 'Amex',
  american_express: 'Amex',
  discover: 'Discover',
  jcb: 'JCB',
  unionpay: 'UnionPay',
  diners: 'Diners',
};

function cardBrandLabel(brand: string): string {
  const key = brand.trim().toLowerCase().replace(/\s+/g, '_');
  return CARD_BRANDS[key] ?? brand;
}

function discountedMonthlyTotal(team: Team): number | null {
  return typeof team.monthly_total === 'number' ? team.monthly_total : null;
}

function monthlySubtotal(team: Team): number | null {
  return typeof team.monthly_subtotal === 'number' ? team.monthly_subtotal : null;
}

function periodTotal(team: Team): number | null {
  return team.period_total ?? team.period_subtotal ?? null;
}

/**
 * Tooltip of the 月费 figure: what the server's monthly_total is made of (Team currency,
 * tax-exclusive). Premium is in the total only when its real price is known. Seat prices are per
 * month; a yearly Team's are the annual-plan prices per month and its discount is per year.
 */
function monthlyFeeTitle(team: Team, total: number, premiumPaid: number, unit: string): string {
  const yearly = team.billing_period === 'yearly';
  const perMonth = yearly ? '/月' : '';
  const lines: string[] = yearly ? ['年付：按年扣费，这里折成每月（月均）'] : [];
  if (team.price_per_seat !== null) {
    lines.push(`ChatGPT ${formatMoney(team.price_per_seat, unit)}${perMonth} × ${chatgptPaidSeats(team)} 席`);
  }
  if (premiumPaid > 0) {
    lines.push(typeof team.premium_price_per_seat === 'number'
      ? `Premium ${formatMoney(team.premium_price_per_seat, unit)}${perMonth} × ${premiumPaid} 席`
      : `Premium ${premiumPaid} 席：单价未知，总额暂不计算`);
  }
  if ((team.discount_amount ?? 0) > 0) lines.push(`优惠 -${formatMoney(team.discount_amount, unit)}${yearly ? '/年' : ''}`);
  if (team.monthly_total == null) lines.push('按已知席位单价计算的原价');
  const annual = periodTotal(team);
  lines.push(yearly
    ? `${annual !== null ? `一年 ${formatMoney(annual, unit)}，` : ''}月均 ${formatMoney(total, unit)}/月，不含税；按年付月价 × 12 推算，以 ChatGPT 账单为准`
    : `合计 ${formatMoney(total, unit)}/月，不含税，以 ChatGPT 账单为准`);
  return lines.join('\n');
}

function promoLabel(team: Team): string {
  if (team.discount_expires_at && !Number.isNaN(Date.parse(team.discount_expires_at))) {
    return `优惠至 ${formatDateSafe(team.discount_expires_at, 'yyyy-MM-dd')}`;
  }
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
  const [showExactTime, setShowExactTime] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [addMemberOpen, setAddMemberOpen] = useState(false);
  const inviteRequestId = useRef(0);
  const [inviteError, setInviteError] = useState('');
  const [inviteSnapshot, setInviteSnapshot] = useState<Awaited<ReturnType<typeof syncTeam>> | null>(null);

  const [settingsOpen, setSettingsOpen] = useState(false);
  const [billingOpen, setBillingOpen] = useState(false);
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
  // 连续 24 小时同步失败后后端已经停发定时请求，只按 6 小时探活一次。
  const isSyncSuspended = Boolean(team.sync_suspended_at);
  const syncFailingFor = brokenForLabel(team.sync_failing_since);
  const isSubscriptionExpired = team.subscription_status === 'expired';
  const isSubscriptionStale = team.subscription_status === 'stale';
  const isNonRenewing = team.subscription_status === 'nonrenewing';
  const isWarning = (isNonRenewing || (team.days_remaining !== null && team.days_remaining <= 3))
    && !authBlocked
    && !isSubscriptionExpired;
  const subtotal = monthlySubtotal(team);
  const confirmedMonthlyTotal = discountedMonthlyTotal(team);
  const monthlyTotal = confirmedMonthlyTotal ?? subtotal;
  const usingOriginalPrice = confirmedMonthlyTotal === null && subtotal !== null;
  const [openingAddMember, setOpeningAddMember] = useState(false);
  const defaultSeatLabel = team.default_seat_type
    ? formatSeatTypeLabel(team.default_seat_type)
    : '—';
  const defaultSeatPill = team.default_seat_type === 'usage_based' || team.default_seat_type === 'default'
    ? SEAT_STYLE[team.default_seat_type].pill
    : TONE.neutral;

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

  // 备注只存在本机（user_display_names，按邮箱），ChatGPT 那边没有这个概念：
  // 保存后就地改这张卡片的成员列表,不要走 startMemberSettle 那条轮询 ChatGPT 的链。
  const handleRemarkSaved = (email: string, remark: string | null) => {
    const key = email.trim().toLowerCase();
    const apply = <T extends { email: string; system_display_name?: string | null }>(rows: T[]): T[] =>
      rows.map((row) => (row.email.trim().toLowerCase() === key ? { ...row, system_display_name: remark } : row));
    setMembersData((prev) =>
      prev
        ? { ...prev, members: apply(prev.members), pending_invites: apply(prev.pending_invites) }
        : prev
    );
  };

  // 请求排序守卫:手动同步和轮询同步共用同一个自增 id,只有最新发出的那次
  // 请求的结果才允许写 state,避免慢的旧响应覆盖后到的新响应。
  const latestRequestId = useRef(0);
  const mountedRef = useRef(true);

  const handleSyncTeam = async (force: boolean) => {
    if (syncing) return;
    setSyncing(true);
    const requestId = ++latestRequestId.current;
    try {
      const result = await syncTeam(team.id, force);
      if (!mountedRef.current || requestId !== latestRequestId.current) return;
      setMembersData(result.members);
      setWorkspaceSettings(result.workspace_settings);
      onSyncSucceeded(result.team);
    } catch (err) {
      if (!mountedRef.current || requestId !== latestRequestId.current) return;
      if (err instanceof TeamAuthRejectedError) {
        // 后端刚把它判成 rejected，卡片立刻挂上遮罩，不用等下次刷新列表。
        onSyncSucceeded({ ...team, auth_state: 'rejected' });
        showToast(err.message, 'error');
        return;
      }
      await refreshMembers(false);
      const errorMsg = err instanceof Error ? err.message : '同步失败';
      showToast(`同步失败，当前显示的是缓存数据${errorMsg ? '：' + errorMsg : ''}`, 'error');
    } finally {
      if (mountedRef.current) setSyncing(false);
    }
  };

  // ── 操作后自动刷新:先静默给 ChatGPT 生效时间,再阶梯轮询,检测到变化即停 ──
  const [settling, setSettling] = useState(false);
  const settleTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const settleStart = useRef(0);
  const settleBaseline = useRef('');
  // 每次 stopSettle/startMemberSettle/卸载都会递增:一次 tick 醒来时如果
  // epoch 已经变了,说明它属于一条已经作废的轮询链(被折叠/新操作/卸载取代),
  // 结果直接丢弃,不写 state 也不再安排下一次。
  const settleEpoch = useRef(0);

  const stopSettle = () => {
    settleEpoch.current += 1;
    if (settleTimer.current) {
      clearTimeout(settleTimer.current);
      settleTimer.current = null;
    }
    setSettling(false);
  };

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      settleEpoch.current += 1;
      if (settleTimer.current) clearTimeout(settleTimer.current);
      if (ownerEmailCopyTimer.current) clearTimeout(ownerEmailCopyTimer.current);
    };
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

    const epoch = settleEpoch.current;
    settleTimer.current = setTimeout(async () => {
      const requestId = ++latestRequestId.current;
      try {
        const result = await syncTeam(team.id, true);
        // 卸载了,或者这条轮询链已经被折叠/新操作/新一轮 start 取代:丢弃结果。
        if (!mountedRef.current || epoch !== settleEpoch.current) return;
        if (requestId === latestRequestId.current) {
          setMembersData(result.members);
          setWorkspaceSettings(result.workspace_settings);
          onSyncSucceeded(result.team);
        }
        if (membersSignature(result.members) !== settleBaseline.current) {
          stopSettle();   // 变化已在 ChatGPT 侧生效,收工
          return;
        }
      } catch { /* 网络抖动忽略,继续下一档 */ }
      if (!mountedRef.current || epoch !== settleEpoch.current) return;
      scheduleSettlePoll();
    }, delay);
  };

  // 邀请 / 踢人 / 撤邀请后调用:记下操作前的成员指纹,启动阶梯轮询。
  const startMemberSettle = () => {
    // 递增 epoch,让上一条链(如果还有 tick 挂在 await 上)在醒来时发现自己
    // 已经作废,而不会跟这条新链抢 settleTimer.current 或互相覆盖状态。
    settleEpoch.current += 1;
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
    } else {
      stopSettle();   // 收起面板后不再需要看不见的轮询继续打 sync?force=true
    }
  };

  const handleForceRefresh = async () => {
    if (!expanded) setExpanded(true);
    await handleSyncTeam(true);
  };

  const initializeInvite = async () => {
    const requestId = ++inviteRequestId.current;
    const syncRequestId = ++latestRequestId.current;
    setOpeningAddMember(true);
    setInviteError('');
    setInviteSnapshot(null);
    try {
      const result = await syncTeam(team.id, false);
      if (!mountedRef.current || requestId !== inviteRequestId.current) return;
      setInviteSnapshot(result);
      if (syncRequestId === latestRequestId.current) {
        setMembersData(result.members);
        setWorkspaceSettings(result.workspace_settings);
        onSyncSucceeded(result.team);
      }
    } catch (err) {
      if (!mountedRef.current || requestId !== inviteRequestId.current) return;
      setInviteError(err instanceof Error ? err.message : '加载失败');
    } finally {
      if (mountedRef.current && requestId === inviteRequestId.current) setOpeningAddMember(false);
    }
  };

  const handleInviteOpenChange = (open: boolean) => {
    if (!open) {
      ++inviteRequestId.current;
      setOpeningAddMember(false);
    }
    setAddMemberOpen(open);
  };

  const handleOpenAddMember = () => {
    setAddMemberOpen(true);
    void initializeInvite();
  };

  const handleOpenSettings = () => setSettingsOpen(true);

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
      ? 'bg-amber-400 animate-pulse'
      : 'bg-emerald-500';

  const statusLabel = syncError
    ? '刷新失败'
    : authBlocked
    ? '登录失效'
    : isSubscriptionExpired
      ? '订阅已到期'
    : isWarning
      ? '即将到期'
      : '正常';

  const borderClass = syncError
    ? 'border-red-500 ring-1 ring-red-500/40 dark:border-red-500 dark:ring-red-500/30'
    : isSubscriptionExpired
    ? 'border-red-400/60 dark:border-red-500/60'
    : isWarning ? 'border-amber-300 dark:border-amber-500/50' : 'border-gray-200 dark:border-ink-800';

  const hoverBorderClass = syncError
    ? 'hover:border-red-500 dark:hover:border-red-500'
    : 'hover:border-blue-300 dark:hover:border-ink-700';

  const unit = moneySuffix(team);
  const teamDisplayName = team.remark ? `${team.name}（${team.remark}）` : team.name;
  // 已付 ChatGPT 席位：有分类型数据时用它（seats_entitled 可能把 Premium 也算进去）。
  const showCodex = team.is_codex_enabled && team.codex_count > 0;
  const premium = premiumSeatUsage(team);
  // 待接受邀请：成员名单拉到了就用它（最新），否则用列表接口缓存的计数。和弹窗用同一份数字。
  const pendingByType = teamPendingCounts(team, membersData?.pending_invites);
  const gptSeats = billedSeatSummary(team, 'default', pendingByType);
  const premiumSeats = premium ? billedSeatSummary(team, 'prolite', pendingByType) : null;
  const seatBlocks = 1 + (showCodex ? 1 : 0) + (premium ? 1 : 0);
  const premiumPaidSeats = premium?.paid ?? 0;
  const isYearly = team.billing_period === 'yearly';
  const annualTotal = isYearly ? periodTotal(team) : null;
  // 默认的「超员需确认」不挂标签；另外两种会改变花钱方式，挂在卡片上一眼能看到。
  const overagePolicy = parseOveragePolicy(team.overage_policy);
  // 续费前 3 天内还有没人用的计费席位（后端判定，窗口外 / 数据不全时为 null）。
  const renewalIdle = team.renewal_idle_seats ?? null;
  const overagePolicyOption = OVERAGE_POLICY_OPTIONS.find((option) => option.value === overagePolicy)!;

  const renewalLabel = isSubscriptionExpired || isNonRenewing ? '到期' : '续费';
  const renewalWhen = isSubscriptionExpired
    ? '已到期'
    : isSubscriptionStale
      ? '数据未同步'
      : team.days_remaining === null
        ? ''
        : team.days_remaining <= 0
          ? '今天'
          : `${team.days_remaining} 天后`;
  const renewalTone = isSubscriptionExpired
    ? 'text-red-600 dark:text-red-400'
    : isWarning
      ? 'text-amber-600 dark:text-amber-400'
      : 'text-gray-900 dark:text-gray-100';

  return (
    <div className="relative">
      <div
        className={`group relative overflow-hidden rounded-2xl border bg-white transition-[border-color,box-shadow] duration-200 hover:shadow-md dark:bg-ink-900 dark:hover:shadow-black/30 ${borderClass} ${hoverBorderClass}`}
      >
        {authBlocked && (
          <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-3 bg-white/90 px-5 text-center backdrop-blur-sm dark:bg-ink-950/85">
            <div className="w-full space-y-1.5">
              <div className="text-xs font-semibold text-red-600 dark:text-red-400">
                {isAuthExpired ? 'Session 已失效' : '登录已失效'}
              </div>
              <div className="text-xs text-gray-600 dark:text-ink-300">
                需要重新导入 Session
                {!isAuthExpired && authBlockedSince && ` · ${authBlockedSince}`}
                {isSyncSuspended && ' · 已暂停自动同步'}
              </div>
              <div className="break-words pt-1 text-base font-semibold text-gray-900 dark:text-gray-100">
                {team.name}
                {team.remark && (
                  <span className="font-medium text-gray-500 dark:text-ink-400">（{team.remark}）</span>
                )}
              </div>
              <div className="inline-flex max-w-full items-center justify-center gap-1.5 text-xs text-gray-600 dark:text-ink-300">
                <span className="break-all">{team.owner_email}</span>
                <button
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation();
                    void handleCopyOwnerEmail();
                  }}
                  className={`shrink-0 rounded-md p-1 transition-colors ${ownerEmailCopied
                    ? 'text-emerald-500 dark:text-emerald-400'
                    : 'text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-ink-800 dark:hover:text-gray-200'
                  }`}
                  title="复制邮箱"
                  aria-label={`复制 ${team.owner_email}`}
                >
                  {ownerEmailCopied ? <Check size={14} /> : <Copy size={14} />}
                </button>
              </div>
              <div className="inline-flex rounded bg-gray-100 px-2 py-0.5 font-mono text-xs text-gray-500 dark:bg-ink-800 dark:text-ink-400" title={team.id}>
                ID {shortTeamId(team.id)}
              </div>
            </div>
            <div className="flex flex-wrap items-center justify-center gap-2 pt-1">
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); onReimport(team); }}
                className={BUTTON.primary}
              >
                <KeyRound size={16} /> 重新导入
              </button>
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); setConfirmDelete(true); }}
                className={`${BUTTON.secondary} text-red-600 hover:text-red-700 dark:text-red-400 dark:hover:text-red-300`}
              >
                <Trash2 size={16} /> 删除
              </button>
            </div>
          </div>
        )}

        <div className="cursor-pointer p-4 sm:p-5" onClick={handleToggleExpanded}>
          {/* Header */}
          <div className="flex items-center gap-2">
            <span className={`size-2.5 shrink-0 rounded-full ${statusDotClass}`} title={statusLabel} aria-label={statusLabel} />
            <h3 className="flex min-w-0 items-baseline text-base font-semibold text-gray-900 dark:text-gray-50">
              <span className="max-w-full shrink-0 truncate" title={team.name}>{team.name}</span>
              {team.remark && (
                <span
                  className="ml-1 min-w-0 max-w-[10rem] truncate text-sm font-medium text-gray-500 dark:text-ink-400"
                  title={team.remark}
                >
                  （{team.remark}）
                </span>
              )}
            </h3>
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); handleOpenRemark(); }}
              className="shrink-0 rounded-md p-1 text-gray-400 transition-colors hover:bg-gray-100 hover:text-blue-600 dark:text-ink-500 dark:hover:bg-ink-800 dark:hover:text-blue-400"
              aria-label="编辑备注"
              title="编辑备注"
            >
              <Pencil size={13} />
            </button>
            <div className="-my-1 -mr-1.5 ml-auto flex shrink-0 items-center">
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); void handleForceRefresh(); }}
                disabled={syncing}
                className="rounded-lg p-2 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 disabled:opacity-50 dark:text-ink-500 dark:hover:bg-ink-800 dark:hover:text-gray-200"
                aria-label="强制刷新这个 Team"
                title="强制刷新"
              >
                <RefreshCw size={16} className={syncing ? 'animate-spin' : ''} />
              </button>
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); void handleOpenSettings(); }}
                className="rounded-lg p-2 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 dark:text-ink-500 dark:hover:bg-ink-800 dark:hover:text-gray-200"
                aria-label="Team 设置"
                title="Team 设置"
              >
                <Settings size={16} />
              </button>
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); setConfirmDelete(true); }}
                className="rounded-lg p-2 text-gray-400 transition-colors hover:bg-red-50 hover:text-red-600 focus-visible:opacity-100 sm:opacity-0 sm:group-hover:opacity-100 dark:text-ink-500 dark:hover:bg-red-500/10 dark:hover:text-red-400"
                aria-label="删除这个 Team"
                title="删除"
              >
                <Trash2 size={16} />
              </button>
            </div>
          </div>
          <div className="mt-1 flex min-w-0 items-center gap-1.5 pl-[18px] text-xs text-gray-500 dark:text-ink-400">
            <Mail size={12} className="shrink-0" />
            <span className="truncate" title={team.owner_email}>{team.owner_email}</span>
            {team.proxy_id && (
              <span className="shrink-0 text-blue-500 dark:text-blue-400" title="通过代理连接">
                <Globe size={12} />
              </span>
            )}
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-1.5 pl-[18px]">
            {syncError && (
              <span className={`${PILL} ${TONE.danger}`} title={`刷新失败：${syncError}`}>
                刷新失败
              </span>
            )}
            {isSyncSuspended && !authBlocked && (
              <span
                className={`${PILL} ${TONE.neutral}`}
                title={`连续同步失败${syncFailingFor ? ` · ${syncFailingFor}` : ''}，已暂停自动同步`}
              >
                同步已暂停
              </span>
            )}
            {renewalIdle && renewalIdle.total_idle > 0 && (
              <span className={`${PILL} ${TONE.warning}`} title={renewalIdleTitle(renewalIdle)}>
                续费前可减 {renewalIdle.total_idle} 席
              </span>
            )}
            <span className={`${PILL} ${team.is_codex_enabled ? SEAT_STYLE.usage_based.pill : TONE.neutral}`}>
              <Zap size={11} /> {team.is_codex_enabled ? 'Codex 已开' : 'Codex 未开'}
            </span>
            {overagePolicy !== 'confirm' && (
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  void handleOpenSettings();
                }}
                className={`${PILL} ${POLICY_CHIP} transition-colors hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/50 dark:hover:bg-ink-800`}
                title={`超员策略：${overagePolicyOption.hint}。点击修改`}
              >
                {overagePolicy === 'auto' ? <CreditCard size={11} /> : <Ban size={11} />}
                {overagePolicyOption.label}
              </button>
            )}
            <span
              className={`${PILL} ${defaultSeatPill}`}
            >
              默认席位 {defaultSeatLabel}
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  setDefaultSeatInfoOpen(true);
                }}
                className="-mr-0.5 inline-flex rounded-full opacity-70 transition hover:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/50"
                aria-label="默认邀请席位说明"
                title="默认邀请席位说明"
              >
                <CircleAlert size={12} />
              </button>
            </span>
          </div>

          {/* Seats: each type is billed differently, so each box wears its seat color. With all
              three, ChatGPT takes the full first row and Codex / Premium share the second. */}
          <div className={`mt-4 grid gap-3 ${seatBlocks > 1 ? 'grid-cols-2' : 'grid-cols-1'}`}>
            <BilledSeatBlock
              seatType="default"
              icon={<Users size={13} className="shrink-0" />}
              summary={gptSeats}
              className={seatBlocks === 3 ? 'col-span-2' : ''}
            />
            {showCodex && (
              <div className={`rounded-xl px-3.5 py-3 ${SEAT_STYLE.usage_based.surface}`}>
                <div className={`flex items-center gap-1.5 whitespace-nowrap text-xs font-medium ${SEAT_STYLE.usage_based.text}`}>
                  <Zap size={13} /> Codex 成员
                </div>
                <div className="mt-1 flex items-baseline gap-1">
                  <span className={`text-xl font-semibold tabular-nums ${SEAT_STYLE.usage_based.text}`}>{team.codex_count}</span>
                  <span className="text-sm text-gray-500 dark:text-ink-400">人</span>
                </div>
              </div>
            )}
            {premiumSeats && (
              <BilledSeatBlock seatType="prolite" icon={<Gem size={13} className="shrink-0" />} summary={premiumSeats} />
            )}
          </div>

          {/* Billing facts */}
          <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3">
            <div className="min-w-0">
              <dt className={`text-[11px] leading-4 ${isNonRenewing && !isSubscriptionExpired ? 'text-amber-600 dark:text-amber-400' : 'text-gray-400 dark:text-ink-500'}`}>
                {renewalLabel}{isNonRenewing && !isSubscriptionExpired ? ' · 不续费' : ''}
              </dt>
              <dd className={`mt-0.5 flex min-w-0 items-center gap-1 text-sm font-medium ${renewalTone}`}>
                <span
                  className={`truncate ${team.active_until ? 'cursor-pointer hover:opacity-80' : ''}`}
                  onClick={team.active_until ? (e) => { e.stopPropagation(); setShowExactTime((v) => !v); } : undefined}
                >
                  {formatShortDate(team.active_until)}
                  {renewalWhen && <span className="font-normal text-gray-500 dark:text-ink-400"> · {renewalWhen}</span>}
                </span>
                {team.active_until && (
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); setShowExactTime((v) => !v); }}
                    aria-expanded={showExactTime}
                    aria-label="查看具体续费时间"
                    title="具体时间"
                    className="shrink-0 rounded p-0.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:text-ink-500 dark:hover:bg-ink-800 dark:hover:text-gray-200"
                  >
                    <ChevronDown size={14} className={`transition-transform ${showExactTime ? 'rotate-180' : ''}`} />
                  </button>
                )}
              </dd>
            </div>
            <div className="min-w-0">
              <dt className="text-[11px] leading-4 text-gray-400 dark:text-ink-500">
                月费
                {usingOriginalPrice && (
                  <Tooltip.Root disableHoverableContent>
                    <Tooltip.Trigger asChild>
                      <span role="img" tabIndex={0} aria-label="未找到优惠信息，显示计算原价" className="ml-1 inline-flex align-middle text-gray-400 outline-none dark:text-ink-500">
                        <CircleAlert size={13} aria-hidden="true" />
                      </span>
                    </Tooltip.Trigger>
                    <Tooltip.Portal>
                      <Tooltip.Content className="z-50 rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-700 shadow-lg dark:border-ink-700 dark:bg-ink-800 dark:text-gray-200" sideOffset={6}>
                        未找到优惠信息，显示计算原价
                      </Tooltip.Content>
                    </Tooltip.Portal>
                  </Tooltip.Root>
                )}
              </dt>
              <dd className="mt-0.5 min-w-0 text-sm font-medium text-gray-900 dark:text-gray-100">
                {monthlyTotal !== null && subtotal !== null ? (
                  <>
                    <span
                      className="whitespace-nowrap"
                      title={monthlyFeeTitle(team, monthlyTotal, premiumPaidSeats, unit)}
                    >
                      {formatMoney(monthlyTotal, unit)}
                      <span className="font-normal text-gray-400 dark:text-ink-500"> /月</span>
                    </span>
                    {/* 年付：上面是按年总额折成的每月，年总额另起一行（按年付月价 × 12 推算，以账单为准）。 */}
                    {isYearly && (
                      <span className="block text-xs font-normal text-gray-500 dark:text-ink-400">
                        年付·月均{annualTotal !== null && <> · <span className="whitespace-nowrap tabular-nums">一年 {formatMoney(annualTotal, unit)}</span></>}
                      </span>
                    )}
                    {(team.discount_amount ?? 0) > 0 && (
                      <span
                        className="block truncate text-xs font-normal text-sky-600 dark:text-sky-400"
                        title={isYearly ? `折扣按年扣：每次年付续费减 ${formatMoney(team.discount_amount, unit)}` : undefined}
                      >
                        -{formatMoney(team.discount_amount, unit)}{isYearly ? '/年' : ''} {promoLabel(team)}
                      </span>
                    )}
                  </>
                ) : team.billing_period === null ? (
                  <span className="font-normal text-gray-400 dark:text-ink-500">计费周期未知</span>
                ) : (
                  <span
                    className="font-normal text-gray-500 dark:text-ink-400"
                    title={team.billing_period === 'monthly' || team.billing_period === 'yearly'
                      ? '尚未读到全部已付席位单价，总额暂不计算'
                      : `不认识的计费周期「${team.billing_period}」，月费暂不计算`}
                  >
                    单价未知
                  </span>
                )}
              </dd>
            </div>
            {showExactTime && team.active_until && (
              <div className="col-span-2 -mt-1 rounded-lg bg-gray-50 px-3 py-2 text-xs tabular-nums text-gray-500 dark:bg-ink-950/60 dark:text-ink-400">
                {formatBeijingDateTime(team.active_until)}
              </div>
            )}
            <div className="min-w-0">
              <dt className="text-[11px] leading-4 text-gray-400 dark:text-ink-500">Credit</dt>
              <dd className="mt-0.5 truncate text-sm font-medium tabular-nums text-gray-900 dark:text-gray-100" title="账户 Credit">
                {formatCredit(team.balance)}
              </dd>
            </div>
            <div className="min-w-0">
              <dt className="text-[11px] leading-4 text-gray-400 dark:text-ink-500">付款卡</dt>
              <dd className="mt-0.5 flex min-w-0 items-center gap-1.5 text-sm font-medium text-gray-900 dark:text-gray-100">
                {/* 行首不再放卡片图标：「付款卡」标签已经说明了，省下的宽度留给后面的账单按钮，免得卡品牌被截断。 */}
                {team.card_last4 ? (
                  <>
                    {team.card_brand && (
                      <span className="min-w-0 truncate" title={cardBrandLabel(team.card_brand)}>{cardBrandLabel(team.card_brand)}</span>
                    )}
                    <span className="shrink-0 tabular-nums">···· {team.card_last4}</span>
                  </>
                ) : (
                  <span className="font-normal text-gray-400 dark:text-ink-500">未绑定</span>
                )}
                {/* 没有付款卡时，只有已经同步到账单才放入口。 */}
                {(team.card_last4 || (team.invoice_count ?? 0) > 0) && (
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); setBillingOpen(true); }}
                    aria-label="查看账单"
                    title="查看账单"
                    className="-my-0.5 shrink-0 rounded p-0.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:text-ink-500 dark:hover:bg-ink-800 dark:hover:text-gray-200"
                  >
                    <Receipt size={14} />
                  </button>
                )}
              </dd>
            </div>
          </dl>

          {/* Footer */}
          <div className="mt-4 flex items-center justify-between gap-3 border-t border-gray-100 pt-3 dark:border-ink-800">
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); void handleOpenAddMember(); }}
                disabled={isSubscriptionExpired}
                title={isSubscriptionExpired ? '订阅已到期，不能添加成员' : undefined}
                className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-lg bg-blue-50 px-3 py-1.5 text-xs font-semibold text-blue-700 transition-colors hover:bg-blue-100 disabled:cursor-not-allowed disabled:opacity-60 dark:bg-blue-500/15 dark:text-blue-300 dark:hover:bg-blue-500/25"
              >
                <UserPlus size={14} />
                添加成员
              </button>
            </div>
            <span className="inline-flex items-center gap-1 whitespace-nowrap text-xs text-gray-400 transition-colors group-hover:text-blue-600 dark:text-ink-500 dark:group-hover:text-blue-400">
              {expanded ? '收起成员' : '查看成员'}
              <ChevronDown size={14} className={`transition-transform duration-300 ${expanded ? 'rotate-180' : ''}`} aria-hidden />
            </span>
          </div>
        </div>

        {/* Animates to the panel's natural height (grid 0fr → 1fr), so long member lists are never clipped. */}
        <div
          className={`grid bg-gray-50/60 transition-[grid-template-rows] duration-300 ease-in-out dark:bg-ink-950/40 ${expanded ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}
        >
          <div className="min-h-0 overflow-hidden" inert={!expanded}>
            <div className="border-t border-gray-100 px-2 pb-3 dark:border-ink-800">
              <MemberPanel
                teamId={team.id}
                team={team}
                data={membersData}
                loading={membersLoading || syncing}
                settling={settling}
                isCodexEnabled={team.is_codex_enabled}
                onRefresh={startMemberSettle}
                onForceRefresh={() => void handleForceRefresh()}
                onRemarkSaved={handleRemarkSaved}
                showToast={showToast}
              />
            </div>
          </div>
        </div>
      </div>

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={`删除 Team「${team.name}」`}
        message={`仅从 TeamBoss 移除本地管理数据，不会影响 OpenAI 端的订阅与成员。`}
        secondaryLabel="重新导入"
        onSecondary={handleReimportInstead}
        confirmLabel="确认删除"
        destructive
        loading={deleting}
        onConfirm={handleDelete}
      />

      <DialogFrame
        open={defaultSeatInfoOpen}
        onOpenChange={setDefaultSeatInfoOpen}
        title="默认邀请席位"
        description="这个 Team 邀请新成员时默认使用的席位类型。"
        footer={
          <button type="button" onClick={() => setDefaultSeatInfoOpen(false)} className={BUTTON.primary}>
            知道了
          </button>
        }
      >
        <div className="flex items-center justify-between gap-3 rounded-lg bg-gray-50 px-4 py-3 dark:bg-ink-950/60">
          <span className="text-sm text-gray-500 dark:text-ink-400">当前默认席位</span>
          <span
            className={`${PILL} ${defaultSeatPill}`}
          >
            {defaultSeatLabel}
          </span>
        </div>
        <div className="mt-4 space-y-2 text-sm leading-6 text-gray-600 dark:text-ink-300">
          <p>管理员邀请时可自选席位，成员邀请使用这个默认值。</p>
          <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800 dark:bg-amber-500/10 dark:text-amber-300">
            默认设为 Codex 可避免新成员占用付费席位。
          </p>
        </div>
      </DialogFrame>

      <DialogFrame
        open={remarkOpen}
        onOpenChange={handleRemarkOpenChange}
        size="sm"
        title="Team 备注"
        description="设置本地显示备注，便于日常识别与管理。"
        footer={
          <>
            <button type="button" onClick={() => handleRemarkOpenChange(false)} disabled={savingRemark} className={BUTTON.secondary}>
              取消
            </button>
            <button type="submit" form={`team-remark-${team.id}`} disabled={savingRemark} className={BUTTON.primary}>
              {savingRemark && <Loader2 size={14} className="animate-spin" />}
              {savingRemark ? '保存中…' : '保存'}
            </button>
          </>
        }
      >
        <form id={`team-remark-${team.id}`} onSubmit={handleSaveRemark}>
          <label htmlFor={`team-remark-input-${team.id}`} className="mb-2 block text-sm font-medium text-gray-700 dark:text-gray-300">
            {team.name}
          </label>
          <input
            id={`team-remark-input-${team.id}`}
            value={remarkDraft}
            onChange={(e) => {
              setRemarkDraft(e.target.value);
              if (remarkError) setRemarkError('');
            }}
            maxLength={80}
            autoFocus
            placeholder="例如：主力 / 备用"
            className={INPUT}
          />
          <div className="mt-1 flex items-center justify-between text-xs text-gray-400 dark:text-ink-500">
            <span>留空则清除备注</span>
            <span className="tabular-nums">{remarkDraft.trim().length}/80</span>
          </div>
          {remarkError && <p role="alert" className="mt-3 text-sm text-red-600 dark:text-red-400">{remarkError}</p>}
        </form>
      </DialogFrame>

      <TeamBillingDialog open={billingOpen} onOpenChange={setBillingOpen} team={team} />

      <AddMemberDialog
        open={addMemberOpen}
        onOpenChange={handleInviteOpenChange}
        teamId={team.id}
        teamName={teamDisplayName}
        team={inviteSnapshot?.team}
        pendingInvites={inviteSnapshot?.members.pending_invites}
        initializing={openingAddMember}
        initializationError={inviteError}
        onRetryInitialization={() => { void initializeInvite(); }}
        onSuccess={startMemberSettle}
      />

      <TeamSettingsDialog
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        teamId={team.id}
        teamName={teamDisplayName}
        ownerEmail={team.owner_email}
        currentProxyId={team.proxy_id}
        overagePolicy={team.overage_policy}
        billing={team}
        onTeamUpdated={(updated) => onTeamSynced({
          ...updated,
          cached_member_emails: updated.cached_member_emails ?? team.cached_member_emails ?? [],
        })}
        initialSettings={workspaceSettings ?? (team.default_seat_type ? {
          default_seat_type: team.default_seat_type,
          cached: true,
          cached_at: team.workspace_settings_cached_at,
        } : null)}
        onChanged={(settings) => {
          setWorkspaceSettings(settings);
          onTeamSynced({ ...team, default_seat_type: settings.default_seat_type, workspace_settings_cached_at: settings.cached_at });
        }}
        onProxyChanged={(proxyId) => onTeamSynced({ ...team, proxy_id: proxyId })}
      />
    </div>
  );
}
