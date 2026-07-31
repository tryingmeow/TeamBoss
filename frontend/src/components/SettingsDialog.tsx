import { useEffect, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { Check, Copy, Globe, Plus, RefreshCw, Trash2, Wifi, WifiOff, X } from 'lucide-react';
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
    if (checkingId === p.id) return <RefreshCw size={14} className="animate-spin text-gray-400" />;
    if (p.status === 'ok') return <Wifi size={14} className="text-emerald-500" />;
    if (p.status === 'error') return <WifiOff size={14} className="text-red-400" />;
    return <Globe size={14} className="text-gray-400" />;
  };

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/60 z-50" />
        <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-full max-w-xl max-h-[85vh] overflow-y-auto rounded-xl bg-white dark:bg-[#1a1d27] border border-gray-200 dark:border-[#2a2d3a] p-6 shadow-2xl">
          <Dialog.Title className="text-lg font-bold text-gray-900 dark:text-gray-100">
            设置
          </Dialog.Title>

          <div className="mt-5 space-y-6">
            <section className="space-y-4">
              <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">系统</h3>

              <div>
                <label className="flex items-center justify-between text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                  <span>同步间隔</span>
                  <span className="text-blue-600 dark:text-blue-400 font-bold">{interval} 分钟</span>
                </label>
                <input
                  type="range"
                  min={5}
                  max={60}
                  step={5}
                  value={interval}
                  onChange={(event) => setInterval_(Number(event.target.value))}
                  className="w-full accent-blue-600 dark:accent-blue-500"
                />
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div className="block">
                  <label htmlFor="apiConcurrency" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                    API 并发
                  </label>
                  <div className="flex items-center w-full px-3 py-2 bg-gray-50 dark:bg-[#0f1117] border border-gray-200 dark:border-[#2a2d3a] rounded-lg focus-within:ring-2 focus-within:ring-blue-500/50 transition-all">
                    <input
                      id="apiConcurrency"
                      type="number"
                      min={1}
                      max={10}
                      value={concurrency}
                      onChange={(event) => setConcurrency(Number(event.target.value))}
                      className="flex-1 min-w-0 bg-transparent border-none p-0 text-sm text-gray-900 dark:text-gray-200 focus:ring-0 [appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none"
                    />
                  </div>
                </div>

                <div className="block">
                  <label htmlFor="kickDelay" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                    踢人延迟
                  </label>
                  <div className={`flex items-center w-full px-3 py-2 bg-gray-50 dark:bg-[#0f1117] border border-gray-200 dark:border-[#2a2d3a] rounded-lg focus-within:ring-2 focus-within:ring-blue-500/50 transition-all ${kickMode !== 'delay_hours' ? 'opacity-50' : ''}`}>
                    <input
                      id="kickDelay"
                      type="number"
                      min={0}
                      max={720}
                      disabled={kickMode !== 'delay_hours'}
                      value={kickDelayHours}
                      onChange={(event) => setKickDelayHours(Number(event.target.value || 0))}
                      className="flex-1 min-w-0 bg-transparent border-none p-0 text-sm text-gray-900 dark:text-gray-200 focus:ring-0 [appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none disabled:cursor-not-allowed"
                    />
                    <span className="text-gray-500 dark:text-gray-400 text-sm ml-2 select-none shrink-0 border-l border-gray-200 dark:border-[#2a2d3a] pl-2">小时</span>
                  </div>
                </div>
              </div>

              <div className="flex gap-4 text-sm text-gray-700 dark:text-gray-300">
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    checked={kickMode === 'delay_hours'}
                    onChange={() => setKickMode('delay_hours')}
                    className="accent-blue-600 dark:accent-blue-500"
                  />
                  到期后
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    checked={kickMode === 'day_end'}
                    onChange={() => setKickMode('day_end')}
                    className="accent-blue-600 dark:accent-blue-500"
                  />
                  当天 23:59
                </label>
              </div>

              <button
                onClick={handleSaveSettings}
                disabled={saving}
                className="px-4 py-2 rounded-lg text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-50"
              >
                {saving ? '保存中...' : '保存'}
              </button>
            </section>

            {/* ── Proxy Section ── */}
            <section className="space-y-3 border-t border-gray-200 dark:border-[#2a2d3a] pt-5">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
                  <Globe size={15} className="text-gray-400" />
                  代理
                </h3>
                <button
                  type="button"
                  onClick={() => setShowAddProxy(!showAddProxy)}
                  className="p-1.5 rounded-lg text-gray-400 hover:text-blue-500 hover:bg-blue-50 dark:hover:bg-blue-500/10 transition-colors"
                  title="添加代理"
                >
                  <Plus size={16} />
                </button>
              </div>

              {showAddProxy && (
                <div className="space-y-2 p-3 rounded-xl border border-dashed border-blue-300 dark:border-blue-500/30 bg-blue-50/30 dark:bg-blue-500/5">
                  <input
                    type="text"
                    placeholder="名称"
                    value={newProxyName}
                    onChange={(e) => setNewProxyName(e.target.value)}
                    className="w-full px-3 py-1.5 bg-white dark:bg-[#0f1117] border border-gray-200 dark:border-[#2a2d3a] rounded-lg text-sm text-gray-900 dark:text-gray-200 focus:outline-none focus:ring-2 focus:ring-blue-500/50"
                  />
                  <input
                    type="text"
                    placeholder="http://user:pass@host:port"
                    value={newProxyUrl}
                    onChange={(e) => setNewProxyUrl(e.target.value)}
                    className="w-full px-3 py-1.5 bg-white dark:bg-[#0f1117] border border-gray-200 dark:border-[#2a2d3a] rounded-lg text-xs font-mono text-gray-900 dark:text-gray-200 focus:outline-none focus:ring-2 focus:ring-blue-500/50"
                  />
                  <div className="flex justify-end gap-2">
                    <button
                      type="button"
                      onClick={() => { setShowAddProxy(false); setNewProxyName(''); setNewProxyUrl(''); }}
                      className="px-3 py-1 rounded-lg text-xs text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800"
                    >
                      取消
                    </button>
                    <button
                      type="button"
                      onClick={handleAddProxy}
                      disabled={addingProxy || !newProxyName.trim() || !newProxyUrl.trim()}
                      className="px-3 py-1 rounded-lg text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-50"
                    >
                      {addingProxy ? '...' : '添加'}
                    </button>
                  </div>
                </div>
              )}

              {proxies.length === 0 && !showAddProxy && (
                <p className="text-xs text-gray-400 dark:text-gray-500 py-2">暂无代理</p>
              )}

              <div className="space-y-1.5">
                {proxies.map((p) => (
                  <div
                    key={p.id}
                    className="group flex items-center gap-2.5 px-3 py-2 rounded-xl bg-gray-50 dark:bg-[#0f1117] border border-gray-100 dark:border-[#2a2d3a] hover:border-gray-300 dark:hover:border-[#3a3d4a] transition-colors"
                  >
                    {proxyStatusIcon(p)}
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium text-gray-800 dark:text-gray-200 truncate">{p.name}</div>
                      <div className="text-[11px] font-mono text-gray-400 dark:text-gray-500 truncate">{p.url.replace(/\/\/([^:]+):([^@]+)@/, '//$1:***@')}</div>
                    </div>
                    <button
                      type="button"
                      onClick={() => handleCheckProxy(p.id)}
                      disabled={checkingId === p.id}
                      className="p-1 rounded-md text-gray-400 hover:text-emerald-500 hover:bg-emerald-50 dark:hover:bg-emerald-500/10 transition-colors opacity-0 group-hover:opacity-100"
                      title="测试连接"
                    >
                      <Wifi size={14} />
                    </button>
                    <button
                      type="button"
                      onClick={() => handleDeleteProxy(p.id)}
                      className="p-1 rounded-md text-gray-400 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-500/10 transition-colors opacity-0 group-hover:opacity-100"
                      title="删除"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                ))}
              </div>
            </section>

            <section className="space-y-4 border-t border-gray-200 dark:border-[#2a2d3a] pt-5">
              <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">管理员</h3>

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                  API Key
                </label>
                <div className="flex gap-2">
                  <input
                    readOnly
                    value={account?.api_key ?? ''}
                    className="flex-1 min-w-0 px-3 py-2 bg-gray-50 dark:bg-[#0f1117] border border-gray-200 dark:border-[#2a2d3a] rounded-lg text-xs font-mono text-gray-900 dark:text-gray-200"
                  />
                  <button
                    type="button"
                    onClick={() => copyApiKey()}
                    className="p-2 rounded-lg bg-gray-100 dark:bg-[#2a2d3a] text-gray-700 dark:text-gray-200 hover:bg-gray-200 dark:hover:bg-[#3a3d4a]"
                    title="复制"
                  >
                    <Copy size={16} />
                  </button>
                  <button
                    type="button"
                    onClick={handleRotateApiKey}
                    disabled={rotating}
                    className="p-2 rounded-lg bg-gray-100 dark:bg-[#2a2d3a] text-gray-700 dark:text-gray-200 hover:bg-gray-200 dark:hover:bg-[#3a3d4a] disabled:opacity-50"
                    title="更换"
                  >
                    <RefreshCw size={16} className={rotating ? 'animate-spin' : ''} />
                  </button>
                </div>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <input
                  type="password"
                  placeholder="当前密码"
                  value={currentPassword}
                  onChange={(event) => setCurrentPassword(event.target.value)}
                  className="px-3 py-2 bg-gray-50 dark:bg-[#0f1117] border border-gray-200 dark:border-[#2a2d3a] rounded-lg text-sm text-gray-900 dark:text-gray-200 focus:outline-none focus:ring-2 focus:ring-blue-500/50"
                />
                <input
                  type="password"
                  placeholder="新密码"
                  value={newPassword}
                  onChange={(event) => setNewPassword(event.target.value)}
                  className="px-3 py-2 bg-gray-50 dark:bg-[#0f1117] border border-gray-200 dark:border-[#2a2d3a] rounded-lg text-sm text-gray-900 dark:text-gray-200 focus:outline-none focus:ring-2 focus:ring-blue-500/50"
                />
                <input
                  type="password"
                  placeholder="确认密码"
                  value={confirmPassword}
                  onChange={(event) => setConfirmPassword(event.target.value)}
                  className="px-3 py-2 bg-gray-50 dark:bg-[#0f1117] border border-gray-200 dark:border-[#2a2d3a] rounded-lg text-sm text-gray-900 dark:text-gray-200 focus:outline-none focus:ring-2 focus:ring-blue-500/50"
                />
              </div>

              <button
                type="button"
                onClick={handleChangePassword}
                disabled={!currentPassword || !newPassword || !confirmPassword}
                className="px-4 py-2 rounded-lg text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-50"
              >
                更新密码
              </button>
            </section>
          </div>

          {(statusText || errorText) && (
            <div className={`mt-4 flex items-center gap-2 text-sm ${errorText ? 'text-red-500' : 'text-green-600 dark:text-green-400'}`}>
              {!errorText && <Check size={16} />}
              {errorText || statusText}
            </div>
          )}

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
