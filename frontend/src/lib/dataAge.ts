import { isValid, parseISO } from 'date-fns';

const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** ISO 时间 → 「刚刚」/「N 分钟前」/「N 小时前」/「N 天前」；空值或无效值返回 null。 */
export function formatDataAge(iso: string | null | undefined, now: number = Date.now()): string | null {
  if (!iso) return null;
  const date = parseISO(iso);
  if (!isValid(date)) return null;
  // 服务器时钟略快时差值为负，按「刚刚」算。
  const diff = Math.max(0, now - date.getTime());
  if (diff < MINUTE) return '刚刚';
  if (diff < HOUR) return `${Math.floor(diff / MINUTE)} 分钟前`;
  if (diff < DAY) return `${Math.floor(diff / HOUR)} 小时前`;
  return `${Math.floor(diff / DAY)} 天前`;
}

/** 「数据刚刚更新」/「数据截至 N 分钟前」；没有时间时返回 null。 */
export function formatDataFreshness(iso: string | null | undefined, now: number = Date.now()): string | null {
  const age = formatDataAge(iso, now);
  if (!age) return null;
  return age === '刚刚' ? '数据刚刚更新' : `数据截至 ${age}`;
}
