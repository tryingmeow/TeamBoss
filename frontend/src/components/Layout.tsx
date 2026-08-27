import { useCallback, useEffect, useState, useRef } from 'react';
import {
  Plus,
  Download,
  Upload,
  RefreshCw,
  Settings,
  Search,
  Users,
  UserPlus,
  Sun,
  Moon,
  Repeat,
  RepeatOff,
} from 'lucide-react';
import * as Tooltip from '@radix-ui/react-tooltip';
import type { Team, Settings as SettingsType } from '../types';
import { useTeams } from '../hooks/useTeams';
import { useSettings } from '../hooks/useSettings';
import { exportAllSessions, importSessions, inviteGptMembers, type InviteGptMembersResult } from '../api/client';
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import Dashboard from './Dashboard';
import DashboardSortControl, { type SortDirection, type SortKey } from './DashboardSortControl';
import AddTeamDialog from './AddTeamDialog';
import AddMemberDialog from './AddMemberDialog';
import SettingsDialog from './SettingsDialog';
import Toast from './Toast';

interface ToastMessage {
  id: number;
  text: string;
  type: 'success' | 'error';
}

const AUTO_REFRESH_STORAGE_KEY = 'auto_team_auto_refresh';

export default function Layout() {
  const location = useLocation();
  const navigate = useNavigate();
  const { settings, save: saveSettings } = useSettings();
  const [autoRefresh, setAutoRefresh] = useState(() => {
    return window.localStorage.getItem(AUTO_REFRESH_STORAGE_KEY) !== 'false';
  });
  const {
    teams,
    loading,
    error,
    syncFailures,
    manualRefresh,
    refresh,
    clearSyncFailure,
    setTeams,
  } = useTeams(
    settings.sync_interval_minutes,
    autoRefresh
  );
  const [search, setSearch] = useState('');
  const [addTeamOpen, setAddTeamOpen] = useState(false);
  const [reimportTarget, setReimportTarget] = useState<Team | null>(null);
  const [addGptMembersOpen, setAddGptMembersOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [dashboardSortKey, setDashboardSortKey] = useState<SortKey>('idle');
  const [dashboardSortDirection, setDashboardSortDirection] = useState<SortDirection>('desc');
  const [toasts, setToasts] = useState<ToastMessage[]>([]);
  const [isDark, setIsDark] = useState(() => document.documentElement.classList.contains('dark'));
  const fileInputRef = useRef<HTMLInputElement>(null);
  const navRef = useRef<HTMLDivElement>(null);
  const toastIdRef = useRef(0);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      const nav = navRef.current;
      const active = nav?.querySelector<HTMLElement>('[aria-current="page"]');
      if (!nav || !active) return;
      nav.scrollTo({
        left: Math.max(active.offsetLeft - (nav.clientWidth - active.offsetWidth) / 2, 0),
        behavior: 'smooth',
      });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [location.pathname]);

  const toggleTheme = () => {
    const root = document.documentElement;
    if (isDark) {
      root.classList.remove('dark');
      setIsDark(false);
    } else {
      root.classList.add('dark');
      setIsDark(true);
    }
  };

  const showToast = useCallback((text: string, type: 'success' | 'error' = 'success') => {
    const id = ++toastIdRef.current;
    setToasts((prev) => [...prev, { id, text, type }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 3000);
  }, []);

  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      await manualRefresh();
      showToast('刷新成功');
    } catch (error) {
      showToast(error instanceof Error ? error.message : '刷新失败', 'error');
    } finally {
      setRefreshing(false);
    }
  };

  const handleToggleAutoRefresh = () => {
    const nextAutoRefresh = !autoRefresh;
    setAutoRefresh(nextAutoRefresh);
    window.localStorage.setItem(AUTO_REFRESH_STORAGE_KEY, String(nextAutoRefresh));
    showToast(nextAutoRefresh ? '已开启自动刷新' : '已关闭自动刷新');
  };

  const handleExport = async () => {
    try {
      const data = await exportAllSessions();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `teams_export_${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
      showToast('导出成功');
    } catch {
      showToast('导出失败', 'error');
    }
  };

  const handleImport = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const text = await file.text();
      const data = JSON.parse(text);
      await importSessions(data);
      await refresh(true);
      showToast('导入成功');
    } catch {
      showToast('导入失败', 'error');
    }
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const handleDelete = (id: string) => {
    setTeams(teams.filter((t) => t.id !== id));
    clearSyncFailure(id);
    showToast('已删除');
  };

  const handleOpenAddTeam = () => {
    setReimportTarget(null);
    setAddTeamOpen(true);
  };

  const handleReimport = (team: Team) => {
    setReimportTarget(team);
    setAddTeamOpen(true);
  };

  const handleAddTeamOpenChange = (open: boolean) => {
    setAddTeamOpen(open);
    if (!open) setReimportTarget(null);
  };

  const handleTeamSynced = (updatedTeam: Team) => {
    setTeams((prev) => prev.map((team) => (team.id === updatedTeam.id ? updatedTeam : team)));
  };

  const handleTeamSyncSucceeded = (updatedTeam: Team) => {
    setTeams((prev) => prev.map((team) => (team.id === updatedTeam.id ? updatedTeam : team)));
    clearSyncFailure(updatedTeam.id);
  };

  const handleSaveSettings = async (data: Partial<SettingsType>) => {
    await saveSettings(data);
    showToast('设置已保存');
  };

  const handleGptMembersAdded = async (result?: unknown) => {
    const data = result as InviteGptMembersResult | undefined;
    const added = data?.added?.length ?? 0;
    const failed = data?.failed?.length ?? 0;
    await refresh(false);
    if (failed > 0) {
      showToast(`已添加 ${added} 个，失败 ${failed} 个`, 'error');
      return;
    }
    showToast(added > 0 ? `已添加 ${added} 个 GPT 成员` : '已提交 GPT 成员邀请');
  };

  const isDashboard = location.pathname.replace(/\/+$/, '') === '/admin/dashboard';

  return (
    <Tooltip.Provider delayDuration={200}>
      <div className="min-h-screen flex flex-col bg-gray-50 dark:bg-[#0f1117] transition-colors duration-300">
        <header className="sticky top-0 z-40 bg-white/80 dark:bg-[#0f1117]/80 backdrop-blur-md border-b border-gray-200 dark:border-[#2a2d3a] transition-colors duration-300 shadow-sm">
          <div className="flex items-center justify-between px-4 h-16">
            <div className="flex shrink-0 items-center gap-3">
              <div className="w-8 h-8 rounded-xl bg-blue-600/10 flex items-center justify-center">
                <Users size={20} className="text-blue-600 dark:text-blue-500" />
              </div>
              <h1 className="text-lg font-bold text-gray-900 dark:text-gray-100 hidden sm:block tracking-tight">
                Team Manager
              </h1>
            </div>

            <div className="relative max-w-md w-full mx-6 hidden md:block">
              <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 dark:text-gray-500" />
              <input
                type="text"
                placeholder="搜索 Team..."
                value={search}
                onChange={(e) => {
                  setSearch(e.target.value);
                  if (location.pathname !== '/admin/dashboard') {
                    navigate('/admin/dashboard');
                  }
                }}
                className="w-full pl-10 pr-4 py-2 bg-gray-100 dark:bg-[#1a1d27] border border-transparent dark:border-[#2a2d3a] rounded-xl text-sm text-gray-900 dark:text-gray-200 placeholder:text-gray-500 dark:placeholder:text-gray-600 focus:outline-none focus:bg-white dark:focus:bg-[#1a1d27] focus:ring-2 focus:ring-blue-500/50 transition-all duration-200"
              />
            </div>

            <div className="flex min-w-0 items-center gap-1.5 overflow-x-auto">
              <Tooltip.Root>
                <Tooltip.Trigger asChild>
                  <button
                    onClick={handleToggleAutoRefresh}
                    aria-pressed={autoRefresh}
                    className={`p-2.5 rounded-xl transition-all duration-200 ${
                      autoRefresh
                        ? 'text-blue-600 dark:text-blue-400'
                        : 'text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-200'
                    }`}
                  >
                    {autoRefresh ? <Repeat size={20} /> : <RepeatOff size={20} />}
                  </button>
                </Tooltip.Trigger>
                <Tooltip.Portal>
                  <Tooltip.Content className="bg-white dark:bg-[#2a2d3a] text-gray-900 dark:text-gray-200 text-xs px-3 py-1.5 rounded-md shadow-xl border border-gray-100 dark:border-transparent" sideOffset={5}>
                    {autoRefresh ? '自动刷新已开启' : '自动刷新已关闭'}
                  </Tooltip.Content>
                </Tooltip.Portal>
              </Tooltip.Root>

              <Tooltip.Root>
                <Tooltip.Trigger asChild>
                  <button
                    onClick={handleOpenAddTeam}
                    className="flex items-center gap-2 px-4 py-2.5 bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-700 hover:to-indigo-700 text-white rounded-xl text-sm font-semibold shadow-md shadow-blue-500/20 hover:shadow-blue-500/30 transition-all duration-200 transform hover:-translate-y-0.5 mr-2"
                  >
                    <Plus size={18} strokeWidth={2.5} />
                    <span className="hidden sm:inline">添加 Team</span>
                  </button>
                </Tooltip.Trigger>
                <Tooltip.Portal>
                  <Tooltip.Content
                    className="bg-white dark:bg-[#2a2d3a] text-gray-900 dark:text-gray-200 text-xs px-3 py-1.5 rounded-md shadow-xl border border-gray-100 dark:border-transparent sm:hidden"
                    sideOffset={5}
                  >
                    添加 Team
                  </Tooltip.Content>
                </Tooltip.Portal>
              </Tooltip.Root>

              <Tooltip.Root>
                <Tooltip.Trigger asChild>
                  <button
                    onClick={() => setAddGptMembersOpen(true)}
                    className="flex items-center gap-2 px-3 py-2.5 bg-gradient-to-r from-violet-500 to-fuchsia-500 hover:from-violet-600 hover:to-fuchsia-600 text-white rounded-xl text-sm font-semibold shadow-md shadow-violet-500/25 hover:shadow-violet-500/40 transition-all duration-200 transform hover:-translate-y-0.5 mr-2 border border-violet-400/20"
                  >
                    <UserPlus size={18} strokeWidth={2.5} />
                    <span className="hidden sm:inline">GPT 成员</span>
                  </button>
                </Tooltip.Trigger>
                <Tooltip.Portal>
                  <Tooltip.Content
                    className="bg-white dark:bg-[#2a2d3a] text-gray-900 dark:text-gray-200 text-xs px-3 py-1.5 rounded-md shadow-xl border border-gray-100 dark:border-transparent sm:hidden"
                    sideOffset={5}
                  >
                    +GPT成员
                  </Tooltip.Content>
                </Tooltip.Portal>
              </Tooltip.Root>

              <Tooltip.Root>
                <Tooltip.Trigger asChild>
                  <button
                    onClick={toggleTheme}
                    className="p-2 text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-[#1a1d27] rounded-xl transition-all duration-200"
                  >
                    {isDark ? <Sun size={18} /> : <Moon size={18} />}
                  </button>
                </Tooltip.Trigger>
                <Tooltip.Portal>
                  <Tooltip.Content className="bg-white dark:bg-[#2a2d3a] text-gray-900 dark:text-gray-200 text-xs px-3 py-1.5 rounded-md shadow-xl border border-gray-100 dark:border-transparent" sideOffset={5}>
                    切换主题
                  </Tooltip.Content>
                </Tooltip.Portal>
              </Tooltip.Root>

              <Tooltip.Root>
                <Tooltip.Trigger asChild>
                  <button
                    onClick={handleExport}
                    className="shrink-0 p-2 text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-[#1a1d27] rounded-xl transition-all duration-200"
                  >
                    <Download size={18} />
                  </button>
                </Tooltip.Trigger>
                <Tooltip.Portal>
                  <Tooltip.Content className="bg-white dark:bg-[#2a2d3a] text-gray-900 dark:text-gray-200 text-xs px-3 py-1.5 rounded-md shadow-xl border border-gray-100 dark:border-transparent" sideOffset={5}>
                    导出 Sessions
                  </Tooltip.Content>
                </Tooltip.Portal>
              </Tooltip.Root>

              <Tooltip.Root>
                <Tooltip.Trigger asChild>
                  <button
                    onClick={() => fileInputRef.current?.click()}
                    className="shrink-0 p-2 text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-[#1a1d27] rounded-xl transition-all duration-200"
                  >
                    <Upload size={18} />
                  </button>
                </Tooltip.Trigger>
                <Tooltip.Portal>
                  <Tooltip.Content className="bg-white dark:bg-[#2a2d3a] text-gray-900 dark:text-gray-200 text-xs px-3 py-1.5 rounded-md shadow-xl border border-gray-100 dark:border-transparent" sideOffset={5}>
                    导入 Sessions
                  </Tooltip.Content>
                </Tooltip.Portal>
              </Tooltip.Root>
              <input
                ref={fileInputRef}
                type="file"
                accept=".json"
                onChange={handleImport}
                className="hidden"
              />

              <Tooltip.Root>
                <Tooltip.Trigger asChild>
                  <button
                    onClick={handleRefresh}
                    disabled={refreshing}
                    aria-label="刷新全部 Team"
                    title="刷新全部 Team"
                    className="p-2 text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-[#1a1d27] rounded-xl transition-all duration-200 disabled:opacity-50"
                  >
                    <RefreshCw size={18} className={refreshing ? 'animate-spin text-blue-500' : ''} />
                  </button>
                </Tooltip.Trigger>
                <Tooltip.Portal>
                  <Tooltip.Content className="bg-white dark:bg-[#2a2d3a] text-gray-900 dark:text-gray-200 text-xs px-3 py-1.5 rounded-md shadow-xl border border-gray-100 dark:border-transparent" sideOffset={5}>
                    刷新全部
                  </Tooltip.Content>
                </Tooltip.Portal>
              </Tooltip.Root>

              <Tooltip.Root>
                <Tooltip.Trigger asChild>
                  <button
                    onClick={() => setSettingsOpen(true)}
                    className="p-2 text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-[#1a1d27] rounded-xl transition-all duration-200"
                  >
                    <Settings size={18} />
                  </button>
                </Tooltip.Trigger>
                <Tooltip.Portal>
                  <Tooltip.Content className="bg-white dark:bg-[#2a2d3a] text-gray-900 dark:text-gray-200 text-xs px-3 py-1.5 rounded-md shadow-xl border border-gray-100 dark:border-transparent" sideOffset={5}>
                    设置
                  </Tooltip.Content>
                </Tooltip.Portal>
              </Tooltip.Root>
            </div>
          </div>
        </header>

        <main className="flex-1 p-6">
          <div className="-mx-6 mb-6 flex items-end border-b border-gray-200 dark:border-[#2a2d3a] sm:mx-0">
            <div ref={navRef} className="flex min-w-0 flex-1 gap-4 overflow-x-auto px-6 sm:px-4">
            <NavLink
              to="/admin/dashboard"
              className={({ isActive }) =>
                `shrink-0 whitespace-nowrap pb-3 px-2 text-sm font-medium transition-colors border-b-2 ${
                  isActive ? 'border-blue-500 text-blue-600 dark:text-blue-400' : 'border-transparent text-gray-500 hover:text-gray-900 dark:hover:text-gray-300'
                }`
              }
            >
              Dashboard
            </NavLink>
            <NavLink
              to="/admin/teams"
              className={({ isActive }) =>
                `shrink-0 whitespace-nowrap pb-3 px-2 text-sm font-medium transition-colors border-b-2 ${
                  isActive ? 'border-blue-500 text-blue-600 dark:text-blue-400' : 'border-transparent text-gray-500 hover:text-gray-900 dark:hover:text-gray-300'
                }`
              }
            >
              队伍概览
            </NavLink>
            <NavLink
              to="/admin/users"
              className={({ isActive }) =>
                `shrink-0 whitespace-nowrap pb-3 px-2 text-sm font-medium transition-colors border-b-2 ${
                  isActive ? 'border-blue-500 text-blue-600 dark:text-blue-400' : 'border-transparent text-gray-500 hover:text-gray-900 dark:hover:text-gray-300'
                }`
              }
            >
              用户管理
            </NavLink>
            <NavLink
              to="/admin/access-tokens"
              className={({ isActive }) =>
                `shrink-0 whitespace-nowrap pb-3 px-2 text-sm font-medium transition-colors border-b-2 ${
                  isActive ? 'border-blue-500 text-blue-600 dark:text-blue-400' : 'border-transparent text-gray-500 hover:text-gray-900 dark:hover:text-gray-300'
                }`
              }
            >
              兑换码
            </NavLink>
            <NavLink
              to="/admin/logs"
              className={({ isActive }) =>
                `shrink-0 whitespace-nowrap pb-3 px-2 text-sm font-medium transition-colors border-b-2 ${
                  isActive ? 'border-blue-500 text-blue-600 dark:text-blue-400' : 'border-transparent text-gray-500 hover:text-gray-900 dark:hover:text-gray-300'
                }`
              }
            >
              系统日志
            </NavLink>
            <NavLink
              to="/admin/finance"
              className={({ isActive }) =>
                `shrink-0 whitespace-nowrap pb-3 px-2 text-sm font-medium transition-colors border-b-2 ${
                  isActive ? 'border-blue-500 text-blue-600 dark:text-blue-400' : 'border-transparent text-gray-500 hover:text-gray-900 dark:hover:text-gray-300'
                }`
              }
            >
              财务
            </NavLink>
            <NavLink
              to="/admin/tg-patrol"
              className={({ isActive }) =>
                `shrink-0 whitespace-nowrap pb-3 px-2 text-sm font-medium transition-colors border-b-2 ${
                  isActive ? 'border-blue-500 text-blue-600 dark:text-blue-400' : 'border-transparent text-gray-500 hover:text-gray-900 dark:hover:text-gray-300'
                }`
              }
            >
              TG 机器人 & 巡逻
            </NavLink>
            </div>

            {isDashboard && (
              <div className="shrink-0 pb-2 pl-2 pr-4">
                <DashboardSortControl
                  sortKey={dashboardSortKey}
                  sortDirection={dashboardSortDirection}
                  onSortKeyChange={setDashboardSortKey}
                  onSortDirectionChange={setDashboardSortDirection}
                />
              </div>
            )}
          </div>

          {isDashboard ? (
            <Dashboard
              teams={teams}
              loading={loading}
              error={error}
              search={search}
              onDelete={handleDelete}
              onReimport={handleReimport}
              onTeamSynced={handleTeamSynced}
              onTeamSyncSucceeded={handleTeamSyncSucceeded}
              syncFailures={syncFailures}
              showToast={showToast}
              sortKey={dashboardSortKey}
              sortDirection={dashboardSortDirection}
            />
          ) : (
            <Outlet />
          )}
        </main>

        <AddTeamDialog
          open={addTeamOpen}
          onOpenChange={handleAddTeamOpenChange}
          onSuccess={() => refresh(true)}
          team={reimportTarget}
        />

        <AddMemberDialog
          open={addGptMembersOpen}
          onOpenChange={setAddGptMembersOpen}
          title="添加 GPT 成员"
          fixedSeatType="default"
          submitLabel="添加"
          submitInvites={({ emails, expires_in, allow_overage }) =>
            inviteGptMembers({ emails, expires_in, allow_overage })
          }
          onSuccess={handleGptMembersAdded}
        />

        <SettingsDialog
          open={settingsOpen}
          onOpenChange={setSettingsOpen}
          settings={settings}
          onSave={handleSaveSettings}
        />

        <div className="fixed bottom-4 right-4 z-50 flex max-w-[calc(100vw-2rem)] flex-col gap-2 sm:max-w-lg">
          {toasts.map((toast) => (
            <Toast key={toast.id} text={toast.text} type={toast.type} />
          ))}
        </div>
      </div>
    </Tooltip.Provider>
  );
}
