import { useEffect, useState } from 'react';
import { Check, Copy, Globe, Loader2, Plus, RefreshCw, Trash2, Wifi, WifiOff } from 'lucide-react';
import type { Settings } from '../types';
import {
  changeAdminPassword,
  checkProxy,
  createProxy,
  deleteProxy,
  fetchAdminAccount,
  fetchProxies,
  rotateAdminApiKey,
  setStoredAdminApiKey,
  type AdminAccount,
  type Proxy,
} from '../api/client';
import DialogFrame from './DialogFrame';
import { BUTTON, INPUT } from './ui';
import { cn } from '../lib/utils';

const SECTION_TITLE = 'text-sm font-semibold text-gray-900 dark:text-gray-100';
const LABEL = 'mb-1.5 block text-sm font-medium text-gray-700 dark:text-ink-200';

interface SettingsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  settings: Settings;
  onSave: (data: Partial<Settings>) => Promise<void>;
}

export default function SettingsDialog({ open, onOpenChange, settings, onSave }: SettingsDialogProps) {
  const [interval, setInterval_] = useState(settings.sync_interval_minutes);
  const [concurrency, setConcurrency] = useState(settings.api_concurrency);
  const [kickMode, setKickMode] = useState(settings.expiry_kick_mode);
  const [kickDelayHours, setKickDelayHours] = useState(settings.expiry_kick_delay_hours);
  const [skipOverageConfirmation, setSkipOverageConfirmation] = useState(settings.skip_overage_confirmation);
  const [account, setAccount] = useState<AdminAccount | null>(null);
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [saving, setSaving] = useState(false);
  const [rotating, setRotating] = useState(false);
  const [statusText, setStatusText] = useState('');
  const [errorText, setErrorText] = useState('');

  // Proxy state
  const [proxies, setProxies] = useState<Proxy[]>([]);
  const [showAddProxy, setShowAddProxy] = useState(false);
  const [newProxyName, setNewProxyName] = useState('');
  const [newProxyUrl, setNewProxyUrl] = useState('');
  const [addingProxy, setAddingProxy] = useState(false);
  const [checkingId, setCheckingId] = useState<number | null>(null);

  useEffect(() => {
    setInterval_(settings.sync_interval_minutes);
    setConcurrency(settings.api_concurrency);
    setKickMode(settings.expiry_kick_mode);
    setKickDelayHours(settings.expiry_kick_delay_hours);
    setSkipOverageConfirmation(settings.skip_overage_confirmation);
  }, [settings]);

  useEffect(() => {
    if (!open) return;
    setStatusText('');
    setErrorText('');
    fetchAdminAccount()
      .then(setAccount)
      .catch((err) => setErrorText(err instanceof Error ? err.message : '加载失败'));
    fetchProxies().then(setProxies).catch(() => {});
  }, [open]);

  const copyApiKey = async (apiKey = account?.api_key) => {
    if (!apiKey) return;
    await navigator.clipboard.writeText(apiKey);
    setStatusText('已复制');
  };

  const handleSaveSettings = async () => {
    setSaving(true);
    setStatusText('');
    setErrorText('');
    try {
      await onSave({
        sync_interval_minutes: interval,
        api_concurrency: concurrency,
        expiry_kick_mode: kickMode,
        expiry_kick_delay_hours: kickDelayHours,
        skip_overage_confirmation: skipOverageConfirmation,
      });
      setStatusText('已保存');
    } catch (err) {
      setErrorText(err instanceof Error ? err.message : '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const handleChangePassword = async () => {
    setStatusText('');
    setErrorText('');
    if (newPassword !== confirmPassword) {
      setErrorText('两次密码不一致');
      return;
    }
    try {
      await changeAdminPassword({
        current_password: currentPassword,
        new_password: newPassword,
      });
      setCurrentPassword('');
      setNewPassword('');
      setConfirmPassword('');
      setStatusText('密码已更新');
    } catch (err) {
      setErrorText(err instanceof Error ? err.message : '更新失败');
    }
  };

  const handleRotateApiKey = async () => {
    setRotating(true);
    setStatusText('');
    setErrorText('');
    try {
      const result = await rotateAdminApiKey();
      setStoredAdminApiKey(result.api_key);
      setAccount({ api_key: result.api_key, api_key_prefix: result.api_key_prefix });
      await copyApiKey(result.api_key);
      setStatusText('API Key 已更新');
    } catch (err) {
      setErrorText(err instanceof Error ? err.message : '更新失败');
    } finally {
      setRotating(false);
    }
  };

  const handleAddProxy = async () => {
    if (!newProxyName.trim() || !newProxyUrl.trim()) return;
    setAddingProxy(true);
    try {
      const created = await createProxy({ name: newProxyName.trim(), url: newProxyUrl.trim() });
      setProxies((prev) => [...prev, { ...created, last_check_at: null, created_at: null }]);
      setNewProxyName('');
      setNewProxyUrl('');
      setShowAddProxy(false);
    } catch (err) {
      setErrorText(err instanceof Error ? err.message : '添加失败');
    } finally {
      setAddingProxy(false);
    }
  };

  const handleDeleteProxy = async (id: number) => {
    try {
      await deleteProxy(id);
      setProxies((prev) => prev.filter((p) => p.id !== id));
    } catch (err) {
      setErrorText(err instanceof Error ? err.message : '删除失败');
    }
  };

  const handleCheckProxy = async (id: number) => {
    setCheckingId(id);
    try {
      const result = await checkProxy(id);
      setProxies((prev) =>
        prev.map((p) => (p.id === id ? { ...p, status: result.status, last_check_at: result.last_check_at } : p)),
      );
    } catch {
      setProxies((prev) => prev.map((p) => (p.id === id ? { ...p, status: 'error' } : p)));
    } finally {
      setCheckingId(null);
    }
  };

  const proxyStatusIcon = (p: Proxy) => {
    if (checkingId === p.id) return <RefreshCw size={14} className="shrink-0 animate-spin text-gray-400" />;
    if (p.status === 'ok') return <Wifi size={14} className="shrink-0 text-emerald-500" />;
    if (p.status === 'error') return <WifiOff size={14} className="shrink-0 text-red-500" />;
    return <Globe size={14} className="shrink-0 text-gray-400" />;
  };

  return (
    <DialogFrame
      open={open}
      onOpenChange={onOpenChange}
      title="设置"
      description="全局设置，对所有 Team 生效。"
      size="lg"
      footer={
        (statusText || errorText) && (
          <p
            role="status"
            className={cn(
              'mr-auto flex items-center gap-1.5 text-sm',
              errorText ? 'text-red-600 dark:text-red-400' : 'text-emerald-600 dark:text-emerald-400',
            )}
          >
            {!errorText && <Check size={16} />}
            {errorText || statusText}
          </p>
        )
      }
    >
      <div className="space-y-6">
        <section className="space-y-4">
          <h3 className={SECTION_TITLE}>常规</h3>

          <div>
            <label htmlFor="syncInterval" className={cn(LABEL, 'flex items-center justify-between')}>
              <span>同步间隔</span>
              <span className="font-semibold tabular-nums text-blue-600 dark:text-blue-400">{interval} 分钟</span>
            </label>
            <input
              id="syncInterval"
              type="range"
              min={5}
              max={60}
              step={5}
              value={interval}
              onChange={(event) => setInterval_(Number(event.target.value))}
              className="w-full accent-blue-600 dark:accent-blue-500"
            />
          </div>

          <div>
            <label htmlFor="apiConcurrency" className={LABEL}>API 并发</label>
            <input
              id="apiConcurrency"
              type="number"
              min={1}
              max={10}
              value={concurrency}
              onChange={(event) => setConcurrency(Number(event.target.value))}
              className={cn(INPUT, 'w-28')}
            />
          </div>

          <fieldset>
            <legend className={LABEL}>到期移出时间</legend>
            <div className="space-y-2 text-sm text-gray-700 dark:text-ink-200">
              <label className="flex flex-wrap items-center gap-2">
                <input
                  type="radio"
                  name="kickMode"
                  checked={kickMode === 'delay_hours'}
                  onChange={() => setKickMode('delay_hours')}
                  className="size-4 accent-blue-600 dark:accent-blue-500"
                />
                到期后
                <input
                  id="kickDelay"
                  type="number"
                  min={0}
                  max={720}
                  disabled={kickMode !== 'delay_hours'}
                  value={kickDelayHours}
                  onChange={(event) => setKickDelayHours(Number(event.target.value || 0))}
                  aria-label="到期后延迟小时数"
                  className={cn(INPUT, 'w-20 py-1.5 disabled:cursor-not-allowed disabled:opacity-50')}
                />
                小时移出
              </label>
              <label className="flex items-center gap-2">
                <input
                  type="radio"
                  name="kickMode"
                  checked={kickMode === 'day_end'}
                  onChange={() => setKickMode('day_end')}
                  className="size-4 accent-blue-600 dark:accent-blue-500"
                />
                到期当天 23:59 移出
              </label>
            </div>
          </fieldset>

          <div>
            <label className="flex items-center gap-2 text-sm font-medium text-gray-700 dark:text-ink-200">
              <input
                type="checkbox"
                checked={skipOverageConfirmation}
                onChange={(event) => setSkipOverageConfirmation(event.target.checked)}
                className="size-4 accent-blue-600 dark:accent-blue-500"
              />
              超额添加不再确认
            </label>
            <p className="mt-1 pl-6 text-xs text-gray-500 dark:text-ink-400">
              席位不足时直接超额添加，不再弹确认框；额外席位照常计费。
            </p>
          </div>

          <div className="flex justify-end">
            <button type="button" onClick={handleSaveSettings} disabled={saving} className={BUTTON.primary}>
              {saving && <Loader2 size={14} className="animate-spin" />}
              {saving ? '保存中…' : '保存'}
            </button>
          </div>
        </section>

        <section className="space-y-3 border-t border-gray-200 pt-5 dark:border-ink-800">
          <div className="flex items-center justify-between gap-3">
            <h3 className={SECTION_TITLE}>代理</h3>
            <button
              type="button"
              onClick={() => setShowAddProxy(!showAddProxy)}
              className={cn(BUTTON.secondary, 'h-8 px-2.5 py-0 text-xs')}
              aria-expanded={showAddProxy}
            >
              <Plus size={14} />
              添加代理
            </button>
          </div>

          {showAddProxy && (
            <div className="space-y-2 rounded-lg border border-gray-200 bg-gray-50 p-3 dark:border-ink-800 dark:bg-ink-950">
              <input
                type="text"
                placeholder="名称"
                aria-label="代理名称"
                value={newProxyName}
                onChange={(e) => setNewProxyName(e.target.value)}
                className={INPUT}
              />
              <input
                type="text"
                placeholder="http://user:pass@host:port"
                aria-label="代理地址"
                value={newProxyUrl}
                onChange={(e) => setNewProxyUrl(e.target.value)}
                className={cn(INPUT, 'font-mono text-xs')}
              />
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => { setShowAddProxy(false); setNewProxyName(''); setNewProxyUrl(''); }}
                  className={cn(BUTTON.secondary, 'h-8 px-3 py-0 text-xs')}
                >
                  取消
                </button>
                <button
                  type="button"
                  onClick={handleAddProxy}
                  disabled={addingProxy || !newProxyName.trim() || !newProxyUrl.trim()}
                  className={cn(BUTTON.primary, 'h-8 px-3 py-0 text-xs')}
                >
                  {addingProxy ? '添加中…' : '添加'}
                </button>
              </div>
            </div>
          )}

          {proxies.length === 0 && !showAddProxy && (
            <p className="text-sm text-gray-500 dark:text-ink-400">暂无代理，所有 Team 直连。</p>
          )}

          {proxies.length > 0 && (
            <ul className="divide-y divide-gray-100 rounded-lg border border-gray-200 dark:divide-ink-800 dark:border-ink-800">
              {proxies.map((p) => (
                <li key={p.id} className="flex items-center gap-2.5 py-1.5 pl-3 pr-1.5">
                  {proxyStatusIcon(p)}
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium text-gray-900 dark:text-gray-100">{p.name}</div>
                    <div className="truncate font-mono text-[11px] text-gray-500 dark:text-ink-400">{p.url.replace(/\/\/([^:]+):([^@]+)@/, '//$1:***@')}</div>
                  </div>
                  <button
                    type="button"
                    onClick={() => handleCheckProxy(p.id)}
                    disabled={checkingId === p.id}
                    className={cn(BUTTON.secondary, 'h-8 px-2.5 py-0 text-xs')}
                    title="测试连接"
                  >
                    测试
                  </button>
                  <button
                    type="button"
                    onClick={() => handleDeleteProxy(p.id)}
                    className={cn(BUTTON.icon, 'hover:bg-red-50 hover:text-red-600 dark:hover:bg-red-500/10 dark:hover:text-red-400')}
                    title="删除"
                    aria-label={`删除 ${p.name}`}
                  >
                    <Trash2 size={15} />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="space-y-4 border-t border-gray-200 pt-5 dark:border-ink-800">
          <h3 className={SECTION_TITLE}>管理员</h3>

          <div>
            <label htmlFor="adminApiKey" className={LABEL}>API Key</label>
            <div className="flex gap-2">
              <input
                id="adminApiKey"
                readOnly
                value={account?.api_key ?? ''}
                className={cn(INPUT, 'min-w-0 flex-1 font-mono text-xs')}
              />
              <button
                type="button"
                onClick={() => copyApiKey()}
                className={cn(BUTTON.secondary, 'size-9 px-0 py-0')}
                title="复制"
                aria-label="复制 API Key"
              >
                <Copy size={16} />
              </button>
              <button
                type="button"
                onClick={handleRotateApiKey}
                disabled={rotating}
                className={cn(BUTTON.secondary, 'size-9 px-0 py-0')}
                title="更换"
                aria-label="更换 API Key"
              >
                <RefreshCw size={16} className={rotating ? 'animate-spin' : ''} />
              </button>
            </div>
          </div>

          <div>
            <span className={LABEL}>修改密码</span>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
              <input
                type="password"
                placeholder="当前密码"
                aria-label="当前密码"
                autoComplete="current-password"
                value={currentPassword}
                onChange={(event) => setCurrentPassword(event.target.value)}
                className={INPUT}
              />
              <input
                type="password"
                placeholder="新密码"
                aria-label="新密码"
                autoComplete="new-password"
                value={newPassword}
                onChange={(event) => setNewPassword(event.target.value)}
                className={INPUT}
              />
              <input
                type="password"
                placeholder="确认新密码"
                aria-label="确认新密码"
                autoComplete="new-password"
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
                className={INPUT}
              />
            </div>
          </div>

          <div className="flex justify-end">
            <button
              type="button"
              onClick={handleChangePassword}
              disabled={!currentPassword || !newPassword || !confirmPassword}
              className={BUTTON.primary}
            >
              更新密码
            </button>
          </div>
        </section>
      </div>
    </DialogFrame>
  );
}
