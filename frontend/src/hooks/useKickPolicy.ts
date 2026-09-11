import { useEffect, useState } from 'react';
import { fetchSettings } from '../api/client';
import type { KickPolicy } from '../lib/expiry';

/**
 * 到期宽限规则（`expiry_kick_mode` / `expiry_kick_delay_hours`）的只读视图。
 *
 * 到期选择器在成员表格的每一行里都要算「预计 X 移出」，但这两个值是全局设置。
 * 缓存放在模块级：一次会话只打一次 `/api/settings`，几十行成员共用同一个结果，
 * 而不是每个浮层各拉一次。
 */
const DEFAULT_POLICY: KickPolicy = { mode: 'delay_hours', delayHours: 0 };

let cached: KickPolicy | null = null;
let inflight: Promise<KickPolicy> | null = null;

async function loadPolicy(): Promise<KickPolicy> {
  const raw = await fetchSettings();
  const mode =
    raw?.expiry_kick_mode?.value === 'day_end' || raw?.expiry_kick_mode?.value === 'day_start'
      ? 'day_end'
      : 'delay_hours';
  const delayHours = Number(raw?.expiry_kick_delay_hours?.value ?? 0);
  const policy: KickPolicy = {
    mode,
    delayHours: Number.isFinite(delayHours) ? delayHours : 0,
  };
  cached = policy;
  return policy;
}

/** 设置被改写之后调用，下一个读取者会重新拉一次，而不是继续用旧规则做预览。 */
export function invalidateKickPolicy(): void {
  cached = null;
  inflight = null;
}

export function useKickPolicy(): KickPolicy {
  const [policy, setPolicy] = useState<KickPolicy>(cached ?? DEFAULT_POLICY);

  useEffect(() => {
    let cancelled = false;
    if (cached) {
      setPolicy(cached);
      return;
    }
    if (!inflight) {
      inflight = loadPolicy().finally(() => {
        inflight = null;
      });
    }
    inflight
      .then((next) => {
        if (!cancelled) setPolicy(next);
      })
      .catch(() => {
        // 设置拉不到时保持默认规则：预览会退化成"到期即移出"，但页面照常可用。
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return policy;
}
