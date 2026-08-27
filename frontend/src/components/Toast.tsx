import { CheckCircle, XCircle } from 'lucide-react';

interface ToastProps {
  text: string;
  type: 'success' | 'error';
}

export default function Toast({ text, type }: ToastProps) {
  return (
    <div
      className={`flex items-start gap-2 rounded-xl px-4 py-2.5 text-sm font-medium shadow-xl backdrop-blur-md animate-in slide-in-from-right-5 fade-in duration-200 ${
        type === 'success'
          ? 'bg-green-50/90 dark:bg-green-900/80 text-green-700 dark:text-green-200 border border-green-200 dark:border-green-700/50'
          : 'bg-red-50/90 dark:bg-red-900/80 text-red-700 dark:text-red-200 border border-red-200 dark:border-red-700/50'
      }`}
    >
      {type === 'success' ? (
        <CheckCircle size={16} className="mt-0.5 shrink-0 text-green-500 dark:text-green-400" />
      ) : (
        <XCircle size={16} className="mt-0.5 shrink-0 text-red-500 dark:text-red-400" />
      )}
      <span className="min-w-0 break-words">{text}</span>
    </div>
  );
}
