import { useState } from 'react';
import { Pencil } from 'lucide-react';
import * as Popover from '@radix-ui/react-popover';
import { updateUserDisplayName } from '../api/client';
import type { ShowToast } from '../types';

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
          className="shrink-0 p-0.5 rounded text-gray-400 hover:text-gray-900 dark:hover:text-gray-200 transition-colors"
          title={remark ? '编辑备注' : '添加备注'}
          aria-label={remark ? '编辑备注' : '添加备注'}
        >
          <Pencil size={10} />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          className="bg-white dark:bg-[#1a1d27] border border-gray-200 dark:border-[#2a2d3a] rounded-xl p-3 shadow-xl z-50 w-64"
          sideOffset={5}
        >
          <p className="text-xs text-gray-500 dark:text-gray-400 mb-2 break-all">备注 · {email}</p>
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
            placeholder="添加备注..."
            className="w-full px-2 py-1.5 bg-white dark:bg-[#11131b] border border-gray-200 dark:border-[#2a2d3a] rounded-lg text-sm text-gray-800 dark:text-gray-200 placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
          <div className="mt-2 flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="px-2.5 py-1 rounded-lg bg-gray-100 dark:bg-[#2a2d3a] text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-[#343849] text-xs transition-colors"
            >
              取消
            </button>
            <button
              type="button"
              onClick={handleSave}
              disabled={saving}
              className="px-2.5 py-1 rounded-lg bg-blue-500 text-white hover:bg-blue-600 text-xs disabled:opacity-60 disabled:cursor-wait transition-colors"
            >
              {saving ? '保存中…' : '保存'}
            </button>
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
