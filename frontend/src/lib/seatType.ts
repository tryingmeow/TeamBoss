/**
 * 本文件是前端席位类型注册表与超员策略文案的正本。后端正本：backend/app/seat_types.py。
 *
 * Only registry types are ever offered as an action (invite, switch, code). Anything else the
 * workspace reports (e.g. `automation`) shows as 「其他（<raw>）」 in a neutral style and gets no
 * menu, matching the backend, which never acts on it.
 */
import type { CodeSeatType, OveragePolicy, SeatType, WorkspaceDefaultSeatType } from '../types';

export interface SeatTypeInfo {
  value: SeatType;
  label: string;
  /** No free paid seat → inviting or switching into it makes ChatGPT add a seat and charge. */
  billed: boolean;
  /** Not yet tested in production: wherever the type is offered or explained it wears a Beta badge. */
  beta?: boolean;
}

export const SEAT_TYPES: Record<SeatType, SeatTypeInfo> = {
  default: { value: 'default', label: 'ChatGPT', billed: true },
  usage_based: { value: 'usage_based', label: 'Codex', billed: false },
  prolite: { value: 'prolite', label: 'Premium', billed: true, beta: true },
};

/** Every type an admin may pick when inviting or switching a member. */
export const SEAT_TYPE_OPTIONS: SeatTypeInfo[] = [SEAT_TYPES.default, SEAT_TYPES.usage_based, SEAT_TYPES.prolite];

/**
 * The workspace "default invite seat type": never Premium (every invite without an explicit type
 * would then take a billed Premium seat, at that Team's Premium price).
 */
export const WORKSPACE_DEFAULT_SEAT_OPTIONS: Array<SeatTypeInfo & { value: WorkspaceDefaultSeatType }> = [
  { ...SEAT_TYPES.default, value: 'default' },
  { ...SEAT_TYPES.usage_based, value: 'usage_based' },
];

/** Redemption codes sell paid seats only (Codex is pay-as-you-go). */
export const CODE_SEAT_OPTIONS: Array<SeatTypeInfo & { value: CodeSeatType }> = [
  { ...SEAT_TYPES.default, value: 'default' },
  { ...SEAT_TYPES.prolite, value: 'prolite' },
];

/** Same rule as the backend's normalize_seat_type: missing/blank = 'default', unknown = null. */
export function parseSeatType(value: string | null | undefined): SeatType | null {
  const raw = (value ?? '').trim();
  if (!raw) return 'default';
  return Object.prototype.hasOwnProperty.call(SEAT_TYPES, raw) ? (raw as SeatType) : null;
}

export function formatSeatTypeLabel(value: string | null | undefined): string {
  const seatType = parseSeatType(value);
  return seatType ? SEAT_TYPES[seatType].label : `其他（${(value ?? '').trim()}）`;
}

export function isBilledSeatType(value: string | null | undefined): boolean {
  const seatType = parseSeatType(value);
  return seatType ? SEAT_TYPES[seatType].billed : false;
}

/**
 * Each seat type keeps one color everywhere it appears: ChatGPT = blue, Codex = purple,
 * Premium = pink. Red / amber / green stay reserved for status; unknown types are gray.
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
  prolite: {
    pill: 'bg-pink-100 text-pink-700 ring-1 ring-inset ring-pink-600/20 dark:bg-pink-500/15 dark:text-pink-300 dark:ring-pink-400/30',
    surface: 'border border-pink-200/80 bg-pink-50 dark:border-pink-400/25 dark:bg-[color-mix(in_oklab,var(--color-pink-500)_10%,var(--color-ink-900))]',
    text: 'text-pink-700 dark:text-pink-300',
    solid: 'bg-pink-500',
    track: 'bg-pink-200/70 dark:bg-pink-400/15',
  },
};

/** Gray: seat types outside the registry. */
export const UNKNOWN_SEAT_STYLE: SeatStyle = {
  pill: 'bg-gray-100 text-gray-600 ring-1 ring-inset ring-gray-500/20 dark:bg-ink-800 dark:text-ink-300 dark:ring-ink-600/50',
  surface: 'border border-gray-200 bg-gray-50 dark:border-ink-800 dark:bg-ink-850',
  text: 'text-gray-600 dark:text-ink-300',
  solid: 'bg-gray-400 dark:bg-ink-500',
  track: 'bg-gray-200 dark:bg-ink-800',
};

export function seatStyle(value: string | null | undefined): SeatStyle {
  const seatType = parseSeatType(value);
  return seatType ? SEAT_STYLE[seatType] : UNKNOWN_SEAT_STYLE;
}

// ── Overage policy (per Team) ───────────────────────────────────────────────────

export const OVERAGE_POLICY_OPTIONS: { value: OveragePolicy; label: string; hint: string }[] = [
  { value: 'forbid', label: '禁止超员', hint: '席位已满时禁止加入，不产生额外扣费' },
  { value: 'confirm', label: '超员需确认', hint: '席位已满时弹窗确认，确认后自动加购扣费' },
  { value: 'auto', label: '超员自动', hint: '席位已满时直接加入，自动加购扣费' },
];

/** Same rule as the backend: missing = 'confirm' (the default); anything unrecognised = 'forbid'. */
export function parseOveragePolicy(value: string | null | undefined): OveragePolicy {
  const raw = (value ?? '').trim().toLowerCase();
  if (!raw) return 'confirm';
  return OVERAGE_POLICY_OPTIONS.some((option) => option.value === raw) ? (raw as OveragePolicy) : 'forbid';
}

export function overagePolicyLabel(value: string | null | undefined): string {
  const policy = parseOveragePolicy(value);
  return OVERAGE_POLICY_OPTIONS.find((option) => option.value === policy)!.label;
}

/** True when an API error looks like an HTTP 403 (used to special-case
 * "Codex 席位未开启" below). Matches both the raw `HTTP 403` fallback and
 * backend detail text mentioning "forbidden". */
export function isForbiddenError(err: unknown): boolean {
  const message = err instanceof Error ? err.message : String(err ?? '');
  return /\b403\b/i.test(message) || /forbidden/i.test(message);
}

/** Start of the backend's 502 detail when a seat switch may or may not have happened. */
const SWITCH_UNCERTAIN_PREFIX = '切换结果不明确';

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
  // 上游超时等结果不明：切换可能已经生效（也可能已加购），原话告诉管理员先刷新再说。
  if (err instanceof Error && err.message.startsWith(SWITCH_UNCERTAIN_PREFIX)) return err.message;
  return '修改席位类型失败';
}
