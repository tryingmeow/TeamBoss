import { useState, useEffect, useCallback } from 'react';
import type { MembersData } from '../types';
import { fetchTeamMembers } from '../api/client';

export function useMembers(teamId: string | null) {
  const [data, setData] = useState<MembersData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (force = false) => {
    if (!teamId) return;
    setLoading(true);
    setError(null);
    try {
      const result = await fetchTeamMembers(teamId, force);
      setData(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }, [teamId]);

  useEffect(() => {
    if (!teamId) {
      setData(null);
      setError(null);
    }
  }, [teamId]);

  return { data, loading, error, refresh, setData };
}
