import { lazy, Suspense } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import PageLoading from './components/PageLoading';
import {
  loadAccessTokens,
  loadFinance,
  loadSystemLogs,
  loadTeamManagement,
  loadTgPatrol,
  loadUserManagement,
} from './adminPages';

// Every route is its own chunk: the public self-service page never downloads admin code,
// and the admin login screen does not download the console behind it.
const JoinPage = lazy(() => import('./components/JoinPage'));
const AdminRoot = lazy(() => import('./components/AdminRoot'));
const TeamManagement = lazy(loadTeamManagement);
const UserManagement = lazy(loadUserManagement);
const AccessTokens = lazy(loadAccessTokens);
const TgPatrol = lazy(loadTgPatrol);
const SystemLogs = lazy(loadSystemLogs);
const Finance = lazy(loadFinance);

export default function App() {
  return (
    <BrowserRouter>
      <Suspense fallback={<PageLoading fullScreen />}>
        <Routes>
          <Route path="/" element={<JoinPage />} />

          {/* AdminRoot = terms → login → console layout; pages render in its <Outlet />. */}
          <Route path="/admin" element={<AdminRoot />}>
            <Route index element={<Navigate to="/admin/dashboard" replace />} />
            {/* The dashboard is drawn by the layout itself (it shares the team list with the header). */}
            <Route path="dashboard" element={null} />
            <Route path="teams" element={<TeamManagement />} />
            <Route path="users" element={<UserManagement />} />
            <Route path="access-tokens" element={<AccessTokens />} />
            <Route path="logs" element={<SystemLogs />} />
            <Route path="finance" element={<Finance />} />
            <Route path="tg-patrol" element={<TgPatrol />} />
          </Route>
        </Routes>
      </Suspense>
    </BrowserRouter>
  );
}
