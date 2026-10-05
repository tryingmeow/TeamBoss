import { useState } from 'react';
import { Pencil } from 'lucide-react';
import * as Popover from '@radix-ui/react-popover';
import { updateUserDisplayName } from '../api/client';
import type { ShowToast } from '../types';
import { BUTTON, INPUT } from './ui';
import { cn } from '../lib/utils';

interface MemberRemarkEditorProps {
  email: string;
  remark: string;
  /** 保存成功后把新备注写回当前卡片的成员列表，避免为了一条本地备注去拉 ChatGPT。 */
  onSaved: (email: string, remark: string | null) => void;
  showToast: ShowToast;
}

export default function MemberRemarkEditor({ email, remark, onSaved, showToast }: MemberRemarkEditorProps) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);

  const handleOpenChange = (next: boolean) => {
    if (next) setDraft(remark);
    setOpen(next);
  };

  const handleSave = async () => {
    if (saving) return;
    setSaving(true);
    try {
      const value = draft.trim();
      const saved = await updateUserDisplayName(email, value || null);
      onSaved(email, saved.system_display_name ?? null);
      setOpen(false);
      showToast(value ? '备注已更新' : '备注已清除');
    } catch (err) {
      // 保留浮层：失败时管理员还能看到自己刚输入的内容并重试，而不是以为已经存上了。
      showToast(err instanceof Error ? err.message : '保存备注失败', 'error');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Popover.Root open={open} onOpenChange={handleOpenChange}>
      <Popover.Trigger asChild>
        <button
          type="button"
          className="inline-flex size-6 shrink-0 items-center justify-center rounded-md text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/50 dark:text-ink-500 dark:hover:bg-ink-800 dark:hover:text-gray-100"
          title={remark ? '编辑备注' : '添加备注'}
          aria-label={remark ? '编辑备注' : '添加备注'}
        >
          <Pencil size={12} />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          className="z-50 w-72 max-w-[calc(100vw-2rem)] rounded-xl border border-gray-200 bg-white p-3 shadow-xl dark:border-ink-800 dark:bg-ink-900"
          sideOffset={6}
          collisionPadding={16}
        >
          <p className="text-sm font-medium text-gray-900 dark:text-gray-100">备注</p>
          <p className="mb-2 truncate text-xs text-gray-500 dark:text-ink-400" title={email}>{email}</p>
          <input
            autoFocus
            type="text"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                void handleSave();
              }
            }}
            maxLength={120}
            placeholder="留空则清除备注"
            className={INPUT}
          />
          <div className="mt-3 flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setOpen(false)}
              className={cn(BUTTON.secondary, 'h-8 px-3 py-0 text-xs')}
            >
              取消
            </button>
            <button
              type="button"
              onClick={handleSave}
              disabled={saving}
              className={cn(BUTTON.primary, 'h-8 px-3 py-0 text-xs')}
            >
              {saving ? '保存中…' : '保存'}
            </button>
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
