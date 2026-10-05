import { Loader2 } from 'lucide-react';

/** Placeholder while a route chunk downloads. */
export default function PageLoading({ fullScreen = false }: { fullScreen?: boolean }) {
  return (
    <div
      role="status"
      aria-label="加载中"
      className={`flex items-center justify-center text-gray-400 dark:text-ink-500 ${fullScreen ? 'min-h-dvh' : 'py-24'}`}
    >
      <Loader2 size={22} className="animate-spin" />
    </div>
  );
}
