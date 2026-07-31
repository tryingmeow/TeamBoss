import { useEffect, useMemo, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { Globe, Loader2, RefreshCw, Settings, X } from 'lucide-react';
import type { SeatType, TeamWorkspaceSettings } from '../types';
import {
  checkProxy,
  fetchProxies,
  fetchTeamWorkspaceSettings,
  updateTeamDefaultSeatType,
  updateTeamProxy,
  type Proxy,
} from '../api/client';
import { formatSeatTypeLabel, normalizeSeatType } from '../lib/seatType';
import ConfirmDialog from './ConfirmDialog';

interface TeamSettingsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  teamId: string;
  currentProxyId: number | null;
  initialSettings?: TeamWorkspaceSettings | null;
  onChanged?: (settings: TeamWorkspaceSettings) => void;
  onProxyChanged?: (proxyId: number | null) => void;
}

const seatLabel = formatSeatTypeLabel;

function normalizeWorkspaceSeatType(value: TeamWorkspaceSettings['default_seat_type']): SeatType {
  return normalizeSeatType(value);
}

export default function TeamSettingsDialog({
  open,
  onOpenChange,
  teamId,
  currentProxyId,
  initialSettings,
  onChanged,
  onProxyChanged,
}: TeamSettingsDialogProps) {
  const [settings, setSettings] = useState<TeamWorkspaceSettings | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [error, setError] = useState('');

  const [proxies, setProxies] = useState<Proxy[]>([]);
  const [selectedProxyId, setSelectedProxyId] = useState<number | null>(currentProxyId);
  const [savingProxy, setSavingProxy] = useState(false);

  useEffect(() => {
    if (!open) return;

    let cancelled = false;
    if (initialSettings) {
      setSettings({ ...initialSettings, default_seat_type: normalizeWorkspaceSeatType(initialSettings.default_seat_type) });
    }
    setLoading(true);
    setError('');
    setSelectedProxyId(currentProxyId);

    fetchTeamWorkspaceSettings(teamId)
      .then((data) => {
        if (!cancelled) setSettings({ ...data, default_seat_type: normalizeWorkspaceSeatType(data.default_seat_type) });
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : '加载失败');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    fetchProxies()
      .then(async (list) => {
        if (cancelled) return;
        setProxies(list);
        if (currentProxyId == null) return;
        const current = list.find((p) => p.id === currentProxyId);
        if (!current || current.status !== 'unknown') return;
        try {
          const result = await checkProxy(currentProxyId);
          if (!cancelled) {
            setProxies((prev) =>
              prev.map((p) =>
                p.id === currentProxyId
                  ? { ...p, status: result.status, last_check_at: result.last_check_at }
                  : p,
              ),
            );
          }
        } catch {
          if (!cancelled) {
            setProxies((prev) => prev.map((p) => (p.id === currentProxyId ? { ...p, status: 'error' } : p)));
          }
        }
      })
      .catch(() => {});

    return () => {
      cancelled = true;
    };
  }, [open, teamId, initialSettings, currentProxyId]);

  const currentSeat = useMemo<SeatType>(
    () => normalizeWorkspaceSeatType(settings?.default_seat_type ?? 'default'),
    [settings]
  );
  const nextSeat: SeatType = currentSeat === 'usage_based' ? 'default' : 'usage_based';

  const handleConfirmSwitch = async () => {
    setSaving(true);
    setError('');
    try {
      const updated = await updateTeamDefaultSeatType(teamId, nextSeat);
      const normalized = { ...updated, default_seat_type: normalizeWorkspaceSeatType(updated.default_seat_type) };
      setSettings(normalized);
      setConfirmOpen(false);
      onChanged?.(normalized);
    } catch (err) {
      setError(err instanceof Error ? err.message : '切换失败');
    } finally {
      setSaving(false);
    }
  };

  const applyProxyCheckResult = (proxyId: number, status: string, lastCheckAt?: string) => {
    setProxies((prev) =>
      prev.map((p) =>
        p.id === proxyId ? { ...p, status, ...(lastCheckAt ? { last_check_at: lastCheckAt } : {}) } : p,
      ),
    );
  };

  const handleProxyChange = async (newId: number | null) => {
    setSelectedProxyId(newId);
    setSavingProxy(true);
    setError('');
    try {
      await updateTeamProxy(teamId, newId);
      onProxyChanged?.(newId);
      if (newId != null) {
        try {
          const result = await checkProxy(newId);
          applyProxyCheckResult(newId, result.status, result.last_check_at);
        } catch {
          applyProxyCheckResult(newId, 'error');
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '切换失败');
      setSelectedProxyId(currentProxyId);
    } finally {
      setSavingProxy(false);
    }
  };

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen) setConfirmOpen(false);
    onOpenChange(nextOpen);
  };

  const proxyStatusDot = (p: Proxy) => {
    if (p.status === 'ok') return 'bg-emerald-500';
    if (p.status === 'error') return 'bg-red-400';
    return 'bg-gray-400';
  };

  return (
    <>
      <Dialog.Root open={open} onOpenChange={handleOpenChange}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-black/60 z-50" />
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-[calc(100vw-2rem)] max-w-sm -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-gray-200 bg-white p-5 shadow-2xl dark:border-[#2a2d3a] dark:bg-[#1a1d27]">
            <Dialog.Title className="flex items-center gap-2 text-base font-bold text-gray-900 dark:text-gray-100">
              <Settings size={17} />
              设置
            </Dialog.Title>

            <div className="mt-5 space-y-4">
              {/* Seat type */}
              <div className="rounded-xl border border-gray-100 bg-gray-50/70 p-3 dark:border-[#2a2d3a] dark:bg-[#0f1117]">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-sm font-medium text-gray-700 dark:text-gray-300">默认席位</span>
                  <button
                    type="button"
                    disabled={loading || !settings}
                    onClick={() => setConfirmOpen(true)}
                    className={`group inline-flex items-center overflow-hidden rounded-full text-xs font-semibold ring-1 transition-all disabled:cursor-not-allowed disabled:opacity-50 ${
                      currentSeat === 'usage_based'
                        ? 'bg-purple-50 text-purple-700 ring-purple-200 hover:bg-purple-100 dark:bg-purple-500/10 dark:text-purple-300 dark:ring-purple-500/30 dark:hover:bg-purple-500/20'
                        : 'bg-blue-50 text-blue-700 ring-blue-200 hover:bg-blue-100 dark:bg-blue-500/10 dark:text-blue-300 dark:ring-blue-500/30 dark:hover:bg-blue-500/20'
                    }`}
                    aria-label="切换默认席位"
                    title="切换"
                  >
                    <span className="flex items-center gap-1.5 px-3 py-1.5">
                      {loading ? (
                        <>
                          <Loader2 size={14} className="animate-spin" />
                          <span>查询中...</span>
                        </>
                      ) : (
                        seatLabel(currentSeat)
                      )}
                    </span>
                    <span className="flex h-7 w-7 items-center justify-center bg-white/70 dark:bg-white/10">
                      <RefreshCw size={12} className="transition-transform group-hover:rotate-180" />
                    </span>
                  </button>
                </div>
              </div>

              {/* Proxy selector */}
              <div className="rounded-xl border border-gray-100 bg-gray-50/70 p-3 dark:border-[#2a2d3a] dark:bg-[#0f1117]">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-sm font-medium text-gray-700 dark:text-gray-300 flex items-center gap-1.5">
                    <Globe size={14} className="text-gray-400" />
                    网络
                  </span>
                  <div className="relative">
                    <select
                      value={selectedProxyId ?? ''}
                      onChange={(e) => {
                        const val = e.target.value;
                        handleProxyChange(val === '' ? null : Number(val));
                      }}
                      disabled={savingProxy}
                      className="appearance-none pl-3 pr-7 py-1.5 rounded-full text-xs font-semibold bg-gray-100 dark:bg-[#2a2d3a] text-gray-700 dark:text-gray-300 border-none focus:outline-none focus:ring-2 focus:ring-blue-500/50 cursor-pointer disabled:opacity-50"
                    >
                      <option value="">直连</option>
                      {proxies.map((p) => (
                        <option key={p.id} value={p.id}>{p.name}</option>
                      ))}
                    </select>
                    <div className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2">
                      {savingProxy ? (
                        <Loader2 size={12} className="animate-spin text-gray-400" />
                      ) : (
                        <span className={`block w-2 h-2 rounded-full ${
                          selectedProxyId == null ? 'bg-emerald-500' : proxyStatusDot(proxies.find((p) => p.id === selectedProxyId) ?? { status: 'unknown' } as Proxy)
                        }`} />
                      )}
                    </div>
                  </div>
                </div>
              </div>
            </div>

            {error && <p className="mt-3 text-xs text-red-500 dark:text-red-400">{error}</p>}

            <Dialog.Close asChild>
              <button className="absolute right-4 top-4 rounded-md p-1 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-200">
                <X size={16} />
              </button>
            </Dialog.Close>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="切换默认席位"
        message={`切换为 ${formatSeatTypeLabel(nextSeat)}？`}
        confirmLabel="切换"
        loading={saving}
        onConfirm={handleConfirmSwitch}
      />
    </>
  );
}
