import { type ReactNode, useEffect } from 'react';
import BrandMark from './BrandMark';
import ThemeToggle from './ThemeToggle';

interface PublicShellProps {
  /** Browser tab title, shown as "<title> · TeamBoss". */
  title: string;
  children: ReactNode;
  /** Max width of the content column. */
  width?: 'sm' | 'md' | 'lg' | 'xl';
}

const WIDTHS = {
  sm: 'max-w-sm',
  md: 'max-w-md',
  lg: 'max-w-lg',
  xl: 'max-w-2xl',
} as const;

/**
 * Frame for screens a visitor sees without signing in: the self-service page, the terms
 * and the admin login. Brand on the left, theme switch on the right, content in a centred
 * column that starts near the top, so results appearing below a form never shift it.
 */
export default function PublicShell({ title, children, width = 'md' }: PublicShellProps) {
  useEffect(() => {
    document.title = `${title} · TeamBoss`;
  }, [title]);

  return (
    <div className="flex min-h-dvh flex-col bg-gray-50 dark:bg-ink-950">
      <header className="flex items-center justify-between px-4 py-3 sm:px-6 sm:py-4">
        <div className="flex items-center gap-2.5">
          <BrandMark size={26} />
          <span className="text-[15px] font-semibold tracking-tight text-gray-900 dark:text-gray-100">TeamBoss</span>
        </div>
        <ThemeToggle />
      </header>
      <main className="flex flex-1 justify-center px-4 pb-12 pt-2 sm:pb-20 sm:pt-[10vh]">
        <div className={`w-full ${WIDTHS[width]}`}>{children}</div>
      </main>
    </div>
  );
}
