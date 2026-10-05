import { useSettings } from './useSettings';
import type { KickPolicy } from '../lib/expiry';

/** 到期预览与设置表单共用同一份状态，保存后已挂载的选择器也能立即更新。 */
export function useKickPolicy(): KickPolicy {
  const { settings } = useSettings();
  const delayHours = Number(settings.expiry_kick_delay_hours);
  return {
    mode: settings.expiry_kick_mode === 'day_end' ? 'day_end' : 'delay_hours',
    delayHours: Number.isFinite(delayHours) ? delayHours : 0,
  };
}
