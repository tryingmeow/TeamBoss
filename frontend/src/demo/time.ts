/** Time helpers. Every fixture timestamp is an offset from the install-time clock. */

export const MINUTE = 60_000;
export const HOUR = 60 * MINUTE;
export const DAY = 24 * HOUR;

/** ISO-8601 in UTC, the shape the backend's `.isoformat()` values parse to. */
export function isoAt(ms: number): string {
  return new Date(ms).toISOString();
}

/** `YYYY-MM-DD` (UTC date part), as used by finance `date` / `snapshot_date`. */
export function dateOnly(ms: number): string {
  return isoAt(ms).slice(0, 10);
}

function pad2(n: number): string {
  return String(n).padStart(2, '0');
}

/** `YYYY-MM-DD HH:MM` in Asia/Shanghai (fixed UTC+8), like the backend's `*_local` fields. */
export function shanghaiLocal(ms: number): string {
  const d = new Date(ms + 8 * HOUR);
  return (
    `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())} ` +
    `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`
  );
}

/** ISO with an explicit +08:00 offset, the format the admin date picker sends. */
export function shanghaiIso(ms: number): string {
  const d = new Date(ms + 8 * HOUR);
  return (
    `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}` +
    `T${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:00+08:00`
  );
}

/** Parses the backend durations syntax (`30m` / `12h` / `7d`); `never` and invalid values return null. */
export function durationMs(value: string): number | null {
  const match = /^\s*(\d+)\s*([mhd])\s*$/i.exec(value);
  if (!match) return null;
  const amount = Number(match[1]);
  if (!Number.isFinite(amount) || amount <= 0) return null;
  const unit = match[2].toLowerCase();
  return amount * (unit === 'm' ? MINUTE : unit === 'h' ? HOUR : DAY);
}

const NEVER_WORDS = new Set(['never', 'none', 'null', 'infinite', 'infinity', 'forever', '永久', '∞']);

export function isNeverDuration(value: string): boolean {
  return NEVER_WORDS.has(value.trim().toLowerCase());
}

/**
 * Deterministic PRNG (mulberry32). The fixture set must look the same on every
 * page load so screenshots are reproducible; only the anchor clock moves.
 */
export function seededRandom(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
