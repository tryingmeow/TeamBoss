import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Check,
  Download,
  EllipsisVertical,
  LogOut,
  Plus,
  RefreshCw,
  Search,
  Settings,
  Upload,
  UserPlus,
} from 'lucide-react';
import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import * as Tooltip from '@radix-ui/react-tooltip';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import type { Team, Settings as SettingsType } from '../types';
import { useTeams } from '../hooks/useTeams';
import { useSettings } from '../hooks/useSettings';
import {
  exportAllSessions,
  importSessions,
  inviteGptMembers,
  type InviteGptMembersResult,
  clearStoredAdminApiKey,
} from '../api/client';
import { prefetchAdminPages } from '../adminPages';
import { activeChatGptSeats, chatgptPaidSeats } from '../lib/seatCapacity';
import { cn } from '../lib/utils';
import Dashboard from './Dashboard';
import DashboardSortControl, { type SortDirection, type SortKey } from './DashboardSortControl';
import AddTeamDialog from './AddTeamDialog';
import AddMemberDialog from './AddMemberDialog';
import SettingsDialog from './SettingsDialog';
import ConfirmDialog from './ConfirmDialog';
import BrandMark from './BrandMark';
import PageLoading from './PageLoading';
import PageShell from './PageShell';
import ThemeToggle from './ThemeToggle';
import Toast from './Toast';
import { BUTTON, CONTAINER, INPUT } from './ui';

interface ToastMessage {
  id: number;
  text: string;
  type: 'success' | 'error';
}

const AUTO_REFRESH_STORAGE_KEY = 'auto_team_auto_refresh';

const NAV_ITEMS = [
  { to: '/admin/dashboard', label: 'Team 列表' },
  { to: '/admin/teams', label: '数据概览' },
  { to: '/admin/users', label: '用户管理' },
  { to: '/admin/access-tokens', label: '兑换码' },
  { to: '/admin/logs', label: '系统日志' },
  { to: '/admin/finance', label: '财务' },
  { to: '/admin/tg-patrol', label: 'TG 与巡逻' },
] as const;

const TOOLTIP_CLASS =
  'z-50 rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-700 shadow-lg dark:border-ink-700 dark:bg-ink-800 dark:text-gray-200';

const MENU_ITEM_CLASS =
  'flex cursor-default select-none items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm text-gray-700 outline-none data-[highlighted]:bg-gray-100 data-[highlighted]:text-gray-900 dark:text-gray-200 dark:data-[highlighted]:bg-ink-800 dark:data-[highlighted]:text-gray-50';

function HeaderTooltip({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <Tooltip.Root>
      <Tooltip.Trigger asChild>{children}</Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content className={TOOLTIP_CLASS} sideOffset={6}>
          {label}
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}

function needsAttention(team: Team, syncFailures: Record<string, string>): boolean {
  return (
    team.status === 'token_expired' ||
    team.auth_state === 'rejected' ||
    team.subscription_status === 'expired' ||
    Boolean(syncFailures[team.id])
  );
}

export default function Layout() {
  const location = useLocation();
  const { settings, loaded: settingsLoaded, load: loadSettings, save: saveSettings } = useSettings();
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
  const [exportOpen, setExportOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [dashboardSortKey, setDashboardSortKey] = useState<SortKey>('idle');
  const [dashboardSortDirection, setDashboardSortDirection] = useState<SortDirection>('desc');
  const [toasts, setToasts] = useState<ToastMessage[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const navRef = useRef<HTMLElement>(null);
  const toastIdRef = useRef(0);
  const toastTimersRef = useRef<Set<ReturnType<typeof setTimeout>>>(new Set());

  useEffect(() => {
    const idle = window.requestIdleCallback ?? ((cb: () => void) => window.setTimeout(cb, 1500));
    idle(() => prefetchAdminPages());
  }, []);

  useEffect(() => {
    // Scroll the tab row only as far as needed to show the active tab in full.
    const frame = window.requestAnimationFrame(() => {
      const nav = navRef.current;
      const active = nav?.querySelector<HTMLElement>('[aria-current="page"]');
      if (!nav || !active) return;
      const edge = 28;
      const bounds = nav.getBoundingClientRect();
      const tab = active.getBoundingClientRect();
      if (tab.left < bounds.left + edge) {
        nav.scrollBy({ left: tab.left - bounds.left - edge, behavior: 'smooth' });
      } else if (tab.right > bounds.right - edge) {
        nav.scrollBy({ left: tab.right - bounds.right + edge, behavior: 'smooth' });
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [location.pathname]);

  useEffect(() => () => {
    toastTimersRef.current.forEach((timer) => clearTimeout(timer));
    toastTimersRef.current.clear();
  }, []);

  const showToast = useCallback((text: string, type: 'success' | 'error' = 'success') => {
    const id = ++toastIdRef.current;
    setToasts((prev) => [...prev, { id, text, type }]);
    const timer = setTimeout(() => {
      toastTimersRef.current.delete(timer);
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 3000);
    toastTimersRef.current.add(timer);
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
    setExporting(true);
    try {
      const data = await exportAllSessions();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `teams_export_${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
      setExportOpen(false);
      showToast('导出成功');
    } catch {
      showToast('导出失败', 'error');
    } finally {
      setExporting(false);
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

  const handleLogout = () => {
    clearStoredAdminApiKey();
    window.location.href = '/admin';
  };

  const handleDelete = (id: string) => {
    setTeams((prev) => prev.filter((t) => t.id !== id));
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

  // The server unbinds a deleted proxy from its Teams; mirror that at once, then re-read.
  const handleProxyDeleted = (proxyId: number) => {
    setTeams((prev) => prev.map((team) => (team.proxy_id === proxyId ? { ...team, proxy_id: null } : team)));
    void refresh();
  };

  const handleGptMembersAdded = async (result?: unknown) => {
    const data = result as InviteGptMembersResult | undefined;
    const added = data?.added?.length ?? 0;
    const failed = data?.failed?.length ?? 0;
    const noPlace = data?.no_place_emails?.length ?? 0;
    await refresh(false);
    if (failed > 0) {
      const parts = [`已添加 ${added} 个`];
      if (noPlace > 0) parts.push(`${noPlace} 个没位置未邀请`);
      if (failed - noPlace > 0) parts.push(`失败 ${failed - noPlace} 个`);
      showToast(parts.join('，'), 'error');
      return;
    }
    showToast(added > 0 ? `已添加 ${added} 个 GPT 成员` : '已提交 GPT 成员邀请');
  };

  const isDashboard = location.pathname.replace(/\/+$/, '') === '/admin/dashboard';

  const summary = useMemo(() => {
    const seatsUsed = teams.reduce((total, team) => total + activeChatGptSeats(team), 0);
    const seatsTotal = teams.reduce((total, team) => total + chatgptPaidSeats(team), 0);
    const attention = teams.filter((team) => needsAttention(team, syncFailures)).length;
    return { seatsUsed, seatsTotal, attention };
  }, [teams, syncFailures]);

  const dashboardDescription = teams.length === 0
    ? '每个 Team 是一个接入的 ChatGPT 工作区。点击卡片查看成员。'
    : (
      <>
        {teams.length} 个 Team · ChatGPT 席位 {summary.seatsUsed}/{summary.seatsTotal}
        {summary.attention > 0 && (
          <span className="text-amber-600 dark:text-amber-400"> · {summary.attention} 个需要处理</span>
        )}
        <span className="hidden xl:inline"> · 点击卡片查看成员</span>
      </>
    );

  return (
    <Tooltip.Provider delayDuration={250}>
      <div className="flex min-h-dvh flex-col bg-gray-50 dark:bg-ink-950">
        <header className="sticky top-0 z-40 border-b border-gray-200 bg-white/85 backdrop-blur-md dark:border-ink-800 dark:bg-ink-950/85">
          <div className={cn(CONTAINER, 'flex h-14 items-center justify-between gap-3')}>
            <NavLink to="/admin/dashboard" className="flex min-w-0 items-center gap-2.5" aria-label="TeamBoss 首页">
              <BrandMark size={26} />
              <span className="truncate text-[15px] font-semibold tracking-tight text-gray-900 dark:text-gray-50">TeamBoss</span>
            </NavLink>

            <div className="flex shrink-0 items-center gap-1 sm:gap-1.5">
              <HeaderTooltip label="导入 Owner 的 Session，接入一个 Team">
                <button type="button" onClick={handleOpenAddTeam} className={cn(BUTTON.primary, 'h-9 px-2.5 sm:px-3.5')} aria-label="添加 Team">
                  <Plus size={17} strokeWidth={2.4} />
                  <span className="hidden sm:inline">添加 Team</span>
                </button>
              </HeaderTooltip>
              <HeaderTooltip label="按空位自动分配 Team，占用 ChatGPT 席位">
                <button type="button" onClick={() => setAddGptMembersOpen(true)} className={cn(BUTTON.secondary, 'h-9 px-2.5 sm:px-3.5')} aria-label="添加 GPT 成员">
                  <UserPlus size={17} />
                  <span className="hidden sm:inline">添加 GPT 成员</span>
                </button>
              </HeaderTooltip>

              <span className="mx-1 hidden h-5 w-px bg-gray-200 sm:block dark:bg-ink-800" aria-hidden="true" />

              <HeaderTooltip label={autoRefresh ? `同步全部 Team（已开启每 ${settings.sync_interval_minutes} 分钟自动刷新）` : '同步全部 Team（自动刷新已关闭）'}>
                <button type="button" onClick={handleRefresh} disabled={refreshing} className={BUTTON.icon} aria-label="同步全部 Team">
                  <RefreshCw size={18} className={refreshing ? 'animate-spin text-blue-500' : ''} />
                </button>
              </HeaderTooltip>
              <ThemeToggle />
              <HeaderTooltip label="设置">
                <button type="button" onClick={() => setSettingsOpen(true)} className={cn(BUTTON.icon, 'hidden sm:inline-flex')} aria-label="设置">
                  <Settings size={18} />
                </button>
              </HeaderTooltip>

              <DropdownMenu.Root>
                <DropdownMenu.Trigger asChild>
                  <button type="button" className={BUTTON.icon} aria-label="更多操作">
                    <EllipsisVertical size={18} />
                  </button>
                </DropdownMenu.Trigger>
                <DropdownMenu.Portal>
                  <DropdownMenu.Content
                    align="end"
                    sideOffset={8}
                    collisionPadding={12}
                    className="sort-menu-content z-50 min-w-[14rem] rounded-xl border border-gray-200 bg-white p-1.5 shadow-xl dark:border-ink-800 dark:bg-ink-900"
                  >
                    <DropdownMenu.CheckboxItem
                      checked={autoRefresh}
                      onCheckedChange={handleToggleAutoRefresh}
                      className={MENU_ITEM_CLASS}
                    >
                      <span className="grid size-4 place-items-center">
                        <DropdownMenu.ItemIndicator>
                          <Check size={15} className="text-blue-600 dark:text-blue-400" />
                        </DropdownMenu.ItemIndicator>
                      </span>
                      <span className="flex flex-col">
                        <span>自动刷新</span>
                        <span className="text-xs text-gray-400 dark:text-ink-500">每 {settings.sync_interval_minutes} 分钟同步一次</span>
                      </span>
                    </DropdownMenu.CheckboxItem>
                    <DropdownMenu.Separator className="my-1 h-px bg-gray-100 dark:bg-ink-800" />
                    <DropdownMenu.Item onSelect={() => fileInputRef.current?.click()} className={MENU_ITEM_CLASS}>
                      <Upload size={16} className="text-gray-400 dark:text-ink-400" /> 导入 Sessions…
                    </DropdownMenu.Item>
                    <DropdownMenu.Item onSelect={() => setExportOpen(true)} className={MENU_ITEM_CLASS}>
                      <Download size={16} className="text-gray-400 dark:text-ink-400" /> 导出 Sessions…
                    </DropdownMenu.Item>
                    <DropdownMenu.Item onSelect={() => setSettingsOpen(true)} className={cn(MENU_ITEM_CLASS, 'sm:hidden')}>
                      <Settings size={16} className="text-gray-400 dark:text-ink-400" /> 设置
                    </DropdownMenu.Item>
                    <DropdownMenu.Separator className="my-1 h-px bg-gray-100 dark:bg-ink-800" />
                    <DropdownMenu.Item
                      onSelect={handleLogout}
                      className={cn(MENU_ITEM_CLASS, 'text-red-600 data-[highlighted]:bg-red-50 data-[highlighted]:text-red-700 dark:text-red-400 dark:data-[highlighted]:bg-red-500/10 dark:data-[highlighted]:text-red-300')}
                    >
                      <LogOut size={16} /> 退出登录
                    </DropdownMenu.Item>
                  </DropdownMenu.Content>
                </DropdownMenu.Portal>
              </DropdownMenu.Root>
              <input
                ref={fileInputRef}
                type="file"
                accept=".json"
                onChange={handleImport}
                className="hidden"
              />
            </div>
          </div>

          <nav
            ref={navRef}
            aria-label="主导航"
            className={cn(CONTAINER, 'no-scrollbar flex gap-1 overflow-x-auto [mask-image:linear-gradient(to_right,transparent,#000_16px,#000_calc(100%_-_28px),transparent)] sm:gap-2 sm:[mask-image:none]')}
          >
            {NAV_ITEMS.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  cn(
                    '-mb-px shrink-0 whitespace-nowrap border-b-2 px-2 pb-2.5 pt-1 text-sm font-medium transition-colors',
                    isActive
                      ? 'border-blue-600 text-blue-600 dark:border-blue-400 dark:text-blue-400'
                      : 'border-transparent text-gray-500 hover:text-gray-900 dark:text-ink-400 dark:hover:text-gray-100'
                  )
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
        </header>

        <main className="flex-1">
          {isDashboard ? (
            <PageShell
              title="Team 列表"
              description={dashboardDescription}
              actions={
                <div className="flex w-full items-center gap-2 md:w-auto">
                  <label className="relative min-w-0 flex-1 md:w-64 md:flex-none">
                    <span className="sr-only">搜索 Team</span>
                    <Search size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 dark:text-ink-500" />
                    <input
                      type="search"
                      placeholder="搜索 Team、邮箱、卡号…"
                      value={search}
                      onChange={(e) => setSearch(e.target.value)}
                      className={cn(INPUT, 'h-11 pl-9')}
                    />
                  </label>
                  <DashboardSortControl
                    sortKey={dashboardSortKey}
                    sortDirection={dashboardSortDirection}
                    onSortKeyChange={setDashboardSortKey}
                    onSortDirectionChange={setDashboardSortDirection}
                  />
                </div>
              }
            >
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
                onAddTeam={handleOpenAddTeam}
              />
            </PageShell>
          ) : (
            <Suspense fallback={<PageLoading />}>
              <Outlet />
            </Suspense>
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
          loaded={settingsLoaded}
          onLoad={loadSettings}
          onSave={handleSaveSettings}
          teams={teams}
          onProxyDeleted={handleProxyDeleted}
        />

        <ConfirmDialog
          open={exportOpen}
          onOpenChange={(open) => {
            if (!exporting) setExportOpen(open);
          }}
          title="导出全部 Session？"
          message={`将下载一个 JSON 文件，内含全部 ${teams.length} 个 Team 的 Owner 登录凭证（access token 与 session cookie）。拿到这个文件的人可以直接接管这些 Team，请只保存在安全的位置。`}
          confirmLabel="下载文件"
          loading={exporting}
          onConfirm={() => void handleExport()}
        />

        <div className="fixed bottom-4 right-4 z-[100] flex w-[min(24rem,calc(100vw-2rem))] flex-col gap-2">
          {toasts.map((toast) => (
            <Toast key={toast.id} text={toast.text} type={toast.type} />
          ))}
        </div>
      </div>
    </Tooltip.Provider>
  );
}
