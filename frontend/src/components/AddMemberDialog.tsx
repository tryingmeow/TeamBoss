import { useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { AlertTriangle, X } from 'lucide-react';
import ExpiryPicker, { type ExpirySelection } from './ExpiryPicker';
import { useKickPolicy } from '../hooks/useKickPolicy';
import { selectionToDuration } from '../lib/expiry';
import { inviteMember, OverageConfirmationError } from '../api/client';
import { SEAT_TYPE_OPTIONS } from '../lib/seatType';
import type { SeatType } from '../types';
import LoadingSpinner from './LoadingSpinner';
import ConfirmDialog from './ConfirmDialog';

interface AddMemberDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  teamId?: string;
  title?: string;
  fixedSeatType?: SeatType;
  submitLabel?: string;
  onSuccess: (result?: unknown) => void;
  submitInvites?: (data: {
    emails: string[];
    seat_type: SeatType;
    expires_in?: string;
    allow_overage?: boolean;
  }) => Promise<unknown>;
}

interface BatchInviteFailure {
  email: string;
  error: string;
}

// Shape returned by batch invite endpoints (e.g. inviteGptMembers) — HTTP 200
// even when some emails failed, with per-email reasons under `failed`.
interface BatchInviteResultLike {
  added?: Array<{ email: string }>;
  failed?: BatchInviteFailure[];
}

function asBatchResult(result: unknown): BatchInviteResultLike | null {
  if (!result || typeof result !== 'object') return null;
  const candidate = result as BatchInviteResultLike;
  if (!Array.isArray(candidate.failed)) return null;
  return candidate;
}

// 新增成员的默认到期时间一直是 30 天，换成结构化选择项后仍然是同一个值。
const DEFAULT_EXPIRY: ExpirySelection = { kind: 'duration', value: '30d' };

function parseEmails(raw: string): string[] {
  return raw.split(/\r?\n/).map((e) => e.trim()).filter(Boolean);
}

export default function AddMemberDialog({
  open,
  onOpenChange,
  teamId,
  title = '添加成员',
  fixedSeatType,
  submitLabel = '确认添加',
  onSuccess,
  submitInvites,
}: AddMemberDialogProps) {
  const [email, setEmail] = useState('');
  const [seatType, setSeatType] = useState<SeatType>('default');
  const [expiry, setExpiry] = useState<ExpirySelection>(DEFAULT_EXPIRY);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const kickPolicy = useKickPolicy();
  const [confirmOverageOpen, setConfirmOverageOpen] = useState(false);
  const [overageMessage, setOverageMessage] = useState('');
  const [pendingInviteEmails, setPendingInviteEmails] = useState<string[]>([]);
  const [batchResult, setBatchResult] = useState<BatchInviteResultLike | null>(null);

  const resetForm = () => {
    setEmail('');
    setSeatType(fixedSeatType ?? 'default');
    setExpiry(DEFAULT_EXPIRY);
    setError('');
    setConfirmOverageOpen(false);
    setOverageMessage('');
    setPendingInviteEmails([]);
    setBatchResult(null);
  };

  // Esc / 点击遮罩关闭时 Radix 只会调用这里的 onOpenChange,不会走下面按钮上
  // 显式绑定的 resetForm——所有关闭路径统一在这里清空表单,而不仅是 Cancel/X。
  const handleDialogOpenChange = (nextOpen: boolean) => {
    if (!nextOpen) resetForm();
    onOpenChange(nextOpen);
  };

  const handleRetryFailed = () => {
    if (!batchResult?.failed?.length) return;
    setEmail(batchResult.failed.map((f) => f.email).join('\n'));
    setBatchResult(null);
    setError('');
  };

  const handleSubmit = async (allowOverage = false, emailsOverride?: string[]) => {
    if (!emailsOverride && !email.trim()) {
      setError('请输入邮箱地址');
      return;
    }
    const emails = emailsOverride ?? parseEmails(email);
    if (emails.length === 0) {
      setError('请输入邮箱地址');
      return;
    }
    setError('');
    setBatchResult(null);
    setLoading(true);
    try {
      const effectiveSeatType = fixedSeatType ?? seatType;
      // 邀请接口只收 expires_in（durations 语法），所以日历选出来的绝对时刻
      // 在这里折算成"从现在起 N 分钟"。请求体形状不变。
      const expiresIn = selectionToDuration(expiry);

      if (submitInvites) {
        const result = await submitInvites({
          emails,
          seat_type: effectiveSeatType,
          expires_in: expiresIn,
          allow_overage: allowOverage,
        });
        onSuccess(result);
        const batch = asBatchResult(result);
        if (batch && batch.failed && batch.failed.length > 0) {
          // 部分或全部失败：弹窗留在原地展示每个失败邮箱的原因，不假装成功关掉。
          setBatchResult(batch);
          return;
        }
        onOpenChange(false);
        resetForm();
        return;
      }

      if (!teamId) {
        throw new Error('Team ID 缺失');
      }

      for (let i = 0; i < emails.length; i++) {
        try {
          await inviteMember(teamId, {
            email: emails[i],
            seat_type: effectiveSeatType,
            expires_in: expiresIn,
            allow_overage: allowOverage,
          });
        } catch (err) {
          if (err instanceof OverageConfirmationError && !allowOverage) {
            const remainingEmails = err.remainingEmails.length > 0 ? err.remainingEmails : emails.slice(i);
            setPendingInviteEmails(remainingEmails);
            setOverageMessage(
              `ChatGPT 席位不足（可用 ${err.capacity.available}，` +
              `已用 ${err.capacity.active_chatgpt}/${err.capacity.seats_entitled}），` +
              `确认后将超额添加，可能产生额外费用。`,
            );
            setConfirmOverageOpen(true);
            return;
          }
          throw err;
        }
      }
      onSuccess();
      onOpenChange(false);
      resetForm();
    } catch (err) {
      if (err instanceof OverageConfirmationError && !allowOverage) {
        const remainingEmails = err.remainingEmails.length > 0 ? err.remainingEmails : emails;
        setPendingInviteEmails(remainingEmails);
        setOverageMessage(err.message || '空闲 GPT 席位不足，继续可能产生额外计费。');
        setConfirmOverageOpen(true);
        return;
      }
      setError(err instanceof Error ? err.message : '添加失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <Dialog.Root open={open} onOpenChange={handleDialogOpenChange}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-black/60 z-50" />
          <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-full max-w-md rounded-xl bg-white dark:bg-[#1a1d27] border border-gray-200 dark:border-[#2a2d3a] p-6 shadow-2xl">
          <Dialog.Title className="text-lg font-bold text-gray-900 dark:text-gray-100">
            {title}
          </Dialog.Title>

          <div className="mt-4 space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-400 mb-1.5">邮箱（每行一个）</label>
              <textarea
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder={'user@example.com\nanother@example.com'}
                rows={4}
                className="w-full px-3 py-2 bg-gray-50 dark:bg-[#0f1117] border border-gray-200 dark:border-[#2a2d3a] rounded-lg text-sm text-gray-900 dark:text-gray-200 placeholder:text-gray-400 dark:placeholder:text-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500 transition-all resize-y min-h-[6rem]"
              />
            </div>

            {!fixedSeatType && (
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-400 mb-2">席位类型</label>
                <div className="flex gap-3">
                  {SEAT_TYPE_OPTIONS.map(({ value, label }) => (
                    <button
                      key={value}
                      type="button"
                      onClick={() => setSeatType(value)}
                      className={`flex-1 py-2 rounded-lg text-sm font-medium transition-all ${
                        seatType === value
                          ? value === 'usage_based'
                            ? 'bg-purple-600 text-white shadow-md shadow-purple-500/20'
                            : 'bg-blue-600 text-white shadow-md shadow-blue-500/20'
                          : 'bg-gray-100 dark:bg-[#2a2d3a] text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-[#3a3d4a]'
                      }`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>
            )}

            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-400 mb-2">过期时间</label>
              <ExpiryPicker value={expiry} onChange={setExpiry} policy={kickPolicy} disabled={loading} />
            </div>

            {error && (
              <p className="text-sm text-red-500 dark:text-red-400">{error}</p>
            )}

            {batchResult?.failed && batchResult.failed.length > 0 && (
              <div className="rounded-lg border border-red-200 dark:border-red-800/50 bg-red-50 dark:bg-red-950/30 p-3 text-sm space-y-2">
                <div className="flex items-center gap-2 font-semibold text-red-700 dark:text-red-300">
                  <AlertTriangle size={15} className="shrink-0" />
                  {(batchResult.added?.length ?? 0) > 0
                    ? `${batchResult.added?.length} 个成功，${batchResult.failed.length} 个失败`
                    : `全部 ${batchResult.failed.length} 个添加失败`}
                </div>
                <div className="max-h-36 overflow-y-auto space-y-1.5 pr-1">
                  {batchResult.failed.map((f, i) => (
                    <div
                      key={`${f.email}-${i}`}
                      className="rounded-md bg-white/70 dark:bg-black/20 px-2 py-1.5 border border-red-100 dark:border-red-900/40"
                    >
                      <div className="text-xs font-medium text-gray-800 dark:text-gray-200 break-all">{f.email}</div>
                      <div className="text-xs text-red-600 dark:text-red-400 mt-0.5">{f.error}</div>
                    </div>
                  ))}
                </div>
                <button
                  type="button"
                  onClick={handleRetryFailed}
                  className="text-xs font-medium text-red-700 dark:text-red-300 underline decoration-dotted underline-offset-2 hover:text-red-800 dark:hover:text-red-200"
                >
                  仅重试失败邮箱
                </button>
              </div>
            )}
          </div>

          <div className="mt-6 flex justify-end gap-3">
            <Dialog.Close asChild>
              <button
                onClick={resetForm}
                className="px-4 py-2 rounded-lg text-sm font-medium text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-[#2a2d3a] hover:bg-gray-200 dark:hover:bg-[#3a3d4a] transition-colors"
              >
                {batchResult ? '完成' : '取消'}
              </button>
            </Dialog.Close>
            <button
              onClick={() => { void handleSubmit(); }}
              disabled={loading}
              className="px-4 py-2 rounded-lg text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 shadow-md shadow-blue-500/20 transition-all disabled:opacity-50 flex items-center gap-2"
            >
              {loading && <LoadingSpinner size={14} />}
              {loading ? '添加中...' : batchResult ? '重新提交' : submitLabel}
            </button>
          </div>

          <Dialog.Close asChild>
            <button
              onClick={resetForm}
              className="absolute top-4 right-4 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 transition-colors p-1 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800"
            >
              <X size={16} />
            </button>
          </Dialog.Close>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      <ConfirmDialog
        open={confirmOverageOpen}
        onOpenChange={setConfirmOverageOpen}
        title="确认超额添加 ChatGPT 席位"
        message={overageMessage}
        confirmLabel="仍然添加"
        destructive
        loading={loading}
        onConfirm={() => {
          const emails = pendingInviteEmails.length > 0 ? pendingInviteEmails : undefined;
          void handleSubmit(true, emails);
        }}
      />
    </>
  );
}
