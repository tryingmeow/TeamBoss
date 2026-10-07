import { cn } from '../lib/utils';

export default function SeatProductionWarning({ compact = false }: { compact?: boolean }) {
  return (
    <div role="note" className={cn(
      'rounded-lg border border-red-300 bg-red-50 text-red-800 dark:border-red-500/50 dark:bg-red-500/10 dark:text-red-300',
      compact ? 'mx-1 mb-1 p-2 text-xs leading-5' : 'p-3 text-sm leading-6',
    )}>
      <p className="font-semibold">生产测试范围</p>
      <p>目前仅对已购买的月付 ChatGPT Standard 席位做过生产测试。Premium、年付及超出已购席位的邀请尚未经过生产测试，不保证费用准确或操作成功。</p>
    </div>
  );
}
