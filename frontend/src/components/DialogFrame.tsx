import type { ReactNode } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { X } from 'lucide-react';
import { BUTTON } from './ui';
import { cn } from '../lib/utils';

const WIDTH = {
  sm: 'max-w-sm',
  md: 'max-w-md',
  lg: 'max-w-xl',
} as const;

interface DialogFrameProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  /** Short text under the title; also the dialog's accessible description. */
  description?: ReactNode;
  size?: keyof typeof WIDTH;
  /** Button row pinned under the scrolling body: cancel/secondary first, primary action last. */
  footer?: ReactNode;
  children?: ReactNode;
  /**
   * Dialogs opened from this one (e.g. a confirm step). They are rendered inside this dialog's
   * content so Radix treats them as nested: clicking in them is not a click outside this
   * dialog, and closing them leaves this one open.
   */
  nested?: ReactNode;
  /** Where focus lands on open; call event.preventDefault() and focus something else. */
  onOpenAutoFocus?: (event: Event) => void;
}

/**
 * Shared modal shell: 16px side margins on phones, body scrolls inside the viewport,
 * title + close button on top, footer always visible.
 */
export default function DialogFrame({
  open,
  onOpenChange,
  title,
  description,
  size = 'md',
  footer,
  children,
  nested,
  onOpenAutoFocus,
}: DialogFrameProps) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-gray-950/40 transition-opacity duration-150 starting:opacity-0 dark:bg-black/60" />
        <Dialog.Content
          {...(description ? {} : { 'aria-describedby': undefined })}
          onOpenAutoFocus={onOpenAutoFocus}
          className={cn(
            'fixed left-1/2 top-1/2 z-50 flex max-h-[calc(100dvh-2rem)] w-[calc(100vw-2rem)] -translate-x-1/2 -translate-y-1/2 flex-col rounded-xl border border-gray-200 bg-white shadow-2xl transition-[opacity,scale] duration-150 starting:scale-95 starting:opacity-0 focus:outline-none dark:border-ink-800 dark:bg-ink-900',
            WIDTH[size],
          )}
        >
          <div className="px-5 pt-5 sm:px-6">
            <Dialog.Title className="pr-8 text-lg font-semibold leading-6 text-gray-900 dark:text-gray-100">
              {title}
            </Dialog.Title>
            {description && (
              <Dialog.Description asChild>
                <div className="mt-1 text-pretty text-sm leading-6 text-gray-600 dark:text-ink-300">{description}</div>
              </Dialog.Description>
            )}
          </div>

          {children && (
            <div className={cn('min-h-0 flex-1 overflow-y-auto px-5 pt-4 sm:px-6', footer ? 'pb-1' : 'pb-5 sm:pb-6')}>
              {children}
            </div>
          )}

          {footer && (
            <div className="flex flex-wrap items-center justify-end gap-2 px-5 pb-5 pt-5 sm:px-6 sm:pb-6">
              {footer}
            </div>
          )}

          {/* Last in DOM order so opening focus lands on the first field (or Cancel), not on close. */}
          <Dialog.Close asChild>
            <button type="button" className={cn(BUTTON.icon, 'absolute right-3 top-3.5 sm:right-4 sm:top-4')} aria-label="关闭">
              <X size={18} />
            </button>
          </Dialog.Close>
          {nested}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
