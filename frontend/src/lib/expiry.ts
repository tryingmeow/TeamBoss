/**
 * 到期时间与"预计移出时间"的前端唯一定义。
 *
 * 这里的计算必须和后端 `backend/app/services/member_expiry.py` 的
 * `compute_effective_kick_at()` 逐字一致——预览一旦和真实踢人时间不一样，
 * 管理员看到的那行小字就是谎言。
 */

/**
 * 后端把"当天 23:59"这个宽限模式钉死在 +08:00
 * （`APP_LOCAL_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")`），
 * 和浏览器所在时区无关。整个前端只有这一处写下这个偏移量：后端哪天把它
 * 做成可配置的，改这一行即可，不用在各处 grep。
 */
export const APP_LOCAL_UTC_OFFSET_MINUTES = 8 * 60;

/** 与 `APP_LOCAL_UTC_OFFSET_MINUTES` 对应的 ISO 后缀，用于写回绝对时间。 */
export const APP_LOCAL_ISO_OFFSET = (() => {
  const sign = APP_LOCAL_UTC_OFFSET_MINUTES < 0 ? '-' : '+';
  const abs = Math.abs(APP_LOCAL_UTC_OFFSET_MINUTES);
  return `${sign}${pad2(Math.floor(abs / 60))}:${pad2(abs % 60)}`;
})();

export interface KickPolicy {
  mode: 'delay_hours' | 'day_end';
  delayHours: number;
}

/** 管理员在九宫格里选中的东西。三种形态对应三种写回方式。 */
export type ExpirySelection =
  | { kind: 'never' }
  /** 后端 durations 语法：`7d` / `12h` / `30m`。 */
  | { kind: 'duration'; value: string }
  /** 带时区偏移的绝对时刻，例如 `2026-10-11T09:30:00+08:00`。 */
  | { kind: 'absolute'; iso: string };

export interface AppLocalParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
}

function pad2(n: number): string {
  return String(n).padStart(2, '0');
}

/** 把一个时刻换算成"应用本地时区"（见上方常量）的年月日时分。 */
export function toAppLocal(date: Date): AppLocalParts {
  const shifted = new Date(date.getTime() + APP_LOCAL_UTC_OFFSET_MINUTES * 60_000);
  return {
    year: shifted.getUTCFullYear(),
    month: shifted.getUTCMonth() + 1,
    day: shifted.getUTCDate(),
    hour: shifted.getUTCHours(),
    minute: shifted.getUTCMinutes(),
  };
}

/** `toAppLocal` 的逆运算。 */
export function fromAppLocal(parts: AppLocalParts): Date {
  const utcMs = Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, 0, 0);
  return new Date(utcMs - APP_LOCAL_UTC_OFFSET_MINUTES * 60_000);
}

/** `MM-DD HH:mm`（应用本地时区）。 */
export function formatAppLocalMinute(date: Date): string {
  const p = toAppLocal(date);
  return `${pad2(p.month)}-${pad2(p.day)} ${pad2(p.hour)}:${pad2(p.minute)}`;
}

/** `YYYY-MM-DD HH:mm`（应用本地时区）。 */
export function formatAppLocalFull(date: Date): string {
  const p = toAppLocal(date);
  return `${p.year}-${pad2(p.month)}-${pad2(p.day)} ${pad2(p.hour)}:${pad2(p.minute)}`;
}

/**
 * 到期时间 → 预计被移出的时刻。
 *
 * 与后端 `compute_effective_kick_at` 同构：
 *   - `day_end`：取到期时刻在 +08:00 那一天的 23:59:00；
 *   - `delay_hours`：到期时刻 + delay_hours 小时（delay 钳在 0–720）。
 */
export function computeEffectiveKickAt(expiresAt: Date, policy: KickPolicy): Date {
  if (policy.mode === 'day_end') {
    const p = toAppLocal(expiresAt);
    return fromAppLocal({ year: p.year, month: p.month, day: p.day, hour: 23, minute: 59 });
  }
  const delay = Math.min(Math.max(Number(policy.delayHours) || 0, 0), 720);
  return new Date(expiresAt.getTime() + delay * 3_600_000);
}

/** 解析后端 durations 语法（`^\d+[mhd]$`）为毫秒；非法返回 null。 */
export function durationToMs(duration: string): number | null {
  const match = /^\s*(\d+)\s*([mhd])\s*$/i.exec(duration);
  if (!match) return null;
  const amount = Number(match[1]);
  if (!Number.isFinite(amount) || amount <= 0) return null;
  const unit = match[2].toLowerCase();
  if (unit === 'm') return amount * 60_000;
  if (unit === 'h') return amount * 3_600_000;
  return amount * 86_400_000;
}

/**
 * 选择项落地后的到期时刻。`never` 以及无法解析的时长返回 null。
 *
 * 时长类的结果只是预览：真正的起算点是后端收到请求的那一刻，差几秒不影响
 * 管理员判断哪一天会被移出。
 */
export function selectionExpiryDate(
  selection: ExpirySelection | null | undefined,
  now: Date = new Date(),
): Date | null {
  if (!selection || selection.kind === 'never') return null;
  if (selection.kind === 'absolute') {
    const parsed = new Date(selection.iso);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }
  const ms = durationToMs(selection.value);
  return ms === null ? null : new Date(now.getTime() + ms);
}

/**
 * 把选择项压成后端 durations 语法。
 *
 * 邀请接口（POST /members/invite）只收 `expires_in`，不收绝对时间，所以
 * 日历选出来的时刻在这里换算成"从现在起 N 分钟"。请求体形状不变。
 */
export function selectionToDuration(
  selection: ExpirySelection,
  now: Date = new Date(),
): string {
  if (selection.kind === 'never') return 'never';
  if (selection.kind === 'duration') return selection.value;
  const target = new Date(selection.iso).getTime();
  if (Number.isNaN(target)) return 'never';
  const minutes = Math.max(1, Math.round((target - now.getTime()) / 60_000));
  return `${minutes}m`;
}

/** 拼出带应用本地偏移的 ISO 字符串，例如 `2026-10-11T09:30:00+08:00`。 */
export function appLocalIso(parts: AppLocalParts): string {
  return (
    `${parts.year}-${pad2(parts.month)}-${pad2(parts.day)}` +
    `T${pad2(parts.hour)}:${pad2(parts.minute)}:00${APP_LOCAL_ISO_OFFSET}`
  );
}

/** 某个时刻在应用本地时区的 `hh:mm`；解析不出来返回 null。 */
export function appLocalHourMinute(value: string | null | undefined): { hour: number; minute: number } | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const p = toAppLocal(date);
  return { hour: p.hour, minute: p.minute };
}

/**
 * 宽限规则的一句话描述，放在预览里说明这个时刻是怎么算出来的。
 * 「系统已选择」= 这不是这次选的，是设置里定好的全局规则。
 */
export function kickPolicyLabel(policy: KickPolicy): string {
  if (policy.mode === 'day_end') return '系统已选择当日末移出';
  const delay = Math.min(Math.max(Number(policy.delayHours) || 0, 0), 720);
  return delay > 0 ? `系统已选择 +${delay}h 移出` : '系统已选择到期即移出';
}

/**
 * 到期时间为空时的三种含义，对应后端 `get_active_expiry_state()`：
 * - `permanent`：有到期记录、被明确设成永久（source 是 system / self_service 等）。
 * - `detected`：巡逻发现的面板外加入者，没有任何授权；巡逻自动踢人开启时可能被移出（超员或严格模式）。
 * - `unrecorded`：本地没有到期记录（面板接管前就在的人，或数据还没同步到 source）。
 * 只有 `permanent` 才能写成"永久"。
 */
export type NoExpiryKind = 'permanent' | 'detected' | 'unrecorded';

export function noExpiryKind(source: string | null | undefined): NoExpiryKind {
  if (source === 'detected') return 'detected';
  if (source === null || source === undefined) return 'unrecorded';
  return 'permanent';
}

export const NO_EXPIRY_LABEL: Record<NoExpiryKind, { short: string; current: string; title: string }> = {
  permanent: { short: '永久', current: '当前永久', title: '已设为永久，不会到期移出' },
  detected: {
    short: '外部加入',
    current: '外部加入，未记录到期',
    title: '在面板外加入，未经授权，也没有到期记录；巡逻自动踢人开启时可能被移出',
  },
  unrecorded: {
    short: '未记录到期',
    current: '当前未记录到期',
    title: '本地没有到期记录，不会到期移出，但也不是设定的永久',
  },
};
