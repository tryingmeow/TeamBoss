import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { differenceInCalendarDays, format, isValid, parseISO } from 'date-fns';
import {
  ArrowRight,
  CalendarClock,
  Clock,
  CreditCard,
  Loader2,
  PieChart,
  RefreshCw,
  Server,
  Shield,
  UserRound,
  Users,
  Wallet,
} from 'lucide-react';
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
import { formatMoney } from '../../lib/money';
import { formatSeatTypeLabel, isCodexSeat } from '../../lib/seatType';
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

function StatCard({
  title,
  value,
  detail,
  icon: Icon,
  iconClassName,
  children,
}: {
  title: string;
  value: ReactNode;
  detail: ReactNode;
  icon: typeof Shield;
  iconClassName?: string;
  children?: ReactNode;
}) {
  return (
    <div className={cn(CARD, 'flex min-w-0 flex-col p-4 sm:p-5')}>
      <div className="flex items-center justify-between gap-2">
        <p className="truncate text-xs font-medium text-gray-500 sm:text-sm dark:text-ink-400">{title}</p>
        <Icon className={cn('size-4 shrink-0 text-gray-400 dark:text-ink-500', iconClassName)} />
      </div>
      <p className="mt-2 break-words text-lg font-semibold tabular-nums tracking-tight text-gray-900 sm:text-2xl dark:text-gray-50">
        {value}
      </p>
      {children}
      <p className="mt-1 text-xs leading-snug text-gray-500 dark:text-ink-400">{detail}</p>
    </div>
  );
}

function daysUntil(value: string) {
  const date = parseISO(value);
  return isValid(date) ? differenceInCalendarDays(date, new Date()) : null;
}

/**
 * 列表不再截断在 7 天内，因此必须区分「已逾期」和「还剩几天」：
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
}: {
  title: string;
  count: number;
  description: string;
  to: string;
}) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-gray-200 px-4 py-4 sm:px-5 dark:border-ink-800">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <h2 className="text-base font-semibold text-gray-900 dark:text-gray-50">{title}</h2>
          <span className={cn(PILL, TONE.neutral, 'tabular-nums')}>{count}</span>
        </div>
        <p className="mt-1 text-xs text-gray-500 dark:text-ink-400">{description}</p>
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

function sameCurrency(a: string | null | undefined, b: string | null | undefined) {
  return Boolean(a && b && a.trim().toUpperCase() === b.trim().toUpperCase());
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

  const seatUtilization = data && data.total_gpt_seats > 0
    ? Math.round((data.inuse_gpt / data.total_gpt_seats) * 100)
    : 0;

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

  const idleCost = useMemo(() => {
    if (!data || !finance) return 0;
    const freeSeatsByTeam = new Map(data.teams.map((team) => [team.team_id, team.free_gpt_seats]));
    return finance.teams.reduce((total, team) => {
      if (team.status !== 'active' || team.will_renew !== 1 || team.monthly_total_base === null || team.seats_entitled <= 0) {
        return total;
      }
      const freeSeats = freeSeatsByTeam.get(team.team_id) ?? 0;
      return total + ((team.monthly_total_base / team.seats_entitled) * freeSeats);
    }, 0);
  }, [data, finance]);

  // 列表是全量，但金额只取七天内：把跨度不同的账单日加在一起得到的总额没有对应的支出行为。
  const renewalAmountNext7 = renewalItems.reduce(
    (total, row) => (row.daysUntil >= 0 && row.daysUntil <= 7 ? total + (row.item.amount_base ?? 0) : total),
    0,
  );
  const baseCurrency = finance?.base_currency || 'USD';

  return (
    <PageShell
      title="数据概览"
      description="席位用量、续费支出和即将到期的成员，一页看完。"
      actions={(
        <button
          type="button"
          onClick={() => fetchUsage(true)}
          disabled={refreshing || loading}
          className={BUTTON.secondary}
        >
          <RefreshCw className={cn('size-4', refreshing && 'animate-spin')} />
          {refreshing ? '刷新中…' : '刷新'}
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
                detail={`${data.total_team - data.active_team} 个非活跃 Team`}
                icon={Server}
              />
              <StatCard
                title="ChatGPT 席位"
                value={`${seatUtilization}%`}
                detail={`已用 ${data.inuse_gpt} / ${data.total_gpt_seats} · 剩余 ${data.free_gpt_seats}`}
                icon={PieChart}
                iconClassName="text-blue-500 dark:text-blue-400"
              >
                <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-gray-100 dark:bg-ink-800">
                  <div className="h-full rounded-full bg-blue-500" style={{ width: `${Math.min(seatUtilization, 100)}%` }} />
                </div>
              </StatCard>
              <StatCard
                title="Codex 席位"
                value={data.inuse_codex}
                detail="使用中 · 按用量计费"
                icon={Users}
                iconClassName="text-purple-500 dark:text-purple-400"
              />
              <StatCard
                title="有空位的 Team"
                value={data.free_team_count}
                detail={`待接受邀请 ${data.pending_gpt_invites}`}
                icon={Shield}
              />
              <StatCard
                title="月预计支出"
                value={formatMoney(finance?.monthly_total_base, baseCurrency)}
                detail={finance?.excluded_teams_count ? `未计入 ${finance.excluded_teams_count} 个异常 Team` : '只计入活跃且自动续费的 Team'}
                icon={Wallet}
              />
              <StatCard
                title="闲置席位折算"
                value={finance ? `约 ${formatMoney(idleCost, baseCurrency)}` : '—'}
                detail={`${data.free_gpt_seats} 个空闲席位按月费分摊`}
                icon={CreditCard}
              />
              <StatCard
                title="近期续费 Team"
                value={renewalItems.length}
                detail={<>7 天内预计支出 <span className="whitespace-nowrap">{formatMoney(renewalAmountNext7, baseCurrency)}</span></>}
                icon={CalendarClock}
              />
              <StatCard
                title="近期到期成员"
                value={expiringMembers.length}
                detail="已设置到期时间的成员"
                icon={UserRound}
              />
            </div>

            <div className="grid grid-cols-1 gap-4 sm:gap-6 lg:grid-cols-2">
              <section className={cn(CARD, 'min-w-0 overflow-hidden')}>
                <SectionHeader
                  title="近期续费 Team"
                  count={renewalItems.length}
                  description={`自动续费的 Team，7 天内预计支出 ${formatMoney(renewalAmountNext7, baseCurrency)}`}
                  to="/admin/finance"
                />
                <div className="max-h-[36rem] divide-y divide-gray-100 overflow-y-auto dark:divide-ink-800">
                  {renewalItems.length === 0 ? (
                    <EmptyState title="暂无待续费的 Team" hint="Team 同步到账单后，会按续费日排在这里。" />
                  ) : renewalItems.map(({ item, daysUntil: remaining }) => {
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
                                {item.card_note && <span className="truncate" title={item.card_note}>· {item.card_note}</span>}
                              </>
                            ) : (
                              <span>未绑定卡片</span>
                            )}
                          </div>
                        </div>
                        <div className="shrink-0 text-right">
                          <div className="whitespace-nowrap text-sm font-semibold tabular-nums text-gray-900 dark:text-gray-100">
                            {formatMoney(item.amount_base, baseCurrency)}
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
              </section>

              <section className={cn(CARD, 'min-w-0 overflow-hidden')}>
                <SectionHeader
                  title="近期到期成员"
                  count={expiringMembers.length}
                  description="按服务到期时间排序，最近的在前"
                  to="/admin/users"
                />
                <div className="max-h-[36rem] divide-y divide-gray-100 overflow-y-auto dark:divide-ink-800">
                  {expiringMembers.length === 0 ? (
                    <EmptyState title="暂无设置了到期时间的成员" hint="给成员设置到期时间后，会按先后排在这里。" />
                  ) : expiringMembers.map((member) => {
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
                          <span className={cn(PILL, isCodexSeat(member.seat_type) ? TONE.codex : TONE.info)}>
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
