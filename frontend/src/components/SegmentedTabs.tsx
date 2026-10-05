import type { ReactNode } from 'react';
import { cn } from '../lib/utils';

export interface SegmentedOption<T extends string> {
  value: T;
  label: ReactNode;
}

interface SegmentedTabsProps<T extends string> {
  value: T;
  onChange: (value: T) => void;
  options: SegmentedOption<T>[];
  /** Names the group for screen readers, e.g. "视图". */
  ariaLabel: string;
  className?: string;
}

/**
 * In-page view switch (sub-tabs inside a page). The main navigation uses an underline;
 * views within a page use this segmented control so the two levels never look alike.
 * Stretches to full width on phones.
 */
export default function SegmentedTabs<T extends string>({
  value,
  onChange,
  options,
  ariaLabel,
  className,
}: SegmentedTabsProps<T>) {
  return (
    <div
      role="tablist"
      aria-label={ariaLabel}
      className={cn('flex w-full rounded-lg bg-gray-100 p-1 sm:inline-flex sm:w-auto dark:bg-ink-800/70', className)}
    >
      {options.map((option) => {
        const active = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(option.value)}
            className={cn(
              'inline-flex min-h-8 flex-1 items-center justify-center gap-1.5 whitespace-nowrap rounded-md px-3 py-1.5 text-sm font-medium transition-colors sm:flex-none',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/50',
              active
                ? 'bg-white text-gray-900 shadow-sm dark:bg-ink-900 dark:text-gray-50'
                : 'text-gray-500 hover:text-gray-900 dark:text-ink-400 dark:hover:text-gray-100'
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
