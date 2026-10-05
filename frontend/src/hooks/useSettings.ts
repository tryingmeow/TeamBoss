import { useState, useEffect, useCallback } from 'react';
import type { Settings } from '../types';
import { fetchSettings, updateSettings } from '../api/client';

const DEFAULT_SETTINGS: Settings = {
  sync_interval_minutes: 15,
  api_concurrency: 4,
  expiry_kick_mode: 'delay_hours',
  expiry_kick_delay_hours: 0,
  skip_overage_confirmation: false,
};

// 多处（Settings 弹窗、AddMemberDialog 的超额确认弹窗）各自 useSettings()，
// 但都要看到同一份设置：状态提到模块级，任何一个实例 load/save 之后广播给
// 其余订阅者，而不是各拉各的、互相看不到对方刚存的值。
let shared: Settings = DEFAULT_SETTINGS;
const listeners = new Set<(next: Settings) => void>();
// 每张车卡都挂着一个 AddMemberDialog，首屏几十个实例只该打一次 /api/settings。
let loaded = false;
let inflight: Promise<void> | null = null;
let savedRevision = 0;
const savedFieldRevisions: Partial<Record<keyof Settings, number>> = {};

function broadcast(next: Settings) {
  shared = next;
  listeners.forEach((listener) => listener(next));
}

async function fetchShared(): Promise<void> {
  const revision = savedRevision;
  try {
    const raw = await fetchSettings();
    const savedSinceRequest: Partial<Settings> = {};
    for (const key of Object.keys(savedFieldRevisions) as Array<keyof Settings>) {
      if ((savedFieldRevisions[key] ?? 0) > revision) {
        Object.assign(savedSinceRequest, { [key]: shared[key] });
      }
    }
    broadcast({
      sync_interval_minutes: Number(raw?.sync_interval_minutes?.value ?? 15),
      api_concurrency: Number(raw?.api_concurrency?.value ?? 4),
      expiry_kick_mode:
        raw?.expiry_kick_mode?.value === 'day_end' || raw?.expiry_kick_mode?.value === 'day_start'
          ? 'day_end'
          : 'delay_hours',
      expiry_kick_delay_hours: Number(raw?.expiry_kick_delay_hours?.value ?? 0),
      skip_overage_confirmation: raw?.skip_overage_confirmation?.value === 'true',
      ...savedSinceRequest,
    });
    loaded = true;
  } catch {
    // keep defaults
  }
}

export function useSettings() {
  const [settings, setSettings] = useState<Settings>(shared);

  useEffect(() => {
    listeners.add(setSettings);
    setSettings(shared);
    return () => {
      listeners.delete(setSettings);
    };
  }, []);

  const load = useCallback(() => {
    inflight ??= fetchShared().finally(() => {
      inflight = null;
    });
    return inflight;
  }, []);

  const save = useCallback(async (data: Partial<Settings>) => {
    await updateSettings(data);
    savedRevision += 1;
    for (const key of Object.keys(data) as Array<keyof Settings>) {
      savedFieldRevisions[key] = savedRevision;
    }
    loaded = true;
    broadcast({ ...shared, ...data });
  }, []);

  useEffect(() => {
    if (!loaded) void load();
  }, [load]);

  return { settings, load, save };
}
