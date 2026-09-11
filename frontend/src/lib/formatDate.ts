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
