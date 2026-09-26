import type { ReactNode } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { X } from 'lucide-react';

interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  message: string;
  confirmLabel?: string;
  secondaryLabel?: string;
  destructive?: boolean;
  loading?: boolean;
  onSecondary?: () => void;
  onConfirm: () => void;
  children?: ReactNode;
}

export default function ConfirmDialog({
  open,
  onOpenChange,
  title,
  message,
  confirmLabel = '确认',
  secondaryLabel,
  destructive = false,
  loading = false,
  onSecondary,
  onConfirm,
  children,
}: ConfirmDialogProps) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/60 z-50 data-[state=open]:animate-in data-[state=open]:fade-in-0" />
        <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[calc(100vw-2rem)] max-w-sm rounded-xl bg-white dark:bg-[#1a1d27] border border-gray-200 dark:border-[#2a2d3a] p-6 shadow-2xl">
          <Dialog.Title className="text-lg font-bold text-gray-900 dark:text-gray-100">{title}</Dialog.Title>
          <Dialog.Description className="mt-2 text-sm text-gray-600 dark:text-gray-400">
            {message}
          </Dialog.Description>
          {children && <div className="mt-4">{children}</div>}
          <div className="mt-6 flex justify-end gap-3">
            <Dialog.Close asChild>
              <button className="px-4 py-2 rounded-lg text-sm font-medium text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-[#2a2d3a] hover:bg-gray-200 dark:hover:bg-[#3a3d4a] transition-colors">
                取消
              </button>
            </Dialog.Close>
            {secondaryLabel && onSecondary && (
              <button
                onClick={onSecondary}
                disabled={loading}
                className="px-4 py-2 rounded-lg text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 shadow-md shadow-blue-500/20 transition-all disabled:opacity-50"
              >
                {secondaryLabel}
              </button>
            )}
            <button
              onClick={onConfirm}
              disabled={loading}
              className={`px-4 py-2 rounded-lg text-sm font-medium text-white transition-all disabled:opacity-50 ${
                destructive
                  ? 'bg-red-500 hover:bg-red-600 shadow-md shadow-red-500/20'
                  : 'bg-blue-600 hover:bg-blue-700 shadow-md shadow-blue-500/20'
              }`}
            >
              {loading ? '处理中...' : confirmLabel}
            </button>
          </div>
          <Dialog.Close asChild>
            <button className="absolute top-4 right-4 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 transition-colors p-1 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800">
              <X size={16} />
            </button>
          </Dialog.Close>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
