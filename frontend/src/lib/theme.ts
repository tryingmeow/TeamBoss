import { useCallback, useSyncExternalStore } from 'react';

/**
 * Theme = an explicit choice saved in localStorage, or (no saved choice) the OS preference.
 * public/theme-init.js applies the same rule before first paint; keep the two in sync.
 */
const STORAGE_KEY = 'teamboss.theme';
type StoredTheme = 'light' | 'dark';

const systemQuery = () => window.matchMedia('(prefers-color-scheme: dark)');

function readStored(): StoredTheme | null {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    return value === 'light' || value === 'dark' ? value : null;
  } catch {
    return null;
  }
}

function writeStored(value: StoredTheme | null): void {
  try {
    if (value) window.localStorage.setItem(STORAGE_KEY, value);
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Storage unavailable (private mode): the choice lasts for this page only.
  }
}

function resolveDark(): boolean {
  const stored = readStored();
  if (stored) return stored === 'dark';
  return systemQuery().matches;
}

const listeners = new Set<() => void>();

function apply(): void {
  document.documentElement.classList.toggle('dark', resolveDark());
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  const media = systemQuery();
  const onSystemChange = () => apply();
  const onStorage = (event: StorageEvent) => {
    if (event.key === STORAGE_KEY) apply();
  };
  media.addEventListener('change', onSystemChange);
  window.addEventListener('storage', onStorage);
  return () => {
    listeners.delete(listener);
    media.removeEventListener('change', onSystemChange);
    window.removeEventListener('storage', onStorage);
  };
}

const getSnapshot = () => document.documentElement.classList.contains('dark');

/**
 * `toggle` flips what is on screen. When the result matches the OS preference the saved
 * choice is dropped, so the page goes back to following the system.
 */
export function useTheme(): { isDark: boolean; toggle: () => void } {
  const isDark = useSyncExternalStore(subscribe, getSnapshot, () => false);
  const toggle = useCallback(() => {
    const nextDark = !getSnapshot();
    writeStored(nextDark === systemQuery().matches ? null : nextDark ? 'dark' : 'light');
    apply();
  }, []);
  return { isDark, toggle };
}
