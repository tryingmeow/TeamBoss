import { Fragment, useState, useEffect, useMemo, useRef, type FormEvent, type ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  fetchTeams,
  getFinanceOverview,
  getFinanceInvoices,
  updateFinanceSettings,
  updateFinanceCardNote,
  refreshFxRates,
  type FinanceAlert,
  type FinanceInvoiceRow,
  type FinanceOverview,
  type FinanceTeamItem,
  type FinanceTimelineItem,
} from '../../api/client';
import {
  AlertTriangle,
  BadgePercent,
  Check,
  ChevronDown,
  Clock,
  CreditCard,
  KeyRound,
  Loader2,
  Mail,
  Pencil,
  Plus,
  Receipt,
  RefreshCw,
  Wallet,
  Zap,
  type LucideIcon,
} from 'lucide-react';
import * as Popover from '@radix-ui/react-popover';
import * as Select from '@radix-ui/react-select';
import { differenceInCalendarDays, parseISO } from 'date-fns';
import { formatDateSafe, formatBeijingDateTime } from '../../lib/formatDate';
import { formatMoney, sameCurrency, teamUnit } from '../../lib/money';
import { cn } from '../../lib/utils';
import CostTrendChart from '../../components/CostTrendChart';
import { InvoiceSubTable } from '../../components/InvoiceTable';
import PageShell from '../../components/PageShell';
import SegmentedTabs from '../../components/SegmentedTabs';
import { BUTTON, CARD, INPUT, PILL, TONE } from '../../components/ui';
import { SEAT_STYLE } from '../../lib/seatType';

/**
 * Premium seats have no upstream price; the backend estimates them in USD. Always labelled 估算.
 * Written like the rest of the 预计月费 column: in the base currency, "≈" only when converted.
 */
function premiumEstimateText(
  team: Pick<FinanceTeamItem, 'premium_monthly_estimate_base' | 'premium_monthly_estimate_usd'>,
  baseCurrency: string,
): string {
  if (typeof team.premium_monthly_estimate_base === 'number') {
    return `${sameCurrency(baseCurrency, 'USD') ? '' : '≈ '}${formatMoney(team.premium_monthly_estimate_base, baseCurrency)}`;
  }
  return formatMoney(team.premium_monthly_estimate_usd ?? 0, 'USD');
}

/** Why a renewing Team is left out of 月预计支出 (the backend counts it in excluded_teams_count). */
function excludedReason(team: FinanceTeamItem): string {
  if (team.billing_period === null) return '计费周期未知';
  if (team.monthly_total_native === null) return '年付';
  return '缺汇率';
}

const BASE_CURRENCIES = ['USD', 'CNY', 'EUR', 'GBP', 'JPY', 'THB', 'SGD', 'HKD'];

type FinanceCardLike = Pick<
  FinanceTimelineItem,
  'card_brand' | 'card_last4' | 'card_key' | 'card_note' | 'card_team_count'
>;

interface FinanceCardSummary extends FinanceCardLike {
  team_count: number;
  team_names: string[];
  monthly_total_base: number;
}

interface TimelineCardGroup {
  key: string;
  items: FinanceTimelineItem[];
  firstDate: number;
  cardLast4: string;
  cardBrand: string;
}

type BillingTab = 'timeline' | 'cards' | 'details';
type TimelineSort = 'date' | 'card';

const BILLING_TABS: { value: BillingTab; label: string }[] = [
  { value: 'timeline', label: '续费时间线' },
  { value: 'cards', label: '卡片' },
  { value: 'details', label: 'Team 明细' },
];

const TIMELINE_SORTS: { value: TimelineSort; label: ReactNode }[] = [
  { value: 'date', label: <><Clock className="size-3.5" />按到期时间</> },
  { value: 'card', label: <><CreditCard className="size-3.5" />按卡片</> },
];

type AlertTone = 'warning' | 'danger' | 'discount';

const ALERT_META: Record<FinanceAlert['type'], { label: string; icon: LucideIcon; tone: AlertTone }> = {
  low_balance: { label: '余额偏低', icon: Wallet, tone: 'warning' },
  discount_expiring: { label: '折扣将到期', icon: BadgePercent, tone: 'discount' },
  token_expired: { label: 'Session 已失效', icon: KeyRound, tone: 'danger' },
  subscription_expired: { label: '订阅已到期', icon: Clock, tone: 'danger' },
  invoice_mismatch: { label: '账单金额不符', icon: Receipt, tone: 'warning' },
  invoice_unpaid: { label: '账单未支付', icon: Receipt, tone: 'warning' },
};

const ALERT_FALLBACK = { label: '预警', icon: AlertTriangle, tone: 'warning' as AlertTone };

const ALERT_TONE: Record<AlertTone, string> = {
  warning: 'border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-500/25 dark:bg-amber-500/10 dark:text-amber-100',
  danger: 'border-red-200 bg-red-50 text-red-900 dark:border-red-500/25 dark:bg-red-500/10 dark:text-red-100',
  discount: 'border-sky-200 bg-sky-50 text-sky-900 dark:border-sky-500/25 dark:bg-sky-500/10 dark:text-sky-100',
};

const ALERT_ICON_TONE: Record<AlertTone, string> = {
  warning: 'text-amber-600 dark:text-amber-400',
  danger: 'text-red-600 dark:text-red-400',
  discount: 'text-sky-600 dark:text-sky-400',
};

const TEAM_STATUS_LABEL: Record<string, string> = {
  active: '正常',
  token_expired: 'Session 已失效',
};

const SUBSCRIPTION_STATUS: Record<FinanceTeamItem['subscription_status'], { label: string; tone: string }> = {
  renewing: { label: '正常续费', tone: TONE.success },
  nonrenewing: { label: '到期不续费', tone: TONE.warning },
  expired: { label: '已到期', tone: TONE.danger },
  stale: { label: '数据未同步', tone: TONE.neutral },
};

function formatCardBrand(brand: string | null | undefined) {
  return brand?.trim() ? brand.trim().toUpperCase() : 'UNKNOWN';
}

/** "VISA •••• 4242"; brand left out when Stripe did not report one. */
function cardLabel(item: Pick<FinanceCardLike, 'card_brand' | 'card_last4'>) {
  const brand = item.card_brand?.trim().toUpperCase();
  return `${brand ? `${brand} ` : ''}•••• ${item.card_last4 || ''}`.trim();
}

function compareTimelineItems(a: FinanceTimelineItem, b: FinanceTimelineItem) {
  const dateDiff = new Date(a.date).getTime() - new Date(b.date).getTime();
  if (dateDiff !== 0) return dateDiff;
  return a.team_name.localeCompare(b.team_name);
}

function timelineCardKey(item: FinanceTimelineItem) {
  if (!item.card_last4) return `no-card:${item.team_id}`;
  return item.card_key || `${(item.card_brand || '').trim().toLowerCase()}:${item.card_last4}`;
}

/**
 * 已逾期必须和"还剩几天"分开：逾期 30 天的续费此前会显示成「今天」并落进
 * 红色≤7天角标里，看上去只是"今天要处理"，而不是"已经欠了一个月"。
 */
function timelineDaysLabel(daysUntil: number): string {
  if (daysUntil < 0) return `已逾期 ${Math.abs(daysUntil)} 天`;
  if (daysUntil === 0) return '今天';
  return `${daysUntil} 天`;
}

function timelineDaysBadgeClass(daysUntil: number) {
  // 逾期用实心红：在一屏红色角标里也能一眼挑出来。
  if (daysUntil < 0) return 'bg-red-600 text-white dark:bg-red-600 dark:text-white';
  if (daysUntil <= 7) return TONE.danger;
  if (daysUntil <= 14) return TONE.warning;
  return TONE.neutral;
}

function StatCard({
  label,
  loading,
  value,
  valueClassName,
  children,
}: {
  label: string;
  loading: boolean;
  value: ReactNode;
  valueClassName?: string;
  children?: ReactNode;
}) {
  return (
    <div className={cn(CARD, 'min-w-0 p-4 sm:p-5')}>
      <p className="truncate text-xs font-medium text-gray-500 sm:text-sm dark:text-ink-400">{label}</p>
      {loading ? (
        <div className="mt-2 h-8 animate-pulse rounded bg-gray-100 dark:bg-ink-800" />
      ) : (
        <>
          <p className={cn('mt-2 break-words text-lg font-semibold tabular-nums tracking-tight text-gray-900 sm:text-2xl dark:text-gray-50', valueClassName)}>
            {value}
          </p>
          {children && <div className="mt-1 space-y-0.5 text-xs leading-snug text-gray-500 dark:text-ink-400">{children}</div>}
        </>
      )}
    </div>
  );
}

function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="px-4 py-10 text-center">
      <p className="text-sm font-medium text-gray-700 dark:text-ink-200">{title}</p>
      {hint && <p className="mt-1 text-xs text-gray-500 dark:text-ink-400">{hint}</p>}
    </div>
  );
}

// 上期实付：日常扫读用基准币，原币精确值留给展开的对账子表。
function LatestInvoiceCell({
  team,
  baseCurrency,
  expanded,
}: {
  team: FinanceTeamItem;
  baseCurrency: string;
  expanded: boolean;
}) {
  const inv = team.latest_invoice;
  const chevron = (
    <ChevronDown
      className={cn('size-3.5 shrink-0 text-gray-400 transition-transform dark:text-ink-500', expanded && 'rotate-180')}
    />
  );

  if (!inv || inv.display_amount === null) {
    return (
      <div className="flex items-center justify-end gap-1.5 whitespace-nowrap text-gray-400 dark:text-ink-500">
        <span>—</span>
        {chevron}
      </div>
    );
  }

  const nativeUnit = teamUnit(team, inv.currency);
  const nativeText = formatMoney(inv.display_amount, nativeUnit);
  const converted = inv.display_amount_base !== null && !sameCurrency(inv.currency, baseCurrency);
  // 和预计月费同一种写法：基准币种（换算过的带 ≈）；原币在悬停提示和明细里。
  const sameAsBase = sameCurrency(inv.currency, baseCurrency);
  const primary = converted
    ? `≈ ${formatMoney(inv.display_amount_base, baseCurrency)}`
    : sameAsBase ? formatMoney(inv.display_amount, baseCurrency) : nativeText;
  const deviating = inv.reconciliation === 'over' || inv.reconciliation === 'under';

  let diffLine: string | null = null;
  if (deviating) {
    const word = (inv.diff_native ?? inv.diff_base ?? 0) > 0 ? '多' : '少';
    diffLine =
      converted && inv.diff_base !== null
        ? `比推算${word} ≈ ${formatMoney(Math.abs(inv.diff_base), baseCurrency)}`
        : `比推算${word} ${formatMoney(Math.abs(inv.diff_native ?? inv.diff_base ?? 0), sameAsBase ? baseCurrency : nativeUnit)}`;
  }

  return (
    <div title={nativeText}>
      <div className="flex items-center justify-end gap-1.5 whitespace-nowrap">
        <span
          className={cn(
            'tabular-nums',
            deviating ? 'font-semibold text-gray-900 dark:text-gray-100' : 'text-gray-600 dark:text-ink-300',
          )}
        >
          {primary}
        </span>
        {inv.reconciliation === 'unpaid' && <span className={cn(PILL, TONE.warning)}>未支付</span>}
        {chevron}
      </div>
      {diffLine ? (
        <div className="mt-0.5 whitespace-nowrap text-right text-xs text-amber-700 dark:text-amber-400">
          {diffLine}
        </div>
      ) : (
        converted && (
          <div className="mt-0.5 whitespace-nowrap text-right text-xs tabular-nums text-gray-500 dark:text-ink-400">
            {nativeText}
          </div>
        )
      )}
    </div>
  );
}

function CardNoteEditor({
  item,
  onSaveNote,
}: {
  item: FinanceCardLike;
  onSaveNote: (item: FinanceCardLike, note: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(item.card_note || '');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  if (!item.card_last4) return null;

  const hasNote = Boolean(item.card_note?.trim());
  const teamCount = item.card_team_count || 0;

  const handleOpenChange = (nextOpen: boolean) => {
    setOpen(nextOpen);
    if (nextOpen) {
      setDraft(item.card_note || '');
      setError('');
    }
  };

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setError('');
    try {
      await onSaveNote(item, draft);
      setOpen(false);
    } catch (err) {
      setError((err as Error).message || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Popover.Root open={open} onOpenChange={handleOpenChange}>
      <Popover.Trigger asChild>
        <button
          type="button"
          className="inline-flex size-9 shrink-0 items-center justify-center rounded-md text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 sm:size-7 dark:text-ink-500 dark:hover:bg-ink-800 dark:hover:text-gray-200"
          title={hasNote ? '编辑卡片备注' : '添加卡片备注'}
          aria-label={hasNote ? '编辑卡片备注' : '添加卡片备注'}
        >
          {hasNote ? <Pencil className="size-3.5" /> : <Plus className="size-3.5" />}
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          className="z-50 w-72 max-w-[calc(100vw-2rem)] rounded-xl border border-gray-200 bg-white p-3 shadow-xl animate-in fade-in zoom-in-95 dark:border-ink-800 dark:bg-ink-900"
          sideOffset={6}
          collisionPadding={16}
          align="end"
        >
          <form onSubmit={handleSubmit} className="space-y-3">
            <div>
              <div className="text-sm font-medium text-gray-900 dark:text-gray-100">卡片备注</div>
              <div className="mt-0.5 flex items-center gap-2 text-xs text-gray-500 dark:text-ink-400">
                <span className="tabular-nums">{cardLabel(item)}</span>
                {teamCount > 1 && <span>{teamCount} 个 Team 共用</span>}
              </div>
            </div>
            <input
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              maxLength={80}
              autoFocus
              placeholder="例如 主卡 / 备用卡"
              className={INPUT}
            />
            {error && <div className="text-xs text-red-600 dark:text-red-400">{error}</div>}
            <div className="flex justify-end gap-2">
              <button type="button" onClick={() => setOpen(false)} className={cn(BUTTON.secondary, 'px-3 py-1.5')}>
                取消
              </button>
              <button type="submit" disabled={saving} className={cn(BUTTON.primary, 'px-3 py-1.5')}>
                {saving && <Loader2 className="size-3.5 animate-spin" />}
                保存
              </button>
            </div>
          </form>
          <Popover.Arrow className="fill-white dark:fill-ink-900" />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

function CardBadge({
  item,
  onSaveNote,
}: {
  item: FinanceCardLike;
  onSaveNote: (item: FinanceCardLike, note: string) => Promise<void>;
}) {
  if (!item.card_last4) return null;

  const hasNote = Boolean(item.card_note?.trim());
  const teamCount = item.card_team_count || 0;

  return (
    <div className="flex w-40 items-center justify-end gap-1">
      <div className="relative min-w-0 flex-1">
        <div
          className={cn(
            'flex w-full min-w-0 items-center gap-1 rounded-md border border-gray-200 bg-white px-2 py-1 text-xs text-gray-500 dark:border-ink-700 dark:bg-ink-900 dark:text-ink-400',
            teamCount > 1 && 'pr-4',
          )}
          title={hasNote ? item.card_note : cardLabel(item)}
        >
          <CreditCard className="size-3.5 shrink-0" />
          <span className="shrink-0 font-mono">{item.card_last4}</span>
          {hasNote && (
            <span className="min-w-0 truncate text-gray-700 dark:text-ink-200">{item.card_note}</span>
          )}
        </div>
        {teamCount > 1 && (
          <span
            className="absolute -right-1.5 -top-2 z-10 inline-flex h-5 min-w-5 items-center justify-center rounded-full border border-blue-200 bg-blue-50 px-1.5 text-[11px] font-semibold leading-none tabular-nums text-blue-700 ring-2 ring-white dark:border-blue-400/30 dark:bg-ink-950 dark:text-blue-300 dark:ring-ink-900"
            title={`${teamCount} 个 Team 使用这张卡`}
          >
            {teamCount}
          </span>
        )}
      </div>
      <CardNoteEditor item={item} onSaveNote={onSaveNote} />
    </div>
  );
}

/** Header of one card's group in the by-card timeline; same card + brand + note layout as the 卡片 tab. */
function CardGroupHeader({
  item,
  onSaveNote,
}: {
  item: FinanceCardLike;
  onSaveNote: (item: FinanceCardLike, note: string) => Promise<void>;
}) {
  if (!item.card_last4) {
    return <span className="text-sm text-gray-500 dark:text-ink-400">未绑定卡片</span>;
  }
  const note = item.card_note?.trim();
  return (
    <div className="flex min-w-0 items-center gap-2 text-sm">
      <CreditCard className="size-4 shrink-0 text-gray-400 dark:text-ink-500" />
      <span className="font-mono text-gray-900 dark:text-gray-100">{item.card_last4}</span>
      {item.card_brand && <span className={cn(PILL, TONE.neutral)}>{formatCardBrand(item.card_brand)}</span>}
      {note && (
        <span className="min-w-0 truncate text-gray-700 dark:text-ink-200" title={note}>
          {note}
        </span>
      )}
      <CardNoteEditor item={item} onSaveNote={onSaveNote} />
    </div>
  );
}

/**
 * One renewal. `card` is the card column; the by-card view leaves it out because the group header
 * already names the card.
 */
function TimelineItemRow({
  item,
  team,
  baseCurrency,
  card,
  className,
}: {
  item: FinanceTimelineItem;
  team: FinanceTeamItem | undefined;
  baseCurrency: string;
  card?: ReactNode;
  className?: string;
}) {
  const daysUntil = differenceInCalendarDays(parseISO(item.date), new Date());
  const converted = item.amount_base !== null && !sameCurrency(item.currency, baseCurrency);
  const withCard = card !== undefined;
  const notRenewing = item.will_renew === 0 && <span className={cn(PILL, TONE.neutral)}>不续费</span>;

  const teamInfo = (
    <div className="min-w-0">
      <div className="truncate font-medium text-gray-900 dark:text-gray-100" title={item.team_name}>{item.team_name}</div>
      {item.owner_email && (
        <div className="mt-0.5 flex min-w-0 items-center gap-1.5 text-xs text-gray-500 dark:text-ink-400">
          <Mail className="size-3.5 shrink-0" />
          <span className="truncate" title={item.owner_email}>{item.owner_email}</span>
        </div>
      )}
    </div>
  );

  const amount = (
    <div className={cn('shrink-0 lg:justify-self-end lg:text-right', !withCard && 'text-right')}>
      <div className="whitespace-nowrap font-medium tabular-nums text-gray-900 dark:text-gray-100">
        {formatMoney(item.amount_native, teamUnit(team, item.currency))}
      </div>
      {converted && (
        <div className="whitespace-nowrap text-xs tabular-nums text-gray-500 dark:text-ink-400">
          ≈ {formatMoney(item.amount_base, baseCurrency)}
        </div>
      )}
    </div>
  );

  return (
    <div
      className={cn(
        'text-sm lg:grid lg:items-center lg:gap-4',
        withCard
          ? 'lg:grid-cols-[3.25rem_6.5rem_minmax(0,1fr)_10rem_10rem_3.5rem]'
          : 'lg:grid-cols-[3.25rem_6.5rem_minmax(0,1fr)_10rem_3.5rem]',
        className,
      )}
    >
      <div className="flex items-center gap-3 lg:contents">
        <div className="tabular-nums text-gray-500 dark:text-ink-400">{formatDateSafe(item.date, 'MM-dd')}</div>
        <span className={cn(PILL, 'w-fit', timelineDaysBadgeClass(daysUntil))}>{timelineDaysLabel(daysUntil)}</span>
        {notRenewing && <span className="ml-auto lg:hidden">{notRenewing}</span>}
      </div>
      {withCard ? (
        <>
          <div className="mt-2 lg:mt-0">{teamInfo}</div>
          <div className="mt-3 flex items-center justify-between gap-3 border-t border-gray-200 pt-3 lg:contents dark:border-ink-700/60">
            {amount}
            <div className="justify-self-end">{card}</div>
          </div>
        </>
      ) : (
        <div className="mt-2 flex items-start justify-between gap-3 lg:contents">
          {teamInfo}
          {amount}
        </div>
      )}
      <div className="hidden justify-self-end lg:block">{notRenewing}</div>
    </div>
  );
}

export default function Finance() {
  const [overview, setOverview] = useState<FinanceOverview | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(true);
  const [overviewError, setOverviewError] = useState('');
  const [refreshingFx, setRefreshingFx] = useState(false);
  const [fxError, setFxError] = useState('');
  const [activeBillingTab, setActiveBillingTab] = useState<BillingTab>('timeline');
  const [timelineSort, setTimelineSort] = useState<TimelineSort>('date');
  const [expandedTeamId, setExpandedTeamId] = useState<string | null>(null);
  const [exactTimeTeamId, setExactTimeTeamId] = useState<string | null>(null);
  const [invoicesByTeam, setInvoicesByTeam] = useState<Record<string, FinanceInvoiceRow[] | 'loading' | 'error'>>({});
  // Premium 在用人数不在财务接口里：从 Team 列表的缓存计数取。拿不到时只显示已付。
  const [premiumInUse, setPremiumInUse] = useState<Map<string, number> | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchTeams()
      .then((teams) => {
        if (!cancelled) setPremiumInUse(new Map(teams.map((team) => [team.id, Number(team.seat_type_counts?.prolite) || 0])));
      })
      .catch(() => {});

    setOverviewLoading(true);
    getFinanceOverview()
      .then(res => {
        if (cancelled) return;
        setOverview(res);
        setOverviewError('');
      })
      .catch(error => {
        if (!cancelled) setOverviewError(error.message || '财务数据加载失败');
      })
      .finally(() => {
        if (!cancelled) setOverviewLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const handleBaseCurrencyChange = async (newCurrency: string) => {
    if (!overview) return;
    try {
      await updateFinanceSettings({ base_currency: newCurrency });
      // Reload overview after changing currency
      const updated = await getFinanceOverview();
      setOverview(updated);
      setOverviewError('');
    } catch (error) {
      setOverviewError((error as Error).message || '切换基准币种失败');
    }
  };

  const handleRefreshFx = async () => {
    setRefreshingFx(true);
    setFxError('');
    try {
      await refreshFxRates();
      // Reload overview after refreshing FX
      const updated = await getFinanceOverview();
      setOverview(updated);
    } catch (error) {
      setFxError((error as Error).message || '汇率刷新失败');
    } finally {
      setRefreshingFx(false);
    }
  };

  const handleCardNoteSave = async (item: FinanceCardLike, note: string) => {
    if (!item.card_last4) return;
    await updateFinanceCardNote({
      card_brand: item.card_brand,
      card_last4: item.card_last4,
      note,
    });
    const updated = await getFinanceOverview();
    setOverview(updated);
    setOverviewError('');
  };

  const handleToggleInvoices = (teamId: string) => {
    const opening = expandedTeamId !== teamId;
    setExpandedTeamId(opening ? teamId : null);
    if (opening && invoicesByTeam[teamId] === undefined) {
      setInvoicesByTeam(prev => ({ ...prev, [teamId]: 'loading' }));
      getFinanceInvoices(teamId)
        .then(res => setInvoicesByTeam(prev => ({ ...prev, [teamId]: res.invoices })))
        .catch(() => setInvoicesByTeam(prev => ({ ...prev, [teamId]: 'error' })));
    }
  };

  // ?team=<id>（Team 卡片账单弹窗的「在财务页查看」）：切到「Team 明细」、展开这个 Team 的账单并滚过去。只认一次。
  const [searchParams] = useSearchParams();
  const deepLinkTeamId = searchParams.get('team');
  const deepLinkHandled = useRef(false);
  const pendingScrollTeamId = useRef<string | null>(null);
  useEffect(() => {
    // handleToggleInvoices / expandedTeamId are read once, when the overview first arrives.
    if (deepLinkHandled.current || !deepLinkTeamId || !overview) return;
    deepLinkHandled.current = true;
    if (!overview.teams.some(team => team.team_id === deepLinkTeamId)) return;
    pendingScrollTeamId.current = deepLinkTeamId;
    setActiveBillingTab('details');
    if (expandedTeamId !== deepLinkTeamId) handleToggleInvoices(deepLinkTeamId);
  }, [deepLinkTeamId, overview]);
  useEffect(() => {
    const target = pendingScrollTeamId.current;
    if (!target || activeBillingTab !== 'details' || expandedTeamId !== target) return;
    pendingScrollTeamId.current = null;
    document.getElementById(`finance-team-${target}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }, [activeBillingTab, expandedTeamId]);

  // Calculate 30-day renewal count
  const renewalCountNext30 = overview
    ? overview.timeline.filter(item => {
      const itemDate = parseISO(item.date);
      const daysUntil = differenceInCalendarDays(itemDate, new Date());
      return item.will_renew === 1 && daysUntil >= 0 && daysUntil <= 30;
      }).length
    : 0;

  const cardSummaries = useMemo<FinanceCardSummary[]>(() => {
    if (!overview) return [];

    const groups = new Map<string, FinanceCardSummary>();

    overview.teams.forEach(team => {
      if (!team.card_last4) return;

      const key = team.card_key || `${(team.card_brand || '').trim().toLowerCase()}:${team.card_last4}`;
      const existing = groups.get(key);
      const monthlyTotal = team.status === 'active' && team.will_renew && team.monthly_total_base !== null
        ? team.monthly_total_base
        : 0;

      if (existing) {
        existing.team_count += 1;
        existing.card_team_count = Math.max(existing.card_team_count || 0, team.card_team_count || 0);
        existing.team_names.push(team.name);
        existing.monthly_total_base += monthlyTotal;
        if (!existing.card_note && team.card_note) existing.card_note = team.card_note;
        return;
      }

      groups.set(key, {
        card_brand: team.card_brand,
        card_last4: team.card_last4,
        card_key: key,
        card_note: team.card_note || '',
        card_team_count: team.card_team_count || 1,
        team_count: 1,
        team_names: [team.name],
        monthly_total_base: monthlyTotal,
      });
    });

    return Array.from(groups.values()).sort((a, b) => {
      if (b.monthly_total_base !== a.monthly_total_base) {
        return b.monthly_total_base - a.monthly_total_base;
      }
      return `${formatCardBrand(a.card_brand)}${a.card_last4}`.localeCompare(`${formatCardBrand(b.card_brand)}${b.card_last4}`);
    });
  }, [overview]);

  const sortedTimeline = useMemo(() => {
    if (!overview) return [];
    return [...overview.timeline].sort(compareTimelineItems);
  }, [overview]);

  const timelineGroups = useMemo<TimelineCardGroup[]>(() => {
    const groups = new Map<string, TimelineCardGroup>();
    sortedTimeline.forEach(item => {
      const key = timelineCardKey(item);
      const existing = groups.get(key);
      const itemDate = new Date(item.date).getTime();

      if (existing) {
        existing.items.push(item);
        existing.firstDate = Math.min(existing.firstDate, itemDate);
        return;
      }

      groups.set(key, {
        key,
        firstDate: itemDate,
        cardLast4: item.card_last4 || '',
        cardBrand: formatCardBrand(item.card_brand),
        items: [item],
      });
    });

    return Array.from(groups.values()).sort((a, b) => {
      if (a.firstDate !== b.firstDate) return a.firstDate - b.firstDate;
      const last4Diff = a.cardLast4.localeCompare(b.cardLast4);
      if (last4Diff !== 0) return last4Diff;
      return a.cardBrand.localeCompare(b.cardBrand);
    });
  }, [sortedTimeline]);

  const teamsById = useMemo(
    () => new Map((overview?.teams ?? []).map(team => [team.team_id, team])),
    [overview],
  );

  const baseCurrency = overview?.base_currency || 'USD';
  // 与后端 excluded_teams_count 同一条件里能从明细看出来的那部分：续费中却算不出月费的 Team。
  const excludedTeams = (overview?.teams ?? []).filter(
    (team) => team.status === 'active' && team.subscription_status === 'renewing' && team.monthly_total_base === null,
  );
  const currencyOptions = BASE_CURRENCIES.includes(baseCurrency) ? BASE_CURRENCIES : [baseCurrency, ...BASE_CURRENCIES];
  const alertCount = overview?.alerts.length || 0;

  return (
    <PageShell
      title="财务"
      description="各 Team 的月费、续费日、扣款卡和 Stripe 账单对账。"
      actions={(
        <>
          <div className="flex items-center gap-2 text-sm text-gray-500 dark:text-ink-400">
            <span id="finance-base-currency" className="whitespace-nowrap">基准币种</span>
            {overviewLoading ? (
              <span className="block h-9 w-20 animate-pulse rounded-lg bg-gray-200 dark:bg-ink-800" />
            ) : (
              <Select.Root value={baseCurrency} onValueChange={handleBaseCurrencyChange}>
                <Select.Trigger
                  aria-labelledby="finance-base-currency"
                  className={cn(INPUT, 'inline-flex h-9 w-auto items-center gap-2 py-0 pr-2.5 text-sm')}
                >
                  <Select.Value />
                  <Select.Icon>
                    <ChevronDown className="size-3.5 text-gray-400 dark:text-ink-500" />
                  </Select.Icon>
                </Select.Trigger>
                <Select.Portal>
                  <Select.Content
                    position="popper"
                    sideOffset={6}
                    collisionPadding={16}
                    className="z-50 max-h-[var(--radix-select-content-available-height)] min-w-[var(--radix-select-trigger-width)] overflow-y-auto rounded-xl border border-gray-200 bg-white p-1 shadow-xl dark:border-ink-800 dark:bg-ink-900"
                  >
                    <Select.Viewport>
                      {currencyOptions.map(curr => (
                        <Select.Item
                          key={curr}
                          value={curr}
                          className="relative flex h-9 cursor-default select-none items-center rounded-md pl-2.5 pr-8 text-sm text-gray-700 outline-none data-[highlighted]:bg-gray-100 data-[highlighted]:text-gray-900 dark:text-ink-300 dark:data-[highlighted]:bg-ink-800 dark:data-[highlighted]:text-gray-100"
                        >
                          <Select.ItemText>{curr}</Select.ItemText>
                          <Select.ItemIndicator className="absolute right-2 inline-flex">
                            <Check className="size-4 text-blue-600 dark:text-blue-400" />
                          </Select.ItemIndicator>
                        </Select.Item>
                      ))}
                    </Select.Viewport>
                  </Select.Content>
                </Select.Portal>
              </Select.Root>
            )}
          </div>
          <span className="whitespace-nowrap text-xs text-gray-500 dark:text-ink-400">
            {overviewLoading
              ? '汇率加载中…'
              : overview?.fx_updated_at
                ? `汇率更新于 ${formatDateSafe(overview.fx_updated_at, 'MM-dd HH:mm')}`
                : '使用内置静态汇率'}
          </span>
          <button
            type="button"
            onClick={handleRefreshFx}
            disabled={refreshingFx}
            className={BUTTON.secondary}
          >
            <RefreshCw className={cn('size-4', refreshingFx && 'animate-spin')} />
            {refreshingFx ? '刷新中…' : '刷新汇率'}
          </button>
        </>
      )}
    >
      <div className="space-y-6">
        {overviewError && (
          <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-300">
            {overviewError}
          </div>
        )}
        {fxError && (
          <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-300">
            刷新汇率失败：{fxError}
          </div>
        )}

        <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
          <StatCard label="月预计支出" loading={overviewLoading} value={formatMoney(overview?.monthly_total_base ?? 0, baseCurrency)}>
            {/* 紧跟总额：说的是这个数没算进哪些 Team。 */}
            {overview?.excluded_teams_count ? (
              <p>
                未计入 {overview.excluded_teams_count} 个 Team
                {excludedTeams.length > 0 && (
                  <>
                    ：{excludedTeams.slice(0, 3).map((team) => `${team.name}（${excludedReason(team)}）`).join('、')}
                    {overview.excluded_teams_count > Math.min(3, excludedTeams.length) && ' 等'}
                  </>
                )}
              </p>
            ) : null}
            {(overview?.premium_monthly_estimate_base_total ?? 0) > 0 && (
              <p>
                另加 <span className={cn('font-medium', SEAT_STYLE.prolite.text)}>Premium 估算</span>{' '}
                <span className="whitespace-nowrap">
                  {sameCurrency(baseCurrency, 'USD') ? '' : '≈ '}{formatMoney(overview?.premium_monthly_estimate_base_total, baseCurrency)}
                </span>
                <span className="block text-[11px]">
                  按每席 {formatMoney(overview?.premium_seat_price_estimate_usd ?? 125, '$')}/月估算，上游没有 Premium 单价
                </span>
              </p>
            )}
            {overview?.last_paid_total_base != null && (
              <p>上期实付合计 <span className="whitespace-nowrap">≈ {formatMoney(overview.last_paid_total_base, baseCurrency)}</span></p>
            )}
          </StatCard>
          <StatCard
            label="折扣共省"
            loading={overviewLoading}
            value={formatMoney(overview?.discount_total_base ?? 0, baseCurrency)}
            valueClassName="text-sky-600 dark:text-sky-400"
          >
            <p>每月折扣合计</p>
          </StatCard>
          <StatCard label="30 天内续费" loading={overviewLoading} value={renewalCountNext30}>
            <p>将自动续费的 Team</p>
          </StatCard>
          <StatCard
            label="预警"
            loading={overviewLoading}
            value={alertCount}
            valueClassName={alertCount > 0 ? 'text-red-600 dark:text-red-400' : undefined}
          >
            <p>{alertCount > 0 ? '详情见下方' : '暂无需要处理的问题'}</p>
          </StatCard>
        </div>

        {overview && overview.alerts.length > 0 && (
          <section>
            <h2 className="mb-3 flex items-center gap-2 text-base font-semibold text-gray-900 dark:text-gray-50">
              预警
              <span className={cn(PILL, TONE.danger, 'tabular-nums')}>{overview.alerts.length}</span>
            </h2>
            <div className="grid gap-2 lg:grid-cols-2">
              {overview.alerts.map((alert, i) => {
                const meta = ALERT_META[alert.type] ?? ALERT_FALLBACK;
                const Icon = meta.icon;
                return (
                  <div
                    key={i}
                    className={cn('flex min-w-0 items-start gap-3 rounded-lg border px-3.5 py-3 text-sm', ALERT_TONE[meta.tone])}
                  >
                    <Icon className={cn('mt-0.5 size-4 shrink-0', ALERT_ICON_TONE[meta.tone])} />
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-baseline gap-x-2">
                        <span className="font-medium">{alert.team_name}</span>
                        <span className="text-xs opacity-75">{meta.label}</span>
                      </div>
                      <div className="mt-0.5 text-xs opacity-90">{alert.detail}</div>
                    </div>
                  </div>
                );
              })}
            </div>
          </section>
        )}

        {/* Keyed on the base currency so the chart refetches its totals after the currency changes. */}
        <CostTrendChart key={baseCurrency} />

        <section className={cn(CARD, 'min-w-0 p-4 sm:p-6')}>
          <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
            <SegmentedTabs value={activeBillingTab} onChange={setActiveBillingTab} options={BILLING_TABS} ariaLabel="账单视图" />
            {activeBillingTab === 'timeline' && (
              <SegmentedTabs value={timelineSort} onChange={setTimelineSort} options={TIMELINE_SORTS} ariaLabel="时间线分组" />
            )}
            {activeBillingTab === 'details' && (
              <p className="text-xs text-gray-500 dark:text-ink-400">
                <span className="md:hidden">左右滑动看全部列，</span>点一行展开该 Team 的 Stripe 账单
              </p>
            )}
          </div>

          {activeBillingTab === 'timeline' && (
            <>
              {overviewLoading ? (
                <div className="space-y-3">
                  {[1, 2, 3].map(i => (
                    <div key={i} className="h-12 animate-pulse rounded-lg bg-gray-100 dark:bg-ink-800" />
                  ))}
                </div>
              ) : !overview || overview.timeline.length === 0 ? (
                <EmptyState title="暂无续费计划" hint="Team 同步到订阅信息后，续费日会按时间排在这里。" />
              ) : timelineSort === 'date' ? (
                <div className="space-y-2">
                  {sortedTimeline.map(item => (
                    <TimelineItemRow
                      key={`${item.team_id}:${item.date}:${timelineCardKey(item)}`}
                      item={item}
                      team={teamsById.get(item.team_id)}
                      baseCurrency={overview.base_currency}
                      card={<CardBadge item={item} onSaveNote={handleCardNoteSave} />}
                      className="rounded-lg border border-gray-200 bg-gray-50 p-3 lg:px-4 lg:py-2.5 dark:border-ink-800 dark:bg-ink-800/40"
                    />
                  ))}
                </div>
              ) : (
                <div className="space-y-3">
                  {timelineGroups.map(group => (
                    <div key={group.key} className="rounded-lg border border-gray-200 dark:border-ink-800">
                      <div className="flex min-h-11 items-center rounded-t-lg border-b border-gray-200 bg-gray-50 px-3 py-1.5 lg:px-4 dark:border-ink-800 dark:bg-ink-800/40">
                        <CardGroupHeader item={group.items[0]} onSaveNote={handleCardNoteSave} />
                      </div>
                      <div className="divide-y divide-gray-100 dark:divide-ink-800">
                        {group.items.map(item => (
                          <TimelineItemRow
                            key={`${item.team_id}:${item.date}`}
                            item={item}
                            team={teamsById.get(item.team_id)}
                            baseCurrency={overview.base_currency}
                            className="px-3 py-3 lg:px-4 lg:py-2.5"
                          />
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}

          {activeBillingTab === 'cards' && (
            <div className="text-sm">
              <div className="hidden gap-4 border-b border-gray-200 px-3 pb-2 text-xs font-medium text-gray-500 md:grid md:grid-cols-[12rem_minmax(0,1fr)_7rem_10rem] dark:border-ink-800 dark:text-ink-400">
                <div>卡片</div>
                <div>备注</div>
                <div className="text-right">绑定</div>
                <div className="text-right">预计月支出</div>
              </div>
              {overviewLoading ? (
                <div className="py-8 text-center text-gray-500 dark:text-ink-400">加载中…</div>
              ) : cardSummaries.length === 0 ? (
                <EmptyState title="暂无卡片" hint="Team 同步到扣款卡片后，会按卡片汇总在这里。" />
              ) : (
                <div className="divide-y divide-gray-100 dark:divide-ink-800">
                  {cardSummaries.map(card => (
                    <div
                      key={card.card_key || `${card.card_brand}:${card.card_last4}`}
                      className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-1 px-1 py-3 transition-colors hover:bg-gray-50 md:grid-cols-[12rem_minmax(0,1fr)_7rem_10rem] md:px-3 dark:hover:bg-ink-800/30"
                    >
                      <div className="flex min-w-0 items-center gap-2">
                        <CreditCard className="size-4 shrink-0 text-gray-400 dark:text-ink-500" />
                        <span className="font-mono text-gray-900 dark:text-gray-100">{card.card_last4}</span>
                        {card.card_brand && <span className={cn(PILL, TONE.neutral)}>{formatCardBrand(card.card_brand)}</span>}
                      </div>
                      <div className="col-start-1 row-start-2 flex min-w-0 items-center gap-1 md:col-start-auto md:row-start-auto">
                        <span
                          className={cn('min-w-0 truncate', card.card_note ? 'text-gray-800 dark:text-ink-200' : 'text-gray-400 dark:text-ink-500')}
                          title={card.card_note || undefined}
                        >
                          {card.card_note || '无备注'}
                        </span>
                        <CardNoteEditor item={card} onSaveNote={handleCardNoteSave} />
                      </div>
                      <div
                        className="col-start-2 row-start-2 whitespace-nowrap text-right text-gray-500 md:col-start-auto md:row-start-auto dark:text-ink-400"
                        title={card.team_names.join('、')}
                      >
                        <span className="font-medium tabular-nums text-gray-900 dark:text-gray-100">{card.team_count}</span> 个 Team
                      </div>
                      <div className="col-start-2 row-start-1 whitespace-nowrap text-right font-medium tabular-nums text-gray-900 md:col-start-auto md:row-start-auto dark:text-gray-100">
                        {formatMoney(card.monthly_total_base, baseCurrency)}
                        <span className="font-normal text-gray-500 md:hidden dark:text-ink-400"> / 月</span>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {activeBillingTab === 'details' && (
            <div className="-mx-4 overflow-x-auto sm:-mx-6">
              <table className="w-full min-w-[56rem] text-left text-sm text-gray-700 dark:text-ink-300">
                <thead className="border-b border-gray-200 text-xs text-gray-500 dark:border-ink-800 dark:text-ink-400">
                  <tr>
                    <th className="whitespace-nowrap px-3 py-2 font-medium first:pl-4 sm:first:pl-6">Team</th>
                    <th className="whitespace-nowrap px-3 py-2 font-medium">席位</th>
                    <th className="whitespace-nowrap px-3 py-2 text-right font-medium">计费</th>
                    <th className="whitespace-nowrap px-3 py-2 text-right font-medium">预计月费</th>
                    <th className="whitespace-nowrap px-3 py-2 text-right font-medium">上期实付</th>
                    <th className="whitespace-nowrap px-3 py-2 text-right font-medium">账户余额</th>
                    <th className="whitespace-nowrap px-3 py-2 font-medium last:pr-4 sm:last:pr-6">到期日</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 dark:divide-ink-800">
                  {overviewLoading ? (
                    <tr><td colSpan={7} className="px-3 py-8 text-center text-gray-500 dark:text-ink-400">加载中…</td></tr>
                  ) : !overview || overview.teams.length === 0 ? (
                    <tr><td colSpan={7}><EmptyState title="暂无 Team" hint="在「Team 列表」添加 Team 后，这里会列出它的计费明细。" /></td></tr>
                  ) : (
                    overview.teams.map((team) => {
                      const sym = teamUnit(team, null);
                      // 月费乘的是已付 ChatGPT 席位；老后端没有这个字段时退回 seats_entitled。
                      const chatgptBilled = team.chatgpt_seats_billed ?? team.seats_entitled;
                      const premiumPaid = team.premium_seats_paid ?? 0;
                      const subscription = SUBSCRIPTION_STATUS[team.subscription_status] ?? SUBSCRIPTION_STATUS.renewing;
                      const monthlyConverted = team.monthly_total_base !== null && !sameCurrency(team.billing_currency, overview.base_currency);
                      let daysBadge: string = TONE.neutral;
                      if (team.days_left !== null) {
                        if (team.days_left <= 7) daysBadge = TONE.danger;
                        else if (team.days_left <= 14) daysBadge = TONE.warning;
                      }
                      return (
                        <Fragment key={team.team_id}>
                        <tr
                          id={`finance-team-${team.team_id}`}
                          onClick={() => handleToggleInvoices(team.team_id)}
                          aria-expanded={expandedTeamId === team.team_id}
                          className={cn(
                            'cursor-pointer align-top transition-colors hover:bg-gray-50 dark:hover:bg-ink-800/30',
                            team.status === 'token_expired' && 'bg-red-50/60 dark:bg-red-500/5',
                          )}
                        >
                          <td className="px-3 py-3.5 first:pl-4 sm:first:pl-6">
                            <div className="flex items-start gap-2.5">
                              <span
                                className={cn(
                                  'mt-1.5 size-2 shrink-0 rounded-full',
                                  team.subscription_status === 'expired'
                                    ? 'bg-red-500'
                                    : team.status === 'active'
                                      ? 'bg-emerald-500'
                                      : team.status === 'token_expired'
                                        ? 'bg-red-500'
                                        : 'bg-gray-400 dark:bg-ink-600',
                                )}
                                title={TEAM_STATUS_LABEL[team.status] ?? team.status}
                              />
                              <div className="min-w-0">
                                <div className="font-medium text-gray-900 dark:text-gray-100">{team.name}</div>
                                {team.remark && (
                                  <div className="mt-0.5 text-xs text-gray-500 dark:text-ink-400">{team.remark}</div>
                                )}
                                <span className={cn(PILL, 'mt-1', subscription.tone)}>{subscription.label}</span>
                              </div>
                            </div>
                          </td>
                          <td className="px-3 py-3.5">
                            <div className="whitespace-nowrap">
                              <span className={cn('text-xs font-medium', SEAT_STYLE.default.text)}>ChatGPT </span>
                              <span className="font-medium tabular-nums text-gray-900 dark:text-gray-100">{team.chatgpt_in_use}</span>
                              <span className="tabular-nums text-gray-500 dark:text-ink-400">/{chatgptBilled}</span>
                            </div>
                            {premiumPaid > 0 && (
                              <div className="mt-0.5 whitespace-nowrap" title="Premium 在用 / 已付">
                                <span className={cn('text-xs font-medium', SEAT_STYLE.prolite.text)}>Premium </span>
                                {premiumInUse?.has(team.team_id) ? (
                                  <>
                                    <span className="font-medium tabular-nums text-gray-900 dark:text-gray-100">{premiumInUse.get(team.team_id)}</span>
                                    <span className="tabular-nums text-gray-500 dark:text-ink-400">/{premiumPaid}</span>
                                  </>
                                ) : (
                                  <>
                                    <span className="text-xs text-gray-500 dark:text-ink-400">已付 </span>
                                    <span className="font-medium tabular-nums text-gray-900 dark:text-gray-100">{premiumPaid}</span>
                                  </>
                                )}
                              </div>
                            )}
                            <span className={cn(PILL, 'mt-1', team.is_codex_enabled ? SEAT_STYLE.usage_based.pill : TONE.neutral)}>
                              <Zap className="size-2.5" />
                              {team.is_codex_enabled ? 'Codex 已开' : 'Codex 未开'}
                              {team.codex_count > 0 ? ` · ${team.codex_count}` : ''}
                            </span>
                          </td>
                          <td className="px-3 py-3.5 text-right">
                            <div className="whitespace-nowrap tabular-nums text-gray-900 dark:text-gray-100">
                              {team.price_per_seat !== null && formatMoney(team.price_per_seat, sym)}
                              <span className="text-gray-500 dark:text-ink-400">
                                {team.price_per_seat !== null ? ' × ' : ''}{chatgptBilled} 席
                              </span>
                            </div>
                            {premiumPaid > 0 && (
                              <div className="mt-0.5 whitespace-nowrap text-xs tabular-nums text-gray-500 dark:text-ink-400">
                                <span className={SEAT_STYLE.prolite.text}>Premium</span>{' '}
                                {formatMoney(overview.premium_seat_price_estimate_usd ?? 125, '$')} × {premiumPaid} 席 · 估算
                              </div>
                            )}
                            {team.price_per_seat === null && (
                              <div className="mt-0.5 whitespace-nowrap text-xs text-gray-500 dark:text-ink-400">单价未知</div>
                            )}
                            {team.discount_amount > 0 && (
                              <div className="mt-0.5 whitespace-nowrap text-xs tabular-nums text-sky-600 dark:text-sky-400">
                                折扣 -{formatMoney(team.discount_amount, sym)}
                              </div>
                            )}
                          </td>
                          <td className="px-3 py-3.5 text-right">
                            <div className="whitespace-nowrap font-medium tabular-nums text-gray-900 dark:text-gray-100">
                              {/* 这一列统一写成基准币种（换算过的带 ≈），原币另起一行。 */}
                              {team.monthly_total_base !== null
                                ? `${monthlyConverted ? '≈ ' : ''}${formatMoney(team.monthly_total_base, overview.base_currency)}`
                                : formatMoney(team.monthly_total_native, sym)}
                            </div>
                            {team.billing_period === null ? (
                              <div className="mt-0.5 whitespace-nowrap text-xs text-gray-500 dark:text-ink-400">
                                计费周期未知
                              </div>
                            ) : team.monthly_total_native === null ? (
                              <div className="mt-0.5 whitespace-nowrap text-xs text-gray-500 dark:text-ink-400">
                                年付，月费暂不计算
                              </div>
                            ) : monthlyConverted && (
                              <div className="mt-0.5 whitespace-nowrap text-xs tabular-nums text-gray-500 dark:text-ink-400">
                                {formatMoney(team.monthly_total_native, sym)}
                              </div>
                            )}
                            {premiumPaid > 0 && (
                              <div className="mt-0.5 whitespace-nowrap text-xs tabular-nums">
                                <span className={SEAT_STYLE.prolite.text}>+ Premium 估算</span>{' '}
                                <span className="text-gray-700 dark:text-ink-200">{premiumEstimateText(team, overview.base_currency)}</span>
                              </div>
                            )}
                          </td>
                          <td className="px-3 py-3.5 text-right">
                            <LatestInvoiceCell
                              team={team}
                              baseCurrency={overview.base_currency}
                              expanded={expandedTeamId === team.team_id}
                            />
                          </td>
                          <td className="whitespace-nowrap px-3 py-3.5 text-right tabular-nums text-gray-600 dark:text-ink-300">
                            {formatMoney(team.balance, sym)}
                          </td>
                          <td className="px-3 py-3.5 last:pr-4 sm:last:pr-6">
                            {team.active_until ? (
                              <div>
                                <div className="flex items-center gap-1.5 whitespace-nowrap">
                                  <span className="tabular-nums text-gray-900 dark:text-gray-100">
                                    {formatDateSafe(team.active_until, 'MM-dd')}
                                  </span>
                                  {team.days_left !== null && (
                                    <span className={cn(PILL, daysBadge)}>
                                      {team.days_left > 0 ? `${team.days_left} 天` : '已过期'}
                                    </span>
                                  )}
                                  <button
                                    type="button"
                                    onClick={(e) => { e.stopPropagation(); setExactTimeTeamId((prev) => (prev === team.team_id ? null : team.team_id)); }}
                                    aria-expanded={exactTimeTeamId === team.team_id}
                                    aria-label="具体续费时间"
                                    title="具体续费时间"
                                    className="rounded p-1 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 dark:text-ink-500 dark:hover:bg-ink-800 dark:hover:text-gray-200"
                                  >
                                    <Clock className="size-3.5" />
                                  </button>
                                </div>
                                {exactTimeTeamId === team.team_id && (
                                  <div className="mt-1 whitespace-nowrap text-xs text-gray-500 dark:text-ink-400">
                                    续费 {formatBeijingDateTime(team.active_until)}
                                  </div>
                                )}
                              </div>
                            ) : (
                              <span className="text-gray-400 dark:text-ink-500">—</span>
                            )}
                          </td>
                        </tr>
                        {expandedTeamId === team.team_id && (
                          <tr className="bg-gray-50 dark:bg-ink-950/40">
                            <td colSpan={7} className="px-4 py-3 sm:px-6">
                              <InvoiceSubTable state={invoicesByTeam[team.team_id]} />
                            </td>
                          </tr>
                        )}
                        </Fragment>
                      );
                    })
                  )}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </PageShell>
  );
}
