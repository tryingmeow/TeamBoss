import { useState, useEffect, useCallback } from 'react';
import type { Settings } from '../types';
import { fetchSettings, updateSettings } from '../api/client';


export function useSettings() {
  const [settings, setSettings] = useState<Settings>({
    sync_interval_minutes: 15,
    api_concurrency: 4,
    expiry_kick_mode: 'delay_hours',
    expiry_kick_delay_hours: 0,
  });

  const load = useCallback(async () => {
    try {
      const raw = await fetchSettings();
      const parsed: Settings = {
        sync_interval_minutes: Number(raw?.sync_interval_minutes?.value ?? 15),
        api_concurrency: Number(raw?.api_concurrency?.value ?? 4),
        expiry_kick_mode:
          raw?.expiry_kick_mode?.value === 'day_end' || raw?.expiry_kick_mode?.value === 'day_start'
            ? 'day_end'
            : 'delay_hours',
        expiry_kick_delay_hours: Number(raw?.expiry_kick_delay_hours?.value ?? 0),
      };
      setSettings(parsed);
    } catch {
      // keep defaults
    }
  }, []);

  const save = useCallback(async (data: Partial<Settings>) => {
    await updateSettings(data);
    setSettings(prev => ({ ...prev, ...data }));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return { settings, load, save };
}
