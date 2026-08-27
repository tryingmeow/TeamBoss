import { type ReactNode, useEffect, useState } from 'react';
import { AlertTriangle } from 'lucide-react';

/**
 * 后台首次打开时的使用条款确认。
 *
 * 只拦管理后台，不拦根路径的成员自助兑换页——成员只是填个邮箱换码，风险由部署者承担，
 * 对他们弹法律条款既没意义又吓人。
 *
 * 同意状态存 localStorage：换浏览器 / 换设备会再弹一次，这是刻意的。真正需要看到这段
 * 的是"第一次在某台机器上打开后台的人"，而不是"这个部署"。
 *
 * key 带版本号：条款内容有实质变化时把 v1 递增，所有人会重新确认一次。
 */
const STORAGE_KEY = 'teamboss.terms.accepted.v1';

interface TermsGateProps {
  children: ReactNode;
}

const TERMS: { title: string; body: string }[] = [
  {
    title: '这不是 OpenAI 的官方产品',
    body: '本项目与 OpenAI 没有任何关联。它依赖 ChatGPT 未公开的内部接口工作，接口、成员操作方式和计费策略都可能被官方随时改动，功能随时可能失效。',
  },
  {
    title: '你要交出的是主账号的完整登录凭证',
    body: '接入团队需要你提供 owner 账号的会话数据（access token + session cookie）。它以明文存放在这台服务器的数据卷里。任何拿到这台服务器、这个数据卷或它的备份的人，都能完全接管你的工作区。',
  },
  {
    title: '账号有被限制或封禁的可能',
    body: '以这种方式访问 ChatGPT 属于服务条款的灰色地带。请不要用你输不起的账号，并自行评估风险。',
  },
  {
    title: '这里有能自动移除成员的定时任务',
    body: '到期自动踢人和超员巡逻踢人都会真的把人移出你的工作区。它们默认关闭，开启前请先弄清楚触发规则，并确认你的成员数据是准确的。',
  },
  {
    title: '后果由你自己承担',
    body: '因使用本项目产生的账号异常、财务损失、数据丢失，作者不承担任何责任。完整免责声明见项目 README。',
  },
];

export default function TermsGate({ children }: TermsGateProps) {
  // null = 还没读完 localStorage，先什么都不渲染，避免同意过的人看到弹窗闪一下
  const [accepted, setAccepted] = useState<boolean | null>(null);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    try {
      setAccepted(window.localStorage.getItem(STORAGE_KEY) === '1');
    } catch {
      // 隐私模式等场景下 localStorage 不可用：正常放行，不要把人挡在后台外面
      setAccepted(true);
    }
  }, []);

  const handleAccept = () => {
    try {
      window.localStorage.setItem(STORAGE_KEY, '1');
    } catch {
      // 存不下就存不下，本次会话照常进入
    }
    setAccepted(true);
  };

  if (accepted === null) return null;
  if (accepted) return <>{children}</>;

  return (
    <div className="min-h-screen bg-[#0f1117] flex items-center justify-center px-4 py-8">
      <div className="w-full max-w-2xl rounded-2xl bg-white dark:bg-[#1a1d27] border border-gray-200 dark:border-[#2a2d3a] shadow-2xl overflow-hidden">
        <div className="flex items-center gap-3 px-6 py-5 border-b border-gray-200 dark:border-[#2a2d3a]">
          <div className="w-10 h-10 shrink-0 rounded-xl bg-amber-500/10 flex items-center justify-center">
            <AlertTriangle size={20} className="text-amber-500" />
          </div>
          <div>
            <h1 className="text-lg font-bold text-gray-900 dark:text-gray-100">使用前请先读完</h1>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              TeamBoss · 首次在这台设备上打开后台
            </p>
          </div>
        </div>

        <div className="px-6 py-5 space-y-4 max-h-[55vh] overflow-y-auto">
          {TERMS.map((term, index) => (
            <div key={term.title} className="flex gap-3">
              <span className="shrink-0 w-6 h-6 rounded-full bg-gray-100 dark:bg-[#0f1117] border border-gray-200 dark:border-[#2a2d3a] flex items-center justify-center text-xs font-semibold text-gray-500 dark:text-gray-400">
                {index + 1}
              </span>
              <div>
                <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
                  {term.title}
                </h2>
                <p className="mt-1 text-sm leading-relaxed text-gray-600 dark:text-gray-400">
                  {term.body}
                </p>
              </div>
            </div>
          ))}
        </div>

        <div className="px-6 py-5 border-t border-gray-200 dark:border-[#2a2d3a] bg-gray-50 dark:bg-[#161923]">
          <label className="flex items-start gap-3 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={checked}
              onChange={(event) => setChecked(event.target.checked)}
              className="mt-0.5 w-4 h-4 shrink-0 rounded border-gray-300 dark:border-[#2a2d3a] text-blue-600 focus:ring-2 focus:ring-blue-500/50"
            />
            <span className="text-sm text-gray-700 dark:text-gray-300">
              我已读完以上全部内容，理解其中的风险，并自行承担后果。
            </span>
          </label>

          <button
            type="button"
            onClick={handleAccept}
            disabled={!checked}
            className="mt-4 w-full px-4 py-2 rounded-lg text-sm font-semibold text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            同意并继续
          </button>
          <p className="mt-3 text-xs text-center text-gray-500 dark:text-gray-400">
            不同意请直接关闭页面，并停止使用本项目。
          </p>
        </div>
      </div>
    </div>
  );
}
