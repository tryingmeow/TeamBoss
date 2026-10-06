const WHOLE = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
const CENTS = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

function toNumber(value: number | string | null | undefined): number | null {
  if (value === null || value === undefined || value === '') return null;
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** 1560 → "1,560", 12.5 → "12.50", 0.004 → "0". Whole numbers drop the cents. */
export function formatAmount(value: number): string {
  const rounded = Math.round(value * 100) / 100;
  return Number.isInteger(rounded) ? WHOLE.format(rounded) : CENTS.format(rounded);
}

/**
 * Amount with its currency: "-$300", "฿561", "12.50 USD".
 * `unit` is a symbol ("$", "฿", "€") or, when the backend has none, an ISO code.
 * Accepts the backend's string amounts (e.g. "-300.0000000000"); unparseable → "—".
 */
export function formatMoney(value: number | string | null | undefined, unit: string | null | undefined): string {
  const amount = toNumber(value);
  if (amount === null) return '—';
  const body = formatAmount(Math.abs(amount));
  const sign = amount < 0 && body !== '0' ? '-' : '';
  const symbol = (unit ?? '').trim();
  if (!symbol) return `${sign}${body}`;
  if (/^[A-Za-z]{3}$/.test(symbol)) return `${sign}${body} ${symbol.toUpperCase()}`;
  return `${sign}${symbol}${body}`;
}

/** Currency codes compare case-insensitively; a missing code never matches. */
export function sameCurrency(a: string | null | undefined, b: string | null | undefined): boolean {
  return Boolean(a && b && a.trim().toUpperCase() === b.trim().toUpperCase());
}

/**
 * A Team's own unit for an amount in `currency`: its symbol when the amount is in its billing
 * currency ("$30 × 25 席", "$675"), else the code. Converted amounts use the base currency code
 * ("≈ 233.33 USD"), like the totals and the trend chart.
 */
export function teamUnit(
  team: { billing_currency?: string | null; billing_symbol?: string | null } | undefined,
  currency: string | null | undefined,
): string {
  const symbol = team?.billing_symbol?.trim();
  if (symbol && (!currency || sameCurrency(currency, team?.billing_currency))) return symbol;
  return currency || team?.billing_currency || '';
}
