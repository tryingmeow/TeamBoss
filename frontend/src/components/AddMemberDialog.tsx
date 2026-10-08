import { useEffect, useMemo, useState, type ReactNode } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { AlertTriangle, Ban, CreditCard, Loader2 } from 'lucide-react';
import ExpiryPicker, { type ExpirySelection } from './ExpiryPicker';
import { useKickPolicy } from '../hooks/useKickPolicy';
import { selectionToDuration } from '../lib/expiry';
import {
  fetchResourceUsage,
  inviteMember,
  OverageConfirmationError,
  type OverageConfirmation,
  type OveragePlanItem,
} from '../api/client';
import { MAX_CONFIRMED_SEATS, newOverageConfirmation } from '../lib/overageConfirmation';
import { SEAT_STYLE, SEAT_TYPES, SEAT_TYPE_OPTIONS, parseOveragePolicy } from '../lib/seatType';
import {
  cachedFreeSeats,
  gateConfirmText,
  gateMessage,
  liveFullConfirmText,
  overagePurchaseText,
  pendingCountsByType,
  seatGate,
  teamPendingCounts,
  type TeamCapacityFields,
} from '../lib/seatCapacity';
import {
  SEAT_PRICE_UNKNOWN_TEXT,
  YEARLY_PURCHASE_NOTE,
  PURCHASE_NOTE,
  formatSeatAmountShort,
  formatSeatCostTotals,
  formatSeatPrice,
  groupSeatCosts,
  seatChargeText,
  teamSeatPrice,
  type SeatCostTotal,
} from '../lib/seatPrice';
import { sameCurrency } from '../lib/money';
import type { PendingInvite, SeatType, Team } from '../types';
import SeatPurchaseConfirmDialog, { type SeatPurchaseRequest } from './SeatPurchaseConfirmDialog';
import SeatProductionWarning from './SeatProductionWarning';
import DialogFrame from './DialogFrame';
import SeatBetaBadge from './BetaBadge';
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
  /** Batch mode (no teamId): every Team, to say before submitting what would be bought where. */
  teams?: Team[];
  title?: string;
  fixedSeatType?: SeatType;
  submitLabel?: string;
  /** `meta.shownInDialog`: the dialog stays open and already shows this result (no toast needed). */
  onSuccess: (result?: unknown, meta?: { shownInDialog?: boolean }) => void;
  submitInvites?: (data: {
    emails: string[];
    seat_type: SeatType;
    expires_in?: string;
    allow_overage?: boolean;
    /** The Teams of the overage plan the admin confirmed; the server overfills no other confirm Team. */
    overage_team_ids?: string[];
    /** How many seats that plan buys; the server buys no more on confirm Teams and asks again for the rest. */
    overage_seat_limit?: number;
  }) => Promise<unknown>;
}

interface BatchInviteFailure {
  email: string;
  error: string;
}

interface BatchInviteAdded {
  email: string;
  team_name?: string;
  /** No free seat was left: ChatGPT added and charged one for this invite. */
  overage?: boolean;
}

/** What a batch run did, merged across confirm rounds. Also handed to onSuccess. */
export interface BatchInviteOutcome {
  added: BatchInviteAdded[];
  failed: BatchInviteFailure[];
  no_place_emails: string[];
  /** The admin declined the purchase: not invited, and not a failure. */
  declined_emails: string[];
}

/** What the confirm step is about to buy, and what to resend once the admin agrees. */
interface OverageAsk {
  seatType: SeatType;
  message: ReactNode;
  emails: string[];
  /**
   * One Team: the seats the message says will be bought. The confirmation sent back covers exactly
   * this many (server-enforced); beyond it the dialog asks again.
   */
  purchaseCount?: number;
  quotePlan?: OveragePlanItem[];
  /**
   * Batch: the plan the admin is shown (its Teams and seat count go back as overage_team_ids /
   * overage_seat_limit) and what the server already did before asking.
   */
  batch?: { planTeamIds: string[]; seatLimit: number; added: BatchInviteAdded[]; failed: BatchInviteFailure[] };
}

const NO_PLACE = '席位不足，未邀请';
const CAP_NOTE = `单次最多确认 ${MAX_CONFIRMED_SEATS} 席，超出部分将分批确认。`;
const REPLAN_NOTE = '超员计划已变更，需重新确认。';

function asAdded(list: unknown): BatchInviteAdded[] {
  if (!Array.isArray(list)) return [];
  return list.filter((item): item is BatchInviteAdded =>
    Boolean(item) && typeof (item as BatchInviteAdded).email === 'string');
}

function asFailures(list: unknown): BatchInviteFailure[] {
  if (!Array.isArray(list)) return [];
  return list.filter((item): item is BatchInviteFailure =>
    Boolean(item) && typeof (item as BatchInviteFailure).email === 'string');
}

const LABEL = 'mb-1.5 block text-sm font-medium text-gray-700 dark:text-ink-200';
const NOTE_WARN = 'border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200';
const NOTE_PLAIN = 'border-gray-200 bg-gray-50 text-gray-700 dark:border-ink-800 dark:bg-ink-950/60 dark:text-ink-200';

// 新增成员的默认到期时间一直是 30 天，换成结构化选择项后仍然是同一个值。
const DEFAULT_EXPIRY: ExpirySelection = { kind: 'duration', value: '30d' };

function parseEmails(raw: string): string[] {
  return raw.split(/\r?\n/).map((e) => e.trim()).filter(Boolean);
}

/** 「A」、「B」 */
function quoteNames(names: string[]): string {
  return names.map((name) => `「${name}」`).join('、');
}

/**
 * What the auto Teams' overflow adds per month, before submitting. The split across Teams is not
 * known in advance, so a total only when every one of them has the same known price and period;
 * otherwise each Team's per-seat price (never a made-up total, never a sum across currencies or
 * periods), with the yearly note once when any of them is yearly.
 */
function autoOverflowCost(autoTeams: Team[], extra: number): string {
  const priced = autoTeams.map((item) => ({ name: item.name, price: teamSeatPrice(item, 'default') }));
  const first = priced[0]?.price ?? null;
  const uniform = first !== null && priced.every(({ price }) =>
    price !== null
    && price.amount === first.amount
    && price.period === first.period
    && sameCurrency(price.currency, first.currency));
  if (uniform) return seatChargeText(first, extra);
  const known = priced.flatMap(({ name, price }) => (price ? [`「${name}」${formatSeatPrice(price)}`] : []));
  if (known.length === 0) return SEAT_PRICE_UNKNOWN_TEXT;
  const yearly = `，${priced.some(({ price }) => price?.period === 'yearly') ? YEARLY_PURCHASE_NOTE : PURCHASE_NOTE}`;
  const unknown = priced.filter(({ price }) => !price).map(({ name }) => name);
  return `每席：${known.join('、')}${yearly}${unknown.length > 0 ? `；${quoteNames(unknown)}${SEAT_PRICE_UNKNOWN_TEXT}` : ''}`;
}

/**
 * "约 +฿2,340 + 税/月" per currency and period (yearly ones with the annual figure and the yearly
 * note, once); seats without a known price are named as such, never priced.
 */
function planCostText(plan: OveragePlanItem[], serverTotals: SeatCostTotal[]): string {
  const grouped = groupSeatCosts(plan.map((item) => ({ price: item.seat_price, seats: item.extra_seats })));
  const totals = serverTotals.length > 0 ? serverTotals : grouped.totals;
  if (totals.length === 0) return SEAT_PRICE_UNKNOWN_TEXT;
  const known = formatSeatCostTotals(totals);
  return grouped.unknownSeats > 0
    ? `${known}。另有 ${grouped.unknownSeats} 席${SEAT_PRICE_UNKNOWN_TEXT}`
    : known;
}

/**
 * The batch confirm step. The counts come from the server's own lead sentence (it knows how
 * many free seats the emails will use first), then the plan: where seats get bought, how many,
 * and what they add per month (per currency).
 */
function BatchPlanMessage({ serverMessage, plan, total, costTotals, invitedOverage, replan }: {
  serverMessage: string;
  plan: OveragePlanItem[];
  total: number;
  /** The server's per-currency totals of the plan (empty = work them out from the plan). */
  costTotals: SeatCostTotal[];
  /** Already invited onto 超员自动 Teams in this run (a seat was bought for each). */
  invitedOverage: number;
  replan: boolean;
}) {
  const cut = serverMessage.indexOf('继续会让');
  let lead = cut > 0 ? serverMessage.slice(0, cut).trim() : '';
  if (replan && !lead.startsWith(REPLAN_NOTE)) lead = `${REPLAN_NOTE}${lead}`;
  const note = lead.startsWith(REPLAN_NOTE) ? REPLAN_NOTE : '';
  const facts = note ? lead.slice(note.length) : lead;
  return (
    <div className="space-y-2">
      {note && <p className="font-medium text-gray-900 dark:text-gray-100">{note}</p>}
      {facts && <p>{facts}</p>}
      {invitedOverage > 0 && <p>已邀请的里有 {invitedOverage} 个在「超员自动」的 Team，已加购席位。</p>}
      {plan.length === 1 ? (
        <p className="font-medium text-gray-900 dark:text-gray-100">
          继续会在「{plan[0].team_name}」自动加购 {plan[0].extra_seats} 个 ChatGPT 席位并扣费，
          {seatChargeText(plan[0].seat_price, plan[0].extra_seats)}。
        </p>
      ) : (
        <>
          <p>继续会在这些 Team 自动加购 ChatGPT 席位并扣费：</p>
          <ul className="divide-y divide-gray-100 rounded-lg border border-gray-200 text-gray-700 dark:divide-ink-800 dark:border-ink-800 dark:text-ink-200">
            {plan.map((item) => (
              <li key={item.team_id || item.team_name} className="flex items-center justify-between gap-3 px-3 py-1.5">
                <span className="min-w-0 truncate" title={item.team_name}>{item.team_name}</span>
                <span className="shrink-0 text-right tabular-nums">
                  <span className="font-medium">+{item.extra_seats} 席</span>
                  <span className="ml-1.5 text-gray-500 dark:text-ink-400">
                    {/* 行内放不下年付的全年金额和说明：写在下面的合计里。 */}
                    {item.seat_price ? formatSeatAmountShort(item.seat_price, item.extra_seats) : '单价未知'}
                  </span>
                </span>
              </li>
            ))}
          </ul>
          <p className="font-medium text-gray-900 dark:text-gray-100">
            共加购 {total} 个 ChatGPT 席位，{planCostText(plan, costTotals)}。
          </p>
        </>
      )}
    </div>
  );
}

function EmailList({ title, hint, emails }: { title: string; hint: string; emails: string[] }) {
  return (
    <div className={cn('rounded-lg border p-3 text-sm', NOTE_PLAIN)}>
      <div className="font-medium text-gray-800 dark:text-gray-200">{title}（{emails.length}）</div>
      <p className="mt-0.5 text-xs text-gray-600 dark:text-ink-300">{hint}</p>
      <div className="mt-2 max-h-28 space-y-1 overflow-y-auto pr-1">
        {emails.map((address, i) => (
          <div key={`${address}-${i}`} className="break-all text-xs text-gray-700 dark:text-ink-200">{address}</div>
        ))}
      </div>
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
  teams,
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
  const quoteRequests = useMemo<SeatPurchaseRequest[]>(() => {
    if (!ask || (ask.seatType !== 'default' && ask.seatType !== 'prolite')) return [];
    if (ask.quotePlan) return ask.quotePlan.map(item => ({ teamId: item.team_id, teamName: item.team_name, seatType: 'default', additionalSeats: item.extra_seats }));
    return teamId ? [{ teamId, teamName, seatType: ask.seatType, additionalSeats: Math.min(MAX_CONFIRMED_SEATS, ask.purchaseCount ?? ask.emails.length) }] : [];
  }, [ask, teamId, teamName]);
  const [batchResult, setBatchResult] = useState<BatchInviteOutcome | null>(null);
  // 批量模式：每个 Team 的缓存 ChatGPT 空位（已扣待接受邀请），用来在提交前说清楚会不会加购。
  const [freeByTeam, setFreeByTeam] = useState<Map<string, number> | null>(null);
  // 每跑完一轮加一：上一轮刚用掉（或加购）的席位必须重新读，不能拿打开弹窗时的空位接着算。
  const [usageRound, setUsageRound] = useState(0);
  const [usageStale, setUsageStale] = useState(false);
  const [emailsKey, setEmailsKey] = useState('');
  // finishBatch 留在输入框里的「没加上的」邮箱；输入框还是它们时才算重试。
  const [leftoverText, setLeftoverText] = useState('');

  const batchMode = !teamId && Boolean(submitInvites);
  const effectiveSeatType = fixedSeatType ?? seatType;
  const emails = parseEmails(email);
  const pendingByType = useMemo(() => pendingCountsByType(pendingInvites), [pendingInvites]);
  // 单个 Team 才能事先判断：缓存的空位 + 这个 Team 的超员策略。服务端还会用实时数据再判一次。
  const gate = teamId && team
    ? seatGate(team, effectiveSeatType, pendingInvites ? pendingByType : teamPendingCounts(team), emails.length)
    : null;
  const gateText = gate ? gateMessage(gate) : null;
  const blocked = gate?.action === 'forbid';

  // 邮箱列表变了（停顿一下之后）也重新读一次空位：弹窗可能开了很久。
  useEffect(() => {
    if (!open || !batchMode) return;
    const timer = setTimeout(() => setEmailsKey(emails.join('\n')), 700);
    return () => clearTimeout(timer);
  }, [open, batchMode, email]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!open || !batchMode) return;
    let cancelled = false;
    fetchResourceUsage(false)
      .then((usage) => {
        if (!cancelled) setFreeByTeam(new Map(usage.teams.map((item) => [item.team_id, item.free_gpt_seats])));
      })
      .catch(() => {
        if (!cancelled) setFreeByTeam(null);
      })
      .finally(() => {
        if (!cancelled) setUsageStale(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, batchMode, usageRound, emailsKey]);

  /** Batch: how many seats ChatGPT would add for these emails, and on which 超员自动 Teams. */
  const batchPreview = useMemo(() => {
    if (!batchMode || !teams || emails.length === 0) return null;
    // 后端批量挑 Team 的候选：状态正常、订阅没到期。再去掉登录失效、暂停同步的（它们接不了邀请）：
    // 空位宁可少算，预告的加购数宁可多说。
    const candidates = teams.filter((item) =>
      item.status === 'active'
      && item.subscription_status !== 'expired'
      && item.auth_state !== 'rejected'
      && !item.sync_suspended_at);
    // 两个来源都看：空位接口（含待接受）和 Team 列表缓存（含 pending_invite_counts），取小的。
    const free = candidates.reduce((total, item) => {
      const fromTeam = cachedFreeSeats(item, 'default', teamPendingCounts(item)).free;
      const fromUsage = freeByTeam?.get(item.id);
      return total + (fromUsage === undefined ? fromTeam : Math.min(fromUsage, fromTeam));
    }, 0);
    const extra = Math.max(0, emails.length - free);
    const autoTeams = candidates.filter((item) => parseOveragePolicy(item.overage_policy) === 'auto');
    const autoNames = autoTeams.map((item) => item.name);
    const hasConfirm = candidates.some((item) => parseOveragePolicy(item.overage_policy) === 'confirm');
    return { free, extra, autoTeams, autoNames, hasConfirm };
  }, [batchMode, teams, emails.length, freeByTeam]);

  const batchNote: { tone: 'warn' | 'plain'; text: string } | null = (() => {
    if (!batchPreview || batchPreview.extra === 0) return null;
    const { free, extra, autoTeams, autoNames, hasConfirm } = batchPreview;
    const lead = `空闲 ChatGPT 席位约 ${free} 个，多出约 ${extra} 人。`;
    const buy = `ChatGPT 会自动加购约 ${extra} 个席位并扣费，${autoOverflowCost(autoTeams, extra)}。`;
    if (autoNames.length === 1) {
      return { tone: 'warn', text: `${lead}多出的会加进「超员自动」的 Team「${autoNames[0]}」，${buy}` };
    }
    if (autoNames.length > 1) {
      return { tone: 'warn', text: `${lead}多出的会加进「超员自动」的 Team（${quoteNames(autoNames)}），${buy}` };
    }
    if (hasConfirm) return { tone: 'warn', text: `${lead}超出部分需确认后加购。` };
    return { tone: 'plain', text: `${lead}其余 Team 均禁止超员，超出成员将跳过邀请。` };
  })();
  const batchWillBuy = Boolean(batchPreview && batchPreview.extra > 0 && batchPreview.autoNames.length > 0);

  const resetForm = () => {
    setEmail('');
    setSeatType(fixedSeatType ?? 'default');
    setExpiry(DEFAULT_EXPIRY);
    setError('');
    setAsk(null);
    setBatchResult(null);
    setLeftoverText('');
    setUsageStale(false);
  };

  // Esc / 点击遮罩关闭时 Radix 只会调用这里的 onOpenChange,不会走下面按钮上
  // 显式绑定的 resetForm——所有关闭路径统一在这里清空表单,而不仅是 Cancel/X。
  const handleDialogOpenChange = (nextOpen: boolean) => {
    if (!nextOpen) resetForm();
    onOpenChange(nextOpen);
  };

  const closeAll = () => {
    onOpenChange(false);
    resetForm();
  };

  /** Shows a finished batch and leaves only the emails that were not invited in the box. */
  const finishBatch = (outcome: BatchInviteOutcome) => {
    const leftover = [...outcome.failed.map((item) => item.email), ...outcome.declined_emails];
    // 这一轮用掉了空位（可能还加购了）：下一轮之前重新读空位，读到之前不让提交。
    setUsageStale(true);
    setUsageRound((round) => round + 1);
    if (leftover.length === 0 && !overagePurchaseText(outcome.added)) {
      onSuccess(outcome);
      closeAll();
      return;
    }
    // 结果留在弹窗里（花了钱、或有人没加上）；输入框只留没邀请的，「重新提交」不会重复邀请。
    // 全部成功时输入框清空、回到普通的「邮箱」状态，可以直接接着加下一批。
    onSuccess(outcome, { shownInDialog: true });
    setBatchResult(outcome);
    setEmail(leftover.join('\n'));
    setLeftoverText(leftover.join('\n'));
  };

  /** Batch (no teamId): the server picks the Teams and asks before overfilling a 超员需确认 Team. */
  const submitBatch = async (list: string[], allowOverage: boolean, prior?: OverageAsk['batch']) => {
    if (!submitInvites) return;
    const carriedAdded = prior?.added ?? [];
    const carriedFailed = prior?.failed ?? [];
    try {
      const result = await submitInvites({
        emails: list,
        seat_type: effectiveSeatType,
        expires_in: selectionToDuration(expiry),
        allow_overage: allowOverage,
        overage_team_ids: allowOverage ? prior?.planTeamIds ?? [] : undefined,
        overage_seat_limit: allowOverage ? prior?.seatLimit ?? 0 : undefined,
      });
      const body = (result && typeof result === 'object' ? result : {}) as Record<string, unknown>;
      const failed = [...carriedFailed, ...asFailures(body.failed)];
      const noPlace = Array.isArray(body.no_place_emails)
        ? body.no_place_emails.filter((item): item is string => typeof item === 'string')
        : failed.filter((item) => item.error === NO_PLACE).map((item) => item.email);
      setAsk(null);
      finishBatch({
        added: [...carriedAdded, ...asAdded(body.added)],
        failed,
        no_place_emails: noPlace,
        declined_emails: [],
      });
    } catch (err) {
      if (err instanceof OverageConfirmationError) {
        // 第一次问，或者确认过的计划已经不成立（服务端带新计划再问）：都重新给他看计划。
        const remaining = err.remainingEmails.length > 0 ? err.remainingEmails : list;
        const added = [...carriedAdded, ...asAdded(err.added)];
        setAsk({
          seatType: 'default',
          quotePlan: err.overagePlan,
          emails: remaining,
          batch: {
            planTeamIds: err.overagePlan.map((item) => item.team_id).filter(Boolean),
            seatLimit: err.extraSeatsTotal,
            added,
            failed: [...carriedFailed, ...asFailures(err.failed)],
          },
          message: err.overagePlan.length > 0
            ? (
              <BatchPlanMessage
                serverMessage={err.message}
                plan={err.overagePlan}
                total={err.extraSeatsTotal}
                costTotals={err.costTotals}
                invitedOverage={added.filter((item) => item.overage).length}
                replan={allowOverage || err.message.startsWith(REPLAN_NOTE)}
              />
            )
            : err.message,
        });
        return;
      }
      setAsk(null);
      const message = err instanceof Error ? err.message : '添加失败';
      if (carriedAdded.length > 0) {
        // 前几轮已经邀请的是真的：照实报出来，其余邮箱留在输入框里。
        finishBatch({
          added: carriedAdded,
          failed: [...carriedFailed, ...list.map((address) => ({ email: address, error: message }))],
          no_place_emails: [],
          declined_emails: [],
        });
        return;
      }
      setError(message);
    }
  };

  /**
   * One Team: invite one by one; the server re-checks every billed invite against live seats.
   * `confirmation` (after the admin agreed to buy N seats) goes with each email until N of them
   * came back as bought; the rest go without it, so a full Team asks again instead of buying more.
   */
  const submitToTeam = async (list: string[], confirmation: OverageConfirmation | null) => {
    if (!teamId) {
      setError('Team ID 缺失');
      return;
    }
    const expiresIn = selectionToDuration(expiry);
    let done = 0;
    // 这次确认已经用掉的加购个数（服务端回 overage=true 的邀请）。
    let bought = 0;
    let attached = false;
    try {
      for (const address of list) {
        const sendConfirmation = confirmation && bought < confirmation.seat_limit ? confirmation : null;
        attached = sendConfirmation !== null;
        const result = await inviteMember(teamId, {
          email: address,
          seat_type: effectiveSeatType,
          expires_in: expiresIn,
          overage_confirmation: sendConfirmation,
        });
        done += 1;
        // 没写 overage 的响应也按加购算：宁可多问一次，不多买。
        if (sendConfirmation && result?.overage !== false) bought += 1;
      }
      setAsk(null);
      onSuccess();
      closeAll();
    } catch (err) {
      const rest = list.slice(done);
      // 已经发出去的邀请是真的：先让卡片刷新，表单里只留下没加上的邮箱。
      if (done > 0) onSuccess();
      setEmail(rest.join('\n'));
      if (err instanceof OverageConfirmationError) {
        // 服务端刚现拉过、空位是 0：剩下的每个邮箱都会加购一个席位，按这个数问。
        const label = SEAT_TYPES[effectiveSeatType].label;
        const text = liveFullConfirmText({
          label,
          teamName: err.teamName || teamName,
          count: rest.length,
          invited: done,
          capacityUnknown: err.capacityUnknown,
          reconfirm: attached && err.confirmationStatus !== null && err.confirmationStatus !== 'missing',
          price: err.seatPrice,
        });
        setAsk({
          seatType: effectiveSeatType,
          emails: rest,
          purchaseCount: rest.length,
          message: rest.length > MAX_CONFIRMED_SEATS ? `${text}${CAP_NOTE}` : text,
        });
        return;
      }
      setAsk(null);
      const message = err instanceof Error ? err.message : '添加失败';
      setError(done > 0 ? `已邀请 ${done} 个，其余未添加：${message}` : message);
    }
  };

  const handleSubmit = async () => {
    if (emails.length === 0) {
      setError('请输入邮箱地址');
      return;
    }
    if (blocked || checkingSeats) return;
    setError('');
    setBatchResult(null);
    setLeftoverText('');
    if (gate?.action === 'confirm') {
      // 缓存显示已满且这个 Team 要先问：先确认再发，确认里写明他看到的加购个数和每月多出的钱。
      const text = gateConfirmText(gate);
      setAsk({
        seatType: effectiveSeatType,
        emails,
        purchaseCount: gate.extra,
        message: gate.extra > MAX_CONFIRMED_SEATS ? `${text}${CAP_NOTE}` : text,
      });
      return;
    }
    setLoading(true);
    try {
      if (submitInvites) await submitBatch(emails, false);
      else await submitToTeam(emails, null);
    } finally {
      setLoading(false);
    }
  };

  const handleConfirmOverage = async () => {
    if (!ask) return;
    setLoading(true);
    try {
      if (submitInvites) await submitBatch(ask.emails, true, ask.batch);
      else await submitToTeam(ask.emails, newOverageConfirmation(effectiveSeatType, ask.purchaseCount ?? ask.emails.length));
    } finally {
      setLoading(false);
    }
  };

  /** 取消加购：只关确认这一步，表单和已填的邮箱都留着。 */
  const handleAskOpenChange = (nextOpen: boolean) => {
    if (nextOpen || loading) return;
    const prior = ask?.batch;
    setAsk(null);
    // 批量模式下服务端在问之前可能已经用空位邀请了一些人：照实报出来；没确认的不算失败。
    if (prior && (prior.added.length > 0 || prior.failed.length > 0)) {
      finishBatch({
        added: prior.added,
        failed: prior.failed,
        no_place_emails: prior.failed.filter((item) => item.error === NO_PLACE).map((item) => item.email),
        declined_emails: ask?.emails ?? [],
      });
    }
  };

  const noPlace = new Set(batchResult?.no_place_emails ?? []);
  const noPlaceList = (batchResult?.failed ?? []).filter((f) => noPlace.has(f.email)).map((f) => f.email);
  const otherFailed = (batchResult?.failed ?? []).filter((f) => !noPlace.has(f.email));
  const declined = batchResult?.declined_emails ?? [];
  const addedCount = batchResult?.added.length ?? 0;
  const purchaseText = batchResult ? overagePurchaseText(batchResult.added) : null;
  // 输入框里还是上一轮留下的内容（没加上的邮箱，或全部成功时的空框）：结果还有效，照常显示；
  // 一开始填新的一批，上一轮的结果就收起来，免得两轮混在一起。
  const showResult = batchResult !== null && email === leftoverText;
  const retrying = showResult && leftoverText !== '';
  // 上一轮刚结束、空位还没重新读到：不让提交，免得按旧空位悄悄加购。
  const checkingSeats = batchMode && usageStale;

  // 会花钱的说法优先：按钮上永远写着要加购几席。
  const submitText = loading
    ? '添加中…'
    : checkingSeats
      ? '核对空位中…'
      : gate?.action === 'auto'
        ? `添加并加购 ${gate.extra} 席`
        : batchWillBuy && batchPreview
          ? `添加并加购约 ${batchPreview.extra} 席`
          : retrying
            ? '重试未成功项'
            : submitLabel;

  return (
    <DialogFrame
      open={open}
      onOpenChange={handleDialogOpenChange}
      title={title}
      description={
        !teamId
          ? '优先分配空闲席位；席位不足时按各 Team 策略处理。'
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
              {showResult ? '完成' : '取消'}
            </button>
          </Dialog.Close>
          <button
            type="button"
            onClick={() => { void handleSubmit(); }}
            disabled={loading || blocked || checkingSeats}
            title={blocked ? gateText ?? undefined : undefined}
            className={BUTTON.primary}
          >
            {(loading || checkingSeats) && <Loader2 size={14} className="animate-spin" />}
            {submitText}
          </button>
        </>
      }
      nested={
        <SeatPurchaseConfirmDialog
          quoteRequests={quoteRequests}
          open={ask !== null}
          onOpenChange={handleAskOpenChange}
          title={`确认加购 ${SEAT_TYPES[ask?.seatType ?? 'default'].label} 席位`}
          message={ask?.message ?? ''}
          confirmLabel="加购并添加"
          destructive
          loading={loading}
          onConfirm={() => { void handleConfirmOverage(); }}
        >
          <p className="text-xs text-gray-600 dark:text-ink-300">超员策略可在 Team 设置里修改。</p>
        </SeatPurchaseConfirmDialog>
      }
    >
      <div className="space-y-5">
        <div>
          <label htmlFor="add-member-emails" className={LABEL}>
            {retrying ? '没加上的邮箱' : '邮箱'} <span className="font-normal text-gray-400 dark:text-ink-500">每行一个</span>
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
            {/* Equal thirds while they fit; on a phone the option carrying a Beta badge may grow to fit it. */}
            <div className="flex gap-1 rounded-lg bg-gray-100 p-1 dark:bg-ink-950" role="group" aria-label="席位类型">
              {SEAT_TYPE_OPTIONS.map(({ value, label }) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setSeatType(value)}
                  aria-pressed={seatType === value}
                  className={cn(
                    'inline-flex h-8 min-w-fit flex-1 items-center justify-center gap-1.5 whitespace-nowrap rounded-md px-1.5 text-sm font-medium transition-colors',
                    seatType === value
                      ? cn('shadow-sm', SEAT_STYLE[value].pill)
                      : 'text-gray-500 hover:text-gray-900 dark:text-ink-400 dark:hover:text-gray-100',
                  )}
                >
                  <span className={cn('size-2 shrink-0 rounded-full', SEAT_STYLE[value].solid)} aria-hidden />
                  {label}
                  <SeatBetaBadge seatType={value} />
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
            <div className="mt-2">
              <SeatProductionWarning compact />
            </div>
          </div>
        )}

        {gateText && (
          <div
            role={blocked ? 'alert' : 'status'}
            className={cn('flex items-start gap-2 rounded-lg border px-3 py-2.5 text-sm leading-6', blocked ? NOTE_PLAIN : NOTE_WARN)}
          >
            {blocked
              ? <Ban size={15} className="mt-1 shrink-0 text-gray-500 dark:text-ink-400" />
              : <CreditCard size={15} className="mt-1 shrink-0 text-amber-600 dark:text-amber-400" />}
            <span>{gateText}</span>
          </div>
        )}

        {batchNote && (
          <div
            role="status"
            className={cn('flex items-start gap-2 rounded-lg border px-3 py-2.5 text-sm leading-6', batchNote.tone === 'warn' ? NOTE_WARN : NOTE_PLAIN)}
          >
            <CreditCard size={15} className={cn('mt-1 shrink-0', batchNote.tone === 'warn' ? 'text-amber-600 dark:text-amber-400' : 'text-gray-500 dark:text-ink-400')} />
            <span>{batchNote.text}</span>
          </div>
        )}

        <div>
          <span className={LABEL}>到期时间</span>
          <ExpiryPicker value={expiry} onChange={setExpiry} policy={kickPolicy} disabled={loading} />
        </div>

        {error && (
          <p role="alert" className="text-sm text-red-600 dark:text-red-400">{error}</p>
        )}

        {showResult && batchResult && (
          <div className="space-y-3" role="status">
            <div className="flex items-center gap-2 text-sm font-medium text-gray-900 dark:text-gray-100">
              {(otherFailed.length > 0 || noPlaceList.length > 0) && <AlertTriangle size={15} className="shrink-0 text-amber-500" />}
              {[
                `已邀请 ${addedCount} 人`,
                noPlaceList.length > 0 ? `席位不足 ${noPlaceList.length} 人` : '',
                declined.length > 0 ? `已取消加购 ${declined.length} 人` : '',
                otherFailed.length > 0 ? `失败 ${otherFailed.length} 人` : '',
              ].filter(Boolean).join('，')}
            </div>

            {purchaseText && (
              <div className={cn('flex items-start gap-2 rounded-lg border px-3 py-2.5 text-sm leading-6', NOTE_WARN)}>
                <CreditCard size={15} className="mt-1 shrink-0 text-amber-600 dark:text-amber-400" />
                <span>{purchaseText}。</span>
              </div>
            )}

            {noPlaceList.length > 0 && (
              <EmailList
                title={NO_PLACE}
                hint="所有可用 Team 席位已满且禁止超员，未发送邀请。"
                emails={noPlaceList}
              />
            )}

            {declined.length > 0 && (
              <EmailList title="已取消加购" hint="已放弃加购席位，未发送邀请。" emails={declined} />
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
          </div>
        )}
      </div>
    </DialogFrame>
  );
}
