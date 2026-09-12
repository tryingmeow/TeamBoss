import { useState, useEffect, useMemo, useRef } from 'react';
import {
  fetchOwners,
  fetchAllMembers,
  updateMemberExpiry,
  updateMemberSeat,
  kickMember,
  kickInvite,
  setExpiry,
  removeExpiry,
  updateUserDisplayName,
  createTgMemberCode,
} from '../../api/client';
import { Edit2, Plus, Trash2, UserX, Check, Copy, MessageCircle, Search, ArrowUpDown, ChevronDown, Zap } from 'lucide-react';
import * as Dialog from '@radix-ui/react-dialog';
import * as Popover from '@radix-ui/react-popover';
import type { SeatType } from '../../types';
import ExpiryPicker, { type ExpirySelection } from '../../components/ExpiryPicker';
import { useKickPolicy } from '../../hooks/useKickPolicy';
import type { KickPolicy } from '../../lib/expiry';
import Toast from '../../components/Toast';
import SystemLogs from './SystemLogs';
import {
  SEAT_TYPE_OPTIONS,
  formatSeatTypeLabel,
  normalizeSeatType,
  seatTypeBadgeClass,
} from '../../lib/seatType';

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
type SeatFilter = 'all' | SeatType;
type ToastType = 'success' | 'error';
type ShowToast = (text: string, type?: ToastType) => void;

interface ToastMessage {
  id: number;
  text: string;
  type: ToastType;
}

const MEMBER_STATUS_FILTERS: {
  value: MemberStatus;
  dotClass: string;
  ringClass: string;
  title: string;
}[] = [
  { value: 'joined', dotClass: 'bg-emerald-500', ringClass: 'ring-emerald-500/60', title: '已加入' },
  { value: 'pending', dotClass: 'bg-amber-500', ringClass: 'ring-amber-500/60', title: '待接受' },
  { value: 'kicked', dotClass: 'bg-rose-500', ringClass: 'ring-rose-500/60', title: '已踢出' },
];

const DEFAULT_MEMBER_STATUS_FILTERS = new Set<MemberStatus>(['joined', 'pending']);

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

function memberExpiryDisplay(expiry?: MemberExpiryView): { primary: string; grace?: string } {
  if (!expiry) return { primary: 'N/A' };
  if (!expiry.expires_at) return { primary: '永不' };
  const primary = expiry.expires_at_local || expiry.expires_at;
  if (!expiry.effective_kick_at || expiry.effective_kick_at === expiry.expires_at) {
    return { primary };
  }
  const label = expiry.kick_label && expiry.kick_label !== '到期' ? expiry.kick_label : '系统宽限';
  return { primary, grace: `${label} 系统宽限（不计时长）` };
}

function formatShortDate(dateStr: string | null): string {
  if (!dateStr) return '—';
  const d = new Date(dateStr);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

function formatBillingCycle(cycle: BillingCycle | string | null | undefined): string {
  if (!cycle) return '—';
  if (typeof cycle === 'string') return cycle;
  const range = `${formatShortDate(cycle.active_start)} - ${formatShortDate(cycle.active_until)}`;
  const days = cycle.days_remaining != null ? ` (${cycle.days_remaining}d)` : '';
  const renew = cycle.subscription_status === 'expired'
    ? ' · 已到期'
    : cycle.subscription_status === 'stale'
      ? ' · 数据未同步'
      : cycle.will_renew ? '' : ' · 到期不续费';
  return `${range}${days}${renew}`;
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

function isForbiddenError(err: unknown): boolean {
  const message = err instanceof Error ? err.message : String(err ?? '');
  return /\b403\b/i.test(message) || /forbidden/i.test(message);
}

function seatUpdateErrorMessage(
  err: unknown,
  nextSeatType: SeatType,
  isCodexEnabled?: boolean | number
): string {
  const codexKnownOff = isCodexEnabled === false || isCodexEnabled === 0;
  if (nextSeatType === 'usage_based' && codexKnownOff && isForbiddenError(err)) {
    return 'Codex 席位未开启，请先开启 Codex 席位';
  }
  return '修改席位类型失败';
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
      className="inline-flex items-center gap-1 p-1 rounded-lg border border-gray-300 dark:border-slate-700 bg-white dark:bg-slate-900"
      role="group"
      aria-label="成员状态筛选"
    >
      {MEMBER_STATUS_FILTERS.map(({ value, dotClass, ringClass, title }) => {
        const active = enabled.has(value);
        return (
          <button
            key={value}
            type="button"
            title={title}
            aria-label={title}
            aria-pressed={active}
            onClick={() => toggle(value)}
            className={`p-2 rounded-md transition-all ${
              active
                ? `bg-gray-100 dark:bg-slate-800 ring-2 ${ringClass}`
                : 'opacity-35 hover:opacity-70 hover:bg-gray-100 dark:hover:bg-slate-800/80 dark:bg-slate-800/50'
            }`}
          >
            <span className={`block w-3 h-3 rounded-full ${dotClass}`} />
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
        <button
          type="button"
          className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg border border-gray-300 dark:border-slate-700 bg-white dark:bg-slate-900 text-sm text-gray-800 dark:text-slate-200 hover:bg-gray-100 dark:hover:bg-slate-800 hover:border-gray-400 dark:hover:border-slate-600 transition-all"
        >
          <span className={`w-2.5 h-2.5 rounded-full shrink-0 ${selected.dotClass}`} />
          <span>{selected.label}</span>
          <ChevronDown className={`w-3.5 h-3.5 text-gray-500 dark:text-slate-400 transition-transform ${open ? 'rotate-180' : ''}`} />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          className="z-50 min-w-[140px] p-1 rounded-lg bg-white dark:bg-slate-900 border border-gray-300 dark:border-slate-700 shadow-xl"
          sideOffset={4}
          align="start"
        >
          {options.map((opt) => (
            <button
              key={opt.value}
              type="button"
              onClick={() => {
                onChange(opt.value);
                setOpen(false);
              }}
              className={`w-full flex items-center gap-2.5 px-3 py-2 text-sm rounded-md transition-colors ${
                value === opt.value
                  ? 'bg-gray-100 dark:bg-slate-800 text-gray-900 dark:text-slate-100'
                  : 'text-gray-700 dark:text-slate-300 hover:bg-gray-100 dark:hover:bg-slate-800 hover:text-gray-900 dark:hover:text-slate-100'
              }`}
            >
              <span className={`w-2.5 h-2.5 rounded-full shrink-0 ${opt.dotClass}`} />
              <span className="flex-1 text-left">{opt.label}</span>
              {value === opt.value && <Check className="w-3.5 h-3.5 text-indigo-400" />}
            </button>
          ))}
          <Popover.Arrow className="fill-gray-200 dark:fill-slate-700" />
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
      className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-gray-300 dark:border-slate-700 bg-white dark:bg-slate-900 text-sm text-gray-800 dark:text-slate-200 hover:bg-gray-100 dark:hover:bg-slate-800 hover:border-gray-400 dark:hover:border-slate-600 transition-all"
    >
      <ArrowUpDown className="w-3.5 h-3.5 text-gray-500 dark:text-slate-400" />
      {label}
      <span className="text-indigo-400 font-medium">{order === 'asc' ? '正序' : '倒序'}</span>
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
    <div className="relative w-48 sm:w-56">
      <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-500 dark:text-slate-400" />
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full pl-8 pr-3 py-1.5 bg-white dark:bg-slate-900 border border-gray-300 dark:border-slate-700 hover:border-gray-400 dark:hover:border-slate-600 rounded-lg text-sm text-gray-800 dark:text-slate-200 placeholder:text-gray-400 dark:text-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500 transition-all"
      />
    </div>
  );
}

// 加入时间那一列的展示时区。到期/踢人时间的计算口径统一在 lib/expiry.ts。
const APP_TIME_ZONE = 'Asia/Shanghai';

function ExpiryCell({
  display,
  grace,
  editable,
  joinedAt,
  policy,
  onSubmit,
}: {
  display: string;
  grace?: string;
  editable: boolean;
  joinedAt?: string | null;
  policy: KickPolicy;
  onSubmit: (selection: ExpirySelection) => void;
}) {
  const [dateOpen, setDateOpen] = useState(false);
  const [durationOpen, setDurationOpen] = useState(false);

  return (
    <td className="px-6 py-4">
      <div className="flex items-start gap-1.5">
        <div className="flex flex-col gap-0.5">
          <span className="text-gray-700 dark:text-slate-300">{display}</span>
          {grace && (
            <span className="whitespace-nowrap text-[10px] text-amber-600 dark:text-amber-400" title="系统宽限不计入购买时长">
              {grace}
            </span>
          )}
        </div>
        {editable && (
          <>
            {/* 两个入口各管一件事，不合并：日历改到哪一天，加号加多少时长。 */}
            <Popover.Root open={dateOpen} onOpenChange={setDateOpen}>
              <Popover.Trigger asChild>
                <button className="p-0.5 rounded text-gray-400 dark:text-slate-500 hover:text-indigo-400 hover:bg-indigo-500/10 transition-colors" title="修改日期">
                  <Edit2 className="w-3.5 h-3.5" />
                </button>
              </Popover.Trigger>
              <Popover.Portal>
                <Popover.Content
                  className="z-50 w-[19rem] p-3 rounded-xl bg-gray-100 dark:bg-slate-800 border border-gray-300 dark:border-slate-700 shadow-2xl animate-in fade-in zoom-in-95"
                  sideOffset={5}
                >
                  <div className="mb-2 text-sm font-medium text-gray-800 dark:text-slate-200">修改日期</div>
                  <ExpiryPicker
                    mode="date"
                    tone="indigo"
                    policy={policy}
                    joinedAt={joinedAt}
                    onSubmit={(selection) => {
                      onSubmit(selection);
                      setDateOpen(false);
                    }}
                  />
                  <Popover.Arrow className="fill-gray-200 dark:fill-slate-700" />
                </Popover.Content>
              </Popover.Portal>
            </Popover.Root>

            <Popover.Root open={durationOpen} onOpenChange={setDurationOpen}>
              <Popover.Trigger asChild>
                <button className="p-0.5 rounded text-gray-400 dark:text-slate-500 hover:text-indigo-400 hover:bg-indigo-500/10 transition-colors" title="增加时长">
                  <Plus className="w-3.5 h-3.5" />
                </button>
              </Popover.Trigger>
              <Popover.Portal>
                <Popover.Content
                  className="z-50 w-[19rem] p-3 rounded-xl bg-gray-100 dark:bg-slate-800 border border-gray-300 dark:border-slate-700 shadow-2xl animate-in fade-in zoom-in-95"
                  sideOffset={5}
                >
                  <div className="mb-2 text-sm font-medium text-gray-800 dark:text-slate-200">增加时长</div>
                  <ExpiryPicker
                    mode="duration"
                    tone="indigo"
                    policy={policy}
                    joinedAt={joinedAt}
                    onSubmit={(selection) => {
                      onSubmit(selection);
                      setDurationOpen(false);
                    }}
                  />
                  <Popover.Arrow className="fill-gray-200 dark:fill-slate-700" />
                </Popover.Content>
              </Popover.Portal>
            </Popover.Root>
          </>
        )}
      </div>
    </td>
  );
}

function CodexBadge({ isCodexEnabled }: { isCodexEnabled?: boolean | number }) {
  if (isCodexEnabled === undefined) return null;
  const enabled = Boolean(isCodexEnabled);
  return (
    <span className={`flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] font-medium w-fit ${enabled ? 'bg-purple-100 text-purple-600 dark:bg-purple-500/20 dark:text-purple-400' : 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'}`} title={`Codex ${enabled ? 'ON' : 'OFF'}`}>
      <Zap size={10} /> {enabled ? 'Codex ON' : 'Codex OFF'}
    </span>
  );
}

function SeatTypeCell({
  seatType,
  editable,
  onChange,
}: {
  seatType: string | null | undefined;
  editable: boolean;
  onChange?: (value: SeatType) => void;
}) {
  return (
    <div className="flex items-center gap-1.5">
      <span
        className={`inline-flex items-center px-2.5 py-1 rounded-md text-xs font-medium ${seatTypeBadgeClass(seatType, 'admin')}`}
      >
        {formatSeatTypeLabel(seatType)}
      </span>
      {editable && onChange && (
        <Popover.Root>
          <Popover.Trigger asChild>
            <button className="p-0.5 rounded text-gray-400 dark:text-slate-500 hover:text-indigo-400 hover:bg-indigo-500/10 transition-colors">
              <Edit2 className="w-3 h-3" />
            </button>
          </Popover.Trigger>
          <Popover.Portal>
            <Popover.Content
              className="z-50 w-48 p-2 rounded-xl bg-gray-100 dark:bg-slate-800 border border-gray-300 dark:border-slate-700 shadow-2xl"
              sideOffset={5}
            >
              <div className="flex flex-col gap-1">
                {SEAT_TYPE_OPTIONS.map(({ value, label }) => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => onChange(value)}
                    className="text-left px-3 py-2 text-sm text-gray-700 dark:text-slate-300 hover:bg-gray-200 dark:bg-slate-700 hover:text-gray-900 dark:text-slate-100 rounded-lg flex items-center justify-between"
                  >
                    <span className="flex items-center gap-2">
                      <span
                        className={`w-2 h-2 rounded-full ${value === 'usage_based' ? 'bg-purple-500' : 'bg-indigo-400'}`}
                      />
                      {label}
                    </span>
                    {normalizeSeatType(seatType) === value && (
                      <Check className="w-4 h-4 text-indigo-400" />
                    )}
                  </button>
                ))}
              </div>
              <Popover.Arrow className="fill-gray-200 dark:fill-slate-700" />
            </Popover.Content>
          </Popover.Portal>
        </Popover.Root>
      )}
    </div>
  );
}

function UserIdentityCell({
  email,
  name,
  systemDisplayName,
  onSave,
  onError,
}: {
  email: string;
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
      onError?.('保存系统显示名称失败');
    } finally {
      setSaving(false);
    }
  };

  const editControl = (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <button
          type="button"
          onClick={openEditor}
          className="p-0.5 rounded text-gray-400 dark:text-slate-500 hover:text-indigo-400 hover:bg-indigo-500/10 transition-colors shrink-0"
          title="设置系统显示名称"
        >
          <Edit2 className="w-3.5 h-3.5" />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          className="z-50 w-72 p-4 rounded-xl bg-gray-100 dark:bg-slate-800 border border-gray-300 dark:border-slate-700 shadow-2xl animate-in fade-in zoom-in-95"
          sideOffset={5}
        >
          <div className="mb-2 text-sm font-medium text-gray-800 dark:text-slate-200">系统显示名称</div>
          <input
            type="text"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="留空则使用邮箱作为主显示名称"
            className="w-full px-3 py-2 bg-white dark:bg-slate-900 border border-gray-300 dark:border-slate-700 rounded-lg text-sm text-gray-800 dark:text-slate-200 placeholder:text-gray-400 dark:text-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500"
            maxLength={120}
          />
          <div className="mt-3 flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="px-3 py-1.5 rounded-lg bg-gray-200 dark:bg-slate-700 text-gray-700 dark:text-slate-300 hover:bg-gray-300 dark:hover:bg-slate-600 text-xs font-medium transition-colors"
            >
              取消
            </button>
            <button
              type="button"
              onClick={handleSave}
              disabled={saving}
              className="px-3 py-1.5 rounded-lg bg-indigo-500 text-white hover:bg-indigo-600 text-xs font-medium transition-colors disabled:opacity-60"
            >
              {saving ? '保存中...' : '保存'}
            </button>
          </div>
          <Popover.Arrow className="fill-gray-200 dark:fill-slate-700" />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );

  return (
    <div className="space-y-0.5">
      {hasCustomName ? (
        <>
          <div className="font-medium text-gray-800 dark:text-slate-200">{customName}</div>
          <div className="flex items-center gap-1 text-gray-500 dark:text-slate-400 text-xs">
            <span className="break-all">{email}</span>
            {editControl}
          </div>
        </>
      ) : (
        <div className="flex items-center gap-1 font-medium text-gray-800 dark:text-slate-200">
          <span className="break-all">{email}</span>
          {editControl}
        </div>
      )}
      {profileName && (
        <div className="text-gray-400 dark:text-slate-500 text-xs">{profileName}</div>
      )}
    </div>
  );
}

function OwnerList({
  search,
  sortOrder,
  showToast,
}: {
  search: string;
  sortOrder: SortOrder;
  showToast: ShowToast;
}) {
  const [owners, setOwners] = useState<OwnerRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshTrigger, setRefreshTrigger] = useState(0);

  useEffect(() => {
    setLoading(true);
    fetchOwners()
      .then((res) => setOwners(res.items))
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [refreshTrigger]);

  const handleUpdateSeat = async (
    teamId: string,
    userId: string,
    seatType: SeatType,
    isCodexEnabled?: boolean | number
  ) => {
    if (!userId) {
      showToast('无法获取管理员 ID，请刷新后重试', 'error');
      return;
    }
    try {
      await updateMemberSeat(teamId, userId, seatType);
      setRefreshTrigger((v) => v + 1);
      showToast('席位类型已更新');
    } catch (err) {
      console.error(err);
      showToast(seatUpdateErrorMessage(err, seatType, isCodexEnabled), 'error');
    }
  };

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

  return (
    <div className="overflow-x-auto rounded-xl border border-gray-200 dark:border-slate-800">
      <table className="w-full text-left text-sm text-gray-700 dark:text-slate-300">
        <thead className="bg-white dark:bg-slate-900 text-gray-500 dark:text-slate-400">
          <tr>
            <th className="px-6 py-4 font-medium">邮箱</th>
            <th className="px-6 py-4 font-medium">姓名</th>
            <th className="px-6 py-4 font-medium">队伍 (状态)</th>
            <th className="px-6 py-4 font-medium">席位类型</th>
            <th className="px-6 py-4 font-medium">卡号后四位</th>
            <th className="px-6 py-4 font-medium">计费周期</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-200 dark:divide-slate-800/50 bg-gray-50 dark:bg-slate-950/50">
          {loading ? (
            <tr><td colSpan={6} className="px-6 py-8 text-center">加载中...</td></tr>
          ) : filteredOwners.length === 0 ? (
            <tr><td colSpan={6} className="px-6 py-8 text-center text-gray-400 dark:text-slate-500">暂无匹配的管理员</td></tr>
          ) : (
            filteredOwners.map((owner, i) => (
              <tr key={`${owner.team_id}-${owner.email}-${i}`} className="transition-colors hover:bg-gray-100 dark:hover:bg-slate-800">
                <td className="px-6 py-4">
                  <UserIdentityCell
                    email={owner.email}
                    name={owner.name}
                    systemDisplayName={owner.system_display_name}
                    onSave={(value) => handleUpdateDisplayName(owner.email, value)}
                    onError={(message) => showToast(message, 'error')}
                  />
                </td>
                <td className="px-6 py-4">{owner.name}</td>
                <td className="px-6 py-4">
                  <div className="flex flex-col items-start gap-1">
                    <span className="font-medium text-gray-700 dark:text-slate-300">{owner.team_name}</span>
                    <CodexBadge isCodexEnabled={owner.is_codex_enabled} />
                  </div>
                </td>
                <td className="px-6 py-4">
                  <SeatTypeCell
                    seatType={owner.seat_type}
                    editable
                    onChange={(value) => handleUpdateSeat(owner.team_id, owner.user_id, value, owner.is_codex_enabled)}
                  />
                </td>
                <td className="px-6 py-4 font-mono text-gray-500 dark:text-slate-400">{owner.card_last4 || '-'}</td>
                <td className="px-6 py-4">{formatBillingCycle(owner.billing_cycle)}</td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

function MemberList({
  search,
  sortOrder,
  seatFilter,
  statusFilters,
  showToast,
}: {
  search: string;
  sortOrder: SortOrder;
  seatFilter: SeatFilter;
  statusFilters: Set<MemberStatus>;
  showToast: ShowToast;
}) {
  const [members, setMembers] = useState<AdminMemberRow[]>([]);
  // Owner 行不进表格（表格是"成员"视图），但必须参与多车队角标的统计：
  // 一个邮箱在 A 队是成员、在 B 队是 Owner，正是最需要人工确认的那种情况。
  const [ownerTeamsByEmail, setOwnerTeamsByEmail] = useState<Map<string, number>>(new Map());
  // 角标点开的是"就这一个邮箱"，不是把邮箱塞进全文搜索——后者会把
  // owner_email 命中的整队人也一起捞出来。
  const [focusEmail, setFocusEmail] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshTrigger, setRefreshTrigger] = useState(0);
  const [copyingBindingEmail, setCopyingBindingEmail] = useState<string | null>(null);
  const [copiedBindingEmail, setCopiedBindingEmail] = useState<string | null>(null);
  const copiedTimerRef = useRef<number | null>(null);
  const kickPolicy = useKickPolicy();

  const fetchMembers = (showLoading = true) => {
    if (showLoading) setLoading(true);
    return fetchAllMembers({ includeOwners: true })
      .then((res) => {
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
          showToast(`${failed.length} 个车队的成员数据未能载入（${names}），列表与角标可能不完整`, 'error');
        }
      })
      .catch(console.error)
      .finally(() => {
        if (showLoading) setLoading(false);
      });
  };

  useEffect(() => {
    fetchMembers();
  }, [refreshTrigger]);

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
      if (seatFilter !== 'all' && normalizeSeatType(member.seat_type) !== seatFilter) {
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
        await removeExpiry(teamId, userId);
      } else if (selection.kind === 'duration') {
        await setExpiry(teamId, userId, selection.value, email);
      } else {
        // 绝对时刻已经带上 +08:00 偏移，后端 parse_optional_datetime 直接收。
        await updateMemberExpiry(teamId, userId, selection.iso);
      }
      void fetchMembers(false);
      showToast('到期时间已更新');
    } catch (err) {
      console.error(err);
      showToast('修改到期时间失败', 'error');
    }
  };

  const handleUpdateSeat = async (
    teamId: string,
    userId: string,
    seatType: SeatType,
    isCodexEnabled?: boolean | number
  ) => {
    try {
      await updateMemberSeat(teamId, userId, seatType);
      setRefreshTrigger((v) => v + 1);
      showToast('席位类型已更新');
    } catch (err) {
      console.error(err);
      showToast(seatUpdateErrorMessage(err, seatType, isCodexEnabled), 'error');
    }
  };

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

  return (
    <div className="overflow-x-auto rounded-xl border border-gray-200 dark:border-slate-800">
      {focusEmail && (
        <div className="flex items-center gap-2 border-b border-gray-200 dark:border-slate-800 bg-amber-50 dark:bg-amber-950/30 px-6 py-2 text-xs text-amber-800 dark:text-amber-200">
          <span>只看邮箱</span>
          <span className="font-mono font-medium">{focusEmail}</span>
          <span className="text-amber-700/70 dark:text-amber-300/70">（已忽略搜索与筛选）</span>
          <button
            type="button"
            onClick={() => setFocusEmail(null)}
            className="ml-auto rounded-full border border-amber-300 dark:border-amber-700/60 px-2 py-px font-medium transition-colors hover:bg-amber-100 dark:hover:bg-amber-900/50"
          >
            取消
          </button>
        </div>
      )}
      <table className="w-full text-left text-sm text-gray-700 dark:text-slate-300">
        <thead className="bg-white dark:bg-slate-900 text-gray-500 dark:text-slate-400">
          <tr>
            <th className="px-6 py-4 font-medium">邮箱 / 姓名</th>
            <th className="px-6 py-4 font-medium">队伍 & Owner (状态)</th>
            <th className="px-6 py-4 font-medium whitespace-nowrap min-w-[7.5rem]">状态</th>
            <th className="px-6 py-4 font-medium">发现</th>
            <th className="px-6 py-4 font-medium">到期</th>
            <th className="px-6 py-4 font-medium">席位类型</th>
            <th className="px-6 py-4 font-medium whitespace-nowrap">TG 绑定</th>
            <th className="px-6 py-4 font-medium text-right">操作</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-200 dark:divide-slate-800/50 bg-gray-50 dark:bg-slate-950/50">
          {loading ? (
            <tr><td colSpan={8} className="px-6 py-8 text-center">加载中...</td></tr>
          ) : filteredMembers.length === 0 ? (
            <tr><td colSpan={8} className="px-6 py-8 text-center text-gray-400 dark:text-slate-500">暂无匹配的成员</td></tr>
          ) : (
            filteredMembers.map((member, i) => (
              <tr key={`${member.team_id}-${member.email}-${member.status}-${i}`} className="transition-colors hover:bg-gray-100 dark:hover:bg-slate-800">
                <td className="px-6 py-4">
                  <UserIdentityCell
                    email={member.email}
                    name={member.name}
                    systemDisplayName={member.system_display_name}
                    onSave={(value) => handleUpdateDisplayName(member.email, value)}
                    onError={(message) => showToast(message, 'error')}
                  />
                </td>
                <td className="px-6 py-4">
                  <div className="flex flex-col items-start gap-1">
                    <span className="flex items-center gap-1.5">
                      <span className="font-medium text-gray-700 dark:text-slate-300">{member.team_name}</span>
                      {(() => {
                        // 已踢出的行不挂角标：角标数的是"当前在册"的车队数，
                        // 挂在一条已经不在册的行上只会两边对不上。
                        if (member.status === 'kicked') return null;
                        const emailKey = (member.email || '').trim().toLowerCase();
                        const teamCount = memberTeamCounts.get(emailKey) ?? 0;
                        const ownerCount = ownerTeamsByEmail.get(emailKey) ?? 0;
                        if (teamCount <= 1 && ownerCount === 0) return null;
                        const title = ownerCount
                          ? `该邮箱在 ${teamCount} 个车队为成员，另有 ${ownerCount} 个车队的 Owner 身份（Owner 行不在本列表中）；点击只看这个邮箱`
                          : `该邮箱同时在 ${teamCount} 个车队，点击只看这个邮箱`;
                        return (
                          <button
                            type="button"
                            onClick={() => setFocusEmail(emailKey)}
                            title={title}
                            className="rounded-full border border-amber-300 dark:border-amber-700/60 bg-amber-50 dark:bg-amber-950/40 px-1.5 py-px text-[11px] font-medium leading-4 text-amber-700 dark:text-amber-300 transition-colors hover:bg-amber-100 dark:hover:bg-amber-900/50"
                          >
                            ×{teamCount} 队{ownerCount ? ` · Owner×${ownerCount}` : ''}
                          </button>
                        );
                      })()}
                    </span>
                    <span className="text-gray-400 dark:text-slate-500 text-xs">{member.owner_email}</span>
                    <CodexBadge isCodexEnabled={member.is_codex_enabled} />
                  </div>
                </td>
                <td className="px-6 py-4 whitespace-nowrap min-w-[7.5rem]">
                  <div className="flex flex-col gap-1 items-start">
                  <span className={`inline-flex items-center gap-2 px-2.5 py-1 rounded-full text-xs font-medium border whitespace-nowrap
                    ${member.status === 'joined' ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20' : ''}
                    ${member.status === 'pending' ? 'bg-amber-500/10 text-amber-400 border-amber-500/20' : ''}
                    ${member.status === 'kicked' ? 'bg-rose-500/10 text-rose-400 border-rose-500/20' : ''}
                  `}>
                    <span className={`w-2 h-2 rounded-full shrink-0
                      ${member.status === 'joined' ? 'bg-emerald-500' : ''}
                      ${member.status === 'pending' ? 'bg-amber-500' : ''}
                      ${member.status === 'kicked' ? 'bg-rose-500' : ''}
                    `} />
                    {member.status_label || member.status}
                  </span>
                  {member.status === 'joined' && (() => {
                    const src = member.expiry?.source || 'system';
                    const styles: Record<string, string> = {
                      system: 'bg-slate-500/10 text-gray-500 dark:text-slate-400 border border-slate-500/20',
                      detected: 'bg-amber-500/10 text-amber-400 border border-amber-500/20',
                      self_service: 'bg-cyan-500/10 text-cyan-400 border border-cyan-500/20',
                    };
                    const labels: Record<string, string> = {
                      system: '系统邀请',
                      detected: '手动拉入',
                      self_service: '自助加入',
                    };
                    return (
                      <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium w-fit ${styles[src] || styles.system}`}>
                        {labels[src] || '系统邀请'}
                      </span>
                    );
                  })()}
                  {member.status === 'kicked' && member.expiry?.kick_source && (
                    <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium w-fit
                      ${member.expiry.kick_source === 'auto_expire' ? 'bg-violet-500/10 text-violet-400 border border-violet-500/20' : ''}
                      ${member.expiry.kick_source === 'admin' ? 'bg-sky-500/10 text-sky-400 border border-sky-500/20' : ''}
                      ${member.expiry.kick_source === 'detected' ? 'bg-amber-500/10 text-amber-400 border border-amber-500/20' : ''}
                    `}>
                      {member.expiry.kick_source === 'auto_expire' && '自动过期'}
                      {member.expiry.kick_source === 'admin' && '手动踢出'}
                      {member.expiry.kick_source === 'detected' && '检测移除'}
                    </span>
                  )}
                  </div>
                </td>

                <td className="px-6 py-4">
                  <span className="text-gray-700 dark:text-slate-300 text-xs">
                    {member.expiry?.first_seen_at
                      ? new Date(member.expiry.first_seen_at).toLocaleString('zh-CN', {
                          timeZone: APP_TIME_ZONE,
                          month: 'numeric',
                          day: 'numeric',
                          hour: '2-digit',
                          minute: '2-digit',
                        })
                      : '—'}
                  </span>
                </td>

                <ExpiryCell
                  display={memberExpiryDisplay(member.expiry).primary}
                  grace={memberExpiryDisplay(member.expiry).grace}
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

                <td className="px-6 py-4">
                  <SeatTypeCell
                    seatType={member.seat_type}
                    editable={member.status === 'joined' && Boolean(member.user_id)}
                    onChange={(value) => handleUpdateSeat(member.team_id, member.user_id, value, member.is_codex_enabled)}
                  />
                </td>

                <td className="px-6 py-4 whitespace-nowrap">
                  {member.status === 'kicked' ? (
                    <span className="text-xs text-gray-400 dark:text-slate-600">
                      {member.tg_binding?.bound ? '已绑定（其他成员）' : '已解绑'}
                    </span>
                  ) : (
                    <div className="flex items-center gap-2">
                      <span
                        className={`inline-flex items-center gap-1 rounded-full border px-2 py-1 text-xs font-medium ${
                          member.tg_binding?.bound
                            ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                            : 'border-slate-300 bg-slate-100 text-slate-500 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-400'
                        }`}
                        title={member.tg_binding?.username ? `@${member.tg_binding.username}` : undefined}
                      >
                        <MessageCircle className="h-3.5 w-3.5" />
                        {member.tg_binding?.bound ? '已绑定' : '未绑定'}
                      </span>
                      <button
                        type="button"
                        onClick={() => handleCopyTgBinding(member.email)}
                        disabled={copyingBindingEmail === (member.email || '').trim().toLowerCase()}
                        className="rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-indigo-500/10 hover:text-indigo-500 disabled:cursor-wait disabled:opacity-50 dark:text-slate-500 dark:hover:text-indigo-400"
                        title={member.tg_binding?.bound ? '复制重新绑定指令' : '复制绑定指令'}
                        aria-label={member.tg_binding?.bound ? `复制 ${member.email} 的重新绑定指令` : `复制 ${member.email} 的绑定指令`}
                      >
                        {copiedBindingEmail === (member.email || '').trim().toLowerCase()
                          ? <Check className="h-4 w-4 text-emerald-500" />
                          : <Copy className="h-4 w-4" />}
                      </button>
                    </div>
                  )}
                </td>

                <td className="px-6 py-4 text-right">
                  {(member.status === 'joined' || member.status === 'pending') && (
                    <Dialog.Root>
                      <Dialog.Trigger asChild>
                        <button className="p-2 rounded-lg text-gray-400 dark:text-slate-500 hover:bg-rose-500/10 hover:text-rose-400 transition-colors">
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </Dialog.Trigger>
                      <Dialog.Portal>
                        <Dialog.Overlay className="fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 animate-in fade-in" />
                        <Dialog.Content className="fixed left-[50%] top-[50%] translate-x-[-50%] translate-y-[-50%] w-full max-w-md bg-white dark:bg-slate-900 border border-gray-200 dark:border-slate-800 rounded-2xl p-6 z-50 shadow-2xl animate-in fade-in zoom-in-95">
                          <Dialog.Title className="text-lg font-semibold text-gray-900 dark:text-slate-100 flex items-center gap-2 mb-2">
                            <UserX className="w-5 h-5 text-rose-500" />
                            确认踢出该成员
                          </Dialog.Title>
                          <Dialog.Description className="text-gray-500 dark:text-slate-400 mb-6 text-sm">
                            您确定要{member.status === 'pending' ? '撤销对' : '踢出'}{' '}
                            <strong className="text-gray-800 dark:text-slate-200">{member.email}</strong> 吗？
                            此操作不可逆转。
                          </Dialog.Description>
                          <div className="flex justify-end gap-3">
                            <Dialog.Close asChild>
                              <button className="px-4 py-2 rounded-lg bg-gray-100 dark:bg-slate-800 text-gray-700 dark:text-slate-300 hover:bg-gray-200 dark:bg-slate-700 font-medium transition-colors">
                                取消
                              </button>
                            </Dialog.Close>
                            <Dialog.Close asChild>
                              <button
                                onClick={() => handleKick(member.team_id, member.status === 'pending' ? member.email : member.user_id, member.status === 'pending')}
                                className="px-4 py-2 rounded-lg bg-rose-500 text-white hover:bg-rose-600 font-medium transition-colors shadow-lg shadow-rose-500/20"
                              >
                                确认踢出
                              </button>
                            </Dialog.Close>
                          </div>
                        </Dialog.Content>
                      </Dialog.Portal>
                    </Dialog.Root>
                  )}
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

export default function UserManagement() {
  // 日常主要在看加入成员，开页就落在这个 tab。
  const [activeTab, setActiveTab] = useState<'owner' | 'members' | 'logs'>('members');
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

  const seatFilterOptions = useMemo(
    () => [
      { value: 'all', label: '全部席位', dotClass: 'bg-slate-500' },
      ...SEAT_TYPE_OPTIONS.map(({ value, label }) => ({
        value,
        label,
        dotClass: value === 'usage_based' ? 'bg-purple-500' : 'bg-blue-500',
      })),
    ],
    []
  );

  return (
    <div className="p-8 max-w-7xl mx-auto space-y-8 animate-in fade-in duration-500">
      {toasts.length > 0 && (
        <div className="fixed right-5 top-20 z-[100] flex w-[min(22rem,calc(100vw-2rem))] flex-col gap-2">
          {toasts.map((toast) => (
            <Toast key={toast.id} text={toast.text} type={toast.type} />
          ))}
        </div>
      )}

      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-slate-100 mb-1">用户管理</h1>
        <p className="text-gray-500 dark:text-slate-400 text-sm">管理队伍管理员与所有加入的成员。</p>
      </div>

      <div className="flex flex-col sm:flex-row sm:items-end justify-between gap-4 border-b border-gray-200 dark:border-slate-800 pb-px">
        <div className="flex gap-4">
          <button
            onClick={() => setActiveTab('owner')}
            className={`pb-3 px-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
              activeTab === 'owner' ? 'border-indigo-500 text-indigo-400' : 'border-transparent text-gray-500 dark:text-slate-400 hover:text-gray-700 dark:text-slate-300'
            }`}
          >
            队伍管理员
          </button>
          <button
            onClick={() => setActiveTab('members')}
            className={`pb-3 px-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
              activeTab === 'members' ? 'border-indigo-500 text-indigo-400' : 'border-transparent text-gray-500 dark:text-slate-400 hover:text-gray-700 dark:text-slate-300'
            }`}
          >
            加入成员
          </button>
          <button
            onClick={() => setActiveTab('logs')}
            className={`pb-3 px-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
              activeTab === 'logs' ? 'border-indigo-500 text-indigo-400' : 'border-transparent text-gray-500 dark:text-slate-400 hover:text-gray-700 dark:text-slate-300'
            }`}
          >
            日志
          </button>
        </div>

        <div className="flex flex-wrap items-center gap-2 pb-2">
          <SearchInput
            value={search}
            onChange={setSearch}
            placeholder={activeTab === 'logs' ? '搜索人员日志、Team 或邮箱...' : '搜索邮箱/姓名/队伍...'}
          />

          {activeTab === 'owner' ? (
            <SortToggle
              label="计费"
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
        </div>
      </div>

      <div className="pt-2">
        {activeTab === 'owner' ? (
          <OwnerList search={search} sortOrder={ownerSortOrder} showToast={showToast} />
        ) : activeTab === 'members' ? (
          <MemberList
            search={search}
            sortOrder={memberSortOrder}
            seatFilter={seatFilter}
            statusFilters={statusFilters}
            showToast={showToast}
          />
        ) : (
          <SystemLogs embedded scope="members" search={search} />
        )}
      </div>
    </div>
  );
}
