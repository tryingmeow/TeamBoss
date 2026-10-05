import { type FormEvent, type ReactNode, useMemo, useState } from 'react';
import {
  AlertCircle,
  ArrowRight,
  CheckCircle2,
  Clock3,
  History,
  KeyRound,
  Loader2,
  Search,
  UserCheck,
  UserX,
  Users,
  type LucideIcon,
} from 'lucide-react';
import {
  queryMembershipStatus,
  redeemAccessToken,
  type MembershipInfo,
  type MembershipTeamEntry,
  type MembershipStatusResult,
  type RedeemAccessTokenResult,
  type RedeemTeamChoice,
  type RedemptionHistoryItem,
  type TokenQueryInfo,
} from '../api/client';
import { cn } from '../lib/utils';
import PublicShell from './PublicShell';
import { BUTTON, CARD, INPUT, PILL, TONE } from './ui';

type Tab = 'redeem' | 'query';
type PanelTone = 'success' | 'warning' | 'info' | 'neutral';

const PANEL: Record<PanelTone, string> = {
  success: 'border-emerald-200 bg-emerald-50/60 dark:border-emerald-500/25 dark:bg-emerald-500/[0.07]',
  warning: 'border-amber-200 bg-amber-50/60 dark:border-amber-500/25 dark:bg-amber-500/[0.07]',
  info: 'border-blue-200 bg-blue-50/60 dark:border-blue-500/25 dark:bg-blue-500/[0.07]',
  neutral: 'border-gray-200 bg-gray-50 dark:border-ink-800 dark:bg-ink-950',
};

const PANEL_ICON: Record<PanelTone, string> = {
  success: 'text-emerald-600 dark:text-emerald-400',
  warning: 'text-amber-600 dark:text-amber-400',
  info: 'text-blue-600 dark:text-blue-400',
  neutral: 'text-gray-500 dark:text-ink-400',
};

const FIELD_LABEL = 'mb-1.5 flex items-baseline gap-1.5 text-sm font-medium text-gray-700 dark:text-gray-300';
// 16px on phones keeps iOS Safari from zooming in when the field gets focus.
const FIELD_INPUT = cn(INPUT, 'py-2.5 text-base sm:text-sm');

/**
 * Current backend messages already say 兑换码 and Team. This only normalises the older
 * wording ("Token 无效", 车队) that can still sit in stored redemption history.
 * Only "Token" followed by Chinese is rewritten, so upstream English errors stay intact.
 */
function friendlyError(message: string): string {
  return message
    .replace(/Token\s*(?=[\u4e00-\u9fff])/g, '兑换码')
    .replace(/\s*车队\s*/g, ' Team ')
    .trim();
}

/** Codes the backend stores as a redemption's error, shown to the member who owns it. */
const HISTORY_ERRORS: Record<string, string> = {
  no_active_team: '当时没有可用的 Team',
  no_available_seat: '当时没有空余席位',
  team_choice_unknown: '没有选择 Team',
  team_choice_not_found: '所选 Team 不存在',
  team_choice_vanished: '所选 Team 已不可用',
  owner_email: 'Owner 邮箱不支持自助续期',
  permanent_membership: '永久有效，无需续期',
  request_aborted: '请求中断，兑换码未使用',
  'local redemption interrupted before remote mutation': '兑换中断，未发出邀请',
  'OpenAI invite result is uncertain': '邀请结果待确认',
};

function historyErrorText(message: string): string {
  const text = message.trim();
  if (HISTORY_ERRORS[text]) return HISTORY_ERRORS[text];
  if (text.startsWith('admin_released')) return '管理员已退回这次兑换';
  if (/^[a-z_]+$/.test(text)) return '未完成';
  return friendlyError(text);
}

function choiceExpiryText(choice: RedeemTeamChoice): string {
  // expires_at 为空有两种完全不同的含义，绝不能都写成"永不过期"：
  // permanent 是真的永久（续期会被拒），unmanaged 是本地压根没有到期记录，
  // 续下去会给这个人新建一条到期即自动踢出的记录。
  if (choice.expiry_state === 'permanent') return '永久有效 · 无需续期';
  if (choice.expiry_state === 'unmanaged') return '未纳入到期管理';
  return `到期 ${formatExpiresAt(choice.expires_at)}`;
}

function choiceBlockedText(choice: RedeemTeamChoice): string | null {
  // 只在后端明确说了"不能续"时才禁用。字段缺失（前端已更新、后端还没重启的
  // 那几秒）必须按可续处理，否则会把所有车队按钮一起变灰，谁都续不了。
  if (choice.renewable !== false) return null;
  if (choice.blocked_reason === 'owner_email') return 'Owner 邮箱不支持自助续期';
  if (choice.blocked_reason === 'permanent_membership') return '永久有效，无需续期';
  return '该 Team 暂不支持续期';
}

/**
 * 公开查询里没有到期时间时的说法。只有后端明确说 permanent 才写"永久有效"；
 * 字段缺失（老后端）或其他情况一律不承诺永久。
 */
function noExpiryText(state: MembershipTeamEntry['expiry_state']): string {
  return state === 'permanent' ? '永久有效' : '到期时间未登记，请联系管理员确认';
}

function membershipExpiryText(expiresAt: string | null, state: MembershipTeamEntry['expiry_state']): string {
  return expiresAt ? formatExpiresAt(expiresAt) : noExpiryText(state);
}

function formatExpiresAt(value: string | null): string {
  if (!value) return '永不过期';
  return new Date(value).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function grantLabel(value: string | null | undefined): string {
  if (!value) return '—';
  if (value === 'never') return '永久';
  const match = /^(\d+)([dhm])$/.exec(value);
  if (!match) return value;
  const unit = { d: '天', h: '小时', m: '分钟' }[match[2] as 'd' | 'h' | 'm'];
  return `${match[1]} ${unit}`;
}

function actionLabel(action: Exclude<RedeemAccessTokenResult['action'], null>): string {
  if (action === 'invited') return '邀请已发送';
  if (action === 'renewed_invite') return '邀请已续期';
  return '成员已续期';
}

function historyActionLabel(action: string): string {
  const labels: Record<string, string> = {
    invited: '加入',
    renewed_member: '续期成员',
    renewed_invite: '续期待接受',
    redeem_failed: '兑换失败',
    redeem_aborted: '兑换中断',
    renew_owner_rejected: 'Owner 拒绝',
    renew_permanent_rejected: '永久有效拒绝',
    renew_multi_team_prompt: '待选择 Team',
    renew_team_choice_invalid: 'Team 选择已失效',
    none: '无可用 Team',
  };
  return labels[action] ?? action;
}

function historyResult(result: string): { label: string; tone: keyof typeof TONE } {
  if (result === 'success') return { label: '成功', tone: 'success' };
  if (result === 'notice') return { label: '提示', tone: 'neutral' };
  return { label: '失败', tone: 'danger' };
}

function tokenStatusTone(status: TokenQueryInfo['token_status']): keyof typeof TONE {
  if (status === 'unused') return 'success';
  if (status === 'used') return 'info';
  if (status === 'pending_confirmation') return 'warning';
  return 'danger';
}

function emailStatusTone(status: string | undefined): keyof typeof TONE {
  if (status === 'joined') return 'success';
  if (status === 'pending') return 'warning';
  return 'neutral';
}

function statusMeta(status: MembershipInfo['status']): { title: string; icon: LucideIcon; tone: PanelTone } {
  if (status === 'joined') return { title: '已加入', icon: UserCheck, tone: 'success' };
  if (status === 'pending') return { title: '待接受', icon: Clock3, tone: 'warning' };
  return { title: '未找到', icon: UserX, tone: 'neutral' };
}

/** Lets a long address break before the "@" first, and anywhere only if it still does not fit. */
function EmailText({ email }: { email: string }) {
  const at = email.indexOf('@');
  if (at <= 0) return <>{email}</>;
  return (
    <>
      {email.slice(0, at)}
      <wbr />
      {email.slice(at)}
    </>
  );
}

function ResultPanel({
  tone,
  icon: Icon,
  title,
  children,
}: {
  tone: PanelTone;
  icon: LucideIcon;
  title: string;
  children?: ReactNode;
}) {
  return (
    <section className={cn('rounded-lg border p-4', PANEL[tone])}>
      <h2 className="flex items-center gap-2 text-sm font-semibold text-gray-900 dark:text-gray-100">
        <Icon size={16} className={cn('shrink-0', PANEL_ICON[tone])} />
        {title}
      </h2>
      {children && <div className="mt-3">{children}</div>}
    </section>
  );
}

function Details({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <dl className={cn('grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-sm', className)}>{children}</dl>
  );
}

function Detail({ label, className, children }: { label: string; className?: string; children: ReactNode }) {
  return (
    <>
      <dt className="whitespace-nowrap text-gray-500 dark:text-ink-400">{label}</dt>
      <dd className={cn('min-w-0 text-gray-900 [overflow-wrap:anywhere] dark:text-gray-100', className)}>{children}</dd>
    </>
  );
}

export default function JoinPage() {
  const params = useMemo(() => new URLSearchParams(window.location.search), []);
  const [tab, setTab] = useState<Tab>(params.get('tab') === 'query' ? 'query' : 'redeem');
  const [email, setEmail] = useState(params.get('email') ?? '');
  const [token, setToken] = useState(params.get('token') ?? '');
  const [redeemLoading, setRedeemLoading] = useState(false);
  const [queryLoading, setQueryLoading] = useState(false);
  const [error, setError] = useState('');
  const [redeemResult, setRedeemResult] = useState<RedeemAccessTokenResult | null>(null);
  const [statusResult, setStatusResult] = useState<MembershipStatusResult | null>(null);
  // 车队选择提示是针对某一对（邮箱, 兑换码）算出来的。之后用户可能在输入框里
  // 把邮箱改了，所以提交选择时必须用生成这份列表时的那对值，不能读当前表单。
  const [promptContext, setPromptContext] = useState<{ email: string; token: string } | null>(null);
  const [pendingTeamId, setPendingTeamId] = useState<string | null>(null);

  const switchTab = (next: Tab) => {
    setTab(next);
    setError('');
    setRedeemResult(null);
    setStatusResult(null);
    setPromptContext(null);
  };

  const submitRedeem = async (teamId?: string) => {
    // 选车队的提交必须先校验、再发请求，全程保留已有的 choices：这份列表只存在
    // 于 redeemResult 里，提前清掉的话任何一次失败（409/429/503）都会让用户连
    // 可点的车队都没有了，只能把整个表单重填一遍。
    const submitEmail = (teamId && promptContext ? promptContext.email : email).trim();
    const submitToken = (teamId && promptContext ? promptContext.token : token).trim();

    setError('');
    if (!teamId) {
      setRedeemResult(null);
      setPromptContext(null);
    }
    setStatusResult(null);

    if (!submitEmail || !submitToken) {
      setError('请输入邮箱和兑换码');
      return;
    }

    setRedeemLoading(true);
    if (teamId) setPendingTeamId(teamId);
    try {
      const data = await redeemAccessToken({
        email: submitEmail,
        token: submitToken,
        ...(teamId ? { team_id: teamId } : {}),
      });
      setRedeemResult(data);
      setPromptContext(
        data.status === 'team_selection_required' ? { email: submitEmail, token: submitToken } : null
      );
    } catch (err) {
      setError(err instanceof Error ? friendlyError(err.message) : '操作失败');
    } finally {
      setRedeemLoading(false);
      setPendingTeamId(null);
    }
  };

  const handleRedeem = async (event: FormEvent) => {
    event.preventDefault();
    await submitRedeem();
  };

  const handleQuery = async (event: FormEvent) => {
    event.preventDefault();
    setError('');
    setRedeemResult(null);
    setStatusResult(null);

    if (!email.trim()) {
      setError('请输入邮箱或兑换码');
      return;
    }

    setQueryLoading(true);
    try {
      // 兑换记录属于隐私数据，后端只在调用方能出示本人的一张兑换码时才返回。
      // 不填也能查到车队和到期时间，只是记录那一段会是空的。
      const proof = token.trim();
      const data = await queryMembershipStatus({
        query: email.trim(),
        ...(proof ? { token: proof } : {}),
      });
      setStatusResult(data);
    } catch (err) {
      setError(err instanceof Error ? friendlyError(err.message) : '查询失败');
    } finally {
      setQueryLoading(false);
    }
  };

  const loading = redeemLoading || queryLoading;
  const status = statusResult && statusResult.query_type === 'email' ? statusMeta(statusResult.membership.status) : null;
  const hasResult = Boolean(redeemResult || statusResult);

  const tabClass = (active: boolean) =>
    cn(
      'h-9 whitespace-nowrap rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/50',
      active
        ? 'bg-white text-blue-600 shadow-sm dark:bg-ink-800 dark:text-blue-400'
        : 'text-gray-500 hover:text-gray-900 dark:text-ink-400 dark:hover:text-gray-100'
    );

  return (
    <PublicShell title="Team 自助服务">
      <div className={cn(CARD, 'p-5 shadow-sm sm:p-7')}>
        <h1 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Team 自助服务</h1>
        <p className="mt-1 text-pretty text-sm text-gray-500 dark:text-ink-400">
          用兑换码加入或续期 Team，也能查询成员状态。
        </p>

        <div role="tablist" aria-label="操作" className="mt-5 grid grid-cols-2 gap-1 rounded-lg bg-gray-100 p-1 dark:bg-ink-950">
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'redeem'}
            onClick={() => switchTab('redeem')}
            className={tabClass(tab === 'redeem')}
          >
            加入 / 续期
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'query'}
            onClick={() => switchTab('query')}
            className={tabClass(tab === 'query')}
          >
            查询
          </button>
        </div>

        <form onSubmit={tab === 'redeem' ? handleRedeem : handleQuery} className="mt-5 space-y-4">
          <label className="block">
            <span className={FIELD_LABEL}>{tab === 'redeem' ? '邮箱' : '邮箱或兑换码'}</span>
            <input
              type="text"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder={tab === 'redeem' ? 'name@example.com' : 'name@example.com 或 atm_...'}
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              inputMode={tab === 'redeem' ? 'email' : 'text'}
              className={FIELD_INPUT}
            />
          </label>

          <label className="block">
            <span className={FIELD_LABEL}>
              兑换码
              {tab === 'query' && (
                <span className="text-xs font-normal text-gray-400 dark:text-ink-500">可选</span>
              )}
            </span>
            <input
              type="text"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              placeholder="atm_..."
              autoComplete="one-time-code"
              autoCapitalize="none"
              spellCheck={false}
              className={cn(FIELD_INPUT, 'font-mono')}
            />
            {tab === 'query' && (
              <span className="mt-1.5 block text-xs text-gray-500 dark:text-ink-400">
                查询邮箱时填上本人用过的兑换码，可同时查看兑换记录。
              </span>
            )}
          </label>

          {error && (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2.5 text-sm text-red-700 dark:border-red-500/25 dark:bg-red-500/10 dark:text-red-300"
            >
              <AlertCircle size={16} className="mt-0.5 shrink-0" />
              <span className="min-w-0 [overflow-wrap:anywhere]">{error}</span>
            </div>
          )}

          <button type="submit" disabled={loading} className={cn(BUTTON.primary, 'w-full py-2.5')}>
            {loading ? (
              <Loader2 size={16} className="animate-spin" />
            ) : tab === 'query' ? (
              <Search size={16} />
            ) : (
              <ArrowRight size={16} />
            )}
            {loading ? '处理中…' : tab === 'query' ? '查询' : '兑换'}
          </button>
        </form>

        <div aria-live="polite" className={cn('space-y-3', hasResult && 'mt-5')}>
          {redeemResult?.status === 'team_selection_required' && (
            <ResultPanel tone="info" icon={Users} title="请选择要续期的 Team">
              <p className="text-sm text-gray-600 dark:text-ink-300">{friendlyError(redeemResult.message)}</p>
              <div className="mt-3 space-y-2">
                {redeemResult.choices.map((choice: RedeemTeamChoice) => {
                  const blocked = choiceBlockedText(choice);
                  return (
                    <button
                      key={choice.team_id}
                      type="button"
                      disabled={redeemLoading || Boolean(blocked)}
                      onClick={() => submitRedeem(choice.team_id)}
                      title={blocked ?? undefined}
                      className={cn(
                        'flex min-h-11 w-full items-center gap-3 rounded-lg border px-3 py-2.5 text-left transition-colors disabled:cursor-not-allowed',
                        blocked
                          ? 'border-gray-200 bg-gray-50 dark:border-ink-800 dark:bg-ink-950'
                          : 'border-gray-200 bg-white hover:border-blue-400 disabled:opacity-60 disabled:hover:border-gray-200 dark:border-ink-800 dark:bg-ink-900 dark:hover:border-blue-500/60 dark:disabled:hover:border-ink-800'
                      )}
                    >
                      <span className="min-w-0 flex-1">
                        <span
                          className={cn(
                            'block text-sm font-medium [overflow-wrap:anywhere]',
                            blocked ? 'text-gray-500 dark:text-ink-400' : 'text-gray-900 dark:text-gray-100'
                          )}
                        >
                          {choice.team_name ?? choice.team_id}
                        </span>
                        <span className="mt-0.5 block text-xs text-gray-500 dark:text-ink-400">
                          {choice.status === 'pending' ? '待接受 · ' : ''}
                          {choiceExpiryText(choice)}
                        </span>
                        {blocked && choice.expiry_state !== 'permanent' && (
                          <span className="mt-0.5 block text-xs text-amber-700 dark:text-amber-300">{blocked}</span>
                        )}
                      </span>
                      {pendingTeamId === choice.team_id ? (
                        <Loader2 size={16} className="shrink-0 animate-spin text-blue-600 dark:text-blue-400" />
                      ) : (
                        !blocked && <ArrowRight size={16} className="shrink-0 text-blue-600 dark:text-blue-400" />
                      )}
                    </button>
                  );
                })}
              </div>
              {redeemResult.choices.some((choice: RedeemTeamChoice) => choice.expiry_state === 'unmanaged') && (
                <p className="mt-3 text-xs text-gray-500 dark:text-ink-400">
                  续期「未纳入到期管理」的 Team 会自动设定到期时间，到期后自动移出。
                </p>
              )}
            </ResultPanel>
          )}

          {redeemResult?.status === 'pending_confirmation' && (
            <ResultPanel tone="warning" icon={Clock3} title="结果确认中">
              <p className="text-sm text-gray-600 dark:text-ink-300">{friendlyError(redeemResult.message)}</p>
            </ResultPanel>
          )}

          {redeemResult?.status === 'ok' && (
            <ResultPanel tone="success" icon={CheckCircle2} title={actionLabel(redeemResult.action)}>
              <Details>
                <Detail label="Team">{redeemResult.team_name}</Detail>
                <Detail label="邮箱">
                  <EmailText email={redeemResult.email} />
                </Detail>
                <Detail label="到期">{formatExpiresAt(redeemResult.expires_at)}</Detail>
              </Details>
              {redeemResult.action !== 'renewed_member' && (
                <p className="mt-3 text-xs text-gray-500 dark:text-ink-400">请到邮箱查收 ChatGPT 的邀请邮件并接受邀请。</p>
              )}
            </ResultPanel>
          )}

          {statusResult?.query_type === 'token' && (
            <ResultPanel tone="info" icon={KeyRound} title="兑换码查询结果">
              <Details>
                <Detail label="状态">
                  <span className={cn(PILL, TONE[tokenStatusTone(statusResult.token.token_status)])}>
                    {statusResult.token.token_status_label}
                  </span>
                </Detail>
                {statusResult.token.token_status === 'unused' ? (
                  <>
                    <Detail label="可用时长">{grantLabel(statusResult.token.grant_expires_in)}</Detail>
                    <Detail label="兑换期限">{formatExpiresAt(statusResult.token.token_expires_at ?? null)}</Detail>
                  </>
                ) : statusResult.usage ? (
                  <>
                    <Detail label="兑换邮箱">
                      <EmailText email={statusResult.usage.email} />
                    </Detail>
                    {statusResult.usage.team_name && <Detail label="Team">{statusResult.usage.team_name}</Detail>}
                    <Detail label="邮箱状态">
                      <span className={cn(PILL, TONE[emailStatusTone(statusResult.usage.email_status)])}>
                        {statusResult.usage.email_status_label}
                      </span>
                    </Detail>
                    <Detail label="兑换时间">{formatExpiresAt(statusResult.usage.used_at)}</Detail>
                    {statusResult.usage.expires_at && (
                      <Detail label="到期">{formatExpiresAt(statusResult.usage.expires_at)}</Detail>
                    )}
                  </>
                ) : null}
              </Details>
            </ResultPanel>
          )}

          {statusResult?.query_type === 'email' && status && (
            <>
              <ResultPanel tone={status.tone} icon={status.icon} title={status.title}>
                <Details>
                  <Detail label="邮箱">
                    <EmailText email={statusResult.membership.email} />
                  </Detail>
                  {(statusResult.membership.memberships?.length ?? 0) > 1 ? (
                    statusResult.membership.memberships.map((entry) => (
                      <Detail key={entry.team_id ?? entry.team_name ?? ''} label="Team">
                        <span className="block font-medium">{entry.team_name}</span>
                        <span className="block text-xs text-gray-500 dark:text-ink-400">
                          {entry.status === 'pending' ? '待接受 · ' : ''}
                          {entry.expires_at ? `到期 ${formatExpiresAt(entry.expires_at)}` : noExpiryText(entry.expiry_state)}
                        </span>
                      </Detail>
                    ))
                  ) : (
                    <>
                      {statusResult.membership.team_name && (
                        <Detail label="Team">{statusResult.membership.team_name}</Detail>
                      )}
                      {statusResult.membership.status !== 'absent' && (
                        <Detail label="到期">
                          {membershipExpiryText(
                            statusResult.membership.expires_at,
                            statusResult.membership.memberships?.[0]?.expiry_state,
                          )}
                        </Detail>
                      )}
                    </>
                  )}
                </Details>
              </ResultPanel>

              <ResultPanel tone="neutral" icon={History} title="兑换记录">
                {statusResult.membership.redemption_history.length === 0 ? (
                  <p className="text-sm text-gray-500 dark:text-ink-400">
                    没有可显示的记录。填上本人用过的兑换码后再查询即可查看。
                  </p>
                ) : (
                  <ol className="divide-y divide-gray-200 dark:divide-ink-800">
                    {statusResult.membership.redemption_history.map((item: RedemptionHistoryItem, index: number) => {
                      const result = historyResult(item.result);
                      return (
                        <li key={`${item.created_at}-${index}`} className="py-3 first:pt-0 last:pb-0">
                          <div className="flex items-center justify-between gap-3">
                            <span className="min-w-0 text-sm font-medium text-gray-900 dark:text-gray-100">
                              {historyActionLabel(item.action)}
                            </span>
                            <span className={cn(PILL, TONE[result.tone])}>{result.label}</span>
                          </div>
                          <Details className="mt-1.5 gap-y-1 text-xs">
                            <Detail label="时间">{formatExpiresAt(item.created_at)}</Detail>
                            {item.team_name && <Detail label="Team">{item.team_name}</Detail>}
                            {item.token_prefix && (
                              <Detail label="兑换码" className="font-mono">
                                {item.token_prefix}...
                              </Detail>
                            )}
                            {/* 只有真正授出去的那次才有"到期"可言。失败/提示行里的
                                到期是这张码的名义面额，显示出来等于给用户一个从
                                未发生过的到期时间。成功且为空则是真的永久。 */}
                            {(item.result === 'success' || item.expires_at) && (
                              <Detail label="到期">{formatExpiresAt(item.expires_at)}</Detail>
                            )}
                            {item.error_message && (
                              <Detail label="原因" className="text-red-600 dark:text-red-400">
                                {historyErrorText(item.error_message)}
                              </Detail>
                            )}
                          </Details>
                        </li>
                      );
                    })}
                  </ol>
                )}
              </ResultPanel>
            </>
          )}
        </div>
      </div>
    </PublicShell>
  );
}
