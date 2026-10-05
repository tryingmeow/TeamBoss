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
