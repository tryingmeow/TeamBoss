import { useState } from 'react';
import { Loader2, X } from 'lucide-react';
import type { MembersData, ShowToast } from '../types';
import MemberRow, { ExpiryLabel } from './MemberRow';
import MemberRemarkEditor from './MemberRemarkEditor';
import LoadingSpinner from './LoadingSpinner';
import ConfirmDialog from './ConfirmDialog';
import { revokeInvite } from '../api/client';
import { formatSeatTypeLabel, seatStyle } from '../lib/seatType';
import { formatAppLocalFull } from '../lib/expiry';
import { PILL, TONE } from './ui';
import { cn } from '../lib/utils';

interface MemberPanelProps {
  teamId: string;
  data: MembersData | null;
  loading: boolean;
  settling?: boolean;
  isCodexEnabled?: boolean;
  onRefresh: () => void;
  onRemarkSaved: (email: string, remark: string | null) => void;
  showToast: ShowToast;
}

export default function MemberPanel({ teamId, data, loading, settling, isCodexEnabled, onRefresh, onRemarkSaved, showToast }: MemberPanelProps) {
  const [revoking, setRevoking] = useState<string | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState<string | null>(null);

  const handleRevoke = async (email: string) => {
    setRevoking(email);
    try {
      await revokeInvite(teamId, email);
      onRefresh();
      setConfirmRevoke(null);
      showToast('邀请已撤销');
    } catch (err) {
      // 不关确认框：失败原因留在眼前，可以直接重试。
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

  const empty = data.members.length === 0 && data.pending_invites.length === 0;

  return (
    <div className="pt-1">
      <div className="flex items-center justify-between gap-2 px-3 py-2 text-xs text-gray-500 dark:text-ink-400">
        <span className="whitespace-nowrap">
          成员 {data.members.length}
          {data.pending_invites.length > 0 && ` · 待接受 ${data.pending_invites.length}`}
        </span>
        {settling && (
          <span className="flex items-center gap-1.5 whitespace-nowrap text-blue-600 dark:text-blue-400">
            <Loader2 size={12} className="animate-spin" />
            同步中…
          </span>
        )}
      </div>

      {empty ? (
        <p className="py-4 text-center text-sm text-gray-500 dark:text-ink-400">暂无成员</p>
      ) : (
        <ul
          aria-label="成员列表"
          className="divide-y divide-gray-100 border-t border-gray-100 dark:divide-ink-800 dark:border-ink-800"
        >
          {data.members.map((m) => (
            <MemberRow
              key={m.id}
              member={m}
              teamId={teamId}
              isCodexEnabled={isCodexEnabled}
              onUpdate={onRefresh}
              onRemarkSaved={onRemarkSaved}
              showToast={showToast}
            />
          ))}
          {data.pending_invites.map((inv) => {
            const remark = inv.system_display_name?.trim() || '';
            return (
              <li key={inv.id} className="flex items-center gap-2 px-3 py-2 transition-colors hover:bg-gray-50 dark:hover:bg-ink-850">
                <div className="min-w-0 flex-1">
                  <div className="flex min-w-0 items-center gap-1">
                    <span
                      className="truncate text-sm font-medium text-gray-700 dark:text-ink-200"
                      title={remark ? `${remark} · ${inv.email}` : inv.email}
                    >
                      {remark || inv.email.split('@')[0]}
                    </span>
                    <MemberRemarkEditor
                      email={inv.email}
                      remark={remark}
                      onSaved={onRemarkSaved}
                      showToast={showToast}
                    />
                  </div>
                  <div className="flex min-w-0 items-center gap-1.5 text-xs text-gray-500 dark:text-ink-400">
                    <span className={cn(PILL, TONE.warning)}>待接受</span>
                    <span className="truncate" title={inv.email}>{inv.email}</span>
                  </div>
                </div>

                <div className="flex shrink-0 flex-col items-end gap-1">
                  <span className={cn(PILL, seatStyle(inv.seat_type).pill)}>
                    {formatSeatTypeLabel(inv.seat_type)}
                  </span>
                  {inv.expires_at && (
                    <span className="text-xs leading-5" title={`到期 ${formatAppLocalFull(new Date(inv.expires_at))}`}>
                      <ExpiryLabel iso={inv.expires_at} />
                    </span>
                  )}
                </div>

                <button
                  type="button"
                  onClick={() => setConfirmRevoke(inv.email)}
                  disabled={revoking === inv.email}
                  className="inline-flex size-9 shrink-0 items-center justify-center rounded-lg text-gray-400 transition-colors hover:bg-red-50 hover:text-red-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500/40 disabled:cursor-wait dark:text-ink-500 dark:hover:bg-red-500/10 dark:hover:text-red-400"
                  title="撤销邀请"
                  aria-label="撤销邀请"
                >
                  {revoking === inv.email ? <Loader2 size={15} className="animate-spin" /> : <X size={16} />}
                </button>
              </li>
            );
          })}
        </ul>
      )}

      <ConfirmDialog
        open={confirmRevoke !== null}
        onOpenChange={(next) => {
          if (!next && !revoking) setConfirmRevoke(null);
        }}
        title="撤销邀请"
        message={
          <>
            确定撤销发给 <span className="break-all font-medium text-gray-900 dark:text-gray-100">{confirmRevoke}</span> 的邀请吗？对方将无法再用这封邀请加入，需要时得重新邀请。
          </>
        }
        confirmLabel="撤销"
        destructive
        loading={revoking !== null && revoking === confirmRevoke}
        onConfirm={() => confirmRevoke && void handleRevoke(confirmRevoke)}
      />
    </div>
  );
}
