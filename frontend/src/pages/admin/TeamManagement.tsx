import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { differenceInCalendarDays, format, isValid, parseISO } from 'date-fns';
import {
  ArrowRight,
  CalendarClock,
  Clock,
  CreditCard,
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
import { formatSeatTypeLabel, seatTypeBadgeClass } from '../../lib/seatType';

type StatColor = 'indigo' | 'emerald' | 'blue' | 'amber' | 'rose' | 'purple' | 'orange' | 'cyan';

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

const statColorClasses: Record<StatColor, string> = {
  indigo: 'bg-indigo-500/10 text-indigo-500 dark:text-indigo-400',
  emerald: 'bg-emerald-500/10 text-emerald-500 dark:text-emerald-400',
  blue: 'bg-blue-500/10 text-blue-500 dark:text-blue-400',
  amber: 'bg-amber-500/10 text-amber-600 dark:text-amber-400',
  rose: 'bg-rose-500/10 text-rose-500 dark:text-rose-400',
  purple: 'bg-purple-500/10 text-purple-500 dark:text-purple-400',
  orange: 'bg-orange-500/10 text-orange-500 dark:text-orange-400',
  cyan: 'bg-cyan-500/10 text-cyan-500 dark:text-cyan-400',
};

function StatCard({
  title,
  value,
  detail,
  icon: Icon,
  color,
}: {
  title: string;
  value: ReactNode;
  detail: ReactNode;
  icon: typeof Shield;
  color: StatColor;
}) {
  return (
    <div className="flex min-w-0 flex-col items-start gap-3 rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900 dark:shadow-none sm:flex-row sm:items-center sm:gap-4 sm:p-5">
      <div className={`shrink-0 rounded-lg p-2.5 sm:p-3 ${statColorClasses[color]}`}>
        <Icon className="h-5 w-5 sm:h-6 sm:w-6" />
      </div>
      <div className="min-w-0 w-full">
        <p className="truncate text-xs font-medium text-gray-500 dark:text-slate-400 sm:text-sm">{title}</p>
        <p className="mt-0.5 whitespace-nowrap text-xl font-bold tabular-nums text-gray-900 dark:text-slate-100 sm:truncate sm:text-2xl">{value}</p>
        <div className="mt-1 min-h-8 text-xs leading-4 text-gray-400 dark:text-slate-500 sm:min-h-0 sm:truncate">{detail}</div>
      </div>
    </div>
  );
}

function formatMoney(amount: number | null | undefined, currency = 'USD') {
  if (amount === null || amount === undefined) return '—';
  try {
    return new Intl.NumberFormat('zh-CN', {
      style: 'currency',
      currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(amount);
  } catch {
    return `${amount.toFixed(2)} ${currency}`;
  }
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
  return `${days}天后`;
}

function dueBadgeClass(days: number) {
  if (days < 0) return 'bg-rose-600 text-white ring-1 ring-rose-700 dark:bg-rose-600 dark:text-white dark:ring-rose-400/60';
  if (days <= 1) return 'bg-rose-50 text-rose-700 dark:bg-rose-500/20 dark:text-rose-300';
  if (days <= 3) return 'bg-orange-50 text-orange-700 dark:bg-orange-500/20 dark:text-orange-300';
  if (days <= 7) return 'bg-amber-50 text-amber-700 dark:bg-amber-500/20 dark:text-amber-300';
  return 'bg-gray-100 text-gray-600 dark:bg-slate-700 dark:text-slate-300';
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
    <div className="flex items-start justify-between gap-4 border-b border-gray-100 px-5 py-4 dark:border-slate-800">
      <div>
        <div className="flex items-center gap-2">
          <h2 className="font-semibold text-gray-900 dark:text-slate-100">{title}</h2>
          <span className="rounded-full bg-gray-100 px-2 py-0.5 text-xs font-semibold tabular-nums text-gray-600 dark:bg-slate-800 dark:text-slate-300">
            {count}
          </span>
        </div>
        <p className="mt-1 text-xs text-gray-500 dark:text-slate-500">{description}</p>
      </div>
      <Link
        to={to}
        className="inline-flex shrink-0 items-center gap-1 text-xs font-medium text-indigo-600 transition-colors hover:text-indigo-500 dark:text-indigo-400 dark:hover:text-indigo-300"
      >
        查看全部
        <ArrowRight className="h-3.5 w-3.5" />
      </Link>
    </div>
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
          `${usageResult.value.errors.length} 个队伍刷新失败（${names}）`
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
    <div className="mx-auto max-w-7xl space-y-6 px-0 py-4 animate-in fade-in duration-500 sm:p-8">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-slate-100">队伍概览</h1>
        </div>
        <button
          type="button"
          onClick={() => fetchUsage(true)}
          disabled={refreshing || loading}
          className="flex shrink-0 items-center gap-2 rounded-lg border border-gray-300 bg-gray-100 px-4 py-2 text-gray-800 transition-colors hover:bg-gray-200 disabled:opacity-60 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200 dark:hover:bg-slate-700"
        >
          <RefreshCw className={`h-4 w-4 ${refreshing ? 'animate-spin text-indigo-400' : ''}`} />
          {refreshing ? '刷新中...' : '刷新'}
        </button>
      </div>

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300">
          {error}
        </div>
      )}

      {loading ? (
        <div className="flex justify-center py-20">
          <RefreshCw className="h-8 w-8 animate-spin text-indigo-500" />
        </div>
      ) : data ? (
        <>
          <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
            <StatCard
              title="活跃队伍"
              value={`${data.active_team} / ${data.total_team}`}
              detail={`${data.total_team - data.active_team} 个非活跃队伍`}
              icon={Server}
              color="indigo"
            />
            <StatCard
              title="GPT 席位利用率"
              value={`${seatUtilization}%`}
              detail={`${data.inuse_gpt} / ${data.total_gpt_seats} · 剩余 ${data.free_gpt_seats}`}
              icon={PieChart}
              color="blue"
            />
            <StatCard
              title="使用中 Codex"
              value={data.inuse_codex}
              detail="Usage-based 席位"
              icon={Users}
              color="purple"
            />
            <StatCard
              title="可接入队伍"
              value={data.free_team_count}
              detail={`待接受邀请 ${data.pending_gpt_invites}`}
              icon={Shield}
              color="cyan"
            />
            <StatCard
              title="月预计支出"
              value={formatMoney(finance?.monthly_total_base, baseCurrency)}
              detail={finance?.excluded_teams_count ? `未计入 ${finance.excluded_teams_count} 个异常队伍` : '仅计入活跃续费队伍'}
              icon={Wallet}
              color="emerald"
            />
            <StatCard
              title="闲置席位折算"
              value={finance ? `约 ${formatMoney(idleCost, baseCurrency)}` : '—'}
              detail={`${data.free_gpt_seats} 个空闲席位 `}
              icon={CreditCard}
              color="amber"
            />
            <StatCard
              title="近期续费团队"
              value={renewalItems.length}
              detail={`七天内预计支出 ${formatMoney(renewalAmountNext7, baseCurrency)}`}
              icon={CalendarClock}
              color="orange"
            />
            <StatCard
              title="近期到期成员"
              value={expiringMembers.length}
              detail="按服务到期时间排序"
              icon={UserRound}
              color="rose"
            />
          </div>

          <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
            <section className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm dark:border-slate-800 dark:bg-slate-900 dark:shadow-none">
              <SectionHeader
                title="近期续费团队"
                count={renewalItems.length}
                description={`七天内预计支出 ${formatMoney(renewalAmountNext7, baseCurrency)}`}
                to="/admin/finance"
              />
              <div className="max-h-[36rem] divide-y divide-gray-100 overflow-y-auto dark:divide-slate-800/70">
                {renewalItems.length === 0 ? (
                  <div className="px-5 py-12 text-center text-sm text-gray-400 dark:text-slate-500">暂无待续费团队</div>
                ) : renewalItems.map(({ item, daysUntil: remaining }) => (
                  <div key={`${item.team_id}:${item.date}`} className="flex items-start gap-3 px-5 py-4 transition-colors hover:bg-gray-50 dark:hover:bg-slate-800/40">
                    <div className="w-20 shrink-0 sm:w-24">
                      <div className="text-xs tabular-nums text-gray-500 dark:text-slate-400">{format(parseISO(item.date), 'MM-dd')}</div>
                      <span className={`mt-1 inline-flex whitespace-nowrap rounded px-2 py-0.5 text-[11px] font-medium ${dueBadgeClass(remaining)}`}>
                        {dueLabel(remaining)}
                      </span>
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium text-gray-900 dark:text-slate-100">{item.team_name}</div>
                      <div className="mt-1 flex min-w-0 items-center gap-1.5 text-xs text-gray-500 dark:text-slate-500">
                        <CreditCard className="h-3.5 w-3.5 shrink-0" />
                        {item.card_last4 ? (
                          <>
                            <span className="shrink-0">{item.card_brand?.toUpperCase() || 'CARD'}</span>
                            <span className="shrink-0 font-mono">•••• {item.card_last4}</span>
                            {item.card_note && <span className="truncate">· {item.card_note}</span>}
                          </>
                        ) : (
                          <span>未绑定卡片</span>
                        )}
                      </div>
                    </div>
                    <div className="shrink-0 text-right">
                      <div className="text-sm font-semibold tabular-nums text-gray-900 dark:text-slate-100">
                        {formatMoney(item.amount_base, baseCurrency)}
                      </div>
                      <div className="mt-1 text-[11px] tabular-nums text-gray-400 dark:text-slate-500">
                        {item.amount_native !== null
                          ? `${item.currency} ${item.amount_native.toFixed(2)}`
                          : `${item.currency} —`}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </section>

            <section className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm dark:border-slate-800 dark:bg-slate-900 dark:shadow-none">
              <SectionHeader
                title="近期到期成员"
                count={expiringMembers.length}
                description="服务到期时间，最近的排在前面"
                to="/admin/users"
              />
              <div className="max-h-[36rem] divide-y divide-gray-100 overflow-y-auto dark:divide-slate-800/70">
                {expiringMembers.length === 0 ? (
                  <div className="px-5 py-12 text-center text-sm text-gray-400 dark:text-slate-500">暂无到期成员</div>
                ) : expiringMembers.map((member) => {
                  const displayName = member.system_display_name?.trim() || member.name?.trim() || member.email;
                  const showEmail = displayName.toLowerCase() !== member.email.toLowerCase();
                  return (
                    <div key={`${member.team_id}:${member.email}`} className="flex items-start gap-3 px-5 py-4 transition-colors hover:bg-gray-50 dark:hover:bg-slate-800/40">
                      <div className="w-20 shrink-0 sm:w-24">
                        <div className="text-xs tabular-nums text-gray-500 dark:text-slate-400">{format(parseISO(member.expiresAt), 'MM-dd')}</div>
                        <span className={`mt-1 inline-flex whitespace-nowrap rounded px-2 py-0.5 text-[11px] font-medium ${dueBadgeClass(member.daysUntil)}`}>
                          {dueLabel(member.daysUntil)}
                        </span>
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-sm font-medium text-gray-900 dark:text-slate-100">{displayName}</div>
                        {showEmail && <div className="mt-0.5 truncate text-xs text-gray-500 dark:text-slate-500">{member.email}</div>}
                        <div className="mt-1 truncate text-xs text-gray-500 dark:text-slate-400">{member.team_name}</div>
                      </div>
                      <div className="shrink-0 text-right">
                        <span className={`inline-flex rounded-md px-2 py-1 text-[11px] font-medium ${seatTypeBadgeClass(member.seat_type, 'admin')}`}>
                          {formatSeatTypeLabel(member.seat_type)}
                        </span>
                        <div className="mt-1.5 flex items-center justify-end gap-1 text-[11px] tabular-nums text-gray-400 dark:text-slate-500">
                          <Clock className="h-3 w-3" />
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
        <div className="rounded-xl border border-rose-200 bg-rose-50 py-16 text-center text-rose-600 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300">
          加载资源数据失败。
        </div>
      )}
    </div>
  );
}
