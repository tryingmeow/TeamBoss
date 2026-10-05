import type { ReactNode } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { Loader2 } from 'lucide-react';
import DialogFrame from './DialogFrame';
import { BUTTON } from './ui';

interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  message: ReactNode;
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
    <DialogFrame
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      description={<div className="break-words">{message}</div>}
      size="sm"
      footer={
        <>
          <Dialog.Close asChild>
            <button type="button" className={BUTTON.secondary}>
              取消
            </button>
          </Dialog.Close>
          {secondaryLabel && onSecondary && (
            <button type="button" onClick={onSecondary} disabled={loading} className={BUTTON.secondary}>
              {secondaryLabel}
            </button>
          )}
          <button
            type="button"
            onClick={onConfirm}
            disabled={loading}
            className={destructive ? BUTTON.danger : BUTTON.primary}
          >
            {loading && <Loader2 size={14} className="animate-spin" />}
            {loading ? '处理中…' : confirmLabel}
          </button>
        </>
      }
    >
      {children}
    </DialogFrame>
  );
}
