import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import JoinPage from './components/JoinPage';
import AdminGate from './components/AdminGate';
import TermsGate from './components/TermsGate';
import Layout from './components/Layout';
import TeamManagement from './pages/admin/TeamManagement';
import UserManagement from './pages/admin/UserManagement';
import AccessTokens from './pages/admin/AccessTokens';
import TgPatrol from './pages/admin/TgPatrol';
import SystemLogs from './pages/admin/SystemLogs';
import Finance from './pages/admin/Finance';

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        {/* User Route */}
        <Route path="/" element={<JoinPage />} />
        
        {/* Admin Routes */}
        {/* TermsGate 包在 AdminGate 外面：条款要在输密码之前看到，而不是登进去才弹 */}
        <Route path="/admin" element={
          <TermsGate>
            <AdminGate>
              <Layout />
            </AdminGate>
          </TermsGate>
        }>
          {/* Sub routes inside Layout */}
          <Route index element={<Navigate to="/admin/dashboard" replace />} />
          <Route path="teams" element={<TeamManagement />} />
          <Route path="users" element={<UserManagement />} />
          <Route path="access-tokens" element={<AccessTokens />} />
          <Route path="tg-patrol" element={<TgPatrol />} />
          <Route path="logs" element={<SystemLogs />} />
          <Route path="finance" element={<Finance />} />
          <Route path="dashboard" element={null} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
