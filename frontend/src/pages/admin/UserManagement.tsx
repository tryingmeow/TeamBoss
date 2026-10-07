import { useState, useEffect, useMemo, useRef, type ReactNode } from 'react';
import {
  fetchOwners,
  fetchTeams,
  syncTeam,
  fetchAllMembers,
  extendMemberExpiry,
  updateMemberExpiry,
  updateMemberSeat,
  kickMember,
  kickInvite,
  removeExpiry,
  updateUserDisplayName,
  createTgMemberCode,
} from '../../api/client';
import {
  ArrowUpDown,
  CalendarDays,
  Check,
  ChevronDown,
  Copy,
  Loader2,
  MessageCircle,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  UserX,
  Zap,
} from 'lucide-react';
import * as Dialog from '@radix-ui/react-dialog';
import * as Popover from '@radix-ui/react-popover';
import type { SeatType, Team } from '../../types';
import ExpiryPicker, { type ExpirySelection } from '../../components/ExpiryPicker';
import LoadingSpinner from '../../components/LoadingSpinner';
import PageShell from '../../components/PageShell';
import SegmentedTabs from '../../components/SegmentedTabs';
import Toast from '../../components/Toast';
import { BUTTON, CARD, INPUT, PILL, TONE } from '../../components/ui';
import { useKickPolicy } from '../../hooks/useKickPolicy';
import { NO_EXPIRY_LABEL, noExpiryKind, type KickPolicy } from '../../lib/expiry';
import { errorText, runPool } from '../../lib/pool';
import { cn } from '../../lib/utils';
import SystemLogs from './SystemLogs';
import {
  SEAT_STYLE,
  SEAT_TYPE_OPTIONS,
  UNKNOWN_SEAT_STYLE,
  formatSeatTypeLabel,
  parseSeatType,
  seatStyle,
} from '../../lib/seatType';
import { pendingCountsByType, seatSwitchGate } from '../../lib/seatCapacity';
import { SeatSwitchOptions, useSeatSwitch } from '../../components/SeatSwitchMenu';
import { ExpiryExtensionRequestIds } from '../../lib/expiryExtensionRequest';
import { currentPeriodStart, formatPeriodRange, periodEnds } from '../../lib/billingPeriod';

interface BillingCycle {
  active_start: string | null;
  active_until: string | null;
  days_remaining: number | null;
  will_renew: boolean;
  subscription_status: 'renewing' | 'nonrenewing' | 'expired' | 'stale';
}

interface OwnerRow {
  email: string;
  name: string;
  system_display_name?: string | null;
  team_id: string;
  team_name: string;
  user_id: string;
  seat_type: string;
  card_last4: string | null;
  billing_cycle: BillingCycle;
  active_until?: string | null;
  is_codex_enabled?: boolean | number;
}

interface MemberExpiryView {
  kick_display?: string | null;
  effective_kick_at?: string | null;
  effective_kick_at_local?: string | null;
  expires_at?: string | null;
  expires_at_local?: string | null;
  kick_label?: string | null;
  first_seen_at?: string | null;
  source?: string | null;
  kick_source?: string | null;
  kicked_at?: string | null;
}

interface AdminMemberRow {
  status: 'joined' | 'pending' | 'kicked';
  status_label?: string;
  team_id: string;
  team_name: string;
  owner_email: string;
  is_owner?: boolean;
  user_id: string;
  email: string;
  name?: string | null;
  system_display_name?: string | null;
  seat_type: string;
  expiry?: MemberExpiryView;
  is_codex_enabled?: boolean | number;
  tg_binding?: {
    bound: boolean;
    username?: string | null;
    paired_at?: string | null;
  };
}

type SortOrder = 'asc' | 'desc';
type MemberStatus = 'joined' | 'pending' | 'kicked';
/** `other` = seat types outside the registry (其他（raw）). */
type SeatFilter = 'all' | SeatType | 'other';
type ToastType = 'success' | 'error';
type ShowToast = (text: string, type?: ToastType) => void;
/** `row` = desktop table cell, `card` = stacked phone/tablet card with larger touch targets. */
type Variant = 'row' | 'card';

interface ToastMessage {
  id: number;
  text: string;
  type: ToastType;
}

const MEMBER_STATUS: Record<MemberStatus, { label: string; dotClass: string; tone: string }> = {
  joined: { label: '已加入', dotClass: 'bg-emerald-500', tone: TONE.success },
  pending: { label: '待接受', dotClass: 'bg-amber-500', tone: TONE.warning },
  kicked: { label: '已踢出', dotClass: 'bg-red-500', tone: TONE.danger },
};

const MEMBER_STATUS_ORDER: MemberStatus[] = ['joined', 'pending', 'kicked'];

const DEFAULT_MEMBER_STATUS_FILTERS = new Set<MemberStatus>(['joined', 'pending']);

const JOIN_SOURCE: Record<string, { label: string; tone: string }> = {
  system: { label: '系统邀请', tone: TONE.neutral },
  detected: { label: '外部加入', tone: TONE.warning },
  self_service: { label: '自助加入', tone: TONE.info },
};

const UNTRACKED_SOURCE = { label: '未登记', tone: TONE.neutral };

const KICK_SOURCE: Record<string, { label: string; tone: string }> = {
  auto_expire: { label: '自动过期', tone: TONE.neutral },
  admin: { label: '手动踢出', tone: TONE.info },
  detected: { label: '检测移除', tone: TONE.warning },
  patrol: { label: '巡逻移除', tone: TONE.warning },
  patrol_premium: { label: '巡逻移除 · Premium', tone: TONE.warning },
};

type TabValue = 'owner' | 'members' | 'logs';

const TABS: { value: TabValue; label: string }[] = [
  { value: 'owner', label: 'Owner' },
  { value: 'members', label: '成员' },
  { value: 'logs', label: '日志' },
];

/** Small icon button next to a value (edit name / date / seat, copy). Size comes from the variant. */
const ICON_BUTTON =
  'inline-flex shrink-0 items-center justify-center rounded-md text-gray-400 transition-colors hover:bg-blue-50 hover:text-blue-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/50 disabled:cursor-wait disabled:opacity-50 dark:text-ink-500 dark:hover:bg-blue-500/10 dark:hover:text-blue-400';
const ICON_BUTTON_SIZE: Record<Variant, string> = { row: 'size-7 -my-1', card: 'size-9' };

const POPOVER =
  'z-50 rounded-xl border border-gray-200 bg-white shadow-xl dark:border-ink-800 dark:bg-ink-900';

async function copyToClipboard(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement('textarea');
  textarea.value = value;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand('copy');
  textarea.remove();
  if (!copied) throw new Error('浏览器未允许复制，请手动复制');
}

function memberExpiryDisplay(
  expiry: MemberExpiryView | undefined,
  isOwner = false,
): { primary: string; title?: string; grace?: string; graceTitle?: string } {
  if (!expiry) return { primary: '—' };
  if (!expiry.expires_at) {
    if (isOwner) return { primary: '—', title: 'Owner 不参与到期管理' };
    const label = NO_EXPIRY_LABEL[noExpiryKind(expiry.source)];
    return { primary: label.short, title: label.title };
  }
  const primary = expiry.expires_at_local || expiry.expires_at;
  if (!expiry.effective_kick_at || expiry.effective_kick_at === expiry.expires_at) {
    return { primary };
  }
  const rule = expiry.kick_label && expiry.kick_label !== '到期' ? `（${expiry.kick_label}）` : '';
  return {
    primary,
    grace: expiry.effective_kick_at_local ? `宽限到 ${expiry.effective_kick_at_local}` : `系统宽限${rule}`,
    graceTitle: `到期后按系统宽限规则${rule}移出`,
  };
}

const DAY_MS = 24 * 60 * 60 * 1000;

/** Past the expiry = red, due within 3 days = amber (the colours 数据概览 uses). Kicked rows are not flagged. */
function expiryUrgency(member: AdminMemberRow): { className: string; title: string } | null {
  if (member.status === 'kicked') return null;
  const expiresAt = parseTimestamp(member.expiry?.expires_at);
  if (expiresAt == null) return null;
  const remaining = expiresAt - Date.now();
  if (remaining <= 0) return { className: 'font-medium text-red-600 dark:text-red-400', title: '已过期' };
  if (remaining <= 3 * DAY_MS) return { className: 'font-medium text-amber-600 dark:text-amber-400', title: '3 天内到期' };
  return null;
}

function parseTimestamp(value: string | null | undefined): number | null {
  if (!value) return null;
  const ts = Date.parse(value);
  return Number.isNaN(ts) ? null : ts;
}

function matchesSearch<T>(
  row: T,
  query: string,
  fields: string[]
): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  const record = row as Record<string, unknown>;
  return fields.some((field) =>
    String(record[field] ?? '').toLowerCase().includes(needle)
  );
}

function sortByTimestamp<T>(
  items: T[],
  getTs: (item: T) => number | null,
  order: SortOrder
): T[] {
  const dir = order === 'asc' ? 1 : -1;
  return [...items].sort((a, b) => {
    const aTs = getTs(a);
    const bTs = getTs(b);
    if (aTs == null && bTs == null) return 0;
    if (aTs == null) return 1;
    if (bTs == null) return -1;
    return (aTs - bTs) * dir;
  });
}

// 加入时间那一列的展示时区。到期/踢人时间的计算口径统一在 lib/expiry.ts。
const APP_TIME_ZONE = 'Asia/Shanghai';

function formatJoinedAt(value: string | null | undefined): string {
  if (!value) return '—';
  return new Date(value).toLocaleString('zh-CN', {
    timeZone: APP_TIME_ZONE,
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function StatusFilterToggle({
  enabled,
  onChange,
}: {
  enabled: Set<MemberStatus>;
  onChange: (next: Set<MemberStatus>) => void;
}) {
  const toggle = (status: MemberStatus) => {
    const next = new Set(enabled);
    if (next.has(status)) {
      if (next.size === 1) return;
      next.delete(status);
    } else {
      next.add(status);
    }
    onChange(next);
  };

  return (
    <div
      className="inline-flex h-9 shrink-0 divide-x divide-gray-200 overflow-hidden rounded-lg border border-gray-200 bg-white dark:divide-ink-800 dark:border-ink-800 dark:bg-ink-900"
      role="group"
      aria-label="按状态筛选"
    >
      {MEMBER_STATUS_ORDER.map((value) => {
        const { label, dotClass } = MEMBER_STATUS[value];
        const active = enabled.has(value);
        return (
          <button
            key={value}
            type="button"
            aria-pressed={active}
            title={active && enabled.size === 1 ? '至少保留一种状态' : undefined}
            onClick={() => toggle(value)}
            className={cn(
              'inline-flex items-center gap-1.5 whitespace-nowrap px-2.5 text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-blue-500/50',
              active
                ? 'bg-gray-100 font-medium text-gray-900 dark:bg-ink-800 dark:text-gray-100'
                : 'text-gray-400 hover:bg-gray-50 hover:text-gray-700 dark:text-ink-500 dark:hover:bg-ink-800/60 dark:hover:text-ink-200'
            )}
          >
            <span className={cn('size-2 rounded-full', dotClass, !active && 'opacity-40')} />
            {label}
          </button>
        );
      })}
    </div>
  );
}

function FilterDropdown({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string; dotClass: string }[];
}) {
  const [open, setOpen] = useState(false);
  const selected = options.find((o) => o.value === value) ?? options[0];

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <button type="button" aria-label="按席位筛选" className={cn(BUTTON.secondary, 'h-9 px-3 py-0 font-normal')}>
          <span className={cn('size-2 rounded-full', selected.dotClass)} />
          {selected.label}
          <ChevronDown className={cn('size-3.5 text-gray-400 transition-transform dark:text-ink-500', open && 'rotate-180')} />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content className={cn(POPOVER, 'min-w-40 p-1')} sideOffset={6} align="start" collisionPadding={16}>
          {options.map((opt) => (
            <button
              key={opt.value}
              type="button"
              onClick={() => {
                onChange(opt.value);
                setOpen(false);
              }}
              className={cn(
                'flex h-9 w-full items-center gap-2.5 whitespace-nowrap rounded-md px-2.5 text-sm transition-colors',
                value === opt.value
                  ? 'bg-gray-100 text-gray-900 dark:bg-ink-800 dark:text-gray-100'
                  : 'text-gray-700 hover:bg-gray-100 hover:text-gray-900 dark:text-ink-300 dark:hover:bg-ink-800 dark:hover:text-gray-100'
              )}
            >
              <span className={cn('size-2 shrink-0 rounded-full', opt.dotClass)} />
              <span className="flex-1 text-left">{opt.label}</span>
              {value === opt.value && <Check className="size-4 text-blue-600 dark:text-blue-400" />}
            </button>
          ))}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

function SortToggle({
  label,
  order,
  onToggle,
}: {
  label: string;
  order: SortOrder;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      title="点击切换排序方向"
      className={cn(BUTTON.secondary, 'h-9 gap-1.5 px-3 py-0 font-normal')}
    >
      <ArrowUpDown className="size-3.5 text-gray-400 dark:text-ink-500" />
      {label}
      <span className="font-medium text-blue-600 dark:text-blue-400">{order === 'asc' ? '近→远' : '远→近'}</span>
    </button>
  );
}

function SearchInput({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
}) {
  return (
    <div className="relative w-full sm:w-64">
      <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-gray-400 dark:text-ink-500" />
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        aria-label="搜索"
        className={cn(INPUT, 'h-9 py-0 pl-9')}
      />
    </div>
  );
}

function LoadErrorBanner({
  children,
  busy,
  onRetry,
}: {
  children: ReactNode;
  busy: boolean;
  onRetry: () => void;
}) {
  return (
    <div
      role="alert"
      className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-300"
    >
      <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">{children}</span>
      <button
        type="button"
        disabled={busy}
        onClick={onRetry}
        className="h-8 shrink-0 rounded-md border border-current px-3 text-xs font-medium transition-colors hover:bg-red-100 disabled:opacity-50 dark:hover:bg-red-500/15"
      >
        重试
      </button>
    </div>
  );
}

function ListState({ children }: { children: ReactNode }) {
  return (
    <div className={cn(CARD, 'flex flex-col items-center gap-3 px-6 py-14 text-center text-sm text-gray-500 dark:text-ink-400')}>
      {children}
    </div>
  );
}

function ListCount({ shown, total }: { shown: number; total: number }) {
  return (
    <p className="px-1 text-xs text-gray-500 dark:text-ink-400">
      {shown === total ? `共 ${total} 条` : `显示 ${shown} 条，共 ${total} 条`}
    </p>
  );
}

/** Label / value grid used inside stacked cards. */
function CardField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="whitespace-nowrap text-xs text-gray-500 dark:text-ink-400">{label}</dt>
      <dd className="min-w-0 text-gray-800 dark:text-ink-200">{children}</dd>
    </>
  );
}

const CARD_FIELDS =
  'mt-3 grid grid-cols-[4.5rem_minmax(0,1fr)] items-baseline gap-x-3 gap-y-3 border-t border-gray-100 pt-3 text-sm dark:border-ink-800';

const TABLE_HEAD =
  'border-b border-gray-200 bg-gray-50/80 text-xs text-gray-500 dark:border-ink-800 dark:bg-ink-950/30 dark:text-ink-400';
const TH = 'whitespace-nowrap px-3 py-2.5 font-medium first:pl-4 last:pr-4';
const TD = 'whitespace-nowrap px-3 py-3 last:pr-4';
const TR = 'align-top transition-colors hover:bg-gray-50 dark:hover:bg-ink-800/40';

function ExpiryControl({
  variant,
  display,
  displayTitle,
  urgency,
  grace,
  graceTitle,
  editable,
  joinedAt,
  policy,
  onSubmit,
}: {
  variant: Variant;
  display: string;
  displayTitle?: string;
  urgency: { className: string; title: string } | null;
  grace?: string;
  graceTitle?: string;
  editable: boolean;
  joinedAt?: string | null;
  policy: KickPolicy;
  onSubmit: (selection: ExpirySelection) => void;
}) {
  const [dateOpen, setDateOpen] = useState(false);
  const [durationOpen, setDurationOpen] = useState(false);

  const trigger = (icon: ReactNode, label: string) => (
    <button
      type="button"
      title={label}
      aria-label={label}
      className={variant === 'card' ? cn(BUTTON.secondary, 'size-9 p-0') : cn(ICON_BUTTON, ICON_BUTTON_SIZE.row)}
    >
      {icon}
    </button>
  );

  return (
    <div className={variant === 'card' ? 'flex flex-wrap items-center gap-x-3 gap-y-2' : 'flex items-start gap-1.5'}>
      <div>
        <div
          className={cn('whitespace-nowrap tabular-nums', urgency?.className ?? 'text-gray-800 dark:text-ink-200')}
          title={urgency?.title ?? displayTitle}
        >
          {display}
        </div>
        {grace && (
          <div className="whitespace-nowrap text-[11px] text-amber-600 dark:text-amber-400" title={graceTitle}>
            {grace}
          </div>
        )}
      </div>
      {editable && (
        <div className={cn('flex items-center', variant === 'card' ? 'gap-1.5' : 'gap-0.5')}>
          {/* 两个入口各管一件事，不合并：日历改到哪一天，加号加多少时长。 */}
          <Popover.Root open={dateOpen} onOpenChange={setDateOpen}>
            <Popover.Trigger asChild>
              {trigger(<CalendarDays className="size-3.5" />, '修改日期')}
            </Popover.Trigger>
            <Popover.Portal>
              <Popover.Content className={cn(POPOVER, 'w-[19rem] p-3')} sideOffset={6} collisionPadding={16}>
                <div className="mb-2 text-sm font-medium text-gray-900 dark:text-gray-100">修改到期日期</div>
                <ExpiryPicker
                  mode="date"
                  policy={policy}
                  joinedAt={joinedAt}
                  onSubmit={(selection) => {
                    onSubmit(selection);
                    setDateOpen(false);
                  }}
                />
              </Popover.Content>
            </Popover.Portal>
          </Popover.Root>

          <Popover.Root open={durationOpen} onOpenChange={setDurationOpen}>
            <Popover.Trigger asChild>
              {trigger(<Plus className="size-3.5" />, '增加时长')}
            </Popover.Trigger>
            <Popover.Portal>
              <Popover.Content className={cn(POPOVER, 'w-[19rem] p-3')} sideOffset={6} collisionPadding={16}>
                <div className="mb-2 text-sm font-medium text-gray-900 dark:text-gray-100">增加时长</div>
                <ExpiryPicker
                  mode="duration"
                  policy={policy}
                  joinedAt={joinedAt}
                  onSubmit={(selection) => {
                    onSubmit(selection);
                    setDurationOpen(false);
                  }}
                />
              </Popover.Content>
            </Popover.Portal>
          </Popover.Root>
        </div>
      )}
    </div>
  );
}

function CodexBadge({ isCodexEnabled }: { isCodexEnabled?: boolean | number }) {
  if (isCodexEnabled === undefined) return null;
  const enabled = Boolean(isCodexEnabled);
  return (
    <span
      className={cn(PILL, enabled ? SEAT_STYLE.usage_based.pill : TONE.neutral)}
      title={enabled ? '这个 Team 已开启 Codex 席位' : '这个 Team 未开启 Codex 席位'}
    >
      <Zap className="size-2.5" />
      {enabled ? 'Codex 已开' : 'Codex 未开'}
    </span>
  );
}

/** What a seat menu needs to know about the member's Team (cached; the server re-checks). */
interface SeatSwitchContext {
  teamId: string;
  userId: string;
  team: Team | null;
  pendingByType: Record<string, number>;
  isCodexEnabled?: boolean | number;
  onSwitched: () => void;
  showToast: ShowToast;
}

function SeatTypeCell({
  variant,
  seatType,
  editable,
  context,
}: {
  variant: Variant;
  seatType: string | null | undefined;
  editable: boolean;
  context?: SeatSwitchContext;
}) {
  const [open, setOpen] = useState(false);
  const seatSwitch = useSeatSwitch({
    apply: (target, allowOverage) =>
      context ? updateMemberSeat(context.teamId, context.userId, target, allowOverage) : Promise.resolve(),
    onSwitched: () => context?.onSwitched(),
    onAsk: () => setOpen(false),
    showToast: context?.showToast ?? (() => {}),
    isCodexEnabled: context?.isCodexEnabled,
  });
  const current = parseSeatType(seatType);
  if (!seatType && !editable) {
    return <span className="text-gray-400 dark:text-ink-500">—</span>;
  }
  // 不认识的席位类型只显示：TeamBoss 不会切换它。
  const canEdit = editable && context && current !== null;
  return (
    <div className="flex min-w-0 items-center gap-1">
      {/* 窄列里放不下「其他（automation）」：截断并给完整名称，不压到旁边一列。 */}
      <span className={cn(PILL, seatStyle(seatType).pill, 'min-w-0 max-w-full shrink')} title={formatSeatTypeLabel(seatType)}>
        <span className="truncate">{formatSeatTypeLabel(seatType)}</span>
      </span>
      {canEdit && (
        <Popover.Root open={open} onOpenChange={setOpen}>
          <Popover.Trigger asChild>
            <button
              type="button"
              title="修改席位类型"
              aria-label="修改席位类型"
              disabled={seatSwitch.busy}
              className={cn(ICON_BUTTON, ICON_BUTTON_SIZE[variant])}
            >
              <Pencil className="size-3.5" />
            </button>
          </Popover.Trigger>
          <Popover.Portal>
            <Popover.Content className={cn(POPOVER, 'w-48 p-1')} sideOffset={6} collisionPadding={16}>
              <SeatSwitchOptions
                current={current}
                gateFor={(target) => seatSwitchGate(context.team, seatType, target, context.pendingByType)}
                disabled={seatSwitch.busy}
                onPick={(target, gate) => {
                  if (gate?.action !== 'forbid') setOpen(false);
                  seatSwitch.pick(target, gate);
                }}
              />
            </Popover.Content>
          </Popover.Portal>
        </Popover.Root>
      )}
      {seatSwitch.dialog}
    </div>
  );
}

function UserIdentityCell({
  variant,
  email,
  name,
  systemDisplayName,
  badge,
  onSave,
  onError,
}: {
  variant: Variant;
  email: string;
  /** Rendered after the edit button on the first line (e.g. the multi-Team badge). */
  badge?: ReactNode;
  name?: string | null;
  systemDisplayName?: string | null;
  onSave: (value: string | null) => Promise<void>;
  onError?: (message: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);

  const customName = systemDisplayName?.trim() || '';
  const hasCustomName = Boolean(customName);
  const profileName = name?.trim() || '';
  const primary = customName || email;

  const openEditor = () => {
    setDraft(customName);
    setOpen(true);
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const value = draft.trim();
      await onSave(value || null);
      setOpen(false);
    } catch (err) {
      console.error(err);
      onError?.('保存显示名称失败');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="min-w-0">
      <div className="flex min-w-0 items-center gap-1">
        <span className="truncate font-medium text-gray-900 dark:text-gray-100" title={primary}>
          {primary}
        </span>
        <Popover.Root open={open} onOpenChange={setOpen}>
          <Popover.Trigger asChild>
            <button
              type="button"
              onClick={openEditor}
              className={cn(ICON_BUTTON, ICON_BUTTON_SIZE[variant])}
              title="设置显示名称"
              aria-label="设置显示名称"
            >
              <Pencil className="size-3.5" />
            </button>
          </Popover.Trigger>
          <Popover.Portal>
            <Popover.Content className={cn(POPOVER, 'w-72 p-4')} sideOffset={6} collisionPadding={16}>
              <div className="text-sm font-medium text-gray-900 dark:text-gray-100">显示名称</div>
              <p className="mt-0.5 text-xs text-gray-500 dark:text-ink-400">只在 TeamBoss 里显示，留空则显示邮箱。</p>
              <input
                type="text"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder="留空则显示邮箱"
                className={cn(INPUT, 'mt-3')}
                maxLength={120}
              />
              <div className="mt-3 flex justify-end gap-2">
                <button type="button" onClick={() => setOpen(false)} className={cn(BUTTON.secondary, 'h-8 px-3 py-0')}>
                  取消
                </button>
                <button type="button" onClick={handleSave} disabled={saving} className={cn(BUTTON.primary, 'h-8 px-3 py-0')}>
                  {saving ? '保存中…' : '保存'}
                </button>
              </div>
            </Popover.Content>
          </Popover.Portal>
        </Popover.Root>
        {badge}
      </div>
      {hasCustomName && (
        <div className="truncate text-xs text-gray-500 dark:text-ink-400" title={email}>
          {email}
        </div>
      )}
      {profileName && (
        <div className="truncate text-xs text-gray-400 dark:text-ink-500" title={profileName}>
          {profileName}
        </div>
      )}
    </div>
  );
}

function BillingCycleCell({
  cycle,
  billingPeriod,
  className,
}: {
  cycle: BillingCycle | string | null | undefined;
  /** The Team's billing interval ('monthly' / 'yearly'); null or missing when unknown. */
  billingPeriod?: string | null;
  className?: string;
}) {
  if (!cycle) return <span className="text-gray-400 dark:text-ink-500">—</span>;
  if (typeof cycle === 'string') return <span>{cycle}</span>;
  const status =
    cycle.subscription_status === 'expired'
      ? { label: '已到期', tone: TONE.danger }
      : cycle.subscription_status === 'stale'
        ? { label: '数据未同步', tone: TONE.neutral }
        : cycle.will_renew
          ? null
          : { label: '到期不续费', tone: TONE.warning };
  const days = cycle.days_remaining;
  // active_start is when the subscription began, not the current period's start.
  const periodStart = currentPeriodStart(cycle.active_start, cycle.active_until, billingPeriod);
  const [subscribedFrom, subscribedUntil] = periodEnds(cycle.active_start, cycle.active_until);
  return (
    <div className={cn('flex items-center gap-x-2 gap-y-1', className)}>
      {periodStart ? (
        <span className="whitespace-nowrap tabular-nums text-gray-800 dark:text-ink-200" title="本期计费周期">
          {formatPeriodRange(periodStart, cycle.active_until)}
        </span>
      ) : (
        <span
          className="whitespace-nowrap tabular-nums text-gray-800 dark:text-ink-200"
          title="计费间隔未知，这里显示的是订阅开始日和本期结束日"
        >
          订阅自 {subscribedFrom} · 至 {subscribedUntil}
        </span>
      )}
      {days != null && (
        <span className="whitespace-nowrap text-xs text-gray-500 dark:text-ink-400">
          {days >= 0 ? `剩 ${days} 天` : `已过 ${-days} 天`}
        </span>
      )}
      {status && <span className={cn(PILL, status.tone)}>{status.label}</span>}
    </div>
  );
}

function TeamName({ name, children }: { name: string; children?: ReactNode }) {
  return (
    <div className="flex min-w-0 items-center gap-1.5">
      <span className="truncate font-medium text-gray-800 dark:text-ink-100" title={name}>
        {name || '—'}
      </span>
      {children}
    </div>
  );
}

function OwnerList({
  search,
  sortOrder,
  reloadSignal,
  showToast,
}: {
  search: string;
  sortOrder: SortOrder;
  /** Bumped by the page after a bulk refresh; reloads the list. */
  reloadSignal: number;
  showToast: ShowToast;
}) {
  const [owners, setOwners] = useState<OwnerRow[]>([]);
  const [teamsById, setTeamsById] = useState<Map<string, Team>>(new Map());
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [refreshTrigger, setRefreshTrigger] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    // The owner rows don't carry the billing interval; the Team list does. Without it the
    // billing column falls back to an honest "订阅自" label, so a failure there is not fatal.
    Promise.all([fetchOwners(), fetchTeams().catch(() => null)])
      .then(([res, teams]) => {
        if (cancelled) return;
        setOwners(res.items);
        if (teams) setTeamsById(new Map(teams.map((team) => [team.id, team])));
        setLoadError('');
      })
      .catch((err) => {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : '加载失败');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [refreshTrigger, reloadSignal]);

  const handleUpdateDisplayName = async (email: string, systemDisplayName: string | null) => {
    await updateUserDisplayName(email, systemDisplayName);
    setRefreshTrigger((v) => v + 1);
  };

  const filteredOwners = useMemo(() => {
    const matched = owners.filter((owner) =>
      matchesSearch(owner, search, ['email', 'name', 'team_name', 'system_display_name'])
    );
    return sortByTimestamp(
      matched,
      (owner) =>
        parseTimestamp(
          (owner.billing_cycle as BillingCycle | undefined)?.active_until ??
            (owner.active_until as string | undefined)
        ),
      sortOrder
    );
  }, [owners, search, sortOrder]);

  const identity = (owner: OwnerRow, variant: Variant) => (
    <UserIdentityCell
      variant={variant}
      email={owner.email}
      name={owner.name}
      systemDisplayName={owner.system_display_name}
      onSave={(value) => handleUpdateDisplayName(owner.email, value)}
      onError={(message) => showToast(message, 'error')}
    />
  );

  // Owner 列表没有待接受邀请的名单：用 Team 列表缓存的计数（缺失时按 0）；服务端会用实时数据再判一次。
  const seat = (owner: OwnerRow, variant: Variant) => (
    <SeatTypeCell
      variant={variant}
      seatType={owner.seat_type}
      editable={Boolean(owner.user_id)}
      context={{
        teamId: owner.team_id,
        userId: owner.user_id,
        team: teamsById.get(owner.team_id) ?? null,
        pendingByType: teamsById.get(owner.team_id)?.pending_invite_counts ?? {},
        isCodexEnabled: owner.is_codex_enabled,
        onSwitched: () => setRefreshTrigger((v) => v + 1),
        showToast,
      }}
    />
  );

  const card4 = (owner: OwnerRow) =>
    owner.card_last4 ? (
      <span className="whitespace-nowrap font-mono text-gray-600 dark:text-ink-300">•••• {owner.card_last4}</span>
    ) : (
      <span className="text-gray-400 dark:text-ink-500">—</span>
    );

  return (
    <section className="space-y-3">
      {loadError && (
        <LoadErrorBanner busy={loading} onRetry={() => setRefreshTrigger((v) => v + 1)}>
          Owner 数据加载失败：{loadError}{owners.length > 0 ? '。下面是上次成功加载的数据。' : ''}
        </LoadErrorBanner>
      )}
      {loading ? (
        <ListState>
          <LoadingSpinner size={22} />
          正在加载 Owner…
        </ListState>
      ) : loadError && owners.length === 0 ? null : filteredOwners.length === 0 ? (
        <ListState>
          {owners.length === 0
            ? '还没有 Team。在「Team 列表」添加 Team 后，它的 Owner 会显示在这里。'
            : '没有符合搜索条件的 Owner。'}
        </ListState>
      ) : (
        <>
          <div className={cn(CARD, 'hidden overflow-x-auto lg:block')}>
            <table className="w-full text-left text-sm text-gray-700 dark:text-ink-300">
              <thead className={TABLE_HEAD}>
                <tr>
                  <th className={TH}>Owner</th>
                  <th className={TH}>Team</th>
                  <th className={TH}>席位</th>
                  <th className={TH}>卡号后四位</th>
                  <th className={TH}>计费周期</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-ink-800">
                {filteredOwners.map((owner, i) => (
                  <tr key={`${owner.team_id}-${owner.email}-${i}`} className={TR}>
                    <td className="py-3 pl-4 pr-3">
                      <div className="max-w-[20rem]">{identity(owner, 'row')}</div>
                    </td>
                    <td className={cn(TD, 'max-w-[18rem]')}>
                      <TeamName name={owner.team_name}>
                        <CodexBadge isCodexEnabled={owner.is_codex_enabled} />
                      </TeamName>
                    </td>
                    <td className={TD}>{seat(owner, 'row')}</td>
                    <td className={TD}>{card4(owner)}</td>
                    <td className={TD}>
                      <BillingCycleCell cycle={owner.billing_cycle} billingPeriod={teamsById.get(owner.team_id)?.billing_period} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="grid gap-3 md:grid-cols-2 lg:hidden">
            {filteredOwners.map((owner, i) => (
              <article key={`${owner.team_id}-${owner.email}-${i}`} className={cn(CARD, 'min-w-0 p-4')}>
                {identity(owner, 'card')}
                <dl className={CARD_FIELDS}>
                  <CardField label="Team">
                    <TeamName name={owner.team_name}>
                      <CodexBadge isCodexEnabled={owner.is_codex_enabled} />
                    </TeamName>
                  </CardField>
                  <CardField label="席位">{seat(owner, 'card')}</CardField>
                  <CardField label="卡号后四位">{card4(owner)}</CardField>
                  <CardField label="计费周期">
                    <BillingCycleCell cycle={owner.billing_cycle} billingPeriod={teamsById.get(owner.team_id)?.billing_period} className="flex-wrap" />
                  </CardField>
                </dl>
              </article>
            ))}
          </div>

          <ListCount shown={filteredOwners.length} total={owners.length} />
        </>
      )}
    </section>
  );
}

function MemberStatusPills({ member }: { member: AdminMemberRow }) {
  const status = MEMBER_STATUS[member.status];
  const joinSource = member.expiry?.source;
  const source =
    member.status === 'joined'
      ? member.is_owner
        ? undefined
        : joinSource == null
          // No local record at all: not a system invite, whatever else it is.
          ? UNTRACKED_SOURCE
          : JOIN_SOURCE[joinSource || 'system'] ?? JOIN_SOURCE.system
      : member.status === 'kicked' && member.expiry?.kick_source
        ? KICK_SOURCE[member.expiry.kick_source]
        : undefined;
  return (
    <>
      <span className={cn(PILL, status?.tone ?? TONE.neutral)}>
        <span className={cn('size-1.5 rounded-full', status?.dotClass ?? 'bg-gray-400')} />
        {status?.label ?? member.status_label ?? member.status}
      </span>
      {source && <span className={cn(PILL, source.tone)}>{source.label}</span>}
    </>
  );
}

function MemberTeamInfo({ member }: { member: AdminMemberRow }) {
  return (
    <div className="flex min-w-0 flex-col items-start gap-0.5">
      <TeamName name={member.team_name}>
        <CodexBadge isCodexEnabled={member.is_codex_enabled} />
      </TeamName>
      <span className="flex min-w-0 max-w-full items-center gap-1 text-xs text-gray-500 dark:text-ink-400">
        <span className="shrink-0 text-gray-400 dark:text-ink-500">Owner</span>
        <span className="truncate" title={member.owner_email}>{member.owner_email || '—'}</span>
      </span>
    </div>
  );
}

function TgBindingCell({
  variant,
  member,
  copying,
  copied,
  onCopy,
}: {
  variant: Variant;
  member: AdminMemberRow;
  copying: boolean;
  copied: boolean;
  onCopy: () => void;
}) {
  const bound = Boolean(member.tg_binding?.bound);
  if (member.status === 'kicked') {
    return (
      <span className="whitespace-nowrap text-xs text-gray-400 dark:text-ink-500">
        {bound ? '已绑定（其他成员）' : '已解绑'}
      </span>
    );
  }
  const action = bound ? '重新绑定指令' : '绑定指令';
  return (
    <div className="flex items-center gap-1.5">
      <span
        className={cn(PILL, bound ? TONE.success : TONE.neutral)}
        title={member.tg_binding?.username ? `@${member.tg_binding.username}` : undefined}
      >
        <MessageCircle className="size-3" />
        {bound ? '已绑定' : '未绑定'}
      </span>
      <button
        type="button"
        onClick={onCopy}
        disabled={copying}
        title={`复制${action}`}
        aria-label={`复制 ${member.email} 的${action}`}
        className={
          variant === 'card'
            ? cn(BUTTON.secondary, 'h-9 gap-1.5 px-2.5 py-0 text-xs disabled:cursor-wait')
            : cn(ICON_BUTTON, ICON_BUTTON_SIZE.row)
        }
      >
        {copied ? <Check className="size-3.5 text-emerald-500" /> : <Copy className="size-3.5" />}
        {variant === 'card' && (copied ? '已复制' : `复制${action}`)}
      </button>
    </div>
  );
}

function KickMemberButton({
  variant,
  member,
  onConfirm,
}: {
  variant: Variant;
  member: AdminMemberRow;
  onConfirm: () => void;
}) {
  const pending = member.status === 'pending';
  const actionLabel = pending ? '撤销邀请' : '踢出成员';
  return (
    <Dialog.Root>
      <Dialog.Trigger asChild>
        <button
          type="button"
          title={actionLabel}
          aria-label={`${actionLabel}：${member.email}`}
          className={cn(
            'inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg font-medium text-gray-500 transition-colors hover:bg-red-50 hover:text-red-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500/40 dark:text-ink-400 dark:hover:bg-red-500/10 dark:hover:text-red-400',
            variant === 'card' ? 'h-9 px-2.5 text-sm' : '-my-1 h-8 px-2 text-xs'
          )}
        >
          <UserX className={variant === 'card' ? 'size-4' : 'size-3.5'} />
          {pending ? '撤销邀请' : '踢出'}
        </button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-gray-900/40 backdrop-blur-sm dark:bg-ink-950/80" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 max-h-[calc(100dvh-2rem)] w-[calc(100vw-2rem)] max-w-md -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-2xl border border-gray-200 bg-white p-6 shadow-2xl dark:border-ink-800 dark:bg-ink-900">
          <Dialog.Title className="flex items-center gap-2 text-lg font-semibold text-gray-900 dark:text-gray-100">
            <UserX className="size-5 shrink-0 text-red-500" />
            {actionLabel}
          </Dialog.Title>
          <Dialog.Description className="mt-2 text-sm leading-6 text-gray-600 dark:text-ink-300">
            {pending ? '撤销发给 ' : '把 '}
            <strong className="font-medium text-gray-900 [overflow-wrap:anywhere] dark:text-gray-100">{member.email}</strong>
            {pending ? ` 的 Team 邀请（${member.team_name}）？` : ` 从 ${member.team_name} 中踢出？`}
            此操作无法撤销。
          </Dialog.Description>
          <div className="mt-6 flex flex-wrap justify-end gap-2">
            <Dialog.Close asChild>
              <button type="button" className={BUTTON.secondary}>取消</button>
            </Dialog.Close>
            <Dialog.Close asChild>
              <button type="button" onClick={onConfirm} className={BUTTON.danger}>
                {pending ? '撤销邀请' : '确认踢出'}
              </button>
            </Dialog.Close>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function MemberList({
  search,
  sortOrder,
  seatFilter,
  statusFilters,
  reloadSignal,
  showToast,
}: {
  search: string;
  sortOrder: SortOrder;
  seatFilter: SeatFilter;
  statusFilters: Set<MemberStatus>;
  /** Bumped by the page after a bulk refresh; reloads the list. */
  reloadSignal: number;
  showToast: ShowToast;
}) {
  const extensionRequestIds = useRef(new ExpiryExtensionRequestIds());
  const [members, setMembers] = useState<AdminMemberRow[]>([]);
  // Owner 行不进表格（表格是"成员"视图），但必须参与多车队角标的统计：
  // 一个邮箱在 A 队是成员、在 B 队是 Owner，正是最需要人工确认的那种情况。
  const [ownerTeamsByEmail, setOwnerTeamsByEmail] = useState<Map<string, number>>(new Map());
  // 角标点开的是"就这一个邮箱"，不是把邮箱塞进全文搜索——后者会把
  // owner_email 命中的整队人也一起捞出来。
  const [focusEmail, setFocusEmail] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  // 席位菜单要知道每个 Team 的缓存空位和超员策略；拿不到时菜单照常可用，由服务端判。
  const [teamsById, setTeamsById] = useState<Map<string, Team>>(new Map());
  const latestLoad = useRef(0);
  const [refreshTrigger, setRefreshTrigger] = useState(0);
  const [copyingBindingEmail, setCopyingBindingEmail] = useState<string | null>(null);
  const [copiedBindingEmail, setCopiedBindingEmail] = useState<string | null>(null);
  const copiedTimerRef = useRef<number | null>(null);
  const kickPolicy = useKickPolicy();

  const fetchMembers = (showLoading = true) => {
    const requestId = ++latestLoad.current;
    if (showLoading) setLoading(true);
    fetchTeams()
      .then((teams) => {
        if (requestId === latestLoad.current) setTeamsById(new Map(teams.map((team) => [team.id, team])));
      })
      .catch(() => {});
    return fetchAllMembers({ includeOwners: true })
      .then((res) => {
        if (requestId !== latestLoad.current) return;
        setLoadError('');
        const items = res.items as AdminMemberRow[];
        // 老后端不返回 is_owner，也就不会因为 include_owners 多给 Owner 行；
        // 这里按"没标就是成员"处理，版本错位时列表内容与改动前一致。
        setMembers(items.filter((item) => item.is_owner !== true));
        const owners = new Map<string, number>();
        items.forEach((item) => {
          if (!item.is_owner) return;
          const email = (item.email || '').trim().toLowerCase();
          if (!email) return;
          owners.set(email, (owners.get(email) ?? 0) + 1);
        });
        setOwnerTeamsByEmail(owners);
        // 拉不到缓存的车队会被服务端跳过。不说出来的话，多车队角标会少算，
        // 而管理员看到的是一个"完整"的列表。
        const failed = (res.errors ?? []) as Array<{ team_name?: string | null; team_id?: string }>;
        if (failed.length) {
          const names = failed.map((e) => e.team_name || e.team_id).filter(Boolean).join('、');
          showToast(`${failed.length} 个 Team 的数据加载失败（${names}）`, 'error');
        }
      })
      .catch((err) => {
        if (requestId === latestLoad.current) setLoadError(err instanceof Error ? err.message : '加载失败');
      })
      .finally(() => {
        if (requestId === latestLoad.current) setLoading(false);
      });
  };

  useEffect(() => {
    fetchMembers();
    return () => { latestLoad.current += 1; };
  }, [refreshTrigger, reloadSignal]);

  useEffect(() => () => {
    if (copiedTimerRef.current !== null) window.clearTimeout(copiedTimerRef.current);
  }, []);

  const handleUpdateDisplayName = async (email: string, systemDisplayName: string | null) => {
    await updateUserDisplayName(email, systemDisplayName);
    setRefreshTrigger((v) => v + 1);
  };

  // 一个邮箱同时在多个车队时，每个车队各自一行、各自的到期时间。行按到期排序会
  // 把同一个人的几行拆得很远，所以这里算一个"在册车队数"，在行上给个可点的角标。
  const memberTeamCounts = useMemo(() => {
    const teamsByEmail = new Map<string, Set<string>>();
    members.forEach((member) => {
      if (member.status === 'kicked') return;
      const email = (member.email || '').trim().toLowerCase();
      if (!email || !member.team_id) return;
      const teams = teamsByEmail.get(email) ?? new Set<string>();
      teams.add(member.team_id);
      teamsByEmail.set(email, teams);
    });
    const counts = new Map<string, number>();
    teamsByEmail.forEach((teams, email) => {
      counts.set(email, teams.size);
    });
    return counts;
  }, [members]);

  const filteredMembers = useMemo(() => {
    const matched = members.filter((member) => {
      // 角标下钻：精确到这一个邮箱，并且跳过席位/状态筛选——角标上的数字是
      // 这个邮箱在册的车队数，点开却被筛掉几行的话，数字和行数对不上。
      if (focusEmail) {
        return (member.email || '').trim().toLowerCase() === focusEmail;
      }
      if (!statusFilters.has(member.status)) return false;
      const memberSeat = parseSeatType(member.seat_type) ?? 'other';
      if (seatFilter !== 'all' && memberSeat !== seatFilter) {
        return false;
      }
      return matchesSearch(member, search, ['email', 'name', 'team_name', 'owner_email', 'system_display_name']);
    });
    return sortByTimestamp(
      matched,
      (member) =>
        parseTimestamp(
          (member.expiry as { effective_kick_at?: string; expires_at?: string } | undefined)
            ?.effective_kick_at ??
            (member.expiry as { expires_at?: string } | undefined)?.expires_at
        ),
      sortOrder
    );
  }, [members, search, sortOrder, seatFilter, statusFilters, focusEmail]);

  const handleUpdateExpiry = async (
    teamId: string,
    userId: string,
    email: string,
    selection: ExpirySelection
  ) => {
    try {
      if (selection.kind === 'never') {
        extensionRequestIds.current.discardMember(teamId, userId);
        await removeExpiry(teamId, userId);
      } else if (selection.kind === 'duration') {
        const intent = { teamId, userId, duration: selection.value };
        const requestId = extensionRequestIds.current.get(intent);
        await extendMemberExpiry(teamId, userId, selection.value, email, requestId);
        extensionRequestIds.current.confirm(intent);
      } else {
        // 绝对时刻已经带上 +08:00 偏移，后端 parse_optional_datetime 直接收。
        await updateMemberExpiry(teamId, userId, selection.iso);
        extensionRequestIds.current.discardMember(teamId, userId);
      }
      void fetchMembers(false);
      showToast('到期时间已更新');
    } catch (err) {
      console.error(err);
      showToast(err instanceof Error ? err.message : '修改到期时间失败', 'error');
    }
  };

  const hasUnknownSeat = useMemo(
    () => members.some((member) => member.seat_type && parseSeatType(member.seat_type) === null),
    [members],
  );

  const pendingByTeam = useMemo(() => {
    const rows = new Map<string, Array<{ seat_type: string }>>();
    members.forEach((member) => {
      if (member.status !== 'pending' || !member.team_id) return;
      rows.set(member.team_id, [...(rows.get(member.team_id) ?? []), member]);
    });
    return new Map([...rows].map(([teamId, invites]) => [teamId, pendingCountsByType(invites)]));
  }, [members]);

  const handleKick = async (teamId: string, identifier: string, isPending: boolean) => {
    try {
      if (isPending) {
        await kickInvite(teamId, identifier);
      } else {
        await kickMember(teamId, identifier);
      }
      setRefreshTrigger((v) => v + 1);
      showToast(isPending ? '邀请已撤销' : '成员已踢出');
    } catch (err) {
      console.error(err);
      showToast(isPending ? '撤销邀请失败' : '踢出成员失败', 'error');
    }
  };

  const handleCopyTgBinding = async (email: string) => {
    const normalizedEmail = email.trim().toLowerCase();
    if (!normalizedEmail) return;
    setCopyingBindingEmail(normalizedEmail);
    try {
      const result = await createTgMemberCode(normalizedEmail);
      await copyToClipboard(result.copy_text);
      setCopiedBindingEmail(normalizedEmail);
      showToast('已复制 TG 机器人地址和绑定指令');
      if (copiedTimerRef.current !== null) window.clearTimeout(copiedTimerRef.current);
      copiedTimerRef.current = window.setTimeout(() => {
        setCopiedBindingEmail((current) => current === normalizedEmail ? null : current);
      }, 2500);
    } catch (err) {
      console.error(err);
      showToast(err instanceof Error ? err.message : '生成 TG 绑定指令失败', 'error');
    } finally {
      setCopyingBindingEmail(null);
    }
  };

  const multiTeamBadge = (member: AdminMemberRow) => {
    // 已踢出的行不挂角标：角标数的是"当前在册"的车队数，
    // 挂在一条已经不在册的行上只会两边对不上。
    if (member.status === 'kicked') return null;
    const emailKey = (member.email || '').trim().toLowerCase();
    const teamCount = memberTeamCounts.get(emailKey) ?? 0;
    const ownerCount = ownerTeamsByEmail.get(emailKey) ?? 0;
    if (teamCount <= 1 && ownerCount === 0) return null;
    const title = ownerCount
      ? `该邮箱在 ${teamCount} 个 Team 是成员，另在 ${ownerCount} 个 Team 是 Owner（Owner 不在本列表中）；点击只看这个邮箱`
      : `该邮箱同时在 ${teamCount} 个 Team，点击只看这个邮箱`;
    return (
      <button
        type="button"
        onClick={() => setFocusEmail(emailKey)}
        title={title}
        className={cn(
          PILL,
          TONE.warning,
          'relative cursor-pointer ring-1 ring-inset ring-amber-300/70 transition-colors after:absolute after:-inset-2 hover:bg-amber-100 dark:ring-amber-500/30 dark:hover:bg-amber-500/25'
        )}
      >
        ×{teamCount} Team{ownerCount ? ` · Owner×${ownerCount}` : ''}
      </button>
    );
  };

  const memberParts = (member: AdminMemberRow, variant: Variant) => {
    const emailKey = (member.email || '').trim().toLowerCase();
    const expiryView = memberExpiryDisplay(member.expiry, member.is_owner);
    return {
      identity: (
        <UserIdentityCell
          variant={variant}
          email={member.email}
          name={member.name}
          systemDisplayName={member.system_display_name}
          badge={multiTeamBadge(member)}
          onSave={(value) => handleUpdateDisplayName(member.email, value)}
          onError={(message) => showToast(message, 'error')}
        />
      ),
      team: <MemberTeamInfo member={member} />,
      joinedAt: (
        <span className="whitespace-nowrap tabular-nums text-gray-700 dark:text-ink-300" title="系统首次发现该成员的时间">
          {formatJoinedAt(member.expiry?.first_seen_at)}
        </span>
      ),
      expiry: (
        <ExpiryControl
          variant={variant}
          display={expiryView.primary}
          displayTitle={expiryView.title}
          urgency={expiryUrgency(member)}
          grace={expiryView.grace}
          graceTitle={expiryView.graceTitle}
          editable={member.status === 'joined'}
          joinedAt={member.expiry?.first_seen_at}
          policy={kickPolicy}
          onSubmit={(selection) =>
            handleUpdateExpiry(
              member.team_id,
              member.user_id,
              member.email,
              selection
            )
          }
        />
      ),
      seat: (
        <SeatTypeCell
          variant={variant}
          seatType={member.seat_type}
          editable={member.status === 'joined' && Boolean(member.user_id)}
          context={{
            teamId: member.team_id,
            userId: member.user_id,
            team: teamsById.get(member.team_id) ?? null,
            pendingByType: pendingByTeam.get(member.team_id) ?? {},
            isCodexEnabled: member.is_codex_enabled,
            onSwitched: () => setRefreshTrigger((v) => v + 1),
            showToast,
          }}
        />
      ),
      tg: (
        <TgBindingCell
          variant={variant}
          member={member}
          copying={copyingBindingEmail === emailKey}
          copied={copiedBindingEmail === emailKey}
          onCopy={() => handleCopyTgBinding(member.email)}
        />
      ),
      kick:
        member.status === 'joined' || member.status === 'pending' ? (
          <KickMemberButton
            variant={variant}
            member={member}
            onConfirm={() => handleKick(member.team_id, member.status === 'pending' ? member.email : member.user_id, member.status === 'pending')}
          />
        ) : null,
    };
  };

  const rowKey = (member: AdminMemberRow, i: number) => `${member.team_id}-${member.email}-${member.status}-${i}`;

  return (
    <section className="space-y-3">
      {loadError && (
        <LoadErrorBanner busy={loading} onRetry={() => setRefreshTrigger((v) => v + 1)}>
          成员数据加载失败：{loadError}{members.length > 0 ? '。下面是上次成功加载的数据。' : ''}
        </LoadErrorBanner>
      )}
      {focusEmail && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-800 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200">
          <span className="min-w-0 flex-1">
            只看 <span className="font-medium [overflow-wrap:anywhere]">{focusEmail}</span> 在各 Team 的记录
            <span className="text-amber-700/80 dark:text-amber-300/70">（搜索和筛选暂不生效）</span>
          </span>
          <button
            type="button"
            onClick={() => setFocusEmail(null)}
            className="h-8 shrink-0 rounded-md border border-amber-300 px-3 text-xs font-medium transition-colors hover:bg-amber-100 dark:border-amber-500/40 dark:hover:bg-amber-500/15"
          >
            显示全部
          </button>
        </div>
      )}
      {loading ? (
        <ListState>
          <LoadingSpinner size={22} />
          正在加载成员…
        </ListState>
      ) : loadError && members.length === 0 ? null : filteredMembers.length === 0 ? (
        <ListState>
          {members.length === 0
            ? '还没有成员。在「Team 列表」邀请成员，或让用户用兑换码自助加入。'
            : '没有符合当前搜索和筛选的成员。'}
        </ListState>
      ) : (
        <>
          <div className={cn(CARD, 'hidden overflow-hidden xl:block')}>
            {/* Fixed column widths: the table must not reflow when filters change what is in it. */}
            <table className="w-full table-fixed text-left text-sm text-gray-700 dark:text-ink-300">
              <colgroup>
                <col />
                <col className="w-[19%]" />
                <col className="w-[5.5rem]" />
                <col className="w-[7rem]" />
                <col className="w-[13.5rem]" />
                {/* 有「其他（automation）」这类长名称时席位列放宽到整枚标签放得下（按全部数据定，筛选不改列宽）。 */}
                <col className={hasUnknownSeat ? 'w-[9.5rem]' : 'w-[7.5rem]'} />
                <col className="w-[7.5rem]" />
                <col className="w-[6.5rem]" />
              </colgroup>
              <thead className={TABLE_HEAD}>
                <tr>
                  <th className={TH}>成员</th>
                  <th className={TH}>Team / Owner</th>
                  <th className={TH}>状态</th>
                  <th className={TH}>加入时间</th>
                  <th className={TH}>到期</th>
                  <th className={TH}>席位</th>
                  <th className={TH}>TG 绑定</th>
                  <th className={TH}><span className="sr-only">操作</span></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-ink-800">
                {filteredMembers.map((member, i) => {
                  const parts = memberParts(member, 'row');
                  return (
                    <tr key={rowKey(member, i)} className={TR}>
                      <td className="py-3 pl-4 pr-3">{parts.identity}</td>
                      <td className={TD}>{parts.team}</td>
                      <td className={TD}>
                        <div className="flex flex-col items-start gap-1">
                          <MemberStatusPills member={member} />
                        </div>
                      </td>
                      <td className={TD}>{parts.joinedAt}</td>
                      <td className={TD}>{parts.expiry}</td>
                      <td className={TD}>{parts.seat}</td>
                      <td className={TD}>{parts.tg}</td>
                      <td className={cn(TD, 'pl-0 text-right')}>{parts.kick}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="grid gap-3 md:grid-cols-2 xl:hidden">
            {filteredMembers.map((member, i) => {
              const parts = memberParts(member, 'card');
              return (
                <article key={rowKey(member, i)} className={cn(CARD, 'min-w-0 p-4')}>
                  <div className="flex items-start gap-2">
                    <div className="min-w-0 flex-1">{parts.identity}</div>
                    {parts.kick && <div className="-mr-1.5 -mt-1.5">{parts.kick}</div>}
                  </div>
                  <div className="mt-2 flex flex-wrap items-center gap-1.5">
                    <MemberStatusPills member={member} />
                  </div>
                  <dl className={CARD_FIELDS}>
                    <CardField label="Team">{parts.team}</CardField>
                    <CardField label="加入时间">{parts.joinedAt}</CardField>
                    <CardField label="到期">{parts.expiry}</CardField>
                    <CardField label="席位">{parts.seat}</CardField>
                    <CardField label="TG 绑定">{parts.tg}</CardField>
                  </dl>
                </article>
              );
            })}
          </div>

          <ListCount shown={filteredMembers.length} total={members.length} />
        </>
      )}
    </section>
  );
}

export default function UserManagement() {
  // 日常主要在看加入成员，开页就落在这个 tab。
  const [activeTab, setActiveTab] = useState<TabValue>('members');
  const [search, setSearch] = useState('');
  const [ownerSortOrder, setOwnerSortOrder] = useState<SortOrder>('asc');
  // 正序 = 到期近的排前面，最该处理的人在第一屏。
  const [memberSortOrder, setMemberSortOrder] = useState<SortOrder>('asc');
  const [seatFilter, setSeatFilter] = useState<SeatFilter>('all');
  const [toasts, setToasts] = useState<ToastMessage[]>([]);
  const [statusFilters, setStatusFilters] = useState<Set<MemberStatus>>(
    () => new Set(DEFAULT_MEMBER_STATUS_FILTERS)
  );
  const toastIdRef = useRef(0);
  const toastTimersRef = useRef<Set<ReturnType<typeof setTimeout>>>(new Set());
  const [refreshProgress, setRefreshProgress] = useState<{ done: number; total: number } | null>(null);
  const [reloadSignal, setReloadSignal] = useState(0);

  useEffect(() => () => {
    toastTimersRef.current.forEach((timer) => clearTimeout(timer));
    toastTimersRef.current.clear();
  }, []);

  const showToast: ShowToast = (text, type = 'success') => {
    const id = ++toastIdRef.current;
    setToasts((prev) => [...prev, { id, text, type }]);
    const timer = setTimeout(() => {
      toastTimersRef.current.delete(timer);
      setToasts((prev) => prev.filter((toast) => toast.id !== id));
    }, 3500);
    toastTimersRef.current.add(timer);
  };

  // 实时刷新所有在用的 Team（同时最多 3 个），然后重载列表。
  const handleRefreshAll = async () => {
    if (refreshProgress) return;
    setRefreshProgress({ done: 0, total: 0 });
    try {
      const teams = (await fetchTeams()).filter((team) => team.status === 'active');
      if (teams.length === 0) {
        showToast('没有可刷新的 Team', 'error');
        return;
      }
      setRefreshProgress({ done: 0, total: teams.length });
      const settled = await runPool(
        teams,
        3,
        (team) => syncTeam(team.id, true),
        (done, total) => setRefreshProgress({ done, total }),
      );
      const failures = settled.flatMap((outcome, i) =>
        outcome.status === 'rejected'
          ? [{ name: teams[i].name || teams[i].id.slice(0, 8), reason: errorText(outcome.reason, '刷新失败') }]
          : [],
      );
      if (failures.length === 0) {
        showToast(`已刷新 ${teams.length} 个 Team`);
      } else {
        const clip = (text: string) => (text.length > 40 ? `${text.slice(0, 40)}…` : text);
        const listed = failures.slice(0, 3).map((f) => `${f.name}（${clip(f.reason)}）`).join('、');
        showToast(`${failures.length} 个 Team 刷新失败：${listed}${failures.length > 3 ? ' 等' : ''}`, 'error');
      }
      setReloadSignal((v) => v + 1);
    } catch (err) {
      showToast(`刷新失败：${errorText(err)}`, 'error');
    } finally {
      setRefreshProgress(null);
    }
  };

  const seatFilterOptions = useMemo(
    () => [
      { value: 'all', label: '全部席位', dotClass: 'bg-gray-400 dark:bg-ink-500' },
      ...SEAT_TYPE_OPTIONS.map(({ value, label }) => ({
        value,
        label,
        dotClass: SEAT_STYLE[value].solid,
      })),
      { value: 'other', label: '其他', dotClass: UNKNOWN_SEAT_STYLE.solid },
    ],
    []
  );

  return (
    <PageShell title="用户管理" description="所有 Team 的 Owner 和成员：调整到期时间和席位，踢出成员，复制 TG 绑定指令。">
      {toasts.length > 0 && (
        <div
          aria-live="polite"
          className="fixed bottom-4 right-4 z-[100] flex w-[min(24rem,calc(100vw-2rem))] flex-col gap-2"
        >
          {toasts.map((toast) => (
            <Toast key={toast.id} text={toast.text} type={toast.type} />
          ))}
        </div>
      )}

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <SegmentedTabs value={activeTab} onChange={setActiveTab} options={TABS} ariaLabel="用户管理视图" className="md:mr-2" />
        <SearchInput
          value={search}
          onChange={setSearch}
          placeholder={activeTab === 'logs' ? '搜索日志、Team 或邮箱' : '搜索邮箱、姓名或 Team'}
        />

        {activeTab === 'owner' ? (
          <SortToggle
            label="计费到期"
            order={ownerSortOrder}
            onToggle={() => setOwnerSortOrder((v) => (v === 'asc' ? 'desc' : 'asc'))}
          />
        ) : activeTab === 'members' ? (
          <>
            <StatusFilterToggle enabled={statusFilters} onChange={setStatusFilters} />
            <FilterDropdown
              value={seatFilter}
              onChange={(v) => setSeatFilter(v as SeatFilter)}
              options={seatFilterOptions}
            />
            <SortToggle
              label="到期"
              order={memberSortOrder}
              onToggle={() => setMemberSortOrder((v) => (v === 'asc' ? 'desc' : 'asc'))}
            />
          </>
        ) : null}

        {activeTab !== 'logs' && (
          <button
            type="button"
            onClick={() => void handleRefreshAll()}
            disabled={refreshProgress !== null}
            title="逐个实时刷新所有 Team 的成员数据"
            className={cn(BUTTON.secondary, 'h-9 gap-1.5 px-3 py-0 font-normal tabular-nums md:ml-auto')}
          >
            {refreshProgress ? (
              <Loader2 className="size-3.5 animate-spin" aria-hidden />
            ) : (
              <RefreshCw className="size-3.5 text-gray-400 dark:text-ink-500" aria-hidden />
            )}
            {refreshProgress
              ? refreshProgress.total > 0
                ? `刷新中 ${refreshProgress.done}/${refreshProgress.total}`
                : '刷新中…'
              : '全部实时刷新'}
          </button>
        )}
      </div>

      {activeTab === 'owner' ? (
        <OwnerList search={search} sortOrder={ownerSortOrder} reloadSignal={reloadSignal} showToast={showToast} />
      ) : activeTab === 'members' ? (
        <MemberList
          search={search}
          sortOrder={memberSortOrder}
          seatFilter={seatFilter}
          statusFilters={statusFilters}
          reloadSignal={reloadSignal}
          showToast={showToast}
        />
      ) : (
        <SystemLogs embedded scope="members" search={search} />
      )}
    </PageShell>
  );
}
