import { X, MailPlus, Loader2 } from 'lucide-react';
import type { MembersData, ShowToast } from '../types';
import MemberRow from './MemberRow';
import LoadingSpinner from './LoadingSpinner';
import { revokeInvite } from '../api/client';
import { formatSeatTypeLabel } from '../lib/seatType';
import { useState } from 'react';

interface MemberPanelProps {
  teamId: string;
  data: MembersData | null;
  loading: boolean;
  settling?: boolean;
  isCodexEnabled?: boolean;
  onRefresh: () => void;
  showToast: ShowToast;
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return '';
  const d = new Date(dateStr);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

export default function MemberPanel({ teamId, data, loading, settling, isCodexEnabled, onRefresh, showToast }: MemberPanelProps) {
  const [revoking, setRevoking] = useState<string | null>(null);

  const handleRevoke = async (email: string) => {
    setRevoking(email);
    try {
      await revokeInvite(teamId, email);
      onRefresh();
      showToast('邀请已撤销');
    } catch (err) {
      showToast(err instanceof Error ? err.message : '撤销邀请失败', 'error');
    } finally {
      setRevoking(null);
    }
  };

  if (loading) {
    return (
      <div className="flex justify-center py-6">
        <LoadingSpinner size={24} />
      </div>
    );
  }

  if (!data) return null;

  return (
    <div className="mt-1 overflow-x-auto">
      {settling && (
        <div className="flex items-center gap-1.5 px-3 py-1.5 text-xs text-blue-500 dark:text-blue-400">
          <Loader2 size={12} className="animate-spin" />
          同步中…改动生效后自动刷新
        </div>
      )}
      <table className="w-full text-left text-sm">
        <thead className="text-xs text-gray-500 dark:text-gray-400 border-b border-gray-100 dark:border-[#2a2d3a]">
          <tr>
            <th className="font-normal py-2 px-3">邮箱 / 姓名</th>
            <th className="font-normal py-2 px-3">到期时间</th>
            <th className="font-normal py-2 px-3">席位类型</th>
            <th className="font-normal py-2 px-3 text-right">操作</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-50 dark:divide-[#2a2d3a]">
          {data.members.map((m) => (
            <MemberRow
              key={m.id}
              member={m}
              teamId={teamId}
              isCodexEnabled={isCodexEnabled}
              onUpdate={onRefresh}
              showToast={showToast}
            />
          ))}
          {data.pending_invites.map((inv) => (
            <tr key={inv.id} className="hover:bg-gray-50 dark:hover:bg-[#222533] group transition-colors">
              <td className="py-2 px-3 min-w-[120px]">
                <div className="text-gray-700 dark:text-gray-300 truncate max-w-[120px] sm:max-w-[160px] flex items-center gap-1">
                  {inv.email}
                  <MailPlus size={14} className="text-yellow-500 shrink-0" />
                </div>
                <div className="text-xs text-gray-400">待接受</div>
              </td>
              <td className="py-2 px-3">
                {inv.expires_at ? (
                  <span className="text-xs text-yellow-600 dark:text-yellow-400">
                    {formatDate(inv.expires_at)}
                  </span>
                ) : (
                  <span className="text-gray-400">—</span>
                )}
              </td>
              <td className="py-2 px-3">
                <span className="text-xs text-gray-500 bg-gray-100 dark:bg-gray-800 px-2 py-0.5 rounded">
                  {formatSeatTypeLabel(inv.seat_type)}
                </span>
              </td>
              <td className="py-2 px-3 text-right">
                <button
                  onClick={() => handleRevoke(inv.email)}
                  disabled={revoking === inv.email}
                  className="text-gray-500 hover:text-red-400 opacity-0 group-hover:opacity-100 transition-opacity p-1"
                  title="撤销邀请"
                >
                  <X size={14} />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {data.members.length === 0 && data.pending_invites.length === 0 && (
        <p className="text-center text-sm text-gray-500 py-4">暂无成员</p>
      )}
    </div>
  );
}
