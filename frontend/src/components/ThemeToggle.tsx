import { Moon, Sun } from 'lucide-react';
import { useTheme } from '../lib/theme';
import { cn } from '../lib/utils';

export default function ThemeToggle({ className }: { className?: string }) {
  const { isDark, toggle } = useTheme();
  const label = isDark ? '切换到浅色' : '切换到深色';
  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={label}
      title={label}
      className={cn(
        'inline-flex size-9 items-center justify-center rounded-lg text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 dark:text-ink-400 dark:hover:bg-ink-800 dark:hover:text-gray-100',
        className,
      )}
    >
      {isDark ? <Sun size={18} /> : <Moon size={18} />}
    </button>
  );
}
