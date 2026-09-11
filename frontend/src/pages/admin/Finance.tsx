import { Fragment, useState, useEffect, useMemo, type FormEvent } from 'react';
import {
  getFinanceOverview,
  getFinanceInvoices,
  updateFinanceSettings,
  updateFinanceCardNote,
  refreshFxRates,
  type FinanceInvoiceRow,
  type FinanceOverview,
  type FinanceTeamItem,
  type FinanceTimelineItem,
} from '../../api/client';
import { AlertTriangle, ChevronDown, Clock, CreditCard, ExternalLink, KeyRound, Loader2, Mail, Pencil, Plus, Wallet, TrendingUp, Zap } from 'lucide-react';
import * as Popover from '@radix-ui/react-popover';
import { differenceInCalendarDays, format, parseISO } from 'date-fns';
import { formatDateSafe } from '../../lib/formatDate';
import CostTrendChart from '../../components/CostTrendChart';

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

interface TimelineRow {
  item: FinanceTimelineItem;
  groupIndex: number;
  groupSize: number;
}

function formatBaseAmount(amount: number | null | undefined, currency: string | undefined) {
  return `${(amount ?? 0).toFixed(2)} ${currency || ''}`.trim();
}

function formatCardBrand(brand: string | null | undefined) {
  return brand?.trim() ? brand.trim().toUpperCase() : 'UNKNOWN';
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
  return `${daysUntil}天`;
}

function timelineDaysBadgeClass(daysUntil: number) {
  if (daysUntil < 0) {
    // 实心 + 描边：逾期在一屏红色角标里也能一眼挑出来。
    return 'bg-rose-600 text-white ring-1 ring-rose-700 dark:bg-rose-600 dark:text-white dark:ring-rose-400/60';
  }
  if (daysUntil <= 7) {
    return 'bg-rose-50 text-rose-700 dark:bg-rose-500/20 dark:text-rose-300';
  }
  if (daysUntil <= 14) {
    return 'bg-amber-50 text-amber-700 dark:bg-amber-500/20 dark:text-amber-300';
  }
  return 'bg-gray-100 text-gray-600 dark:bg-slate-700 dark:text-slate-300';
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
      className={`h-3.5 w-3.5 shrink-0 text-gray-400 transition-transform dark:text-slate-500 ${
        expanded ? 'rotate-180' : ''
      }`}
    />
  );

  if (!inv || inv.display_amount === null) {
    return (
      <div className="flex items-center justify-end gap-1.5 whitespace-nowrap text-gray-400 dark:text-slate-500">
        <span>—</span>
        {chevron}
      </div>
    );
  }

  const nativeText = `${inv.currency || ''} ${inv.display_amount.toFixed(2)}`.trim();
  const primary =
    inv.display_amount_base !== null
      ? `≈ ${baseCurrency} ${inv.display_amount_base.toFixed(2)}`
      : nativeText;
  const deviating = inv.reconciliation === 'over' || inv.reconciliation === 'under';

  let diffLine: string | null = null;
  if (deviating) {
    const word = (inv.diff_native ?? 0) > 0 ? '多' : '少';
    diffLine =
      inv.diff_base !== null
        ? `比推算${word} ≈ ${baseCurrency} ${Math.abs(inv.diff_base).toFixed(2)}`
        : `比推算${word} ${inv.currency || ''} ${Math.abs(inv.diff_native ?? 0).toFixed(2)}`;
  }

  return (
    <div title={nativeText}>
      <div className="flex items-center justify-end gap-1.5 whitespace-nowrap">
        <span
          className={
            deviating
              ? 'font-semibold text-gray-900 dark:text-slate-100'
              : 'text-gray-500 dark:text-slate-400'
          }
        >
          {primary}
        </span>
        {inv.reconciliation === 'unpaid' && (
          <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[11px] font-medium text-amber-700 dark:bg-amber-500/20 dark:text-amber-300">
            未支付
          </span>
        )}
        {chevron}
      </div>
      {diffLine ? (
        <div className="mt-0.5 whitespace-nowrap text-right text-xs text-amber-600 dark:text-amber-400">
          {diffLine}
        </div>
      ) : (
        inv.display_amount_base !== null && (
          <div className="mt-0.5 whitespace-nowrap text-right text-xs text-gray-400 dark:text-slate-500">
            {nativeText}
          </div>
        )
      )}
    </div>
  );
}

function invoiceStatusCell(status: string | null) {
  if (status === 'open') {
    return (
      <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[11px] font-medium text-amber-700 dark:bg-amber-500/20 dark:text-amber-300">
        未支付
      </span>
    );
  }
  const label =
    status === 'paid' ? '已支付' : status === 'void' ? '已作废' : status === 'draft' ? '草稿' : status || '—';
  return <span className="text-gray-500 dark:text-slate-400">{label}</span>;
}

function formatInvoicePeriod(row: FinanceInvoiceRow) {
  if (!row.period_start && !row.period_end) return '—';
  const fmt = (value: string | null) => (value ? format(parseISO(value), 'MM-dd') : '?');
  return `${fmt(row.period_start)} ~ ${fmt(row.period_end)}`;
}

// 展开的对账子表：金额保持原币种，和 Stripe 发票页逐行核对用。
function InvoiceSubTable({ state }: { state: FinanceInvoiceRow[] | 'loading' | 'error' | undefined }) {
  if (state === undefined || state === 'loading') {
    return (
      <div className="flex items-center gap-2 py-1 text-xs text-gray-500 dark:text-slate-400">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        加载账单...
      </div>
    );
  }
  if (state === 'error') {
    return <div className="py-1 text-xs text-rose-500 dark:text-rose-400">账单载入失败</div>;
  }
  if (state.length === 0) {
    return <div className="py-1 text-xs text-gray-500 dark:text-slate-400">暂无账单数据</div>;
  }

  const headerCurrency = state[0].currency || '';
  return (
    <table className="w-full text-xs">
      <thead className="text-gray-500 dark:text-slate-500">
        <tr>
          <th className="py-1.5 pr-3 text-left font-medium">账期</th>
          <th className="py-1.5 pr-3 text-left font-medium">状态</th>
          <th className="py-1.5 pr-3 text-right font-medium">应付 ({headerCurrency})</th>
          <th className="py-1.5 pr-3 text-right font-medium">实付 ({headerCurrency})</th>
          <th className="py-1.5 pr-3 text-left font-medium">说明</th>
          <th className="py-1.5 text-right font-medium" />
        </tr>
      </thead>
      <tbody className="divide-y divide-gray-100 dark:divide-slate-800/50">
        {state.map(row => {
          const amount = (value: number | null) => {
            if (value === null) return '—';
            const text = value.toFixed(2);
            return row.currency && row.currency !== headerCurrency ? `${row.currency} ${text}` : text;
          };
          return (
            <tr key={row.invoice_id} className={row.status === 'void' ? 'opacity-60' : ''}>
              <td className="whitespace-nowrap py-1.5 pr-3 text-gray-700 dark:text-slate-300" title={row.number || undefined}>
                {formatInvoicePeriod(row)}
              </td>
              <td className="whitespace-nowrap py-1.5 pr-3">{invoiceStatusCell(row.status)}</td>
              <td className="whitespace-nowrap py-1.5 pr-3 text-right tabular-nums text-gray-700 dark:text-slate-300">
                {amount(row.amount_due)}
              </td>
              <td className="whitespace-nowrap py-1.5 pr-3 text-right tabular-nums text-gray-700 dark:text-slate-300">
                {amount(row.amount_paid)}
              </td>
              <td className="max-w-[18rem] truncate py-1.5 pr-3 text-gray-400 dark:text-slate-500" title={row.description || undefined}>
                {row.description || '—'}
              </td>
              <td className="py-1.5 text-right">
                {row.hosted_invoice_url && (
                  <a
                    href={row.hosted_invoice_url}
                    target="_blank"
                    rel="noreferrer"
                    onClick={event => event.stopPropagation()}
                    className="inline-flex rounded p-1 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 dark:text-slate-500 dark:hover:bg-slate-700/70 dark:hover:text-slate-200"
                    title="在 Stripe 查看发票"
                  >
                    <ExternalLink className="h-3.5 w-3.5" />
                  </a>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
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
          className="rounded p-1 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 dark:text-slate-500 dark:hover:bg-slate-700/70 dark:hover:text-slate-200"
          title={hasNote ? '编辑卡片备注' : '添加卡片备注'}
        >
          {hasNote ? <Pencil className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          className="z-50 w-72 rounded-xl border border-gray-200 bg-white p-3 shadow-2xl animate-in fade-in zoom-in-95 dark:border-slate-700 dark:bg-slate-900"
          sideOffset={6}
          align="end"
        >
          <form onSubmit={handleSubmit} className="space-y-3">
            <div>
              <div className="text-sm font-medium text-gray-900 dark:text-slate-100">卡片备注</div>
              <div className="mt-0.5 flex items-center gap-2 text-xs text-gray-500 dark:text-slate-500">
                <span>{formatCardBrand(item.card_brand)}</span>
                <span className="font-mono">{item.card_last4}</span>
                {teamCount > 1 && <span>{teamCount} 个团队</span>}
              </div>
            </div>
            <input
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              maxLength={80}
              autoFocus
              placeholder="例如 主卡 / 备用卡"
              className="w-full rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-sm text-gray-900 placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-indigo-500 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-200 dark:placeholder:text-slate-600"
            />
            {error && <div className="text-xs text-rose-400">{error}</div>}
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="rounded-lg bg-gray-100 px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-200 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
              >
                取消
              </button>
              <button
                type="submit"
                disabled={saving}
                className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-60"
              >
                {saving && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                保存
              </button>
            </div>
          </form>
          <Popover.Arrow className="fill-white dark:fill-slate-700" />
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
    <div className="mt-0.5 flex w-40 items-center justify-end gap-1.5">
      <div className="relative min-w-0 flex-1">
        <div
          className={`flex w-full min-w-0 items-center gap-1 rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400 ${teamCount > 1 ? 'pr-4' : ''}`}
          title={hasNote ? item.card_note : `${formatCardBrand(item.card_brand)} ${item.card_last4}`}
        >
          <CreditCard className="h-3.5 w-3.5 shrink-0" />
          <span className="shrink-0 font-mono">{item.card_last4}</span>
          {hasNote && (
            <span className="min-w-0 truncate text-gray-700 dark:text-slate-300">{item.card_note}</span>
          )}
        </div>
        {teamCount > 1 && (
          <span
            className="absolute -right-1.5 -top-2 z-10 inline-flex h-5 min-w-5 items-center justify-center rounded-full border border-sky-200 bg-sky-50 px-1.5 text-[11px] font-bold leading-none tabular-nums text-sky-700 shadow-sm ring-2 ring-white dark:border-sky-400/35 dark:bg-slate-950/95 dark:text-sky-200 dark:shadow-[0_6px_18px_rgba(14,165,233,0.16)] dark:ring-slate-800"
            title={`${teamCount} 个团队使用此卡`}
          >
            {teamCount}
          </span>
        )}
      </div>
      <CardNoteEditor item={item} onSaveNote={onSaveNote} />
    </div>
  );
}

function TimelineCardSlot({
  row,
  onSaveNote,
}: {
  row: TimelineRow;
  onSaveNote: (item: FinanceCardLike, note: string) => Promise<void>;
}) {
  const { item, groupIndex, groupSize } = row;

  if (groupSize <= 1) {
    return <CardBadge item={item} onSaveNote={onSaveNote} />;
  }

  const isFirst = groupIndex === 0;
  const isLast = groupIndex === groupSize - 1;

  return (
    <div className="relative mt-0.5 flex h-8 w-40 items-center justify-end">
      <span
        className={`absolute left-3 w-px bg-gray-300 dark:bg-slate-600 ${
          isFirst ? 'top-4' : 'top-0'
        } ${isLast ? 'bottom-4' : 'bottom-0'}`}
      />
      {!isFirst && (
        <>
          <span className="absolute left-3 top-1/2 h-px w-8 bg-gray-300 dark:bg-slate-600" />
          <span className="absolute left-10 top-1/2 h-1.5 w-1.5 -translate-y-1/2 rounded-full bg-sky-500 dark:bg-sky-400" />
        </>
      )}
      {isFirst ? (
        <CardBadge item={item} onSaveNote={onSaveNote} />
      ) : (
        <span
          className="h-6 w-32"
          title={`${formatCardBrand(item.card_brand)} ${item.card_last4 || ''}`}
        />
      )}
    </div>
  );
}

export default function Finance() {
  const [overview, setOverview] = useState<FinanceOverview | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(true);
  const [overviewError, setOverviewError] = useState('');
  const [refreshingFx, setRefreshingFx] = useState(false);
  const [fxError, setFxError] = useState('');
  const [activeBillingTab, setActiveBillingTab] = useState<'timeline' | 'cards' | 'details'>('timeline');
  const [timelineSort, setTimelineSort] = useState<'date' | 'card'>('date');
  const [expandedTeamId, setExpandedTeamId] = useState<string | null>(null);
  const [invoicesByTeam, setInvoicesByTeam] = useState<Record<string, FinanceInvoiceRow[] | 'loading' | 'error'>>({});

  useEffect(() => {
    let cancelled = false;

    setOverviewLoading(true);
    getFinanceOverview()
      .then(res => {
        if (cancelled) return;
        setOverview(res);
        setOverviewError('');
      })
      .catch(error => {
        if (!cancelled) setOverviewError(error.message || 'Failed to load overview');
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
      setOverviewError((error as Error).message || 'Failed to update settings');
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
      setFxError((error as Error).message || 'Failed to refresh FX rates');
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

  const timelineRows = useMemo<TimelineRow[]>(() => {
    if (!overview) return [];
    if (timelineSort === 'date') {
      return sortedTimeline.map(item => ({ item, groupIndex: 0, groupSize: 1 }));
    }

    const groups = new Map<string, { items: FinanceTimelineItem[]; firstDate: number; cardLast4: string; cardBrand: string }>();
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
    }).flatMap(group =>
      group.items.map((item, index) => ({
        item,
        groupIndex: index,
        groupSize: group.items.length,
      })),
    );
  }, [overview, sortedTimeline, timelineSort]);

  return (
    <div className="mx-auto max-w-full space-y-6 p-4 animate-in fade-in duration-500 sm:space-y-8 sm:p-8">
      {/* Header with controls */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <h1 className="mb-1 flex items-center gap-2 whitespace-nowrap text-2xl font-bold text-gray-900 dark:text-slate-100">
            <Wallet className="h-6 w-6 shrink-0 text-blue-400" />
            财务总览
          </h1>
          <p className="text-gray-500 dark:text-slate-400 text-sm">
            查看团队预计成本、Credit 和续费计划。
          </p>
        </div>
        <div className="grid w-full grid-cols-2 gap-3 sm:flex sm:w-auto sm:items-center">
          <div>
            <label className="block text-xs font-medium text-gray-600 dark:text-slate-400 mb-1.5">
              基准币种
            </label>
            {overviewLoading ? (
              <div className="h-9 w-24 animate-pulse rounded bg-gray-200 dark:bg-slate-800" />
            ) : (
              <select
                value={overview?.base_currency || 'USD'}
                onChange={(e) => handleBaseCurrencyChange(e.target.value)}
                className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-800 focus:border-indigo-500 focus:outline-none dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
              >
                {BASE_CURRENCIES.map(curr => (
                  <option key={curr} value={curr}>{curr}</option>
                ))}
              </select>
            )}
          </div>

          <button
            onClick={handleRefreshFx}
            disabled={refreshingFx}
            className="col-span-2 row-start-2 mt-0 flex items-center justify-center gap-2 rounded-lg bg-indigo-600 px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-700 disabled:bg-indigo-600 disabled:opacity-50 sm:col-auto sm:row-auto sm:mt-6"
          >
            {refreshingFx ? (
              <>
                <div className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
                刷新中...
              </>
            ) : (
              <>
                <TrendingUp className="w-4 h-4" />
                刷新汇率
              </>
            )}
          </button>

          <div>
            <label className="block text-xs font-medium text-gray-600 dark:text-slate-400 mb-1.5">
              汇率更新
            </label>
            {overviewLoading ? (
              <div className="flex h-9 items-center rounded bg-gray-200 px-3 text-xs text-gray-500 dark:bg-slate-800 dark:text-slate-400">
                加载中...
              </div>
            ) : (
              <div className="flex h-9 items-center whitespace-nowrap rounded bg-gray-100 px-3 text-xs text-gray-700 dark:bg-slate-800 dark:text-slate-300">
                {overview?.fx_updated_at
                  ? formatDateSafe(overview.fx_updated_at, 'MM-dd HH:mm')
                  : '内置静态汇率'}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Error messages */}
      {overviewError && (
        <div className="p-4 bg-rose-500/10 border border-rose-500/30 rounded-lg text-rose-400 text-sm">
          {overviewError}
        </div>
      )}
      {fxError && (
        <div className="p-4 bg-rose-500/10 border border-rose-500/30 rounded-lg text-rose-400 text-sm">
          刷新汇率失败: {fxError}
        </div>
      )}

      {/* Alerts */}
      {overview && overview.alerts.length > 0 && (
        <div className="space-y-2">
          {overview.alerts.map((alert, i) => {
            let icon = <AlertTriangle className="w-4 h-4" />;
            let bgColor = 'bg-amber-500/10 border-amber-500/30 text-amber-400';

            if (alert.type === 'discount_expiring') {
              icon = <Clock className="w-4 h-4" />;
              bgColor = 'bg-blue-500/10 border-blue-500/30 text-blue-400';
            } else if (alert.type === 'token_expired') {
              icon = <KeyRound className="w-4 h-4" />;
              bgColor = 'bg-rose-500/10 border-rose-500/30 text-rose-400';
            } else if (alert.type === 'subscription_expired') {
              icon = <Clock className="w-4 h-4" />;
              bgColor = 'bg-rose-500/10 border-rose-500/30 text-rose-400';
            }

            return (
              <div
                key={i}
                className={`flex items-start gap-3 p-3 border rounded-lg ${bgColor} text-sm`}
              >
                {icon}
                <div>
                  <div className="font-medium">{alert.team_name}</div>
                  <div className="text-xs opacity-90">{alert.detail}</div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Stats Cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900 dark:shadow-none">
          <div className="mb-2 text-xs font-medium text-gray-500 dark:text-slate-400">月预计支出</div>
          {overviewLoading ? (
            <div className="h-8 animate-pulse rounded bg-gray-200 dark:bg-slate-800" />
          ) : (
            <>
              <div className="text-2xl font-bold text-gray-900 dark:text-slate-100 sm:text-3xl">
                {formatBaseAmount(overview?.monthly_total_base, overview?.base_currency || 'USD')}
              </div>
              {overview?.excluded_teams_count ? (
                <div className="mt-1.5 text-xs text-gray-500 dark:text-slate-400">
                  {overview.excluded_teams_count} 个团队未计入
                </div>
              ) : null}
              {overview?.last_paid_total_base != null && (
                <div className="mt-1.5 text-xs text-gray-500 dark:text-slate-400">
                  上期实付合计 ≈ {overview.base_currency} {overview.last_paid_total_base.toFixed(2)}
                </div>
              )}
            </>
          )}
        </div>

        <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900 dark:shadow-none">
          <div className="mb-2 text-xs font-medium text-gray-500 dark:text-slate-400">折扣共省</div>
          {overviewLoading ? (
            <div className="h-8 animate-pulse rounded bg-gray-200 dark:bg-slate-800" />
          ) : (
            <div className="text-2xl font-bold text-emerald-600 dark:text-emerald-400 sm:text-3xl">
              {formatBaseAmount(overview?.discount_total_base, overview?.base_currency || 'USD')}
            </div>
          )}
        </div>

        <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900 dark:shadow-none">
          <div className="mb-2 text-xs font-medium text-gray-500 dark:text-slate-400">30天内续费</div>
          {overviewLoading ? (
            <div className="h-8 animate-pulse rounded bg-gray-200 dark:bg-slate-800" />
          ) : (
            <div className="text-2xl font-bold text-gray-900 dark:text-slate-100 sm:text-3xl">
              {renewalCountNext30}
            </div>
          )}
        </div>

        <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900 dark:shadow-none">
          <div className="mb-2 text-xs font-medium text-gray-500 dark:text-slate-400">预警数量</div>
          {overviewLoading ? (
            <div className="h-8 animate-pulse rounded bg-gray-200 dark:bg-slate-800" />
          ) : (
            <div className="text-2xl font-bold text-rose-600 dark:text-rose-400 sm:text-3xl">
              {overview?.alerts.length || 0}
            </div>
          )}
        </div>
      </div>

      {/* Cost trend */}
      <CostTrendChart />

      {/* Renewal Timeline / Cards */}
      <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900 dark:shadow-none sm:p-6">
        <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
          <div className="grid grid-cols-3 rounded-lg border border-gray-200 bg-gray-100/80 p-1 dark:border-slate-800 dark:bg-slate-950/60 sm:inline-flex">
            <button
              type="button"
              onClick={() => setActiveBillingTab('timeline')}
              className={`whitespace-nowrap rounded-md px-2 py-1.5 text-sm font-medium transition-colors sm:px-3 ${
                activeBillingTab === 'timeline'
                  ? 'bg-white text-gray-900 shadow-sm dark:bg-slate-800 dark:text-slate-100 dark:shadow-none'
                  : 'text-gray-500 hover:text-gray-900 dark:text-slate-400 dark:hover:text-slate-200'
              }`}
            >
              续费时间线
            </button>
            <button
              type="button"
              onClick={() => setActiveBillingTab('cards')}
              className={`whitespace-nowrap rounded-md px-2 py-1.5 text-sm font-medium transition-colors sm:px-3 ${
                activeBillingTab === 'cards'
                  ? 'bg-white text-gray-900 shadow-sm dark:bg-slate-800 dark:text-slate-100 dark:shadow-none'
                  : 'text-gray-500 hover:text-gray-900 dark:text-slate-400 dark:hover:text-slate-200'
              }`}
            >
              卡片
            </button>
            <button
              type="button"
              onClick={() => setActiveBillingTab('details')}
              className={`whitespace-nowrap rounded-md px-2 py-1.5 text-sm font-medium transition-colors sm:px-3 ${
                activeBillingTab === 'details'
                  ? 'bg-white text-gray-900 shadow-sm dark:bg-slate-800 dark:text-slate-100 dark:shadow-none'
                  : 'text-gray-500 hover:text-gray-900 dark:text-slate-400 dark:hover:text-slate-200'
              }`}
            >
              明细
            </button>
          </div>
          {activeBillingTab === 'timeline' && (
            <div className="grid grid-cols-2 rounded-lg border border-gray-200 bg-gray-100/80 p-1 dark:border-slate-800 dark:bg-slate-950/60 sm:inline-flex">
              <button
                type="button"
                onClick={() => setTimelineSort('date')}
                className={`inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-md px-2 py-1.5 text-sm font-medium transition-colors sm:px-3 ${
                  timelineSort === 'date'
                    ? 'bg-white text-gray-900 shadow-sm dark:bg-slate-800 dark:text-slate-100 dark:shadow-none'
                    : 'text-gray-500 hover:text-gray-900 dark:text-slate-400 dark:hover:text-slate-200'
                }`}
              >
                <Clock className="h-3.5 w-3.5" />
                到期时间
              </button>
              <button
                type="button"
                onClick={() => setTimelineSort('card')}
                className={`inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-md px-2 py-1.5 text-sm font-medium transition-colors sm:px-3 ${
                  timelineSort === 'card'
                    ? 'bg-white text-gray-900 shadow-sm dark:bg-slate-800 dark:text-slate-100 dark:shadow-none'
                    : 'text-gray-500 hover:text-gray-900 dark:text-slate-400 dark:hover:text-slate-200'
                }`}
              >
                <CreditCard className="h-3.5 w-3.5" />
                按卡片
              </button>
            </div>
          )}
        </div>

        {activeBillingTab === 'timeline' && (
          <>
            {overviewLoading ? (
              <div className="space-y-3">
                {[1, 2, 3].map(i => (
                  <div key={i} className="h-12 animate-pulse rounded bg-gray-200 dark:bg-slate-800" />
                ))}
              </div>
            ) : !overview || overview.timeline.length === 0 ? (
              <div className="py-8 text-center text-gray-500 dark:text-slate-400">暂无续费计划</div>
            ) : (
              <div className="space-y-3 overflow-x-auto pb-1">
                {timelineRows
                  .map((row) => {
                    const { item } = row;
                    const daysUntil = differenceInCalendarDays(parseISO(item.date), new Date());
                    const badgeBg = timelineDaysBadgeClass(daysUntil);
                    const rowKey = `${item.team_id}:${item.date}:${timelineCardKey(item)}`;

                    return (
                      <div
                        key={rowKey}
                        className="rounded-lg border border-gray-100 bg-gray-50/80 p-3 text-sm dark:border-transparent dark:bg-slate-800/50 sm:grid sm:min-w-[54rem] sm:grid-cols-[5rem_4.25rem_minmax(0,1fr)_8.5rem_10rem_4.5rem] sm:items-center sm:gap-4"
                      >
                        <div className="flex items-center gap-3 sm:contents">
                          <div className="text-gray-500 dark:text-slate-400">
                            {formatDateSafe(item.date, 'MM-dd')}
                          </div>
                          <span className={`rounded px-2.5 py-1 text-xs font-medium whitespace-nowrap ${badgeBg}`}>
                            {timelineDaysLabel(daysUntil)}
                          </span>
                        </div>
                        <div className="mt-3 min-w-0 sm:mt-0">
                          <div className="font-medium text-gray-900 dark:text-slate-200">{item.team_name}</div>
                          {item.owner_email && (
                            <div className="mt-1 flex items-center gap-1.5 text-xs text-gray-500 dark:text-slate-500">
                              <Mail className="h-3.5 w-3.5 shrink-0" />
                              <span className="break-all">{item.owner_email}</span>
                            </div>
                          )}
                        </div>
                        <div className="mt-3 flex items-end justify-between gap-3 border-t border-gray-200/70 pt-3 dark:border-slate-700/70 sm:contents">
                          <div className="text-left sm:justify-self-end sm:text-right">
                            <div className="font-medium text-gray-900 dark:text-slate-100">
                              {item.amount_native !== null
                                ? `${item.currency} ${item.amount_native.toFixed(2)}`
                                : `${item.currency} —`}
                            </div>
                            {item.amount_base !== null && (
                              <div className="text-xs text-gray-500 dark:text-slate-400">
                                ≈ {overview!.base_currency} {item.amount_base.toFixed(2)}
                              </div>
                            )}
                          </div>
                          <div className="justify-self-end">
                            <TimelineCardSlot row={row} onSaveNote={handleCardNoteSave} />
                          </div>
                          <div className="hidden justify-self-end sm:block">
                            {item.will_renew === 0 && (
                              <span className="rounded bg-gray-100 px-2.5 py-1 text-xs font-medium text-gray-600 dark:bg-gray-700 dark:text-gray-300">
                                不续费
                              </span>
                            )}
                          </div>
                        </div>
                        {item.will_renew === 0 && (
                          <span className="mt-2 inline-flex rounded bg-gray-100 px-2.5 py-1 text-xs font-medium text-gray-600 dark:bg-gray-700 dark:text-gray-300 sm:hidden">
                            不续费
                          </span>
                        )}
                      </div>
                    );
                  })}
              </div>
            )}
          </>
        )}

        {activeBillingTab === 'cards' && (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[48rem] text-left text-sm text-gray-700 dark:text-slate-300">
              <thead className="border-b border-gray-200 text-xs text-gray-500 dark:border-slate-800 dark:text-slate-500">
                <tr>
                  <th className="px-3 py-2 font-medium">卡片</th>
                  <th className="px-3 py-2 font-medium">备注</th>
                  <th className="px-3 py-2 font-medium text-right">绑定团队</th>
                  <th className="px-3 py-2 font-medium text-right">预计月支出</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-slate-800/60">
                {overviewLoading ? (
                  <tr>
                    <td colSpan={4} className="px-3 py-8 text-center text-gray-500 dark:text-slate-400">加载中...</td>
                  </tr>
                ) : cardSummaries.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="px-3 py-8 text-center text-gray-500 dark:text-slate-400">暂无卡片</td>
                  </tr>
                ) : (
                  cardSummaries.map(card => (
                    <tr key={card.card_key || `${card.card_brand}:${card.card_last4}`} className="hover:bg-gray-50 dark:hover:bg-slate-800/30">
                      <td className="px-3 py-3">
                        <div className="flex items-center gap-2">
                          <CreditCard className="h-4 w-4 text-gray-400 dark:text-slate-500" />
                          <span className="font-mono text-gray-900 dark:text-slate-100">{card.card_last4}</span>
                          <span className="rounded border border-gray-200 bg-gray-50 px-2 py-1 text-xs font-medium text-gray-600 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-300">
                            {formatCardBrand(card.card_brand)}
                          </span>
                        </div>
                      </td>
                      <td className="px-3 py-3">
                        <div className="flex min-w-0 items-center gap-2">
                          <span className={`min-w-0 truncate ${card.card_note ? 'text-gray-800 dark:text-slate-200' : 'text-gray-400 dark:text-slate-500'}`}>
                            {card.card_note || '-'}
                          </span>
                          <CardNoteEditor item={card} onSaveNote={handleCardNoteSave} />
                        </div>
                      </td>
                      <td className="px-3 py-3 text-right">
                        <span className="font-medium text-gray-900 dark:text-slate-100" title={card.team_names.join(', ')}>
                          {card.team_count}
                        </span>
                      </td>
                      <td className="px-3 py-3 text-right font-medium text-gray-900 dark:text-slate-100">
                        {formatBaseAmount(card.monthly_total_base, overview?.base_currency || 'USD')}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        )}

        {activeBillingTab === 'details' && (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[56rem] text-left text-sm text-gray-700 dark:text-slate-300">
              <thead className="border-b border-gray-200 text-xs text-gray-500 dark:border-slate-800 dark:text-slate-500">
                <tr>
                  <th className="px-4 py-2 font-medium">Team</th>
                  <th className="px-4 py-2 font-medium">席位</th>
                  <th className="px-4 py-2 font-medium text-right">计费</th>
                  <th className="px-4 py-2 font-medium text-right">预计月费</th>
                  <th className="px-4 py-2 font-medium text-right">上期实付</th>
                  <th className="px-4 py-2 font-medium text-right">Credit 余额</th>
                  <th className="px-4 py-2 font-medium">到期日</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-slate-800/50">
                {overviewLoading ? (
                  <tr><td colSpan={7} className="px-4 py-8 text-center text-gray-500 dark:text-slate-400">加载中...</td></tr>
                ) : !overview || overview.teams.length === 0 ? (
                  <tr><td colSpan={7} className="px-4 py-8 text-center text-gray-500 dark:text-slate-400">暂无团队</td></tr>
                ) : (
                  overview.teams.map((team) => {
                    const sym = team.billing_symbol || team.billing_currency;
                    let daysBadge = 'bg-gray-100 text-gray-600 dark:bg-slate-800 dark:text-slate-300';
                    if (team.days_left !== null) {
                      if (team.days_left <= 7) daysBadge = 'bg-rose-50 text-rose-700 dark:bg-rose-500/20 dark:text-rose-300';
                      else if (team.days_left <= 14) daysBadge = 'bg-amber-50 text-amber-700 dark:bg-amber-500/20 dark:text-amber-300';
                    }
                    return (
                      <Fragment key={team.team_id}>
                      <tr
                        onClick={() => handleToggleInvoices(team.team_id)}
                        className={`cursor-pointer transition-colors hover:bg-gray-50 dark:hover:bg-slate-800/30 ${
                          team.status === 'token_expired' ? 'bg-rose-500/5' : ''
                        }`}
                      >
                        <td className="px-4 py-3.5">
                          <div className="flex items-center gap-2.5">
                            <span
                              className={`h-2 w-2 shrink-0 rounded-full ${
                                team.subscription_status === 'expired'
                                  ? 'bg-red-500 shadow-[0_0_6px_rgba(239,68,68,0.6)]'
                                  : team.status === 'active'
                                  ? 'bg-emerald-500 shadow-[0_0_6px_rgba(16,185,129,0.6)]'
                                  : team.status === 'token_expired'
                                    ? 'bg-rose-500 shadow-[0_0_6px_rgba(244,63,94,0.6)]'
                                    : 'bg-gray-400 dark:bg-slate-600'
                              }`}
                              title={team.status}
                            />
                            <div className="min-w-0">
                              <div className="font-medium text-gray-900 dark:text-slate-100">{team.name}</div>
                              {team.remark && (
                                <div className="mt-0.5 text-xs text-gray-500 dark:text-slate-500">{team.remark}</div>
                              )}
                              <div className={`mt-1 inline-flex rounded px-1.5 py-0.5 text-[11px] font-medium ${
                                team.subscription_status === 'expired'
                                  ? 'bg-red-100 text-red-700 dark:bg-red-500/20 dark:text-red-300'
                                  : team.subscription_status === 'stale'
                                    ? 'bg-gray-100 text-gray-500 dark:bg-slate-800 dark:text-slate-400'
                                  : team.subscription_status === 'nonrenewing'
                                    ? 'bg-amber-100 text-amber-700 dark:bg-amber-500/20 dark:text-amber-300'
                                    : 'bg-emerald-100 text-emerald-700 dark:bg-emerald-500/20 dark:text-emerald-300'
                              }`}>
                                {team.subscription_status === 'expired'
                                  ? '已到期'
                                  : team.subscription_status === 'stale'
                                    ? '数据未同步'
                                  : team.subscription_status === 'nonrenewing' ? '到期不续费' : '正常续费'}
                              </div>
                            </div>
                          </div>
                        </td>
                        <td className="px-4 py-3.5">
                          <div className="whitespace-nowrap">
                            <span className="text-xs text-gray-500 dark:text-slate-400">ChatGPT </span>
                            <span className="font-medium text-gray-900 dark:text-slate-100">{team.chatgpt_in_use}</span>
                            <span className="text-gray-400 dark:text-slate-500">/{team.seats_entitled}</span>
                          </div>
                          <div className="mt-1">
                            <span
                              className={`inline-flex items-center gap-1 whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] font-medium ${
                                team.is_codex_enabled
                                  ? 'bg-purple-100 text-purple-600 dark:bg-purple-500/20 dark:text-purple-400'
                                  : 'bg-gray-100 text-gray-500 dark:bg-slate-800 dark:text-slate-500'
                              }`}
                            >
                              <Zap size={10} />
                              Codex {team.is_codex_enabled ? 'ON' : 'OFF'}
                              {team.codex_count > 0 ? ` · ${team.codex_count}` : ''}
                            </span>
                          </div>
                        </td>
                        <td className="px-4 py-3.5 text-right">
                          <div className="whitespace-nowrap text-gray-900 dark:text-slate-100">
                            {sym} {team.price_per_seat}
                            <span className="text-gray-400 dark:text-slate-500"> × {team.seats_entitled} 席</span>
                          </div>
                          {team.discount_amount > 0 && (
                            <div className="mt-0.5 whitespace-nowrap text-xs text-emerald-600 dark:text-emerald-400">
                              折扣 −{sym} {team.discount_amount.toFixed(2)}
                            </div>
                          )}
                        </td>
                        <td className="px-4 py-3.5 text-right">
                          <div className="whitespace-nowrap font-medium text-gray-900 dark:text-slate-100">
                            {team.monthly_total_base !== null
                              ? `≈ ${overview!.base_currency} ${team.monthly_total_base.toFixed(2)}`
                              : team.monthly_total_native !== null
                                ? `${sym} ${team.monthly_total_native.toFixed(2)}`
                                : `${sym} —`}
                          </div>
                          {team.billing_period === null ? (
                            <div className="mt-0.5 whitespace-nowrap text-xs text-gray-500 dark:text-slate-400">
                              计费周期未知
                            </div>
                          ) : team.monthly_total_native === null ? (
                            <div className="mt-0.5 whitespace-nowrap text-xs text-gray-500 dark:text-slate-400">
                              年付，月费暂不计算
                            </div>
                          ) : team.monthly_total_base !== null && (
                            <div className="mt-0.5 whitespace-nowrap text-xs text-gray-400 dark:text-slate-500">
                              {sym} {team.monthly_total_native.toFixed(2)}
                            </div>
                          )}
                        </td>
                        <td className="px-4 py-3.5 text-right">
                          <LatestInvoiceCell
                            team={team}
                            baseCurrency={overview!.base_currency}
                            expanded={expandedTeamId === team.team_id}
                          />
                        </td>
                        <td className="px-4 py-3.5 text-right font-mono text-gray-500 dark:text-slate-400">
                          {team.balance ?? '—'}
                        </td>
                        <td className="px-4 py-3.5">
                          {team.active_until ? (
                            <div className="flex items-center gap-2 whitespace-nowrap">
                              <span className="text-gray-900 dark:text-slate-100">
                                {formatDateSafe(team.active_until, 'MM-dd')}
                              </span>
                              {team.days_left !== null && (
                                <span className={`rounded px-2 py-0.5 text-xs font-medium ${daysBadge}`}>
                                  {team.days_left > 0 ? `${team.days_left}天` : '已过期'}
                                </span>
                              )}
                            </div>
                          ) : (
                            <span className="text-gray-400 dark:text-slate-500">-</span>
                          )}
                        </td>
                      </tr>
                      {expandedTeamId === team.team_id && (
                        <tr className="bg-gray-50/60 dark:bg-slate-900/40">
                          <td colSpan={7} className="px-4 py-3">
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
      </div>
    </div>
  );
}
