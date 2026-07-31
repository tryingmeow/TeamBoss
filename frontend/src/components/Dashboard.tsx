import { useMemo, useState } from 'react';
import { ArrowDown, ArrowUp, AlertCircle } from 'lucide-react';
import type { Team, ShowToast } from '../types';
import TeamCard from './TeamCard';
import { activeChatGptSeats } from '../lib/seatCapacity';

interface DashboardProps {
  teams: Team[];
  loading: boolean;
  error: string | null;
  search: string;
  onDelete: (id: string) => void;
  onReimport: (team: Team) => void;
  onTeamSynced: (team: Team) => void;
  onTeamSyncSucceeded: (team: Team) => void;
  syncFailures: Record<string, string>;
  showToast: ShowToast;
}

type SortKey = 'name' | 'renewal' | 'idle';
type SortDirection = 'asc' | 'desc';

const SORT_LABELS: Record<SortKey, string> = {
  name: '名称',
  renewal: '距续费时间',
  idle: 'GPT 空闲率',
};

function renewalTimestamp(team: Team): number | null {
  if (!team.active_until) return null;
  const value = new Date(team.active_until).getTime();
  return Number.isNaN(value) ? null : value;
}

function idleRate(team: Team): number | null {
  const entitled = Number(team.seats_entitled) || 0;
  if (entitled <= 0) return null;
  const freeSeats = Math.max(0, entitled - activeChatGptSeats(team));
  return freeSeats / entitled;
}

function compareNullableNumbers(a: number | null, b: number | null): number {
  if (a === null && b === null) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  return a - b;
}

function SkeletonCard() {
  return (
    <div className="bg-white dark:bg-[#1a1d27] rounded-xl border border-gray-100 dark:border-[#2a2d3a] p-5 animate-pulse shadow-sm">
      <div className="flex items-center gap-3 mb-6">
        <div className="w-3 h-3 rounded-full bg-gray-200 dark:bg-[#2a2d3a]" />
        <div className="h-4 w-24 bg-gray-200 dark:bg-[#2a2d3a] rounded" />
      </div>
      <div className="flex justify-center gap-8 mb-6">
        <div className="w-16 h-16 rounded-full bg-gray-200 dark:bg-[#2a2d3a]" />
        <div className="w-12 h-12 rounded-full bg-gray-200 dark:bg-[#2a2d3a]" />
      </div>
      <div className="space-y-3">
        <div className="h-3 w-full bg-gray-200 dark:bg-[#2a2d3a] rounded" />
        <div className="h-3 w-3/4 bg-gray-200 dark:bg-[#2a2d3a] rounded" />
      </div>
    </div>
  );
}

export default function Dashboard({
  teams,
  loading,
  error,
  search,
  onDelete,
  onReimport,
  onTeamSynced,
  onTeamSyncSucceeded,
  syncFailures,
  showToast,
}: DashboardProps) {
  const [sortKey, setSortKey] = useState<SortKey>('name');
  const [sortDirection, setSortDirection] = useState<SortDirection>('asc');

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    const matched = q ? teams.filter(
      (t) =>
        t.name.toLowerCase().includes(q) ||
        t.owner_email.toLowerCase().includes(q) ||
        (t.card_last4 ?? '').includes(q) ||
        t.billing_currency.toLowerCase().includes(q) ||
        (t.cached_member_emails ?? []).some((e) => e.includes(q))
    ) : teams;

    return [...matched].sort((a, b) => {
      const expiredComparison =
        Number(b.subscription_status === 'expired') -
        Number(a.subscription_status === 'expired');
      if (expiredComparison !== 0) return expiredComparison;

      let comparison: number;
      let keepMissingLast = false;
      if (sortKey === 'renewal') {
        const aValue = renewalTimestamp(a);
        const bValue = renewalTimestamp(b);
        comparison = compareNullableNumbers(aValue, bValue);
        keepMissingLast = aValue === null || bValue === null;
      } else if (sortKey === 'idle') {
        const aValue = idleRate(a);
        const bValue = idleRate(b);
        comparison = compareNullableNumbers(aValue, bValue);
        keepMissingLast = aValue === null || bValue === null;
      } else {
        comparison = a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' });
      }

      if (comparison === 0) {
        comparison = a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' });
      }
      if (keepMissingLast) return comparison;
      return sortDirection === 'asc' ? comparison : -comparison;
    });
  }, [teams, search, sortKey, sortDirection]);

  const directionLabel = sortKey === 'name'
    ? (sortDirection === 'asc' ? 'A → Z' : 'Z → A')
    : sortKey === 'renewal'
      ? (sortDirection === 'asc' ? '近 → 远' : '远 → 近')
      : sortDirection === 'asc'
        ? '低 → 高'
        : '高 → 低';

  if (loading && teams.length === 0) {
    return (
      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-4 p-6">
        {Array.from({ length: 8 }).map((_, i) => (
          <SkeletonCard key={i} />
        ))}
      </div>
    );
  }

  if (error && filtered.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-24 gap-3">
        <AlertCircle size={48} className="text-red-500 dark:text-red-400" />
        <p className="text-lg font-semibold text-gray-900 dark:text-gray-100">加载 Team 列表失败</p>
        <p className="text-sm text-gray-600 dark:text-gray-400">{error}</p>
        <button
          onClick={() => window.location.reload()}
          className="mt-4 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-medium text-sm transition-colors"
        >
          重试
        </button>
      </div>
    );
  }

  if (filtered.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-24 text-gray-400 dark:text-gray-500">
        <p className="text-lg">
          {search ? '没有匹配的 Team' : '暂无 Team，点击右上角添加'}
        </p>
      </div>
    );
  }

  return (
    <div>
      <div className="flex flex-wrap items-center justify-end gap-2 px-6 pb-2">
        <span className="text-xs font-medium text-gray-500 dark:text-gray-400">排序</span>
        <select
          value={sortKey}
          onChange={(event) => setSortKey(event.target.value as SortKey)}
          aria-label="Team 排序字段"
          className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-xs font-medium text-gray-700 outline-none transition focus:border-blue-400 focus:ring-2 focus:ring-blue-500/20 dark:border-[#2a2d3a] dark:bg-[#1a1d27] dark:text-gray-200"
        >
          {Object.entries(SORT_LABELS).map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
        <button
          type="button"
          onClick={() => setSortDirection((current) => current === 'asc' ? 'desc' : 'asc')}
          aria-label={`切换排序方向，当前 ${directionLabel}`}
          title={`当前：${directionLabel}`}
          className="inline-flex min-w-[4.75rem] items-center justify-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 py-2 text-xs font-medium text-gray-700 transition hover:border-blue-300 hover:text-blue-600 dark:border-[#2a2d3a] dark:bg-[#1a1d27] dark:text-gray-300 dark:hover:border-blue-500/50 dark:hover:text-blue-400"
        >
          {sortDirection === 'asc' ? <ArrowUp size={13} /> : <ArrowDown size={13} />}
          {directionLabel}
        </button>
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-4 p-6">
        {filtered.map((team) => (
          <TeamCard
            key={team.id}
            team={team}
            onDelete={onDelete}
            onReimport={onReimport}
            onTeamSynced={onTeamSynced}
            onSyncSucceeded={onTeamSyncSucceeded}
            syncError={syncFailures[team.id]}
            showToast={showToast}
          />
        ))}
      </div>
    </div>
  );
}
