import { type ReactNode, useEffect, useId, useState } from 'react';
import { AlertTriangle } from 'lucide-react';
import { cn } from '../lib/utils';
import PublicShell from './PublicShell';
import { BUTTON, CARD } from './ui';

/**
 * 后台首次打开时的使用条款确认：每一条单独勾选，全部勾完才能进。
 *
 * 只拦管理后台，不拦根路径的成员自助兑换页——成员只是填个邮箱换码，风险由部署者承担，
 * 对他们弹法律条款既没意义又吓人。
 *
 * 同意状态存 localStorage：换浏览器 / 换设备会再弹一次，这是刻意的。真正需要看到这段
 * 的是"第一次在某台机器上打开后台的人"，而不是"这个部署"。
 *
 * key 带版本号：条款内容有实质变化时递增版本号，所有人会重新确认一次。
 */
const STORAGE_KEY = 'teamboss.terms.accepted.v2';

interface TermsGateProps {
  children: ReactNode;
}

interface Term {
  id: string;
  /** The checkbox sentence: first person, one complete statement. */
  text: ReactNode;
  /** Background facts under the sentence; inside the label, so clicking it ticks the box too. */
  note?: string;
}

function Key({ children }: { children: ReactNode }) {
  return <strong className="font-semibold text-gray-900 dark:text-gray-50">{children}</strong>;
}

const GROUPS: { title: string; terms: Term[] }[] = [
  {
    title: '风险',
    terms: [
      {
        id: 'unofficial',
        text: (
          <>
            我明白 TeamBoss 依赖 ChatGPT / OpenAI <Key>未公开的私有接口</Key>，官方随时可能改动它们，功能很可能
            <Key>有时效性</Key>，随时会不经通知地失效。
          </>
        ),
        note: '本项目与 OpenAI 没有任何关联，不是官方产品。',
      },
      {
        id: 'credentials',
        text: (
          <>
            我知晓 Owner 账号的登录凭证会<Key>以明文存放在这台服务器上</Key>，拿到服务器、数据卷或备份的人，就能完全接管我的
            Team。
          </>
        ),
        note: '接入 Team 需要提供 Owner 账号的会话数据（access token 和 session cookie）。',
      },
      {
        id: 'auto-kick',
        text: (
          <>
            我知道到期踢人和巡逻踢人会<Key>真的把成员移出 Team</Key>，用之前我会先弄清触发规则，并核对成员数据。
          </>
        ),
        note: '设了到期时间的成员，到点会被自动移出。巡逻踢人出厂是空跑演练，在「TG 与巡逻」页激活后，会在超员或有人占用 Premium 席位时，移除绕过 TeamBoss 加入的成员。',
      },
      {
        id: 'premium-beta',
        text: (
          <>
            我知晓 <Key>Premium 席位是 Beta 功能</Key>，从未在生产环境中测试过，随时可能出错。
          </>
        ),
        note: '涉及 Premium 的邀请、换席位、兑换码和巡逻都算在内；Premium 席位单价高、按月扣费，出错的代价也更大。',
      },
    ],
  },
  {
    title: '责任',
    terms: [
      {
        id: 'review',
        text: (
          <>
            我会在投入生产使用前，<Key>自己审查一遍代码</Key>，或者让 AI Agent 替我审查。
          </>
        ),
        note: '它会用 Owner 凭证邀请、移除成员，必要时加购席位并扣费，上线前值得先看清它到底做了什么。',
      },
      {
        id: 'own-risk',
        text: (
          <>
            我同意<Key>自行承担</Key>使用 TeamBoss 的<Key>全部风险</Key>，一切后果由我自行负责。
          </>
        ),
        note: '用非官方接口操作 ChatGPT 处在服务条款的灰色地带，账号有被限制或封禁的可能，别用输不起的账号。',
      },
      {
        id: 'no-liability',
        text: (
          <>
            我同意作者对任何损失<Key>概不负责</Key>，包括扣费、加购的席位、被移除的成员，以及账号被限制或封禁。
          </>
        ),
        note: '完整免责声明见项目 README。',
      },
    ],
  },
];

const TERM_COUNT = GROUPS.reduce((sum, group) => sum + group.terms.length, 0);

export default function TermsGate({ children }: TermsGateProps) {
  // null = 还没读完 localStorage，先什么都不渲染，避免同意过的人看到弹窗闪一下
  const [accepted, setAccepted] = useState<boolean | null>(null);
  const [checked, setChecked] = useState<ReadonlySet<string>>(() => new Set());
  const baseId = useId();
  const progressId = `${baseId}-progress`;

  useEffect(() => {
    try {
      setAccepted(window.localStorage.getItem(STORAGE_KEY) === '1');
    } catch {
      // 隐私模式等场景下 localStorage 不可用：正常放行，不要把人挡在后台外面
      setAccepted(true);
    }
  }, []);

  const toggle = (id: string, on: boolean) => {
    setChecked((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const allChecked = checked.size === TERM_COUNT;

  const handleAccept = () => {
    if (!allChecked) return;
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
    <PublicShell title="使用条款" width="xl">
      <div className={cn(CARD, 'shadow-sm')}>
        <div className="flex items-start gap-3 border-b border-gray-200 px-5 py-5 sm:px-7 dark:border-ink-800">
          <div className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-amber-50 dark:bg-amber-500/15">
            <AlertTriangle size={20} className="text-amber-600 dark:text-amber-400" />
          </div>
          <div className="min-w-0">
            <h1 className="text-lg font-semibold text-gray-900 dark:text-gray-100">使用前请逐条确认</h1>
            <p className="mt-0.5 text-sm text-gray-500 dark:text-ink-400">
              第一次在这台设备上打开管理后台，需要读完并勾选下面每一条。
            </p>
          </div>
        </div>

        <div className="space-y-6 px-5 py-6 sm:px-7">
          {GROUPS.map((group) => (
            <fieldset key={group.title}>
              <legend className="mb-2.5 text-xs font-medium text-gray-500 dark:text-ink-400">{group.title}</legend>
              <div className="space-y-2.5">
                {group.terms.map((term) => (
                  <label
                    key={term.id}
                    className={cn(
                      'flex cursor-pointer gap-3 rounded-lg border border-gray-200 px-3.5 py-3 transition-colors hover:bg-gray-50 dark:border-ink-800 dark:hover:bg-ink-850',
                      'has-[:checked]:border-blue-200 has-[:checked]:bg-blue-50/60 dark:has-[:checked]:border-blue-400/30 dark:has-[:checked]:bg-blue-500/[0.07]',
                      'has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-blue-500/40',
                    )}
                  >
                    <input
                      type="checkbox"
                      checked={checked.has(term.id)}
                      onChange={(event) => toggle(term.id, event.target.checked)}
                      aria-labelledby={`${baseId}-${term.id}`}
                      aria-describedby={term.note ? `${baseId}-${term.id}-note` : undefined}
                      className="mt-1 size-4 shrink-0 cursor-pointer accent-blue-600 focus-visible:outline-none"
                    />
                    <span className="min-w-0">
                      <span id={`${baseId}-${term.id}`} className="block text-sm leading-6 text-gray-700 dark:text-gray-300">
                        {term.text}
                      </span>
                      {term.note && (
                        <span id={`${baseId}-${term.id}-note`} className="mt-1 block text-xs leading-5 text-gray-500 dark:text-ink-400">
                          {term.note}
                        </span>
                      )}
                    </span>
                  </label>
                ))}
              </div>
            </fieldset>
          ))}
        </div>

        <div className="sticky bottom-0 rounded-b-xl border-t border-gray-200 bg-gray-50 px-5 py-4 sm:px-7 sm:py-5 dark:border-ink-800 dark:bg-ink-925">
          <div className="flex items-center justify-between gap-3">
            <p id={progressId} aria-live="polite" className="text-sm text-gray-600 dark:text-ink-300">
              {allChecked ? (
                '已全部确认'
              ) : (
                <>
                  已确认 <span className="font-semibold tabular-nums text-gray-900 dark:text-gray-100">{checked.size}</span>
                  <span className="tabular-nums"> / {TERM_COUNT}</span> 条
                </>
              )}
            </p>
            <button
              type="button"
              onClick={handleAccept}
              disabled={!allChecked}
              aria-describedby={progressId}
              className={cn(BUTTON.primary, 'shrink-0 px-5 py-2.5 sm:px-6')}
            >
              同意并继续
            </button>
          </div>
          <p className="mt-2.5 text-xs text-gray-500 dark:text-ink-400">不同意请直接关闭页面，并停止使用本项目。</p>
        </div>
      </div>
    </PublicShell>
  );
}
