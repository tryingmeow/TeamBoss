import { type FormEvent, type ReactNode, useEffect, useState } from 'react';
import { KeyRound, Loader2, LogIn } from 'lucide-react';
import {
  clearStoredAdminApiKey,
  fetchAdminAccount,
  getStoredAdminApiKey,
  loginAdmin,
  setStoredAdminApiKey,
} from '../api/client';


interface AdminGateProps {
  children: ReactNode;
}


export default function AdminGate({ children }: AdminGateProps) {
  const [ready, setReady] = useState(false);
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let mounted = true;
    async function checkStoredKey() {
      if (!getStoredAdminApiKey()) {
        setReady(false);
        return;
      }
      try {
        await fetchAdminAccount();
        if (mounted) setReady(true);
      } catch {
        clearStoredAdminApiKey();
        if (mounted) setReady(false);
      }
    }
    checkStoredKey();
    return () => {
      mounted = false;
    };
  }, []);

  const handleLogin = async (event: FormEvent) => {
    event.preventDefault();
    setLoading(true);
    setError('');
    try {
      const result = await loginAdmin(password);
      setStoredAdminApiKey(result.api_key);
      setReady(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录失败');
    } finally {
      setLoading(false);
    }
  };

  if (ready) return <>{children}</>;

  return (
    <div className="min-h-screen bg-[#0f1117] flex items-center justify-center px-4">
      <form
        onSubmit={handleLogin}
        className="w-full max-w-sm rounded-2xl bg-white dark:bg-[#1a1d27] border border-gray-200 dark:border-[#2a2d3a] p-6 shadow-2xl"
      >
        <div className="flex items-center gap-3 mb-6">
          <div className="w-10 h-10 rounded-xl bg-blue-600/10 flex items-center justify-center">
            <KeyRound size={20} className="text-blue-600 dark:text-blue-400" />
          </div>
          <div>
            <h1 className="text-lg font-bold text-gray-900 dark:text-gray-100">管理员登录</h1>
            <p className="text-xs text-gray-500 dark:text-gray-400">Team Manager</p>
          </div>
        </div>

        <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
          密码
        </label>
        <input
          autoFocus
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          className="w-full px-3 py-2 bg-gray-50 dark:bg-[#0f1117] border border-gray-200 dark:border-[#2a2d3a] rounded-lg text-sm text-gray-900 dark:text-gray-200 focus:outline-none focus:ring-2 focus:ring-blue-500/50"
        />

        {error && <div className="mt-3 text-sm text-red-500">{error}</div>}

        <button
          type="submit"
          disabled={loading || !password}
          className="mt-5 w-full flex items-center justify-center gap-2 px-4 py-2 rounded-lg text-sm font-semibold text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-50"
        >
          {loading ? <Loader2 size={16} className="animate-spin" /> : <LogIn size={16} />}
          登录
        </button>
      </form>
    </div>
  );
}
