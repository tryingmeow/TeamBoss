import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { differenceInCalendarDays, format, isValid, parseISO } from 'date-fns';
import {
  ArrowRight,
  CalendarClock,
  Clock,
  CreditCard,
  Crown,
  Info,
  Loader2,
  RefreshCw,
  Server,
  Shield,
  UserRound,
  Users,
  Wallet,
  Zap,
} from 'lucide-react';
import * as Tooltip from '@radix-ui/react-tooltip';
import {
  fetchAllMembers,
  fetchResourceUsage,
  getFinanceOverview,
  type FinanceOverview,
  type FinanceTimelineItem,
  type UsageData,
} from '../../api/client';
import PageShell from '../../components/PageShell';
import { BUTTON, CARD, PILL, TONE } from '../../components/ui';
import { formatMoney, sameCurrency } from '../../lib/money';
import { SEAT_STYLE, formatSeatTypeLabel, seatStyle } from '../../lib/seatType';
import type { SeatType } from '../../types';
import { cn } from '../../lib/utils';

interface DashboardMember {
  status: string;
  team_id: string;
  team_name: string;
  email: string;
  name?: string | null;
  system_display_name?: string | null;
  seat_type?: string | null;
  expiry?: {
    expires_at?: string | null;
  };
}

interface ExpiringMember extends DashboardMember {
  expiresAt: string;
  daysUntil: number;
}

const STAT_TONES = {
  emerald: {
    iconBg: 'bg-emerald-100/80 text-emerald-700 ring-1 ring-inset ring-emerald-500/25 dark:bg-emerald-500/20 dark:text-emerald-300 dark:ring-emerald-400/30',
    surface: 'border-emerald-200/80 bg-gradient-to-b from-emerald-50/60 to-white dark:border-emerald-500/25 dark:from-emerald-500/[0.08] dark:to-ink-900',
    borderHover: 'hover:border-emerald-400/80 dark:hover:border-emerald-400/50',
  },
  blue: {
    iconBg: 'bg-blue-100/80 text-blue-700 ring-1 ring-inset ring-blue-500/25 dark:bg-blue-500/20 dark:text-blue-300 dark:ring-blue-400/30',
    surface: 'border-blue-200/80 bg-gradient-to-b from-blue-50/60 to-white dark:border-blue-500/25 dark:from-blue-500/[0.08] dark:to-ink-900',
    borderHover: 'hover:border-blue-400/80 dark:hover:border-blue-400/50',
  },
  purple: {
    iconBg: 'bg-purple-100/80 text-purple-700 ring-1 ring-inset ring-purple-500/25 dark:bg-purple-500/20 dark:text-purple-300 dark:ring-purple-400/30',
    surface: 'border-purple-200/80 bg-gradient-to-b from-purple-50/60 to-white dark:border-purple-500/25 dark:from-purple-500/[0.08] dark:to-ink-900',
    borderHover: 'hover:border-purple-400/80 dark:hover:border-purple-400/50',
  },
  sky: {
    iconBg: 'bg-sky-100/80 text-sky-700 ring-1 ring-inset ring-sky-500/25 dark:bg-sky-500/20 dark:text-sky-300 dark:ring-sky-400/30',
    surface: 'border-sky-200/80 bg-gradient-to-b from-sky-50/60 to-white dark:border-sky-500/25 dark:from-sky-500/[0.08] dark:to-ink-900',
    borderHover: 'hover:border-sky-400/80 dark:hover:border-sky-400/50',
  },
  indigo: {
    iconBg: 'bg-indigo-100/80 text-indigo-700 ring-1 ring-inset ring-indigo-500/25 dark:bg-indigo-500/20 dark:text-indigo-300 dark:ring-indigo-400/30',
    surface: 'border-indigo-200/80 bg-gradient-to-b from-indigo-50/60 to-white dark:border-indigo-500/25 dark:from-indigo-500/[0.08] dark:to-ink-900',
    borderHover: 'hover:border-indigo-400/80 dark:hover:border-indigo-400/50',
  },
  amber: {
    iconBg: 'bg-amber-100/80 text-amber-700 ring-1 ring-inset ring-amber-500/25 dark:bg-amber-500/20 dark:text-amber-300 dark:ring-amber-400/30',
    surface: 'border-amber-200/80 bg-gradient-to-b from-amber-50/60 to-white dark:border-amber-500/25 dark:from-amber-500/[0.08] dark:to-ink-900',
    borderHover: 'hover:border-amber-400/80 dark:hover:border-amber-400/50',
  },
  rose: {
    iconBg: 'bg-rose-100/80 text-rose-700 ring-1 ring-inset ring-rose-500/25 dark:bg-rose-500/20 dark:text-rose-300 dark:ring-rose-400/30',
    surface: 'border-rose-200/80 bg-gradient-to-b from-rose-50/60 to-white dark:border-rose-500/25 dark:from-rose-500/[0.08] dark:to-ink-900',
    borderHover: 'hover:border-rose-400/80 dark:hover:border-rose-400/50',
  },
  orange: {
    iconBg: 'bg-orange-100/80 text-orange-700 ring-1 ring-inset ring-orange-500/25 dark:bg-orange-500/20 dark:text-orange-300 dark:ring-orange-400/30',
    surface: 'border-orange-200/80 bg-gradient-to-b from-orange-50/60 to-white dark:border-orange-500/25 dark:from-orange-500/[0.08] dark:to-ink-900',
    borderHover: 'hover:border-orange-400/80 dark:hover:border-orange-400/50',
  },
  pink: {
    iconBg: 'bg-pink-100/80 text-pink-700 ring-1 ring-inset ring-pink-500/25 dark:bg-pink-500/20 dark:text-pink-300 dark:ring-pink-400/30',
    surface: 'border-pink-200/80 bg-gradient-to-b from-pink-50/60 to-white dark:border-pink-500/25 dark:from-pink-500/[0.08] dark:to-ink-900',
    borderHover: 'hover:border-pink-400/80 dark:hover:border-pink-400/50',
  },
} as const;

const TOOLTIP_CLASS =
  'z-50 max-w-xs rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-700 shadow-lg dark:border-ink-700 dark:bg-ink-800 dark:text-gray-200';

type StatToneKey = keyof typeof STAT_TONES;

function StatCard({
  title,
  value,
  detail,
  tooltip,
  icon: Icon,
  toneKey = 'blue',
  seat,
  children,
}: {
  title: ReactNode;
  value: ReactNode;
  detail?: ReactNode;
  tooltip?: ReactNode;
  icon: typeof Shield;
  toneKey?: StatToneKey;
  /** Tiles about one seat type wear that seat's color (same as the Team cards). */
  seat?: SeatType;
  children?: ReactNode;
}) {
  const resolvedToneKey: StatToneKey = seat
    ? seat === 'default'
      ? 'blue'
      : seat === 'usage_based'
        ? 'purple'
        : 'pink'
    : toneKey;
  const tone = STAT_TONES[resolvedToneKey];

  return (
    <div className={cn(CARD, 'group relative flex min-w-0 flex-col justify-between p-4 transition-all duration-200 hover:shadow-md sm:p-5', tone.surface, tone.borderHover)}>
      <div>
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-1.5">
            <div className="truncate text-xs font-medium text-gray-500 sm:text-sm dark:text-ink-400">{title}</div>
            {tooltip && (
              <Tooltip.Root>
                <Tooltip.Trigger asChild>
                  <button
                    type="button"
                    className="shrink-0 text-gray-400 transition-colors hover:text-gray-600 dark:text-ink-500 dark:hover:text-ink-300"
                    aria-label="说明"
                  >
                    <Info className="size-3.5" />
                  </button>
                </Tooltip.Trigger>
                <Tooltip.Portal>
                  <Tooltip.Content className={TOOLTIP_CLASS} sideOffset={4}>
                    {tooltip}
                  </Tooltip.Content>
                </Tooltip.Portal>
              </Tooltip.Root>
            )}
          </div>
          <div className={cn('flex size-8 shrink-0 items-center justify-center rounded-lg transition-transform duration-200 group-hover:scale-105 sm:size-9', tone.iconBg)}>
            <Icon className="size-4 sm:size-4.5" />
          </div>
        </div>
        <p className="mt-2.5 break-words text-xl font-bold tabular-nums tracking-tight text-gray-900 sm:text-2xl dark:text-gray-50">
          {value}
        </p>
        {children}
      </div>
      {detail ? <div className="mt-2 text-xs leading-snug text-gray-500 dark:text-ink-400">{detail}</div> : null}
    </div>
  );
}

/** The two lists show what needs attention soon; anything further out stays on the linked page. */
const WINDOW_DAYS = 30;

function daysUntil(value: string) {
  const date = parseISO(value);
  return isValid(date) ? differenceInCalendarDays(date, new Date()) : null;
}

/**
 * 列表里有已逾期的条目，必须区分「已逾期」和「还剩几天」：
 * 否则逾期一个月的条目会和明天到期的共用同一个红角标，看上去只是今天要处理。
 * 角标分档与财务总览页保持一致。
 */
function dueLabel(days: number) {
  if (days < 0) return `已逾期 ${Math.abs(days)} 天`;
  if (days === 0) return '今天';
  if (days === 1) return '明天';
  return `${days} 天后`;
}

function dueBadgeClass(days: number) {
  if (days < 0) return 'bg-red-600 text-white dark:bg-red-600 dark:text-white';
  if (days <= 1) return TONE.danger;
  if (days <= 7) return TONE.warning;
  return TONE.neutral;
}

function DueCell({ value, days }: { value: string; days: number }) {
  return (
    <div className="w-[5.5rem] shrink-0">
      <div className="text-xs tabular-nums text-gray-500 dark:text-ink-400">{format(parseISO(value), 'MM-dd')}</div>
      <span className={cn(PILL, 'mt-1', dueBadgeClass(days))}>{dueLabel(days)}</span>
    </div>
  );
}

function SectionHeader({
  title,
  count,
  description,
  to,
  icon: Icon,
  iconBg,
  iconColor,
}: {
  title: string;
  count: number;
  description: string;
  to: string;
  icon?: typeof Shield;
  iconBg?: string;
  iconColor?: string;
}) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-gray-200 px-4 py-4 sm:px-5 dark:border-ink-800">
      <div className="flex items-start gap-3 min-w-0">
        {Icon && (
          <div className={cn('flex size-9 shrink-0 items-center justify-center rounded-lg mt-0.5', iconBg)}>
            <Icon className={cn('size-4.5', iconColor)} />
          </div>
        )}
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h2 className="text-base font-semibold text-gray-900 dark:text-gray-50">{title}</h2>
            <span className={cn(PILL, TONE.neutral, 'tabular-nums')}>{count}</span>
          </div>
          <p className="mt-1 text-xs text-gray-500 dark:text-ink-400">{description}</p>
        </div>
      </div>
      <Link
        to={to}
        className="inline-flex min-h-9 shrink-0 items-center gap-1 whitespace-nowrap text-sm font-medium text-blue-600 transition-colors hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300"
      >
        查看全部
        <ArrowRight className="size-3.5" />
      </Link>
    </div>
  );
}

function EmptyState({ title, hint }: { title: string; hint: string }) {
  return (
    <div className="px-6 py-14 text-center">
      <p className="text-sm font-medium text-gray-700 dark:text-ink-200">{title}</p>
      <p className="mt-1 text-xs text-gray-500 dark:text-ink-400">{hint}</p>
    </div>
  );
}

function MoreLink({ to, children }: { to: string; children: ReactNode }) {
  return (
    <Link
      to={to}
      className="flex min-h-11 items-center justify-between gap-3 border-t border-gray-200 px-4 py-2.5 text-xs text-gray-500 transition-colors hover:bg-gray-50 hover:text-gray-900 sm:px-5 dark:border-ink-800 dark:text-ink-400 dark:hover:bg-ink-800/40 dark:hover:text-gray-100"
    >
      <span className="min-w-0">{children}</span>
      <ArrowRight className="size-3.5 shrink-0" />
    </Link>
  );
}

export default function TeamManagement() {
  const [data, setData] = useState<UsageData | null>(null);
  const [finance, setFinance] = useState<FinanceOverview | null>(null);
  const [members, setMembers] = useState<DashboardMember[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');

  const fetchUsage = async (refresh = false) => {
    if (refresh) setRefreshing(true);

    const usageRequest = fetchResourceUsage(refresh);
    const memberRequest = refresh
      ? usageRequest.then(() => fetchAllMembers({ status: 'joined' }))
      : fetchAllMembers({ status: 'joined' });

    const [usageResult, financeResult, memberResult] = await Promise.allSettled([
      usageRequest,
      getFinanceOverview(),
      memberRequest,
    ]);

    const failures: string[] = [];
    if (usageResult.status === 'fulfilled') {
      setData(usageResult.value);
      if (usageResult.value.errors.length > 0) {
        const names = usageResult.value.errors
          .map((item) => item.team_name || item.team_id)
          .join('、');
        failures.push(
          `${usageResult.value.errors.length} 个 Team 刷新失败（${names}）`
        );
      }
    } else {
      failures.push('席位数据加载失败');
    }

    if (financeResult.status === 'fulfilled') {
      setFinance(financeResult.value);
    } else {
      failures.push('财务数据加载失败');
    }

    if (memberResult.status === 'fulfilled') {
      setMembers(memberResult.value.items as DashboardMember[]);
    } else {
      failures.push('成员数据加载失败');
    }

    setError(failures.length > 0 ? `${failures.join('；')}。` : '');
    setLoading(false);
    setRefreshing(false);
  };

  useEffect(() => {
    fetchUsage();
  }, []);

  const [seatView, setSeatView] = useState<'default' | 'prolite'>('default');
  const [idleCostView, setIdleCostView] = useState<'all' | 'default' | 'prolite'>('all');

  const seatUtilization = data && data.total_gpt_seats > 0
    ? Math.round((data.inuse_gpt / data.total_gpt_seats) * 100)
    : 0;

  const totalPremiumPaid = useMemo(() => {
    if (!data) return 0;
    return data.teams.reduce((sum, team) => sum + (team.status === 'active' ? (team.premium_seats_paid ?? 0) : 0), 0);
  }, [data]);

  const inusePremium = useMemo(() => {
    if (!data) return 0;
    if (typeof data.inuse_premium === 'number') return data.inuse_premium;
    return data.teams.reduce((sum, team) => sum + (team.status === 'active' ? (team.inuse_premium ?? 0) : 0), 0);
  }, [data]);

  const freePremiumSeats = Math.max(0, totalPremiumPaid - inusePremium);
  const premiumUtilization = totalPremiumPaid > 0
    ? Math.round((inusePremium / totalPremiumPaid) * 100)
    : (inusePremium > 0 ? 100 : 0);

  const renewalItems = useMemo(() => {
    if (!finance) return [];
    return finance.timeline
      .map((item) => ({ item, daysUntil: daysUntil(item.date) }))
      .filter((row): row is { item: FinanceTimelineItem; daysUntil: number } => (
        row.daysUntil !== null
        && row.item.will_renew === 1
      ))
      .sort((a, b) => {
        if (a.daysUntil !== b.daysUntil) return a.daysUntil - b.daysUntil;
        return a.item.team_name.localeCompare(b.item.team_name);
      });
  }, [finance]);

  const expiringMembers = useMemo<ExpiringMember[]>(() => members
    .flatMap((member) => {
      const expiresAt = member.expiry?.expires_at;
      if (!expiresAt) return [];
      const remaining = daysUntil(expiresAt);
      if (remaining === null) return [];
      return [{ ...member, expiresAt, daysUntil: remaining }];
    })
    .sort((a, b) => {
      const dateDiff = parseISO(a.expiresAt).getTime() - parseISO(b.expiresAt).getTime();
      if (dateDiff !== 0) return dateDiff;
      return a.email.localeCompare(b.email);
    }), [members]);

  const upcomingRenewals = renewalItems.filter((row) => row.daysUntil <= WINDOW_DAYS);
  const laterRenewalCount = renewalItems.length - upcomingRenewals.length;
  const upcomingMembers = expiringMembers.filter((member) => member.daysUntil <= WINDOW_DAYS);
  const laterMemberCount = expiringMembers.length - upcomingMembers.length;

  const idleCost = useMemo(() => {
    const result = { default: 0, prolite: 0, all: 0 };
    if (!data || !finance) return result;
    const usageByTeam = new Map(data.teams.map((team) => [team.team_id, team]));
    finance.teams.forEach((team) => {
      if (team.status !== 'active' || team.will_renew !== 1) return;
      const usage = usageByTeam.get(team.team_id);
      if (!usage) return;
      const defaultPrice = team.price_per_seat_base;
      const premiumPrice = team.premium_price_per_seat_base;
      if (typeof defaultPrice === 'number' && Number.isFinite(defaultPrice)) {
        result.default += defaultPrice * usage.free_gpt_seats;
      }
      if (typeof premiumPrice === 'number' && Number.isFinite(premiumPrice)) {
        result.prolite += premiumPrice * (usage.free_premium_seats ?? 0);
      }
    });
    result.all = result.default + result.prolite;
    return result;
  }, [data, finance]);

  // 金额只取七天内：把跨度不同的账单日加在一起得到的总额没有对应的支出行为。
  const renewalAmountNext7 = renewalItems.reduce(
    (total, row) => (row.daysUntil >= 0 && row.daysUntil <= 7 ? total + (row.item.amount_base ?? 0) : total),
    0,
  );
  const baseCurrency = finance?.base_currency || 'USD';
  // 月预计支出里已含的真实 Premium：有 Team 不是基准币种就是换算出来的，带 ≈（和财务页同一写法）。
  const premiumIncludedConverted = (finance?.teams ?? []).some((team) => (
    team.status === 'active'
    && team.subscription_status === 'renewing'
    && team.premium_price_source === 'upstream'
    && (team.premium_seats_paid ?? 0) > 0
    && team.monthly_total_native !== null
    && !sameCurrency(team.billing_currency, baseCurrency)
  ));

  return (
    <PageShell
      title="数据概览"
      actions={(
        <button
          type="button"
          onClick={() => fetchUsage(true)}
          disabled={refreshing || loading}
          className={BUTTON.secondary}
        >
          <RefreshCw className={cn('size-4', refreshing && 'animate-spin')} />
          刷新
        </button>
      )}
    >
      <div className="space-y-6">
        {error && (
          <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-300">
            {error}
          </div>
        )}

        {loading ? (
          <div className="flex justify-center py-20">
            <Loader2 className="size-7 animate-spin text-blue-500" />
          </div>
        ) : data ? (
          <>
            <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
              <StatCard
                title="活跃 Team"
                value={`${data.active_team} / ${data.total_team}`}
                detail={data.total_team - data.active_team > 0 ? (
                  <span className="font-medium text-amber-600 dark:text-amber-400">
                    {data.total_team - data.active_team} 个非活跃 Team
                  </span>
                ) : null}
                tooltip="正常同步的活跃 Team 数 / 总接入数"
                icon={Server}
                toneKey="emerald"
              />
              <StatCard
                title={(
                  <div className="flex items-center gap-1 rounded-md bg-gray-100/90 p-0.5 dark:bg-ink-800/90">
                    <button
                      type="button"
                      onClick={() => setSeatView('default')}
                      className={cn(
                        'rounded px-1 py-0.5 text-[10px] font-medium transition-all sm:px-1.5 sm:text-xs',
                        seatView === 'default'
                          ? 'bg-blue-600 text-white shadow-xs'
                          : 'text-gray-600 hover:text-gray-900 dark:text-ink-300 dark:hover:text-gray-100'
                      )}
                    >
                      ChatGPT
                    </button>
                    <button
                      type="button"
                      onClick={() => setSeatView('prolite')}
                      className={cn(
                        'inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 text-xs font-medium transition-all',
                        seatView === 'prolite'
                          ? 'bg-pink-600 text-white shadow-xs'
                          : 'text-gray-600 hover:text-gray-900 dark:text-ink-300 dark:hover:text-gray-100'
                      )}
                    >
                      Premium
                      <span className="text-[10px] opacity-80">Beta</span>
                    </button>
                  </div>
                )}
                value={seatView === 'default'
                  ? `${seatUtilization}%`
                  : totalPremiumPaid > 0
                    ? `${premiumUtilization}%`
                    : inusePremium
                }
                detail={seatView === 'default'
                  ? `已用 ${data.inuse_gpt} / ${data.total_gpt_seats} · 剩余 ${data.free_gpt_seats}`
                  : totalPremiumPaid > 0
                    ? `已用 ${inusePremium} / ${totalPremiumPaid} · 剩余 ${freePremiumSeats}`
                    : `使用中 ${inusePremium} · 预付席位 0`
                }
                tooltip={seatView === 'default' ? 'ChatGPT 席位使用率与可用空位' : 'Premium 席位使用率与可用空位'}
                icon={seatView === 'default' ? Users : Crown}
                seat={seatView}
              >
                <div className={cn('mt-2 h-1.5 overflow-hidden rounded-full', SEAT_STYLE[seatView].track)}>
                  <div
                    className={cn('h-full rounded-full transition-all duration-300', SEAT_STYLE[seatView].solid)}
                    style={{
                      width: seatView === 'default'
                        ? `${Math.min(seatUtilization, 100)}%`
                        : `${totalPremiumPaid > 0 ? Math.min(premiumUtilization, 100) : (inusePremium > 0 ? 100 : 0)}%`
                    }}
                  />
                </div>
              </StatCard>
              <StatCard
                title="Codex 席位"
                value={data.inuse_codex}
                tooltip="使用中 · 按用量计费，不占月租席位"
                icon={Zap}
                seat="usage_based"
              />
              <StatCard
                title="有空位的 Team"
                value={data.free_team_count}
                detail={data.pending_gpt_invites > 0 ? `待接受邀请 ${data.pending_gpt_invites}` : null}
                tooltip="有空闲席位可加入的 Team 数量"
                icon={Shield}
                toneKey="sky"
              />
              <StatCard
                title="月预计支出"
                value={formatMoney(finance?.monthly_total_base, baseCurrency)}
                detail={
                  finance?.excluded_teams_count ? (
                    <span className="font-medium text-amber-600 dark:text-amber-400">
                      未计入 {finance.excluded_teams_count} 个费用不明或异常的 Team
                    </span>
                  ) : (finance?.premium_monthly_base_total ?? 0) > 0 ? (
                    <span className="block">
                      含 <span className={SEAT_STYLE.prolite.text}>Premium</span>{' '}
                      {premiumIncludedConverted ? '≈ ' : ''}{formatMoney(finance?.premium_monthly_base_total, baseCurrency)}
                    </span>
                  ) : null
                }
                tooltip={
                  (finance?.premium_monthly_base_total ?? 0) > 0
                    ? '只计入活跃且自动续费的 Team（已剔除异常及已取消续费的 Team），年付按月均，含已读到单价的 Premium 席位；不含税'
                    : '只计入活跃且自动续费的 Team（已剔除异常及已取消续费的 Team），年付按月均；不含税'
                }
                icon={Wallet}
                toneKey="indigo"
              />
              <StatCard
                title="闲置席位折算"
                value={finance ? `约 ${formatMoney(idleCost[idleCostView], baseCurrency)}` : '—'}
                icon={CreditCard}
                toneKey="amber"
              >
                <div role="group" aria-label="闲置席位折算类型" className="mt-2 flex w-fit items-center gap-0.5 rounded-md bg-gray-100/90 p-0.5 sm:gap-1 dark:bg-ink-800/90">
                  {([
                    ['all', 'All', 'bg-amber-600 text-white shadow-xs'],
                    ['default', 'ChatGPT', 'bg-blue-600 text-white shadow-xs'],
                    ['prolite', 'Premium', 'bg-pink-600 text-white shadow-xs'],
                  ] as const).map(([view, label, selectedClass]) => (
                    <button
                      key={view}
                      type="button"
                      aria-pressed={idleCostView === view}
                      onClick={() => setIdleCostView(view)}
                      className={cn(
                        'rounded px-1 py-0.5 text-[10px] font-medium transition-all sm:px-1.5 sm:text-xs',
                        idleCostView === view
                          ? selectedClass
                          : 'text-gray-600 hover:text-gray-900 dark:text-ink-300 dark:hover:text-gray-100',
                      )}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </StatCard>
              <StatCard
                title="近期续费 Team"
                value={upcomingRenewals.length}
                detail={renewalAmountNext7 > 0 ? <>7 天内预计支出 <span className="whitespace-nowrap">{formatMoney(renewalAmountNext7, baseCurrency)}</span></> : null}
                tooltip={`未来 ${WINDOW_DAYS} 天内自动续费的 Team`}
                icon={CalendarClock}
                toneKey="rose"
              />
              <StatCard
                title="近期到期成员"
                value={upcomingMembers.length}
                tooltip={`${WINDOW_DAYS} 天内到期（含已过期）的成员`}
                icon={UserRound}
                toneKey="orange"
              />
            </div>

            <div className="grid grid-cols-1 gap-4 sm:gap-6 lg:grid-cols-2">
              <section className={cn(CARD, 'flex min-w-0 flex-col overflow-hidden')}>
                <SectionHeader
                  title="近期续费 Team"
                  count={upcomingRenewals.length}
                  description={`${WINDOW_DAYS} 天内自动续费的 Team（含已过续费日），7 天内预计支出 ${formatMoney(renewalAmountNext7, baseCurrency)}`}
                  to="/admin/finance"
                  icon={CalendarClock}
                  iconBg={STAT_TONES.rose.iconBg}
                  iconColor="text-rose-600 dark:text-rose-400"
                />
                <div className="max-h-[36rem] flex-1 divide-y divide-gray-100 overflow-y-auto dark:divide-ink-800">
                  {upcomingRenewals.length === 0 ? (
                    renewalItems.length === 0
                      ? <EmptyState title="暂无待续费的 Team" hint="Team 同步到账单后，会按续费日排在这里。" />
                      : <EmptyState title={`${WINDOW_DAYS} 天内没有要续费的 Team`} hint="更晚的续费在财务页查看。" />
                  ) : upcomingRenewals.map(({ item, daysUntil: remaining }) => {
                    const converted = !sameCurrency(item.currency, baseCurrency);
                    return (
                      <div key={`${item.team_id}:${item.date}`} className="flex items-start gap-3 px-4 py-3.5 transition-colors hover:bg-gray-50 sm:px-5 dark:hover:bg-ink-800/40">
                        <DueCell value={item.date} days={remaining} />
                        <div className="min-w-0 flex-1">
                          <div className="truncate text-sm font-medium text-gray-900 dark:text-gray-100" title={item.team_name}>{item.team_name}</div>
                          <div className="mt-1 flex min-w-0 items-center gap-1.5 text-xs text-gray-500 dark:text-ink-400">
                            <CreditCard className="size-3.5 shrink-0" />
                            {item.card_last4 ? (
                              <>
                                {item.card_brand && <span className="shrink-0">{item.card_brand.toUpperCase()}</span>}
                                <span className="shrink-0 font-mono">•••• {item.card_last4}</span>
                              </>
                            ) : (
                              <span>未绑定卡片</span>
                            )}
                          </div>
                          {/* Own line: squeezed inline on a phone it left only a dangling "·". */}
                          {item.card_last4 && item.card_note && (
                            <div className="mt-0.5 truncate pl-5 text-xs text-gray-500 dark:text-ink-400" title={item.card_note}>
                              {item.card_note}
                            </div>
                          )}
                        </div>
                        <div className="shrink-0 text-right">
                          <div className="whitespace-nowrap text-sm font-semibold tabular-nums text-gray-900 dark:text-gray-100">
                            {formatMoney(item.amount_base, baseCurrency)}
                            {/* 这次续费要扣的钱：年付 Team 是一整年的。 */}
                            {item.billing_period === 'yearly' && item.amount_native !== null && (
                              <span className="font-normal text-gray-500 dark:text-ink-400"> /年</span>
                            )}
                          </div>
                          {item.amount_native !== null && (converted || item.amount_base === null) && (
                            <div className="mt-1 whitespace-nowrap text-xs tabular-nums text-gray-500 dark:text-ink-400">
                              {formatMoney(item.amount_native, item.currency)}
                            </div>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
                {laterRenewalCount > 0 && (
                  <MoreLink to="/admin/finance">另有 {laterRenewalCount} 个 Team 在 {WINDOW_DAYS} 天后续费，到财务页查看</MoreLink>
                )}
              </section>

              <section className={cn(CARD, 'flex min-w-0 flex-col overflow-hidden')}>
                <SectionHeader
                  title="近期到期成员"
                  count={upcomingMembers.length}
                  description={`${WINDOW_DAYS} 天内到期（含已过期）的成员，最近的在前`}
                  to="/admin/users"
                  icon={UserRound}
                  iconBg={STAT_TONES.orange.iconBg}
                  iconColor="text-orange-600 dark:text-orange-400"
                />
                <div className="max-h-[36rem] flex-1 divide-y divide-gray-100 overflow-y-auto dark:divide-ink-800">
                  {upcomingMembers.length === 0 ? (
                    expiringMembers.length === 0
                      ? <EmptyState title="暂无设置了到期时间的成员" hint="给成员设置到期时间后，会按先后排在这里。" />
                      : <EmptyState title={`${WINDOW_DAYS} 天内没有到期的成员`} hint="更晚到期的成员在用户管理页查看。" />
                  ) : upcomingMembers.map((member) => {
                    const displayName = member.system_display_name?.trim() || member.name?.trim() || member.email;
                    const showEmail = displayName.toLowerCase() !== member.email.toLowerCase();
                    return (
                      <div key={`${member.team_id}:${member.email}`} className="flex items-start gap-3 px-4 py-3.5 transition-colors hover:bg-gray-50 sm:px-5 dark:hover:bg-ink-800/40">
                        <DueCell value={member.expiresAt} days={member.daysUntil} />
                        <div className="min-w-0 flex-1">
                          <div className="truncate text-sm font-medium text-gray-900 dark:text-gray-100" title={displayName}>{displayName}</div>
                          {showEmail && (
                            <div className="mt-0.5 truncate text-xs text-gray-500 dark:text-ink-400" title={member.email}>{member.email}</div>
                          )}
                          <div className="mt-1 truncate text-xs text-gray-500 dark:text-ink-400" title={member.team_name}>{member.team_name}</div>
                        </div>
                        <div className="flex shrink-0 flex-col items-end">
                          <span className={cn(PILL, seatStyle(member.seat_type).pill)}>
                            {formatSeatTypeLabel(member.seat_type)}
                          </span>
                          <div className="mt-1.5 flex items-center gap-1 text-xs tabular-nums text-gray-500 dark:text-ink-400">
                            <Clock className="size-3" />
                            {format(parseISO(member.expiresAt), 'HH:mm')}
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
                {laterMemberCount > 0 && (
                  <MoreLink to="/admin/users">另有 {laterMemberCount} 位成员在 {WINDOW_DAYS} 天后到期，到用户管理查看</MoreLink>
                )}
              </section>
            </div>
          </>
        ) : (
          <div className={cn(CARD, 'px-6 py-16 text-center')}>
            <p className="text-sm font-medium text-gray-900 dark:text-gray-100">席位数据加载失败</p>
            <p className="mt-1 text-xs text-gray-500 dark:text-ink-400">检查后端是否在运行，然后点「刷新」重试。</p>
          </div>
        )}
      </div>
    </PageShell>
  );
}
