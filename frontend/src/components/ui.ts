/**
 * Shared class strings so pages look like one product. Use with `cn()` from lib/utils
 * when a call site needs to add or override a class.
 */

/** Horizontal frame shared by the admin header, tab bar and every page body. */
export const CONTAINER = 'mx-auto w-full max-w-[96rem] px-4 sm:px-6 lg:px-8';

export const CARD = 'rounded-xl border border-gray-200 bg-white dark:border-ink-800 dark:bg-ink-900';

const BUTTON_BASE =
  'inline-flex shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-lg text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/50 disabled:cursor-not-allowed disabled:opacity-50';

export const BUTTON = {
  primary: `${BUTTON_BASE} bg-blue-600 px-3.5 py-2 text-white shadow-sm hover:bg-blue-700`,
  secondary: `${BUTTON_BASE} border border-gray-200 bg-white px-3.5 py-2 text-gray-700 hover:bg-gray-50 hover:text-gray-900 dark:border-ink-800 dark:bg-ink-900 dark:text-gray-200 dark:hover:bg-ink-800`,
  danger: `${BUTTON_BASE} bg-red-600 px-3.5 py-2 text-white shadow-sm hover:bg-red-700`,
  /** Square icon-only button; always pair with aria-label. */
  icon: `${BUTTON_BASE} size-9 text-gray-500 hover:bg-gray-100 hover:text-gray-900 dark:text-ink-400 dark:hover:bg-ink-800 dark:hover:text-gray-100`,
} as const;

/** 16px text on phones so iOS Safari does not zoom in when the field gets focus. */
export const INPUT =
  'w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-base text-gray-900 sm:text-sm placeholder:text-gray-400 transition-colors focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/30 dark:border-ink-800 dark:bg-ink-950 dark:text-gray-100 dark:placeholder:text-ink-500';

/** Small status pill; add a tone from TONE. */
export const PILL = 'inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-md px-1.5 py-0.5 text-[11px] font-medium';

export const TONE = {
  neutral: 'bg-gray-100 text-gray-600 dark:bg-ink-800 dark:text-ink-300',
  info: 'bg-blue-50 text-blue-700 dark:bg-blue-500/15 dark:text-blue-300',
  success: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300',
  warning: 'bg-amber-50 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300',
  danger: 'bg-red-50 text-red-700 dark:bg-red-500/15 dark:text-red-300',
} as const;
// Seat-type colors (ChatGPT / Codex / Premium) live in SEAT_STYLE (lib/seatType), not here.
