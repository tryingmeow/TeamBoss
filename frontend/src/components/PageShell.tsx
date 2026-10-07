import { type ReactNode, useEffect } from 'react';
import { CONTAINER } from './ui';

interface PageShellProps {
  title: string;
  badge?: ReactNode;
  /** One line telling a first-time visitor what this page is for. */
  description?: ReactNode;
  /** Page-level controls (search, filters, primary action). They wrap below the title on phones. */
  actions?: ReactNode;
  children: ReactNode;
}

/** Frame for every admin page: same width, gutters and header everywhere. */
export default function PageShell({ title, badge, description, actions, children }: PageShellProps) {
  useEffect(() => {
    document.title = `${title} · TeamBoss`;
  }, [title]);

  return (
    <div className={`${CONTAINER} pb-12 pt-5 sm:pt-7`}>
      <header className="mb-5 flex flex-col gap-3 sm:mb-6 md:flex-row md:items-end md:justify-between md:gap-6">
        <div className="min-w-0">
          <div className="flex items-center gap-2.5">
            <h1 className="text-xl font-semibold tracking-tight text-gray-900 sm:text-2xl dark:text-gray-50">{title}</h1>
            {badge}
          </div>
          {description && <div className="mt-1 text-sm text-gray-500 dark:text-ink-400">{description}</div>}
        </div>
        {actions && <div className="flex min-w-0 flex-wrap items-center gap-2 md:shrink-0 md:justify-end">{actions}</div>}
      </header>
      {children}
    </div>
  );
}
