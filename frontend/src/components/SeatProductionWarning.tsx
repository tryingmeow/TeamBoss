import { AlertTriangle } from 'lucide-react';
import { cn } from '../lib/utils';

export default function SeatProductionWarning({ compact = false }: { compact?: boolean }) {
  return (
    <div
      role="note"
      className={cn(
        'flex items-start gap-2 rounded-lg border border-amber-200/70 bg-amber-50/50 text-amber-900/80 dark:border-amber-500/20 dark:bg-amber-500/5 dark:text-amber-300/90',
        compact ? 'mx-1 mb-1 p-2 text-xs leading-5' : 'px-3 py-2 text-xs leading-5',
      )}
    >
      <AlertTriangle size={14} className="mt-0.5 shrink-0 text-amber-600 dark:text-amber-400" />
      <div className="space-y-0.5 text-xs leading-5">
        <span className="font-medium text-amber-950 dark:text-amber-200">生产测试范围：</span>
        <span className="text-gray-600 dark:text-ink-300">
          目前仅对已购买的月付 ChatGPT Standard 席位做过生产测试。Premium、年付及超出已购席位尚未完整测试。
        </span>
      </div>
    </div>
  );
}
