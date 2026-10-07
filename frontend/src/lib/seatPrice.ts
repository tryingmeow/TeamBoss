import type { SeatType, Team } from '../types';
import { formatMoney } from './money';

/** Billing periods TeamBoss tracks cost for; anything else is unknown. */
export type SeatPricePeriod = 'monthly' | 'yearly';

/**
 * One seat's price PER MONTH in a Team's billing currency, tax-exclusive (what ChatGPT's
 * 「管理席位」 shows as 「฿780 + 税费/月」). For a yearly Team `amount` is ChatGPT's annual-plan
 * price per month (the pricing config's `year` bucket), so one seat costs amount × 12 a year.
 * Same shape as the server's
 * `seat_price` in 409s.
 */
export interface SeatPrice {
  amount: number;
  currency: string;
  symbol: string | null;
  period: SeatPricePeriod;
}

/** One currency + period's total per month in a batch confirmation (never summed across either). */
export interface SeatCostTotal {
  amount: number;
  currency: string;
  symbol: string | null;
  period: SeatPricePeriod;
}

export const SEAT_PRICE_UNKNOWN_TEXT = '单价未知，以 ChatGPT 账单为准';
/** Said whenever a purchase lands on a yearly Team: no claim about how ChatGPT bills the rest of the term. */
export const PURCHASE_NOTE = '以上为席位原价，实际补差及扣款以 ChatGPT 账单为准';
export const YEARLY_PURCHASE_NOTE = `按年计费；${PURCHASE_NOTE}`;

const MONTHS: Record<SeatPricePeriod, number> = { monthly: 1, yearly: 12 };

function parsePeriod(raw: unknown): SeatPricePeriod | null {
  return raw === 'monthly' || raw === 'yearly' ? raw : null;
}

/** Server `seat_price` → SeatPrice; anything malformed is unknown (null), never a guess. */
export function parseSeatPrice(raw: unknown): SeatPrice | null {
  if (!raw || typeof raw !== 'object') return null;
  const item = raw as Record<string, unknown>;
  const amount = typeof item.amount === 'number' ? item.amount : Number.NaN;
  const currency = typeof item.currency === 'string' ? item.currency.trim() : '';
  const period = parsePeriod(item.period);
  if (!Number.isFinite(amount) || amount <= 0 || !currency || !period) return null;
  const symbol = typeof item.symbol === 'string' && item.symbol.trim() ? item.symbol.trim() : null;
  return { amount, currency, symbol, period };
}

export function parseSeatCostTotals(raw: unknown): SeatCostTotal[] {
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((entry) => {
    const price = parseSeatPrice(entry);
    return price ? [{ amount: price.amount, currency: price.currency, symbol: price.symbol, period: price.period }] : [];
  });
}

/**
 * The per-seat monthly price of `seatType` in `team`, from the synced Team row (the server sends
 * a price only when it matches the Team's billing period). Only billed types (ChatGPT `default`,
 * Premium `prolite`) of a monthly or yearly Team have one; anything else is null.
 */
export function teamSeatPrice(
  team: Pick<Team, 'billing_period' | 'billing_currency' | 'billing_symbol' | 'price_per_seat'>
    & { premium_price_per_seat?: number | null },
  seatType: SeatType | string,
): SeatPrice | null {
  const period = parsePeriod(team.billing_period);
  if (!period) return null;
  const amount = seatType === 'default' ? team.price_per_seat
    : seatType === 'prolite' ? team.premium_price_per_seat ?? null
      : null;
  return parseSeatPrice({
    amount,
    currency: team.billing_currency,
    symbol: team.billing_symbol,
    period,
  });
}

function priceUnit(price: { currency: string; symbol: string | null }): string {
  return price.symbol || price.currency;
}

/** A monthly amount in this price's currency: "฿780 + 税/月", yearly "฿630 + 税/月（年付，一年 ฿7,560）". */
function perMonth(amount: number, price: { currency: string; symbol: string | null; period: SeatPricePeriod }): string {
  const unit = priceUnit(price);
  const text = `${formatMoney(amount, unit)} + 税/月`;
  return price.period === 'yearly' ? `${text}（年付，一年 ${formatMoney(amount * MONTHS.yearly, unit)}）` : text;
}

/** One seat: "฿780 + 税/月", or for a yearly Team "฿630 + 税/月（年付，一年 ฿7,560）". */
export function formatSeatPrice(price: SeatPrice): string {
  return perMonth(price.amount, price);
}

/**
 * What buying `seats` seats adds: "约 +฿780 + 税/月", several "约 +฿2,340 + 税/月（每席 ฿780）";
 * yearly "约 +฿630 + 税/月（年付，一年 ฿7,560），年付加购按 ChatGPT 规则结算，以账单为准" (several:
 * "…（年付，一年 ฿22,680；每席 ฿630/月）…"). Unknown price → SEAT_PRICE_UNKNOWN_TEXT.
 * Only the per-period price; nothing about how ChatGPT prorates. Same text as the server's
 * services/pricing.seat_charge_text.
 */
export function seatChargeText(price: SeatPrice | null, seats: number): string {
  if (!price) return SEAT_PRICE_UNKNOWN_TEXT;
  const count = Math.max(1, Math.floor(seats));
  const unit = priceUnit(price);
  const total = price.amount * count;
  if (price.period === 'yearly') {
    const each = count > 1 ? `；每席 ${formatMoney(price.amount, unit)}/月` : '';
    return `约 +${formatMoney(total, unit)} + 税/月（年付，一年 ${formatMoney(total * MONTHS.yearly, unit)}${each}），${YEARLY_PURCHASE_NOTE}`;
  }
  const text = `约 +${formatMoney(total, unit)} + 税/月`;
  return `${count > 1 ? `${text}（每席 ${formatMoney(price.amount, unit)}）` : text}，${PURCHASE_NOTE}`;
}

/**
 * Compact amount for a tight spot (a menu row, a plan row): "฿2,340 + 税/月", yearly
 * "฿1,890 + 税/月 · 年付". The full text (annual figure, YEARLY_PURCHASE_NOTE) must be shown
 * nearby: in a tooltip, the confirm dialog or a total line.
 */
export function formatSeatAmountShort(price: SeatPrice, seats = 1): string {
  const text = perMonth(price.amount * Math.max(1, Math.floor(seats)), { ...price, period: 'monthly' });
  return price.period === 'yearly' ? `${text} · 年付` : text;
}

/** Group seat costs per currency and period; different currencies or periods are never added together. */
export function groupSeatCosts(items: Array<{ price: SeatPrice | null; seats: number }>): {
  totals: SeatCostTotal[];
  unknownSeats: number;
} {
  const groups = new Map<string, SeatCostTotal>();
  let unknownSeats = 0;
  for (const { price, seats } of items) {
    if (seats <= 0) continue;
    if (!price) {
      unknownSeats += seats;
      continue;
    }
    const key = `${price.currency.toUpperCase()}:${price.period}`;
    const current = groups.get(key);
    if (current) current.amount += price.amount * seats;
    else groups.set(key, { amount: price.amount * seats, currency: price.currency, symbol: price.symbol, period: price.period });
  }
  return { totals: [...groups.values()], unknownSeats };
}

/** "约 +฿2,340 + 税/月 · +NZ$120 + 税/月"; a yearly group adds its annual figure and the yearly note once. */
export function formatSeatCostTotals(totals: SeatCostTotal[]): string {
  if (totals.length === 0) return '';
  const text = `约 ${totals.map((total) => `+${perMonth(total.amount, total)}`).join(' · ')}`;
  return `${text}，${totals.some((total) => total.period === 'yearly') ? YEARLY_PURCHASE_NOTE : PURCHASE_NOTE}`;
}
