import { useMemo, useState, type ReactNode } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { AlertTriangle, Ban, CreditCard, Loader2 } from 'lucide-react';
import ExpiryPicker, { type ExpirySelection } from './ExpiryPicker';
import { useKickPolicy } from '../hooks/useKickPolicy';
import { selectionToDuration } from '../lib/expiry';
import { inviteMember, OverageConfirmationError, type OveragePlanItem } from '../api/client';
import { SEAT_STYLE, SEAT_TYPES, SEAT_TYPE_OPTIONS, parseSeatType } from '../lib/seatType';
import { gateMessage, pendingCountsByType, seatGate, type TeamCapacityFields } from '../lib/seatCapacity';
import type { PendingInvite, SeatType } from '../types';
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
  /** Cached seats and overage policy of that Team: decide before submitting whether this costs money. */
  team?: TeamCapacityFields | null;
  /** That Team's pending invites (they hold seats too). */
  pendingInvites?: PendingInvite[] | null;
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
  no_place_emails?: string[];
}

/** What the confirm step is about to buy, and what to resend once the admin agrees. */
interface OverageAsk {
  seatType: SeatType;
  message: ReactNode;
  emails: string[];
  /** Batch: invites the server already made before asking (kept for the final result). */
  carry?: { added: Array<{ email: string }>; failed: BatchInviteFailure[] };
}

const NO_PLACE = '没位置，未邀请';

function asBatchResult(result: unknown): BatchInviteResultLike | null {
  if (!result || typeof result !== 'object') return null;
  const candidate = result as BatchInviteResultLike;
  if (!Array.isArray(candidate.failed)) return null;
  return candidate;
}

function asFailures(list: unknown[]): BatchInviteFailure[] {
  return list.filter((item): item is BatchInviteFailure =>
    Boolean(item) && typeof (item as BatchInviteFailure).email === 'string');
}

const LABEL = 'mb-1.5 block text-sm font-medium text-gray-700 dark:text-ink-200';

// 新增成员的默认到期时间一直是 30 天，换成结构化选择项后仍然是同一个值。
const DEFAULT_EXPIRY: ExpirySelection = { kind: 'duration', value: '30d' };

function parseEmails(raw: string): string[] {
  return raw.split(/\r?\n/).map((e) => e.trim()).filter(Boolean);
}

function BatchPlanMessage({ plan, total, remaining, added, addedOverage }: {
  plan: OveragePlanItem[];
  total: number;
  remaining: number;
  added: number;
  /** Of `added`, how many went onto 超员自动 Teams (a seat was already bought for each). */
  addedOverage: number;
}) {
  const leftover = Math.max(0, remaining - total);
  return (
    <div className="space-y-2">
      <p>
        {added > 0 && (
          <>
            {added} 个已加入
            {addedOverage > 0 && <>（其中 {addedOverage} 个在「超员自动」的 Team，已加购席位）</>}。
          </>
        )}
        {remaining} 个邮箱没有空位，继续会让 ChatGPT 在这些 Team 自动加购 ChatGPT 席位并扣费：
      </p>
      <ul className="divide-y divide-gray-100 rounded-lg border border-gray-200 text-gray-700 dark:divide-ink-800 dark:border-ink-800 dark:text-ink-200">
        {plan.map((item) => (
          <li key={item.team_id || item.team_name} className="flex items-center justify-between gap-3 px-3 py-1.5">
            <span className="min-w-0 truncate" title={item.team_name}>{item.team_name}</span>
            <span className="shrink-0 font-medium tabular-nums">+{item.extra_seats} 席</span>
          </li>
        ))}
      </ul>
      <p className="font-medium text-gray-900 dark:text-gray-100">共加购 {total} 个 ChatGPT 席位。</p>
      {leftover > 0 && <p>其余 {leftover} 个邮箱没位置，不会邀请。</p>}
    </div>
  );
}

export default function AddMemberDialog({
  open,
  onOpenChange,
  teamId,
  teamName,
  team,
  pendingInvites,
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
  const [ask, setAsk] = useState<OverageAsk | null>(null);
  const [batchResult, setBatchResult] = useState<BatchInviteResultLike | null>(null);

  const effectiveSeatType = fixedSeatType ?? seatType;
  const emails = parseEmails(email);
  const pendingByType = useMemo(() => pendingCountsByType(pendingInvites), [pendingInvites]);
  // 单个 Team 才能事先判断：缓存的空位 + 这个 Team 的超员策略。服务端还会用实时数据再判一次。
  const gate = teamId && team ? seatGate(team, effectiveSeatType, pendingByType, emails.length) : null;
  const gateText = gate ? gateMessage(gate) : null;
  const blocked = gate?.action === 'forbid';

  const resetForm = () => {
    setEmail('');
    setSeatType(fixedSeatType ?? 'default');
    setExpiry(DEFAULT_EXPIRY);
    setError('');
    setAsk(null);
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

  const closeAll = () => {
    onOpenChange(false);
    resetForm();
  };

  /** Batch (no teamId): the server picks the Teams and asks once before overfilling any. */
  const submitBatch = async (list: string[], allowOverage: boolean, carry?: OverageAsk['carry']) => {
    if (!submitInvites) return;
    try {
      const result = await submitInvites({
        emails: list,
        seat_type: effectiveSeatType,
        expires_in: selectionToDuration(expiry),
        allow_overage: allowOverage,
      });
      const batch = asBatchResult(result);
      const merged: BatchInviteResultLike | null = carry
        ? {
          ...(batch ?? {}),
          added: [...carry.added, ...(batch?.added ?? [])],
          failed: [...carry.failed, ...(batch?.failed ?? [])],
        }
        : batch;
      setAsk(null);
      onSuccess(merged ?? result);
      if (merged?.failed && merged.failed.length > 0) {
        // 部分或全部失败：弹窗留在原地展示每个失败邮箱的原因，不假装成功关掉。
        setBatchResult(merged);
        return;
      }
      closeAll();
    } catch (err) {
      if (err instanceof OverageConfirmationError && !allowOverage) {
        const remaining = err.remainingEmails.length > 0 ? err.remainingEmails : list;
        const added = (err.added as Array<{ email: string; overage?: boolean }>).filter((item) => item && typeof item.email === 'string');
        setAsk({
          seatType: 'default',
          emails: remaining,
          carry: { added, failed: asFailures(err.failed) },
          message: err.overagePlan.length > 0
            ? (
              <BatchPlanMessage
                plan={err.overagePlan}
                total={err.extraSeatsTotal}
                remaining={remaining.length}
                added={added.length}
                addedOverage={added.filter((item) => item.overage).length}
              />
            )
            : err.message,
        });
        return;
      }
      setAsk(null);
      setError(err instanceof Error ? err.message : '添加失败');
    }
  };

  /** One Team: invite one by one; the server re-checks every billed invite against live seats. */
  const submitToTeam = async (list: string[], allowOverage: boolean) => {
    if (!teamId) {
      setError('Team ID 缺失');
      return;
    }
    const expiresIn = selectionToDuration(expiry);
    let done = 0;
    try {
      for (const address of list) {
        await inviteMember(teamId, {
          email: address,
          seat_type: effectiveSeatType,
          expires_in: expiresIn,
          allow_overage: allowOverage,
        });
        done += 1;
      }
      setAsk(null);
      onSuccess();
      closeAll();
    } catch (err) {
      const rest = list.slice(done);
      // 已经发出去的邀请是真的：先让卡片刷新，表单里只留下没加上的邮箱。
      if (done > 0) onSuccess();
      setEmail(rest.join('\n'));
      if (err instanceof OverageConfirmationError && !allowOverage) {
        setAsk({
          seatType: parseSeatType(err.seatType) ?? effectiveSeatType,
          emails: err.remainingEmails.length > 0 ? err.remainingEmails : rest,
          message: done > 0 ? `已添加 ${done} 个。${err.message}` : err.message,
        });
        return;
      }
      setAsk(null);
      const message = err instanceof Error ? err.message : '添加失败';
      setError(done > 0 ? `已添加 ${done} 个，其余未添加：${message}` : message);
    }
  };

  const handleSubmit = async () => {
    if (emails.length === 0) {
      setError('请输入邮箱地址');
      return;
    }
    if (blocked) return;
    setError('');
    setBatchResult(null);
    if (gate?.action === 'confirm') {
      // 缓存显示已满且这个 Team 要先问：先确认再发，确认后才带上 allow_overage。
      setAsk({ seatType: effectiveSeatType, emails, message: gateText ?? '' });
      return;
    }
    setLoading(true);
    try {
      if (submitInvites) await submitBatch(emails, false);
      else await submitToTeam(emails, false);
    } finally {
      setLoading(false);
    }
  };

  const handleConfirmOverage = async () => {
    if (!ask) return;
    setLoading(true);
    try {
      if (submitInvites) await submitBatch(ask.emails, true, ask.carry);
      else await submitToTeam(ask.emails, true);
    } finally {
      setLoading(false);
    }
  };

  const handleAskOpenChange = (nextOpen: boolean) => {
    if (nextOpen || loading) return;
    // 批量模式下服务端在问之前已经用空位加了一些人：取消加购也要把这部分如实报出来。
    if (ask?.carry && (ask.carry.added.length > 0 || ask.carry.failed.length > 0)) {
      const result: BatchInviteResultLike = {
        added: ask.carry.added,
        failed: [
          ...ask.carry.failed,
          ...ask.emails.map((address) => ({ email: address, error: '没确认加购，未邀请' })),
        ],
      };
      onSuccess(result);
      setBatchResult(result);
    }
    setAsk(null);
  };

  const noPlace = new Set(
    batchResult?.no_place_emails ?? (batchResult?.failed ?? []).filter((f) => f.error === NO_PLACE).map((f) => f.email),
  );
  const noPlaceList = (batchResult?.failed ?? []).filter((f) => noPlace.has(f.email));
  const otherFailed = (batchResult?.failed ?? []).filter((f) => !noPlace.has(f.email));
  const addedCount = batchResult?.added?.length ?? 0;

  const submitText = loading
    ? '添加中…'
    : batchResult
      ? '重新提交'
      : gate?.action === 'auto'
        ? `添加并加购 ${gate.extra} 席`
        : submitLabel;

  return (
    <>
      <DialogFrame
        open={open}
        onOpenChange={handleDialogOpenChange}
        title={title}
        description={
          !teamId
            ? '先用有空闲 ChatGPT 席位的 Team。空位不够时按各 Team 的超员策略：「超员自动」直接加购，「超员需确认」先问你，「禁止超员」不加。'
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
              disabled={loading || blocked}
              title={blocked ? gateText ?? undefined : undefined}
              className={BUTTON.primary}
            >
              {loading && <Loader2 size={14} className="animate-spin" />}
              {submitText}
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
              <div className="grid grid-cols-3 gap-1 rounded-lg bg-gray-100 p-1 dark:bg-ink-950" role="group" aria-label="席位类型">
                {SEAT_TYPE_OPTIONS.map(({ value, label }) => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => setSeatType(value)}
                    aria-pressed={seatType === value}
                    className={cn(
                      'inline-flex h-8 min-w-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-md px-1 text-sm font-medium transition-colors',
                      seatType === value
                        ? cn('shadow-sm', SEAT_STYLE[value].pill)
                        : 'text-gray-500 hover:text-gray-900 dark:text-ink-400 dark:hover:text-gray-100',
                    )}
                  >
                    <span className={cn('size-2 shrink-0 rounded-full', SEAT_STYLE[value].solid)} aria-hidden />
                    {label}
                  </button>
                ))}
              </div>
              {gate && !gateText && (
                <p className="mt-1.5 text-xs text-gray-500 dark:text-ink-400">
                  {gate.billed
                    ? `还有 ${gate.free} 个 ${gate.label} 空位，不会加购。`
                    : `${SEAT_TYPES[effectiveSeatType].label} 按用量计费，不占付费席位。`}
                </p>
              )}
            </div>
          )}

          {gateText && (
            <div
              role={blocked ? 'alert' : 'status'}
              className={cn(
                'flex items-start gap-2 rounded-lg border px-3 py-2.5 text-sm leading-6',
                blocked
                  ? 'border-gray-200 bg-gray-50 text-gray-700 dark:border-ink-800 dark:bg-ink-950/60 dark:text-ink-200'
                  : 'border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200',
              )}
            >
              {blocked
                ? <Ban size={15} className="mt-1 shrink-0 text-gray-500 dark:text-ink-400" />
                : <CreditCard size={15} className="mt-1 shrink-0 text-amber-600 dark:text-amber-400" />}
              <span>
                {gateText}
                {gate?.action === 'confirm' && ' 提交前会再问你一次。'}
              </span>
            </div>
          )}

          <div>
            <span className={LABEL}>到期时间</span>
            <ExpiryPicker value={expiry} onChange={setExpiry} policy={kickPolicy} disabled={loading} />
          </div>

          {error && (
            <p role="alert" className="text-sm text-red-600 dark:text-red-400">{error}</p>
          )}

          {batchResult?.failed && batchResult.failed.length > 0 && (
            <div className="space-y-3">
              <div className="flex items-center gap-2 text-sm font-medium text-gray-900 dark:text-gray-100">
                <AlertTriangle size={15} className="shrink-0 text-amber-500" />
                {[
                  addedCount > 0 ? `${addedCount} 个已添加` : '',
                  noPlaceList.length > 0 ? `${noPlaceList.length} 个没位置` : '',
                  otherFailed.length > 0 ? `${otherFailed.length} 个失败` : '',
                ].filter(Boolean).join('，')}
              </div>

              {noPlaceList.length > 0 && (
                <div className="rounded-lg border border-gray-200 bg-gray-50 p-3 text-sm dark:border-ink-800 dark:bg-ink-950/60">
                  <div className="font-medium text-gray-800 dark:text-gray-200">{NO_PLACE}</div>
                  <p className="mt-0.5 text-xs text-gray-500 dark:text-ink-400">能用的 Team 都满了，又不允许再超员，这些邮箱没有发邀请，也没有加购。</p>
                  <div className="mt-2 max-h-28 space-y-1 overflow-y-auto pr-1">
                    {noPlaceList.map((f, i) => (
                      <div key={`${f.email}-${i}`} className="break-all text-xs text-gray-700 dark:text-ink-200">{f.email}</div>
                    ))}
                  </div>
                </div>
              )}

              {otherFailed.length > 0 && (
                <div className="space-y-2 rounded-lg border border-red-200 bg-red-50 p-3 text-sm dark:border-red-500/30 dark:bg-red-500/10">
                  <div className="max-h-36 space-y-1.5 overflow-y-auto pr-1">
                    {otherFailed.map((f, i) => (
                      <div
                        key={`${f.email}-${i}`}
                        className="rounded-md border border-red-100 bg-white px-2 py-1.5 dark:border-red-500/20 dark:bg-ink-900"
                      >
                        <div className="break-all text-xs font-medium text-gray-800 dark:text-gray-200">{f.email}</div>
                        <div className="mt-0.5 text-xs text-red-600 dark:text-red-400">{f.error}</div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <button
                type="button"
                onClick={handleRetryFailed}
                className="text-xs font-medium text-blue-700 underline decoration-dotted underline-offset-2 hover:text-blue-800 dark:text-blue-300 dark:hover:text-blue-200"
              >
                把没加上的邮箱放回输入框
              </button>
            </div>
          )}
        </div>
      </DialogFrame>

      <ConfirmDialog
        open={ask !== null}
        onOpenChange={handleAskOpenChange}
        title={`确认加购 ${SEAT_TYPES[ask?.seatType ?? 'default'].label} 席位`}
        message={ask?.message ?? ''}
        confirmLabel="加购并添加"
        destructive
        loading={loading}
        onConfirm={() => { void handleConfirmOverage(); }}
      >
        <p className="text-xs text-gray-500 dark:text-ink-400">超员策略可在 Team 设置里修改。</p>
      </ConfirmDialog>
    </>
  );
}
