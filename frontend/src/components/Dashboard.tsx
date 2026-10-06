import { useMemo } from 'react';
import { AlertCircle, Plus, SearchX } from 'lucide-react';
import type { Team, ShowToast } from '../types';
import TeamCard from './TeamCard';
import type { SortDirection, SortKey } from './DashboardSortControl';
import { activeChatGptSeats, chatgptPaidSeats } from '../lib/seatCapacity';
import { BUTTON } from './ui';

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
  onAddTeam: () => void;
}

const GRID = 'grid grid-cols-1 gap-4 lg:grid-cols-2 xl:grid-cols-3 min-[1800px]:grid-cols-4';

function renewalTimestamp(team: Team): number | null {
  if (!team.active_until) return null;
  const value = new Date(team.active_until).getTime();
  return Number.isNaN(value) ? null : value;
}

function idleRate(team: Team): number | null {
  const entitled = chatgptPaidSeats(team);
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
    <div className="animate-pulse rounded-2xl border border-gray-200 bg-white p-5 dark:border-ink-800 dark:bg-ink-900">
      <div className="mb-3 flex items-center gap-2">
        <div className="size-2.5 rounded-full bg-gray-200 dark:bg-ink-800" />
        <div className="h-4 w-32 rounded bg-gray-200 dark:bg-ink-800" />
      </div>
      <div className="mb-5 h-3 w-48 rounded bg-gray-100 dark:bg-ink-800/70" />
      <div className="mb-5 h-16 rounded-xl bg-gray-100 dark:bg-ink-800/70" />
      <div className="grid grid-cols-2 gap-3">
        <div className="h-3 rounded bg-gray-100 dark:bg-ink-800/70" />
        <div className="h-3 rounded bg-gray-100 dark:bg-ink-800/70" />
        <div className="h-3 rounded bg-gray-100 dark:bg-ink-800/70" />
        <div className="h-3 rounded bg-gray-100 dark:bg-ink-800/70" />
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
  onAddTeam,
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
      <div className={GRID} aria-busy="true">
        {Array.from({ length: 6 }).map((_, i) => (
          <SkeletonCard key={i} />
        ))}
      </div>
    );
  }

  if (error && filtered.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 rounded-2xl border border-dashed border-gray-300 px-6 py-20 text-center dark:border-ink-800">
        <AlertCircle size={36} className="text-red-500 dark:text-red-400" />
        <p className="text-base font-semibold text-gray-900 dark:text-gray-100">加载 Team 列表失败</p>
        <p className="max-w-md text-sm text-gray-500 dark:text-ink-400">{error}</p>
        <button type="button" onClick={() => window.location.reload()} className={`${BUTTON.primary} mt-2`}>
          重试
        </button>
      </div>
    );
  }

  if (filtered.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 rounded-2xl border border-dashed border-gray-300 px-6 py-20 text-center dark:border-ink-800">
        {search ? (
          <>
            <SearchX size={32} className="text-gray-300 dark:text-ink-600" />
            <p className="text-sm text-gray-500 dark:text-ink-400">没有匹配“{search.trim()}”的 Team</p>
          </>
        ) : (
          <>
            <p className="text-base font-semibold text-gray-900 dark:text-gray-100">还没有接入 Team</p>
            <p className="max-w-md text-sm text-gray-500 dark:text-ink-400">
              用 ChatGPT Team 的 Owner 账号 Session 接入第一个 Team，之后就能在这里管理席位、成员和续费。
            </p>
            <button type="button" onClick={onAddTeam} className={`${BUTTON.primary} mt-2`}>
              <Plus size={16} /> 添加 Team
            </button>
          </>
        )}
      </div>
    );
  }

  return (
    <div className={GRID}>
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
