import { FormEvent, Fragment, useMemo, useState } from 'react';
import {
  AlertCircle,
  ArrowRight,
  CheckCircle2,
  Clock3,
  History,
  KeyRound,
  Loader2,
  Mail,
  Search,
  UserCheck,
  UserX,
  Users,
} from 'lucide-react';
import {
  queryMembershipStatus,
  redeemAccessToken,
  type MembershipInfo,
  type MembershipStatusResult,
  type RedeemAccessTokenResult,
  type RedeemTeamChoice,
  type RedemptionHistoryItem,
} from '../api/client';

type Tab = 'redeem' | 'query';

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
  return '该车队暂不支持续期';
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
    renew_multi_team_prompt: '待选择车队',
    renew_team_choice_invalid: '车队选择已失效',
    none: '无可用 Team',
  };
  return labels[action] ?? action;
}

function statusMeta(status: MembershipInfo['status']) {
  if (status === 'joined') {
    return {
      title: '已加入',
      icon: UserCheck,
      className: 'border-green-200 dark:border-green-800/50 bg-green-50 dark:bg-green-950/30 text-green-800 dark:text-green-200',
      mutedClassName: 'text-green-700/70 dark:text-green-300/70',
    };
  }
  if (status === 'pending') {
    return {
      title: '待接受',
      icon: Clock3,
      className: 'border-yellow-200 dark:border-yellow-800/50 bg-yellow-50 dark:bg-yellow-950/30 text-yellow-800 dark:text-yellow-200',
      mutedClassName: 'text-yellow-700/70 dark:text-yellow-300/70',
    };
  }
  return {
    title: '未找到',
    icon: UserX,
    className: 'border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/40 text-gray-700 dark:text-gray-300',
    mutedClassName: 'text-gray-500 dark:text-gray-500',
  };
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
      setError('请输入邮箱和 Token');
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
      setError(err instanceof Error ? err.message : '操作失败');
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
      setError('请输入邮箱或 Token');
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
      setError(err instanceof Error ? err.message : '查询失败');
    } finally {
      setQueryLoading(false);
    }
  };

  const loading = redeemLoading || queryLoading;
  const status = statusResult && statusResult.query_type === 'email' ? statusMeta(statusResult.membership.status) : null;
  const StatusIcon = status?.icon;

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-[#0f1117] flex items-center justify-center px-4 py-10 transition-colors">
      <div className="w-full max-w-md">
        <div className="mb-6 flex items-center justify-center gap-3">
          <div className="w-10 h-10 rounded-2xl bg-blue-600/10 flex items-center justify-center">
            <Users size={22} className="text-blue-600 dark:text-blue-500" />
          </div>
          <div>
            <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100">Team Access</h1>
          </div>
        </div>

        <div className="bg-white dark:bg-[#1a1d27] border border-gray-200 dark:border-[#2a2d3a] rounded-2xl shadow-xl overflow-hidden">
          <div className="grid grid-cols-2 p-1.5 gap-1.5 bg-gray-100 dark:bg-[#0f1117]">
            <button
              type="button"
              onClick={() => switchTab('redeem')}
              className={`py-2 rounded-xl text-sm font-semibold transition-all ${
                tab === 'redeem'
                  ? 'bg-white dark:bg-[#1a1d27] text-blue-600 dark:text-blue-400 shadow-sm'
                  : 'text-gray-500 dark:text-gray-500 hover:text-gray-900 dark:hover:text-gray-300'
              }`}
            >
              加入 / 续期
            </button>
            <button
              type="button"
              onClick={() => switchTab('query')}
              className={`py-2 rounded-xl text-sm font-semibold transition-all ${
                tab === 'query'
                  ? 'bg-white dark:bg-[#1a1d27] text-blue-600 dark:text-blue-400 shadow-sm'
                  : 'text-gray-500 dark:text-gray-500 hover:text-gray-900 dark:hover:text-gray-300'
              }`}
            >
              查询
            </button>
          </div>

          <form onSubmit={tab === 'redeem' ? handleRedeem : handleQuery} className="p-6 space-y-4">
            <label className="block">
              <span className="flex items-center gap-2 text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                <Mail size={15} />
                邮箱或 Token
              </span>
              <input
                type="text"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="user@example.com 或 atm_..."
                autoComplete="off"
                className="w-full px-3 py-2.5 bg-gray-50 dark:bg-[#0f1117] border border-gray-200 dark:border-[#2a2d3a] rounded-xl text-sm text-gray-900 dark:text-gray-200 placeholder:text-gray-400 dark:placeholder:text-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500 transition-all"
              />
            </label>

            <label className="block">
              <span className="flex items-center gap-2 text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                <KeyRound size={15} />
                Token
                {tab === 'query' && (
                  <span className="font-normal text-xs text-gray-400 dark:text-gray-500">
                    （可选）
                  </span>
                )}
              </span>
              <input
                type="text"
                value={token}
                onChange={(event) => setToken(event.target.value)}
                placeholder="atm_..."
                autoComplete="one-time-code"
                className="w-full px-3 py-2.5 bg-gray-50 dark:bg-[#0f1117] border border-gray-200 dark:border-[#2a2d3a] rounded-xl text-sm text-gray-900 dark:text-gray-200 placeholder:text-gray-400 dark:placeholder:text-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500 transition-all font-mono"
              />
            </label>

            {error && (
              <div className="flex items-start gap-2 rounded-xl border border-red-200 dark:border-red-800/50 bg-red-50 dark:bg-red-950/30 px-3 py-2 text-sm text-red-700 dark:text-red-300">
                <AlertCircle size={16} className="mt-0.5 shrink-0" />
                <span>{error}</span>
              </div>
            )}

            {redeemResult?.status === 'team_selection_required' && (
              <div className="rounded-xl border border-blue-200 dark:border-blue-800/50 bg-blue-50 dark:bg-blue-950/30 p-3 text-sm text-blue-800 dark:text-blue-200 space-y-2">
                <div className="flex items-center gap-2 font-semibold">
                  <Users size={16} />
                  请选择要续期的车队
                </div>
                <p className="text-xs text-blue-700/80 dark:text-blue-300/80">
                  {redeemResult.message}
                </p>
                <div className="space-y-1.5 pt-0.5">
                  {redeemResult.choices.map((choice: RedeemTeamChoice) => {
                    const blocked = choiceBlockedText(choice);
                    return (
                      <button
                        key={choice.team_id}
                        type="button"
                        disabled={redeemLoading || Boolean(blocked)}
                        onClick={() => submitRedeem(choice.team_id)}
                        title={blocked ?? undefined}
                        className="w-full flex items-center justify-between gap-2 rounded-lg border border-blue-200 dark:border-blue-800/50 bg-white/70 dark:bg-[#0f1117]/70 px-3 py-2 text-left transition-colors hover:border-blue-400 hover:bg-white dark:hover:bg-[#0f1117] disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:border-blue-200 dark:disabled:hover:border-blue-800/50"
                      >
                        <span className="min-w-0">
                          <span className="block font-medium truncate">{choice.team_name ?? choice.team_id}</span>
                          <span className="block text-xs text-blue-700/70 dark:text-blue-300/70">
                            {choice.status === 'pending' ? '待接受 · ' : ''}
                            {choiceExpiryText(choice)}
                          </span>
                          {blocked && (
                            <span className="block text-xs text-gray-500 dark:text-gray-400">{blocked}</span>
                          )}
                        </span>
                        {pendingTeamId === choice.team_id ? (
                          <Loader2 size={15} className="shrink-0 animate-spin opacity-60" />
                        ) : (
                          !blocked && <ArrowRight size={15} className="shrink-0 opacity-60" />
                        )}
                      </button>
                    );
                  })}
                </div>
                {redeemResult.choices.some((choice: RedeemTeamChoice) => choice.expiry_state === 'unmanaged') && (
                  <p className="text-xs text-blue-700/80 dark:text-blue-300/80">
                    续期未管理车队将自动设定到期时间，到期后自动移出。
                  </p>
                )}
              </div>
            )}

            {redeemResult?.status === 'pending_confirmation' && (
              <div className="rounded-xl border border-yellow-200 dark:border-yellow-800/50 bg-yellow-50 dark:bg-yellow-950/30 p-3 text-sm text-yellow-800 dark:text-yellow-200 space-y-2">
                <div className="flex items-center gap-2 font-semibold">
                  <Clock3 size={16} />
                  结果确认中
                </div>
                <p className="text-xs text-yellow-700/80 dark:text-yellow-300/80">
                  {redeemResult.message}
                </p>
              </div>
            )}

            {redeemResult?.status === 'ok' && (
              <div className="rounded-xl border border-green-200 dark:border-green-800/50 bg-green-50 dark:bg-green-950/30 p-3 text-sm text-green-800 dark:text-green-200 space-y-2">
                <div className="flex items-center gap-2 font-semibold">
                  <CheckCircle2 size={16} />
                  {actionLabel(redeemResult.action)}
                </div>
                <div className="grid grid-cols-[68px_1fr] gap-y-1 text-xs">
                  <span className="text-green-700/70 dark:text-green-300/70">Team</span>
                  <span className="font-medium">{redeemResult.team_name}</span>
                  <span className="text-green-700/70 dark:text-green-300/70">邮箱</span>
                  <span className="font-medium">{redeemResult.email}</span>
                  <span className="text-green-700/70 dark:text-green-300/70">到期</span>
                  <span className="font-medium">{formatExpiresAt(redeemResult.expires_at)}</span>
                </div>
              </div>
            )}

            {statusResult && statusResult.query_type === 'token' && (
              <div className="rounded-xl border border-blue-200 dark:border-blue-800/50 bg-blue-50 dark:bg-blue-950/30 p-3 text-sm text-blue-800 dark:text-blue-200 space-y-2">
                <div className="flex items-center gap-2 font-semibold">
                  <KeyRound size={16} />
                  Token 查询结果
                </div>
                <div className="grid grid-cols-[68px_1fr] gap-y-1 text-xs">
                  <span className="text-blue-700/70 dark:text-blue-300/70">状态</span>
                  <span className="font-medium">{statusResult.token.token_status_label}</span>
                  
                  {statusResult.token.token_status === 'unused' ? (
                    <>
                      <span className="text-blue-700/70 dark:text-blue-300/70">时长</span>
                      <span className="font-medium">{statusResult.token.grant_expires_in}</span>
                      <span className="text-blue-700/70 dark:text-blue-300/70">Token过期</span>
                      <span className="font-medium">{formatExpiresAt(statusResult.token.token_expires_at ?? null)}</span>
                    </>
                  ) : statusResult.usage ? (
                    <>
                      <span className="text-blue-700/70 dark:text-blue-300/70">目标邮箱</span>
                      <span className="font-medium">{statusResult.usage.email}</span>
                      <span className="text-blue-700/70 dark:text-blue-300/70">邮箱状态</span>
                      <span className="font-medium text-emerald-600 dark:text-emerald-400">{statusResult.usage.email_status_label}</span>
                      <span className="text-blue-700/70 dark:text-blue-300/70">使用时间</span>
                      <span className="font-medium">{formatExpiresAt(statusResult.usage.used_at)}</span>
                      {statusResult.usage.expires_at && (
                        <>
                          <span className="text-blue-700/70 dark:text-blue-300/70">到期时间</span>
                          <span className="font-medium">{formatExpiresAt(statusResult.usage.expires_at)}</span>
                        </>
                      )}
                    </>
                  ) : null}
                </div>
              </div>
            )}

            {statusResult && statusResult.query_type === 'email' && status && StatusIcon && (
              <>
                <div className={`rounded-xl border p-3 text-sm space-y-2 ${status.className}`}>
                  <div className="flex items-center gap-2 font-semibold">
                    <StatusIcon size={16} />
                    {status.title}
                  </div>
                  <div className="grid grid-cols-[68px_1fr] gap-y-1 text-xs">
                    <span className={status.mutedClassName}>邮箱</span>
                    <span className="font-medium">{statusResult.membership.email}</span>
                    {(statusResult.membership.memberships?.length ?? 0) > 1 ? (
                      statusResult.membership.memberships.map(entry => (
                        <Fragment key={entry.team_id ?? entry.team_name ?? ''}>
                          <span className={status.mutedClassName}>Team</span>
                          <span className="font-medium">
                            {entry.team_name}
                            <span className="ml-1.5 font-normal opacity-75">
                              {entry.status === 'pending' ? '待接受 · ' : ''}
                              到期 {formatExpiresAt(entry.expires_at)}
                            </span>
                          </span>
                        </Fragment>
                      ))
                    ) : (
                      <>
                        {statusResult.membership.team_name && (
                          <>
                            <span className={status.mutedClassName}>Team</span>
                            <span className="font-medium">{statusResult.membership.team_name}</span>
                          </>
                        )}
                        {statusResult.membership.status !== 'absent' && (
                          <>
                            <span className={status.mutedClassName}>到期</span>
                            <span className="font-medium">{formatExpiresAt(statusResult.membership.expires_at)}</span>
                          </>
                        )}
                      </>
                    )}
                  </div>
                </div>

                <div className="rounded-xl border border-gray-200 dark:border-[#2a2d3a] bg-gray-50 dark:bg-[#0f1117] p-3 text-sm space-y-2">
                  <div className="flex items-center gap-2 font-semibold text-gray-800 dark:text-gray-200">
                    <History size={16} />
                    兑换历史
                  </div>

                  {statusResult.membership.redemption_history.length === 0 ? (
                    <div className="text-xs text-gray-500 dark:text-gray-500 py-1">暂无兑换历史</div>
                  ) : (
                    <div className="space-y-2">
                      {statusResult.membership.redemption_history.map((item: RedemptionHistoryItem, index: number) => (
                        <div
                          key={`${item.created_at}-${index}`}
                          className="rounded-lg bg-white dark:bg-[#1a1d27] border border-gray-200 dark:border-[#2a2d3a] p-2"
                        >
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-xs font-semibold text-gray-800 dark:text-gray-200">
                              {historyActionLabel(item.action)}
                            </span>
                            <span
                              className={`text-[11px] px-1.5 py-0.5 rounded font-medium ${
                                item.result === 'success'
                                  ? 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300'
                                  : item.result === 'notice'
                                    ? 'bg-gray-100 dark:bg-slate-800 text-gray-600 dark:text-slate-300'
                                    : 'bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300'
                              }`}
                            >
                              {item.result === 'success' ? '成功' : item.result === 'notice' ? '提示' : '失败'}
                            </span>
                          </div>
                          <div className="mt-1 grid grid-cols-[52px_1fr] gap-y-0.5 text-[11px] text-gray-500 dark:text-gray-500">
                            <span>时间</span>
                            <span>{formatExpiresAt(item.created_at)}</span>
                            {item.team_name && (
                              <>
                                <span>Team</span>
                                <span>{item.team_name}</span>
                              </>
                            )}
                            {item.token_prefix && (
                              <>
                                <span>Token</span>
                                <span className="font-mono">{item.token_prefix}...</span>
                              </>
                            )}
                            {/* 只有真正授出去的那次才有"到期"可言。失败/提示行里的
                                到期是这张码的名义面额，显示出来等于给用户一个从
                                未发生过的到期时间。成功且为空则是真的永久。 */}
                            {(item.result === 'success' || item.expires_at) && (
                              <>
                                <span>到期</span>
                                <span>{formatExpiresAt(item.expires_at)}</span>
                              </>
                            )}
                            {item.error_message && (
                              <>
                                <span>错误</span>
                                <span className="text-red-500 dark:text-red-400">{item.error_message}</span>
                              </>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </>
            )}

            <button
              type="submit"
              disabled={loading}
              className="w-full flex items-center justify-center gap-2 py-2.5 rounded-xl text-sm font-semibold text-white bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-700 hover:to-indigo-700 shadow-md shadow-blue-500/20 transition-all disabled:opacity-60"
            >
              {loading ? (
                <Loader2 size={16} className="animate-spin" />
              ) : tab === 'query' ? (
                <Search size={16} />
              ) : (
                <ArrowRight size={16} />
              )}
              {loading ? '处理中...' : tab === 'query' ? '查询' : '确认'}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
