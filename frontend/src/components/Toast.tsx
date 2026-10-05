import { CheckCircle2, XCircle } from 'lucide-react';
import { cn } from '../lib/utils';

interface ToastProps {
  text: string;
  type: 'success' | 'error';
}

export default function Toast({ text, type }: ToastProps) {
  const Icon = type === 'success' ? CheckCircle2 : XCircle;
  return (
    <div
      role={type === 'error' ? 'alert' : 'status'}
      className={cn(
        'flex items-start gap-2.5 rounded-xl border bg-white px-3.5 py-3 text-sm font-medium text-gray-900 shadow-lg transition-[opacity,translate] duration-200 starting:translate-y-2 starting:opacity-0 dark:bg-ink-800 dark:text-gray-100 dark:shadow-black/40',
        type === 'success' ? 'border-gray-200 dark:border-ink-700' : 'border-red-200 dark:border-red-500/40',
      )}
    >
      <Icon
        size={18}
        className={cn(
          'shrink-0',
          type === 'success' ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400',
        )}
      />
      <span className="min-w-0 break-words leading-[18px]">{text}</span>
    </div>
  );
}
