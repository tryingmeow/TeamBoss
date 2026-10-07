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
const STORAGE_KEY = 'teamboss.terms.accepted.v3';

interface TermsGateProps {
  children: ReactNode;
}

interface Term {
  id: string;
  /** The checkbox sentence: affirmative formal statement. */
  text: ReactNode;
  /** Professional background context and obligations under the statement. */
  note?: string;
}

function Key({ children }: { children: ReactNode }) {
  return <strong className="font-semibold text-gray-900 dark:text-gray-50">{children}</strong>;
}

const GROUPS: { title: string; terms: Term[] }[] = [
  {
    title: '系统运行与安全边界',
    terms: [
      {
        id: 'unofficial',
        text: (
          <>
            本人已充分知悉 TeamBoss 基于 ChatGPT / OpenAI <Key>未公开的私有协议与非官方接口</Key>运行，其功能具备
            <Key>强时效性与不确定性</Key>，随时可能因上游接口调整或服务策略变更而失效。
          </>
        ),
        note: '本项目为独立第三方开源工具，与 OpenAI 官方无任何隶属、合作或商业关联。',
      },
      {
        id: 'credentials',
        text: (
          <>
            本人已明确知晓工作区 Owner 凭证（包括 Access Token 及 Session Cookie）将
            <Key>以明文形式持久化于本地服务器</Key>，任何获取宿主机、数据卷或备份访问权限的主体均可完整控制对应工作区。
          </>
        ),
        note: '部署者须自行承担基础设施与宿主机的安全隔离、存储权限管控及凭证防护责任。',
      },
      {
        id: 'auto-kick',
        text: (
          <>
            本人已明确知晓到期清理与巡逻机制将对工作区成员
            <Key>实际执行不可逆的移出与权限变更操作</Key>，承诺在启用相关自动化能力前核验触发规则及成员数据。
          </>
        ),
        note: '到期成员将按既定策略自动解除席位；巡逻剔除功能出厂默认处于演练模式（Dry-Run），激活后将依据规则自动移除违规或超员接入的外部成员。',
      },
      {
        id: 'premium-beta',
        text: (
          <>
            本人已明确知晓 <Key>Premium 席位管理属于实验性功能（Beta）</Key>，尚未经过生产环境充分验证，可能存在未知缺陷或处理异常。
          </>
        ),
        note: '涉及 Premium 席位之邀请分配、席位切换、兑换流转及巡逻逻辑均属实验性范围；鉴于该类席位单价较高且涉及周期性账单变更，请谨慎评估启用风险。',
      },
    ],
  },
  {
    title: '使用责任与免责声明',
    terms: [
      {
        id: 'review',
        text: (
          <>
            本人承诺在正式投入生产环境使用前，
            <Key>独立完成源代码审查与业务逻辑评估</Key>（或经由受信自动化审计工具完成合规复核）。
          </>
        ),
        note: '系统将持 Owner 权限自动执行成员增删、席位调整及计费增购等高危操作，部署者有责任在投产前充分审阅其执行逻辑与安全边界。',
      },
      {
        id: 'own-risk',
        text: (
          <>
            本人确认自愿并<Key>独立承担使用本项目的全部风险与后果</Key>，包括但不限于合规风险、资产损失及账号安全风险。
          </>
        ),
        note: '经由非官方逆向通道管理工作区可能违反服务提供商的服务条款，存在工作区功能受限、速率拦截或账号被处置的潜在风险。',
      },
      {
        id: 'no-liability',
        text: (
          <>
            本人确认免除项目开发者及贡献者的全部法律责任与经济赔偿义务，同意开发者对因使用本项目导致的
            <Key>任何直接或间接损失概不负责</Key>。
          </>
        ),
        note: '免责范围涵盖但不限于自动扣费、席位增购、成员变更、数据灭失及账号处置等一切连带后果。详见项目根目录完整免责声明。',
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
    <PublicShell title="安全须知与风险确认" width="xl">
      <div className={cn(CARD, 'shadow-sm')}>
        <div className="flex items-start gap-3 border-b border-gray-200 px-5 py-5 sm:px-7 dark:border-ink-800">
          <div className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-amber-50 dark:bg-amber-500/15">
            <AlertTriangle size={20} className="text-amber-600 dark:text-amber-400" />
          </div>
          <div className="min-w-0">
            <h1 className="text-lg font-semibold text-gray-900 dark:text-gray-100">安全须知与风险确认</h1>
            <p className="mt-0.5 text-sm text-gray-500 dark:text-ink-400">
              首次在此设备访问管理控制台须完成风险知悉确认。请逐项审阅并勾选确认以下条款。
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
                '已完成全部条款确认'
              ) : (
                <>
                  已确认 <span className="font-semibold tabular-nums text-gray-900 dark:text-gray-100">{checked.size}</span>
                  <span className="tabular-nums"> / {TERM_COUNT}</span> 项
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
          <p className="mt-2.5 text-xs text-gray-500 dark:text-ink-400">若不同意上述任一条款，请立即关闭本页面并终止使用本项目。</p>
        </div>
      </div>
    </PublicShell>
  );
}
