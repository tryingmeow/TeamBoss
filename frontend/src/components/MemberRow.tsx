import { useState } from 'react';
import { Crown, Trash2, Pencil } from 'lucide-react';
import * as Popover from '@radix-ui/react-popover';
import type { Member, ShowToast } from '../types';
import { removeMember, changeSeat, setExpiry, removeExpiry } from '../api/client';
import {
  SEAT_TYPE_OPTIONS,
  formatSeatTypeLabel,
  normalizeSeatType,
  seatTypeBadgeClass,
  seatUpdateErrorMessage,
} from '../lib/seatType';
import ExpiryPicker from './ExpiryPicker';
import ConfirmDialog from './ConfirmDialog';

interface MemberRowProps {
  member: Member;
  teamId: string;
  isCodexEnabled?: boolean;
  onUpdate: () => void;
  showToast: ShowToast;
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return '';
  const d = new Date(dateStr);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

export default function MemberRow({ member, teamId, isCodexEnabled, onUpdate, showToast }: MemberRowProps) {
  const [seatOpen, setSeatOpen] = useState(false);
  const [expiryOpen, setExpiryOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [loading, setLoading] = useState(false);

  const handleChangeSeat = async (newSeat: string) => {
    setLoading(true);
    try {
      await changeSeat(teamId, member.id, newSeat);
      onUpdate();
      setSeatOpen(false);
      showToast('席位类型已更新');
    } catch (err) {
      // 保留浮层：失败时让管理员看到当前选择仍未生效，而不是悄悄关掉装作成功。
      showToast(seatUpdateErrorMessage(err, normalizeSeatType(newSeat), isCodexEnabled), 'error');
    } finally {
      setLoading(false);
    }
  };

  const handleSetExpiry = async (value: string) => {
    setLoading(true);
    try {
      if (value === '永不') {
        await removeExpiry(teamId, member.id);
      } else {
        await setExpiry(teamId, member.id, value, member.email);
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
  const seatColor = seatTypeBadgeClass(member.seat_type);

  return (
    <>
      <tr className="hover:bg-gray-50 dark:hover:bg-[#222533] group transition-colors">
        <td className="py-2 px-3 min-w-[120px]">
          <div className="text-sm text-gray-900 dark:text-gray-200 font-medium truncate max-w-[120px] sm:max-w-[160px] flex items-center gap-1">
            {member.name || member.email.split('@')[0]}
            {member.is_owner && (
              <Crown size={14} className="text-yellow-500 shrink-0" />
            )}
          </div>
          <div className="text-xs text-gray-500 truncate max-w-[120px] sm:max-w-[160px]">
            {member.email}
          </div>
        </td>

        <td className="py-2 px-3">
          {!member.is_owner ? (
            <Popover.Root open={expiryOpen} onOpenChange={setExpiryOpen}>
              <Popover.Trigger asChild>
                <button className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-900 dark:hover:text-gray-200">
                  {member.expires_at ? formatDate(member.expires_at) : '—'}
                  <Pencil size={10} />
                </button>
              </Popover.Trigger>
              <Popover.Portal>
                <Popover.Content
                  className="bg-white dark:bg-[#1a1d27] border border-gray-200 dark:border-[#2a2d3a] rounded-xl p-3 shadow-xl z-50 w-64"
                  sideOffset={5}
                >
                  <p className="text-xs text-gray-500 dark:text-gray-400 mb-2">设置过期时间</p>
                  <ExpiryPicker
                    value=""
                    onChange={handleSetExpiry}
                  />
                </Popover.Content>
              </Popover.Portal>
            </Popover.Root>
          ) : (
            <span className="text-gray-400 text-xs">—</span>
          )}
        </td>

        <td className="py-2 px-3">
          <Popover.Root open={seatOpen} onOpenChange={setSeatOpen}>
            <Popover.Trigger asChild>
              <button
                className={`flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${seatColor} ${
                  loading ? 'opacity-60 cursor-wait' : 'hover:opacity-80'
                }`}
                disabled={loading}
              >
                {seatLabel}
                <Pencil size={10} />
              </button>
            </Popover.Trigger>
            <Popover.Portal>
              <Popover.Content
                className="bg-white dark:bg-[#1a1d27] border border-gray-200 dark:border-[#2a2d3a] rounded-xl p-2 shadow-xl z-50"
                sideOffset={5}
              >
                {SEAT_TYPE_OPTIONS.map(({ value, label }) => (
                  <button
                    key={value}
                    onClick={() => handleChangeSeat(value)}
                    disabled={loading}
                    className="block w-full text-left px-3 py-1.5 text-sm text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-[#2a2d3a] rounded-lg flex items-center justify-between disabled:opacity-60 disabled:cursor-wait"
                  >
                    {label}
                    {normalizeSeatType(member.seat_type) === value && (
                      <span className="text-xs text-blue-500">✓</span>
                    )}
                  </button>
                ))}
              </Popover.Content>
            </Popover.Portal>
          </Popover.Root>
        </td>

        <td className="py-2 px-3 text-right">
          {!member.is_owner && (
            <button
              onClick={() => setConfirmDelete(true)}
              className="text-gray-500 hover:text-red-400 opacity-0 group-hover:opacity-100 transition-opacity p-1"
            >
              <Trash2 size={14} />
            </button>
          )}
        </td>
      </tr>

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title="移除成员"
        message={`确定要移除 ${member.email} 吗？`}
        confirmLabel="移除"
        destructive
        loading={loading}
        onConfirm={handleDelete}
      />
    </>
  );
}
