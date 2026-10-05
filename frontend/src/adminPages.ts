// Chunk loaders for the admin pages. App.tsx wraps them in React.lazy; the console layout
// calls prefetchAdminPages() once it is up so switching tabs never waits on the network.
export const loadTeamManagement = () => import('./pages/admin/TeamManagement');
export const loadUserManagement = () => import('./pages/admin/UserManagement');
export const loadAccessTokens = () => import('./pages/admin/AccessTokens');
export const loadTgPatrol = () => import('./pages/admin/TgPatrol');
export const loadSystemLogs = () => import('./pages/admin/SystemLogs');
export const loadFinance = () => import('./pages/admin/Finance');

export function prefetchAdminPages(): void {
  for (const load of [loadTeamManagement, loadUserManagement, loadAccessTokens, loadTgPatrol, loadSystemLogs, loadFinance]) {
    void load().catch(() => {
      // A failed prefetch is retried by React.lazy when the tab is actually opened.
    });
  }
}
