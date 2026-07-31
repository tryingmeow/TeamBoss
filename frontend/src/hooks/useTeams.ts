import { useState, useEffect, useCallback, useRef } from 'react';
import type { Team } from '../types';
import { fetchTeams, syncAllTeams } from '../api/client';

export function useTeams(syncInterval: number = 15, autoRefresh = true) {
  const [teams, setTeams] = useState<Team[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [syncFailures, setSyncFailures] = useState<Record<string, string>>({});
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const refresh = useCallback(async (showLoading = false) => {
    if (showLoading) setLoading(true);
    setError(null);
    try {
      const data = await fetchTeams();
      setTeams(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  const manualRefresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await syncAllTeams();
      const failures = result.results.filter((item) => item.status === 'failed');
      setSyncFailures(Object.fromEntries(
        failures.map((item) => [item.team_id, item.error || '同步失败'])
      ));
      const data = await fetchTeams();
      setTeams(data);
      if (failures.length > 0) {
        const names = failures.map((item) => {
          const team = data.find((candidate) => candidate.id === item.team_id);
          if (!team) return item.team_id;
          const displayName = team.remark ? `${team.name}（${team.remark}）` : team.name;
          return team.owner_email ? `${displayName}（${team.owner_email}）` : displayName;
        });
        throw new Error(`${failures.length} 个 Team 刷新失败：${names.join('、')}；其他 Team 已更新`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '刷新失败');
      throw err;
    } finally {
      setLoading(false);
    }
  }, []);

  const clearSyncFailure = useCallback((teamId: string) => {
    setSyncFailures((current) => {
      if (!(teamId in current)) return current;
      const next = { ...current };
      delete next[teamId];
      return next;
    });
  }, []);

  useEffect(() => {
    refresh(true);
  }, [refresh]);

  useEffect(() => {
    if (intervalRef.current) clearInterval(intervalRef.current);
    intervalRef.current = null;
    if (!autoRefresh || syncInterval <= 0) return;

    intervalRef.current = setInterval(() => refresh(false), syncInterval * 60 * 1000);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
      intervalRef.current = null;
    };
  }, [syncInterval, autoRefresh, refresh]);

  return {
    teams,
    loading,
    error,
    syncFailures,
    refresh,
    manualRefresh,
    clearSyncFailure,
    setTeams,
  };
}
