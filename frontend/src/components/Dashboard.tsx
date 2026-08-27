import { useMemo } from 'react';
import { AlertCircle } from 'lucide-react';
import type { Team, ShowToast } from '../types';
import TeamCard from './TeamCard';
import type { SortDirection, SortKey } from './DashboardSortControl';
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
  sortKey: SortKey;
  sortDirection: SortDirection;
}

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
  sortKey,
  sortDirection,
}: DashboardProps) {
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
      // 用不了的 Team 永远排最前面：会话失效，以及会话还在但 token 已被上游吊销。
      const unusable = (t: Team) =>
        Number(t.status === 'token_expired' || t.auth_state === 'rejected');
      const invalidSessionComparison = unusable(b) - unusable(a);
      if (invalidSessionComparison !== 0) return invalidSessionComparison;

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
    <div className="grid grid-cols-1 gap-4 px-6 pb-6 lg:grid-cols-2 xl:grid-cols-3">
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
  );
}
