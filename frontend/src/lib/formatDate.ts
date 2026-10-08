import { format, isValid, parseISO } from 'date-fns';

/**
 * date-fns `format` throws `RangeError: Invalid time value` on an invalid
 * date instead of degrading like `toLocaleString` does. This wraps a
 * possibly-invalid date string/Date and falls back to a neutral placeholder
 * instead of throwing.
 */
export function formatDateSafe(
  value: string | Date | null | undefined,
  formatStr: string,
  placeholder = '—'
): string {
  if (!value) return placeholder;
  const date = typeof value === 'string' ? parseISO(value) : value;
  if (!isValid(date)) return placeholder;
  return format(date, formatStr);
}

/** 精确到秒的北京时间，用于悬停提示；无效或空值返回空串。 */
export function formatBeijingDateTime(value: string | Date | null | undefined, includeSuffix = true): string {
  if (!value) return '';
  const date = typeof value === 'string' ? parseISO(value) : value;
  if (!isValid(date)) return '';
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(date);
  const formatted = parts.replace(/\//g, '-');
  return includeSuffix ? `${formatted} 北京时间` : formatted;
}
