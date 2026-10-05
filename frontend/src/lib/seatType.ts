import type { SeatType } from '../types';

export const SEAT_TYPE_OPTIONS: { value: SeatType; label: string }[] = [
  { value: 'default', label: 'ChatGPT' },
  { value: 'usage_based', label: 'Codex' },
];

const CODEX_ALIASES = new Set(['usage_based', 'codex']);
const CHATGPT_ALIASES = new Set(['default', 'gpt', 'chatgpt']);

/** Map API / legacy values to canonical seat type sent to backend. */
export function normalizeSeatType(value: string | null | undefined): SeatType {
  const raw = (value || '').trim().toLowerCase();
  if (CODEX_ALIASES.has(raw)) return 'usage_based';
  if (CHATGPT_ALIASES.has(raw)) return 'default';
  return 'default';
}

/** Display label for UI — only ChatGPT or Codex. */
export function formatSeatTypeLabel(value: string | null | undefined): string {
  return normalizeSeatType(value) === 'usage_based' ? 'Codex' : 'ChatGPT';
}

export function isCodexSeat(value: string | null | undefined): boolean {
  return normalizeSeatType(value) === 'usage_based';
}

/**
 * The two seat types are billed differently, so each keeps one color everywhere it
 * appears: ChatGPT = blue, Codex = purple. Red / amber / green stay reserved for status.
 */
export interface SeatStyle {
  /** Badge colors; pair with PILL. Tinted fill, accent text, hairline ring. */
  pill: string;
  /** Tinted panel (stat box, chosen option): opaque background + border, so it reads
   * the same on a card or on the page. */
  surface: string;
  /** Accent text for labels, icons and numbers. */
  text: string;
  /** Solid swatch: legend dots and progress fill. */
  solid: string;
  /** Progress-bar track on a surface. */
  track: string;
}

export const SEAT_STYLE: Record<SeatType, SeatStyle> = {
  default: {
    pill: 'bg-blue-100 text-blue-700 ring-1 ring-inset ring-blue-600/20 dark:bg-blue-500/15 dark:text-blue-300 dark:ring-blue-400/30',
    surface: 'border border-blue-200/80 bg-blue-50 dark:border-blue-400/25 dark:bg-[color-mix(in_oklab,var(--color-blue-500)_10%,var(--color-ink-900))]',
    text: 'text-blue-700 dark:text-blue-300',
    solid: 'bg-blue-500',
    track: 'bg-blue-200/70 dark:bg-blue-400/15',
  },
  usage_based: {
    pill: 'bg-purple-100 text-purple-700 ring-1 ring-inset ring-purple-600/20 dark:bg-purple-500/15 dark:text-purple-300 dark:ring-purple-400/30',
    surface: 'border border-purple-200/80 bg-purple-50 dark:border-purple-400/25 dark:bg-[color-mix(in_oklab,var(--color-purple-500)_10%,var(--color-ink-900))]',
    text: 'text-purple-700 dark:text-purple-300',
    solid: 'bg-purple-500',
    track: 'bg-purple-200/70 dark:bg-purple-400/15',
  },
};

export function seatStyle(value: string | null | undefined): SeatStyle {
  return SEAT_STYLE[normalizeSeatType(value)];
}

/** True when an API error looks like an HTTP 403 (used to special-case
 * "Codex 席位未开启" below). Matches both the raw `HTTP 403` fallback and
 * backend detail text mentioning "forbidden". */
export function isForbiddenError(err: unknown): boolean {
  const message = err instanceof Error ? err.message : String(err ?? '');
  return /\b403\b/i.test(message) || /forbidden/i.test(message);
}

/** Friendly message for a failed seat-type change, matching the wording
 * used in the admin User Management table. */
export function seatUpdateErrorMessage(
  err: unknown,
  nextSeatType: SeatType,
  isCodexEnabled?: boolean | number
): string {
  const codexKnownOff = isCodexEnabled === false || isCodexEnabled === 0;
  if (nextSeatType === 'usage_based' && codexKnownOff && isForbiddenError(err)) {
    return 'Codex 席位未开启，请先开启 Codex 席位';
  }
  return '修改席位类型失败';
}
