import { useRef, useState } from 'react';
import { CalendarClock, ChevronDown, Crown, Trash2 } from 'lucide-react';
import * as Popover from '@radix-ui/react-popover';
import type { Member, ShowToast } from '../types';
import { removeMember, changeSeat, extendMemberExpiry, removeExpiry, updateMemberExpiry } from '../api/client';
import { formatSeatTypeLabel, parseSeatType, seatStyle } from '../lib/seatType';
import { seatSwitchGate, type TeamCapacityFields } from '../lib/seatCapacity';
import { SeatSwitchOptions, useSeatSwitch } from './SeatSwitchMenu';
import { NO_EXPIRY_LABEL, formatAppLocalFull, noExpiryKind, toAppLocal } from '../lib/expiry';
import ExpiryPicker, { type ExpirySelection } from './ExpiryPicker';
import MemberRemarkEditor from './MemberRemarkEditor';
import { useKickPolicy } from '../hooks/useKickPolicy';
import ConfirmDialog from './ConfirmDialog';
import { ExpiryExtensionRequestIds } from '../lib/expiryExtensionRequest';
import { PILL } from './ui';
import { cn } from '../lib/utils';

interface MemberRowProps {
  member: Member;
  teamId: string;
  /** Cached seats and overage policy of the member's Team; decides grey / confirm in the seat menu. */
  team?: TeamCapacityFields | null;
  /** Pending invites per seat type on this Team (they hold seats too). */
  pendingByType?: Record<string, number>;
  isCodexEnabled?: boolean;
  onUpdate: () => void;
  onRemarkSaved: (email: string, remark: string | null) => void;
  showToast: ShowToast;
}

const SOON_MS = 3 * 24 * 3_600_000;

const POPOVER =
  'z-50 rounded-xl border border-gray-200 bg-white shadow-xl dark:border-ink-800 dark:bg-ink-900';

function seatPillClass(seatType: string | null | undefined): string {
  return cn(PILL, seatStyle(seatType).pill);
}

/**
 * 到期日（应用时区的 月/日，跨年时带年份）；已过期标红，3 天内到期标黄。
 * 没有到期时间时按 `source` 区分"永久"和"没记录"，两者不能都叫不过期。
 */
export function ExpiryLabel({ iso, source }: { iso: string | null; source?: string | null }) {
  if (!iso) {
    const kind = noExpiryKind(source);
    return (
      <span className={kind === 'detected' ? 'text-amber-600 dark:text-amber-400' : 'text-gray-400 dark:text-ink-500'}>
        {NO_EXPIRY_LABEL[kind].short}
      </span>
    );
  }
  const date = new Date(iso);
  const left = date.getTime() - Date.now();
  const p = toAppLocal(date);
  const sameYear = p.year === toAppLocal(new Date()).year;
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 tabular-nums',
        left <= 0
          ? 'text-red-600 dark:text-red-400'
          : left < SOON_MS
            ? 'text-amber-600 dark:text-amber-400'
            : 'text-gray-600 dark:text-ink-300',
      )}
    >
      <CalendarClock size={12} className="shrink-0 opacity-70" />
      {sameYear ? `${p.month}/${p.day}` : `${p.year}/${p.month}/${p.day}`}
    </span>
  );
}

export default function MemberRow({
  member,
  teamId,
  team,
  pendingByType,
  isCodexEnabled,
  onUpdate,
  onRemarkSaved,
  showToast,
}: MemberRowProps) {
  const [seatOpen, setSeatOpen] = useState(false);
  const [expiryOpen, setExpiryOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [loading, setLoading] = useState(false);
  const extensionRequestIds = useRef(new ExpiryExtensionRequestIds());
  const kickPolicy = useKickPolicy();

  // 失败时浮层保留：让管理员看到当前选择仍未生效，而不是悄悄关掉装作成功。
  const seatSwitch = useSeatSwitch({
    apply: (seatType, allowOverage) => changeSeat(teamId, member.id, seatType, allowOverage),
    onSwitched: () => {
      onUpdate();
      setSeatOpen(false);
    },
    onAsk: () => setSeatOpen(false),
    showToast,
    isCodexEnabled,
  });
  const currentSeat = parseSeatType(member.seat_type);

  const handleSetExpiry = async (selection: ExpirySelection) => {
    setLoading(true);
    try {
      if (selection.kind === 'never') {
        // 永不过期走的仍然是"删除到期时间"这个接口，语义没变。
        extensionRequestIds.current.discardMember(teamId, member.id);
        await removeExpiry(teamId, member.id);
      } else if (selection.kind === 'duration') {
        const intent = { teamId, userId: member.id, duration: selection.value };
        const requestId = extensionRequestIds.current.get(intent);
        await extendMemberExpiry(teamId, member.id, selection.value, member.email, requestId);
        extensionRequestIds.current.confirm(intent);
      } else {
        await updateMemberExpiry(teamId, member.id, selection.iso);
        extensionRequestIds.current.discardMember(teamId, member.id);
      }
      onUpdate();
      setExpiryOpen(false);
      showToast('到期时间已更新');
    } catch (err) {
      showToast(err instanceof Error ? err.message : '修改到期时间失败', 'error');
    } finally {
      setLoading(false);
    }
  };

  const handleDelete = async () => {
    setLoading(true);
    try {
      await removeMember(teamId, member.id);
      onUpdate();
      setConfirmDelete(false);
      showToast('成员已移除');
    } catch (err) {
      // 不关闭确认弹窗：用户还能看到失败原因并重试，而不是以为已经移除了。
      showToast(err instanceof Error ? err.message : '移除成员失败', 'error');
    } finally {
      setLoading(false);
    }
  };

  const seatLabel = formatSeatTypeLabel(member.seat_type);
  // 备注优先显示：管理员认人靠自己写的备注，ChatGPT 侧的 name 跟在后面，不丢。
  const remark = member.system_display_name?.trim() || '';
  const profileName = member.name?.trim() || '';
  const primaryName = remark || profileName || member.email.split('@')[0];
  const noExpiry = NO_EXPIRY_LABEL[noExpiryKind(member.source)];
  const expiryTitle = member.expires_at ? `到期 ${formatAppLocalFull(new Date(member.expires_at))}` : noExpiry.title;

  return (
    <li className="flex items-center gap-2 px-3 py-2 transition-colors hover:bg-gray-50 dark:hover:bg-ink-850">
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-1">
          <span
            className="truncate text-sm font-medium text-gray-900 dark:text-gray-100"
            title={[remark, profileName, member.email].filter(Boolean).join(' · ')}
          >
            {primaryName}
            {remark && profileName && (
              <span className="font-normal text-gray-400 dark:text-ink-500"> · {profileName}</span>
            )}
          </span>
          {member.is_owner && (
            <Crown size={13} className="shrink-0 text-amber-500" aria-label="所有者" />
          )}
          <MemberRemarkEditor
            email={member.email}
            remark={remark}
            onSaved={onRemarkSaved}
            showToast={showToast}
          />
        </div>
        <div className="truncate text-xs text-gray-500 dark:text-ink-400" title={member.email}>
          {member.email}
        </div>
      </div>

      <div className="flex shrink-0 flex-col items-end gap-1">
        {currentSeat === null ? (
          // 不认识的席位类型：只显示，不给菜单（后端也绝不会动它）。
          <span className={seatPillClass(member.seat_type)} title="TeamBoss 不管理这种席位">{seatLabel}</span>
        ) : (
          <Popover.Root open={seatOpen} onOpenChange={setSeatOpen}>
            <Popover.Trigger asChild>
              <button
                type="button"
                className={cn(seatPillClass(member.seat_type), 'transition-opacity', seatSwitch.busy ? 'cursor-wait opacity-60' : 'hover:opacity-80')}
                disabled={seatSwitch.busy}
                aria-label={`席位类型 ${seatLabel}，点击修改`}
              >
                {seatLabel}
                <ChevronDown size={11} className="opacity-70" />
              </button>
            </Popover.Trigger>
            <Popover.Portal>
              <Popover.Content className={cn(POPOVER, 'w-48 p-1')} sideOffset={6} align="end" collisionPadding={16}>
                <SeatSwitchOptions
                  current={currentSeat}
                  gateFor={(target) => seatSwitchGate(team, member.seat_type, target, pendingByType)}
                  disabled={seatSwitch.busy}
                  onPick={seatSwitch.pick}
                />
              </Popover.Content>
            </Popover.Portal>
          </Popover.Root>
        )}

        {!member.is_owner && (
          <Popover.Root open={expiryOpen} onOpenChange={setExpiryOpen}>
            <Popover.Trigger asChild>
              <button
                type="button"
                className="-mr-1 inline-flex items-center gap-0.5 rounded-md px-1 text-xs leading-5 transition-colors hover:bg-gray-100 dark:hover:bg-ink-800"
                title={expiryTitle}
                aria-label={`到期时间 ${expiryTitle}，点击修改`}
              >
                <ExpiryLabel iso={member.expires_at} source={member.source} />
                <ChevronDown size={11} className="text-gray-400 dark:text-ink-500" />
              </button>
            </Popover.Trigger>
            <Popover.Portal>
              <Popover.Content
                className={cn(POPOVER, 'max-h-[var(--radix-popover-content-available-height)] w-[19rem] max-w-[calc(100vw-2rem)] overflow-y-auto p-3')}
                sideOffset={6}
                align="end"
                collisionPadding={16}
              >
                <p className="text-sm font-medium text-gray-900 dark:text-gray-100">修改到期时间</p>
                <p className="mb-2.5 truncate text-xs text-gray-500 dark:text-ink-400" title={member.email}>
                  {member.email} · {member.expires_at ? `当前 ${formatAppLocalFull(new Date(member.expires_at))}` : noExpiry.current}
                </p>
                <ExpiryPicker
                  onSubmit={handleSetExpiry}
                  joinedAt={member.created_time}
                  policy={kickPolicy}
                  disabled={loading}
                />
              </Popover.Content>
            </Popover.Portal>
          </Popover.Root>
        )}
      </div>

      {member.is_owner ? (
        <span className="size-9 shrink-0" aria-hidden />
      ) : (
        <button
          type="button"
          onClick={() => setConfirmDelete(true)}
          className="inline-flex size-9 shrink-0 items-center justify-center rounded-lg text-gray-400 transition-colors hover:bg-red-50 hover:text-red-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500/40 dark:text-ink-500 dark:hover:bg-red-500/10 dark:hover:text-red-400"
          title="移除成员"
          aria-label="移除成员"
        >
          <Trash2 size={15} />
        </button>
      )}

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title="移除成员"
        message={<>确定把 <span className="break-all font-medium text-gray-900 dark:text-gray-100">{member.email}</span> 移出这个 Team 吗？</>}
        confirmLabel="移除"
        destructive
        loading={loading}
        onConfirm={handleDelete}
      />
      {seatSwitch.dialog}
    </li>
  );
}
