import { useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { AlertTriangle, Loader2 } from 'lucide-react';
import ExpiryPicker, { type ExpirySelection } from './ExpiryPicker';
import { useKickPolicy } from '../hooks/useKickPolicy';
import { useSettings } from '../hooks/useSettings';
import { selectionToDuration } from '../lib/expiry';
import { inviteMember, OverageConfirmationError } from '../api/client';
import { SEAT_TYPE_OPTIONS } from '../lib/seatType';
import type { SeatType } from '../types';
import ConfirmDialog from './ConfirmDialog';
import DialogFrame from './DialogFrame';
import { BUTTON, INPUT } from './ui';
import { cn } from '../lib/utils';

interface AddMemberDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  teamId?: string;
  /** Name of the Team the invites go to; shown under the title of the per-Team dialog. */
  teamName?: string;
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

const LABEL = 'mb-1.5 block text-sm font-medium text-gray-700 dark:text-ink-200';

// 新增成员的默认到期时间一直是 30 天，换成结构化选择项后仍然是同一个值。
const DEFAULT_EXPIRY: ExpirySelection = { kind: 'duration', value: '30d' };

function parseEmails(raw: string): string[] {
  return raw.split(/\r?\n/).map((e) => e.trim()).filter(Boolean);
}

export default function AddMemberDialog({
  open,
  onOpenChange,
  teamId,
  teamName,
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
  const { settings, save: saveSettings } = useSettings();
  const [confirmOverageOpen, setConfirmOverageOpen] = useState(false);
  const [overageMessage, setOverageMessage] = useState('');
  const [pendingInviteEmails, setPendingInviteEmails] = useState<string[]>([]);
  const [batchResult, setBatchResult] = useState<BatchInviteResultLike | null>(null);
  const [skipOverageChecked, setSkipOverageChecked] = useState(false);

  const resetForm = () => {
    setEmail('');
    setSeatType(fixedSeatType ?? 'default');
    setExpiry(DEFAULT_EXPIRY);
    setError('');
    setConfirmOverageOpen(false);
    setOverageMessage('');
    setPendingInviteEmails([]);
    setBatchResult(null);
    setSkipOverageChecked(false);
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

  const handleSubmit = async (allowOverageArg?: boolean, emailsOverride?: string[]) => {
    // 首次提交没显式传 allowOverage 时，跟随全局「不再提示」开关：开了就直接
    // 超额添加，不用先撞一次 409 再弹确认框。
    const allowOverage = allowOverageArg ?? settings.skip_overage_confirmation;
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
      <DialogFrame
        open={open}
        onOpenChange={handleDialogOpenChange}
        title={title}
        description={
          !teamId
            ? '系统自动为每个邮箱挑选有空闲 ChatGPT 席位的 Team。'
            : teamName && (
              <>
                邀请加入 <span className="font-medium text-gray-900 dark:text-gray-100">{teamName}</span>
              </>
            )
        }
        footer={
          <>
            <Dialog.Close asChild>
              <button type="button" onClick={resetForm} className={BUTTON.secondary}>
                {batchResult ? '完成' : '取消'}
              </button>
            </Dialog.Close>
            <button
              type="button"
              onClick={() => { void handleSubmit(); }}
              disabled={loading}
              className={BUTTON.primary}
            >
              {loading && <Loader2 size={14} className="animate-spin" />}
              {loading ? '添加中…' : batchResult ? '重新提交' : submitLabel}
            </button>
          </>
        }
      >
        <div className="space-y-5">
          <div>
            <label htmlFor="add-member-emails" className={LABEL}>
              邮箱 <span className="font-normal text-gray-400 dark:text-ink-500">每行一个</span>
            </label>
            <textarea
              id="add-member-emails"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder={'user@example.com\nanother@example.com'}
              rows={4}
              className={cn(INPUT, 'min-h-24 resize-y')}
            />
          </div>

          {!fixedSeatType && (
            <div>
              <span className={LABEL}>席位类型</span>
              <div className="grid grid-cols-2 gap-1 rounded-lg bg-gray-100 p-1 dark:bg-ink-950" role="group" aria-label="席位类型">
                {SEAT_TYPE_OPTIONS.map(({ value, label }) => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => setSeatType(value)}
                    aria-pressed={seatType === value}
                    className={cn(
                      'h-8 whitespace-nowrap rounded-md text-sm font-medium transition-colors',
                      seatType === value
                        ? cn(
                          'bg-white shadow-sm dark:bg-ink-800',
                          value === 'usage_based' ? 'text-purple-700 dark:text-purple-300' : 'text-blue-700 dark:text-blue-300',
                        )
                        : 'text-gray-500 hover:text-gray-900 dark:text-ink-400 dark:hover:text-gray-100',
                    )}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
          )}

          <div>
            <span className={LABEL}>到期时间</span>
            <ExpiryPicker value={expiry} onChange={setExpiry} policy={kickPolicy} disabled={loading} />
          </div>

          {error && (
            <p className="text-sm text-red-600 dark:text-red-400">{error}</p>
          )}

          {batchResult?.failed && batchResult.failed.length > 0 && (
            <div className="space-y-2 rounded-lg border border-red-200 bg-red-50 p-3 text-sm dark:border-red-500/30 dark:bg-red-500/10">
              <div className="flex items-center gap-2 font-medium text-red-700 dark:text-red-300">
                <AlertTriangle size={15} className="shrink-0" />
                {(batchResult.added?.length ?? 0) > 0
                  ? `${batchResult.added?.length} 个成功，${batchResult.failed.length} 个失败`
                  : `全部 ${batchResult.failed.length} 个添加失败`}
              </div>
              <div className="max-h-36 space-y-1.5 overflow-y-auto pr-1">
                {batchResult.failed.map((f, i) => (
                  <div
                    key={`${f.email}-${i}`}
                    className="rounded-md border border-red-100 bg-white px-2 py-1.5 dark:border-red-500/20 dark:bg-ink-900"
                  >
                    <div className="break-all text-xs font-medium text-gray-800 dark:text-gray-200">{f.email}</div>
                    <div className="mt-0.5 text-xs text-red-600 dark:text-red-400">{f.error}</div>
                  </div>
                ))}
              </div>
              <button
                type="button"
                onClick={handleRetryFailed}
                className="text-xs font-medium text-red-700 underline decoration-dotted underline-offset-2 hover:text-red-800 dark:text-red-300 dark:hover:text-red-200"
              >
                仅重试失败邮箱
              </button>
            </div>
          )}
        </div>
      </DialogFrame>

      <ConfirmDialog
        open={confirmOverageOpen}
        onOpenChange={setConfirmOverageOpen}
        title="确认超额添加 ChatGPT 席位"
        message={overageMessage}
        confirmLabel="仍然添加"
        destructive
        loading={loading}
        onConfirm={() => {
          if (skipOverageChecked) {
            // 存设置失败也不拦邀请，只是别声称保存成功了；下次照样弹确认框。
            void saveSettings({ skip_overage_confirmation: true }).catch(() => {});
          }
          const emails = pendingInviteEmails.length > 0 ? pendingInviteEmails : undefined;
          void handleSubmit(true, emails);
        }}
      >
        <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-ink-200">
          <input
            type="checkbox"
            checked={skipOverageChecked}
            onChange={(e) => setSkipOverageChecked(e.target.checked)}
            className="size-4 accent-blue-600 dark:accent-blue-500"
          />
          不再提示，以后超额直接添加
        </label>
      </ConfirmDialog>
    </>
  );
}
