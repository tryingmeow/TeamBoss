import { useState, useEffect, useCallback } from 'react';
import type { Settings } from '../types';
import { fetchSettings, updateSettings } from '../api/client';

const DEFAULT_SETTINGS: Settings = {
  sync_interval_minutes: 15,
  api_concurrency: 4,
  expiry_kick_mode: 'delay_hours',
  expiry_kick_delay_hours: 0,
};

interface SettingsSnapshot {
  settings: Settings;
  /**
   * True only after a GET /api/settings has succeeded. Until then `settings` are the
   * client defaults (0h grace!) plus whatever this tab saved itself, so nothing may
   * present them as the server's values or write them back.
   */
  loaded: boolean;
}

// 多处（Settings 弹窗、到期时间选择器的移出规则）各自 useSettings()，
// 但都要看到同一份设置：状态提到模块级，任何一个实例 load/save 之后广播给
// 其余订阅者，而不是各拉各的、互相看不到对方刚存的值。
let snapshot: SettingsSnapshot = { settings: DEFAULT_SETTINGS, loaded: false };
const listeners = new Set<(next: SettingsSnapshot) => void>();
// 每张车卡都挂着一个 AddMemberDialog，首屏几十个实例只该打一次 /api/settings。
let inflight: Promise<boolean> | null = null;
let savedRevision = 0;
const savedFieldRevisions: Partial<Record<keyof Settings, number>> = {};

function broadcast(next: SettingsSnapshot) {
  snapshot = next;
  listeners.forEach((listener) => listener(next));
}

async function fetchShared(): Promise<boolean> {
  const revision = savedRevision;
  try {
    const raw = await fetchSettings();
    const savedSinceRequest: Partial<Settings> = {};
    for (const key of Object.keys(savedFieldRevisions) as Array<keyof Settings>) {
      if ((savedFieldRevisions[key] ?? 0) > revision) {
        Object.assign(savedSinceRequest, { [key]: snapshot.settings[key] });
      }
    }
    broadcast({
      settings: {
        sync_interval_minutes: Number(raw?.sync_interval_minutes?.value ?? 15),
        api_concurrency: Number(raw?.api_concurrency?.value ?? 4),
        expiry_kick_mode:
          raw?.expiry_kick_mode?.value === 'day_end' || raw?.expiry_kick_mode?.value === 'day_start'
            ? 'day_end'
            : 'delay_hours',
        expiry_kick_delay_hours: Number(raw?.expiry_kick_delay_hours?.value ?? 0),
        ...savedSinceRequest,
      },
      loaded: true,
    });
    return true;
  } catch {
    // Keep what we have. `loaded` stays as it was: a failed refetch does not make
    // previously loaded values unknown, and never makes the defaults known.
    return false;
  }
}

/**
 * The fields of `next` that differ from `base`. The settings form sends only these, so
 * a tab holding older values cannot overwrite fields the admin did not touch.
 */
export function changedSettings(base: Settings, next: Settings): Partial<Settings> {
  const changes: Partial<Settings> = {};
  for (const key of Object.keys(next) as Array<keyof Settings>) {
    if (next[key] !== base[key]) Object.assign(changes, { [key]: next[key] });
  }
  return changes;
}

export function useSettings() {
  const [state, setState] = useState<SettingsSnapshot>(snapshot);

  useEffect(() => {
    listeners.add(setState);
    setState(snapshot);
    return () => {
      listeners.delete(setState);
    };
  }, []);

  /** Fetches the server's settings (sharing one request in flight). Resolves true on success. */
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
    // A partial save (e.g. only the sync interval) says nothing about the other
    // fields, so it must not mark the settings as loaded: they may still be defaults.
    broadcast({ settings: { ...snapshot.settings, ...data }, loaded: snapshot.loaded });
  }, []);

  useEffect(() => {
    if (!snapshot.loaded) void load();
  }, [load]);

  return { settings: state.settings, loaded: state.loaded, load, save };
}
