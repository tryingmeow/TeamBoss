import { subMonths } from 'date-fns';

const PERIOD_MONTHS: Record<string, number> = { monthly: 1, yearly: 12 };
/** A subscription younger than this is still in its first period, whatever the interval. */
const FIRST_PERIOD_MAX_MS = 31 * 24 * 60 * 60 * 1000;

function parse(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/**
 * Start of the billing period that ends at `activeUntil` (the next renewal or expiry).
 * The backend's `active_start` is when the subscription began, which can be many periods
 * back, so it is only used directly while the subscription is in its first period.
 * Null when the period can't be told (interval unknown and the subscription is older).
 */
export function currentPeriodStart(
  activeStart: string | null | undefined,
  activeUntil: string | null | undefined,
  billingPeriod: string | null | undefined,
): Date | null {
  const until = parse(activeUntil);
  if (!until) return null;
  const start = parse(activeStart);
  const months = billingPeriod ? PERIOD_MONTHS[billingPeriod] : undefined;
  if (months) {
    const derived = subMonths(until, months);
    return start && start > derived ? start : derived;
  }
  if (start && until.getTime() - start.getTime() <= FIRST_PERIOD_MAX_MS) return start;
  return null;
}

function monthDay(date: Date, withYear: boolean): string {
  const md = `${date.getMonth() + 1}/${date.getDate()}`;
  return withYear ? `${date.getFullYear()}/${md}` : md;
}

/** Both ends as "M/D", with years when the two fall in different years ("2026/4/23", "2027/4/23"). */
export function periodEnds(
  start: Date | string | null | undefined,
  end: Date | string | null | undefined,
): [string, string] {
  const from = start instanceof Date ? start : parse(start);
  const to = end instanceof Date ? end : parse(end);
  const withYear = Boolean(from && to && from.getFullYear() !== to.getFullYear());
  return [from ? monthDay(from, withYear) : '—', to ? monthDay(to, withYear) : '—'];
}

/** "9/7 – 10/7", or "2026/4/23 – 2027/4/23" across a year boundary. */
export function formatPeriodRange(start: Date | string | null | undefined, end: Date | string | null | undefined): string {
  return periodEnds(start, end).join(' – ');
}
