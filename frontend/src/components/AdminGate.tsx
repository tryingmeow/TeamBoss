import { type FormEvent, type ReactNode, useEffect, useState } from 'react';
import { AlertCircle, Loader2, LogIn } from 'lucide-react';
import {
  ApiError,
  clearStoredAdminApiKey,
  getStoredAdminApiKey,
  loginAdmin,
  setStoredAdminApiKey,
  verifyStoredAdminKey,
} from '../api/client';
import PublicShell from './PublicShell';
import PageLoading from './PageLoading';
import { BUTTON, CARD, INPUT } from './ui';

interface AdminGateProps {
  children: ReactNode;
}

type GateState = 'checking' | 'login' | 'ready';

export default function AdminGate({ children }: AdminGateProps) {
  const [state, setState] = useState<GateState>(() => (getStoredAdminApiKey() ? 'checking' : 'login'));
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (state !== 'checking') return;
    let mounted = true;
    verifyStoredAdminKey()
      .then(() => {
        if (mounted) setState('ready');
      })
      .catch((err) => {
        if (!mounted) return;
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          // The key was rejected (expired, rotated, or the password changed): sign in again.
          clearStoredAdminApiKey();
          setError('');
        } else {
          // Network or server trouble: keep the key, a reload will retry it.
          setError('登录验证暂时失败，请刷新页面重试。');
        }
        setState('login');
      });
    return () => {
      mounted = false;
    };
  }, [state]);

  const handleLogin = async (event: FormEvent) => {
    event.preventDefault();
    setLoading(true);
    setError('');
    try {
      const result = await loginAdmin(password);
      setStoredAdminApiKey(result.api_key);
      setState('ready');
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录失败');
    } finally {
      setLoading(false);
    }
  };

  if (state === 'ready') return <>{children}</>;
  if (state === 'checking') return <PageLoading fullScreen />;

  return (
    <PublicShell title="管理员登录" width="sm">
      <form onSubmit={handleLogin} className={`${CARD} p-6 shadow-sm sm:p-7`}>
        <h1 className="text-lg font-semibold text-gray-900 dark:text-gray-100">管理员登录</h1>
        <p className="mt-1 text-sm text-gray-500 dark:text-ink-400">
          首次登录使用部署时设置的 <code className="rounded bg-gray-100 px-1 py-0.5 text-xs text-gray-700 dark:bg-ink-800 dark:text-ink-200">AUTO_TEAM_ADMIN_PASSWORD</code>，在后台改过密码则用新密码。
        </p>

        <label htmlFor="admin-password" className="mt-6 mb-2 block text-sm font-medium text-gray-700 dark:text-gray-300">
          密码
        </label>
        <input
          id="admin-password"
          autoFocus
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          className={INPUT}
        />

        {error && (
          <div
            role="alert"
            className="mt-3 flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2.5 text-sm text-red-700 dark:border-red-500/25 dark:bg-red-500/10 dark:text-red-300"
          >
            <AlertCircle size={16} className="mt-0.5 shrink-0" />
            <span className="min-w-0">{error}</span>
          </div>
        )}

        <button type="submit" disabled={loading || !password} className={`${BUTTON.primary} mt-5 w-full py-2.5`}>
          {loading ? <Loader2 size={16} className="animate-spin" /> : <LogIn size={16} />}
          登录
        </button>
      </form>
    </PublicShell>
  );
}
