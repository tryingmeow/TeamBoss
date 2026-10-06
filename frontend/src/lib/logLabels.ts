/**
 * Display text for operation-log rows. The backend stores machine codes (action names,
 * trigger types, `key=value` details); the UI shows these labels and keeps the raw value
 * available as a tooltip. Unknown codes fall through unchanged, so a new backend action
 * still shows up — just untranslated.
 */
import { formatDateSafe } from './formatDate';
import { formatSeatTypeLabel, overagePolicyLabel } from './seatType';

const ACTION_LABELS: Record<string, string> = {
  // Admin account and settings
  admin_login: '管理员登录',
  change_admin_password: '修改管理员密码',
  rotate_admin_api_key: '重置 API Key',
  update_settings: '修改系统设置',
  update_patrol_settings: '修改巡逻设置',
  activate_patrol: '启用巡逻',
  patrol_activate: '启用巡逻',
  patrol_manual_run: '手动巡逻',
  update_user_display_name: '修改成员备注',

  // Teams
  add_team: '添加 Team',
  import_session: '导入 Session',
  reimport_team: '重新导入 Session',
  delete_team: '删除 Team',
  update_team_proxy: '修改 Team 代理',
  update_team_remark: '修改 Team 备注',
  get_workspace_settings: '读取工作区设置',
  change_default_seat_type: '修改默认席位',
  set_overage_policy: '修改超员策略',
  sync_team: '同步 Team',
  sync_all: '同步全部 Team',
  refresh_token: '刷新登录令牌',
  token_auto_refresh: '自动刷新令牌',
  token_proactive_refresh: '提前刷新令牌',
  session_file_update: '保存 Session 文件',

  // Members and invites
  invite_member: '邀请成员',
  remove_member: '移除成员',
  change_seat: '修改席位',
  revoke_invite: '撤回邀请',
  set_expiry: '设置到期',
  extend_expiry: '延长到期',
  remove_expiry: '取消到期',
  member_cache_refresh: '刷新成员缓存',
  member_expiry_write_failed: '到期记录写入失败',
  member_expiry_write_skipped: '兑换已结束，无需补记',
  invite_gpt_member: '添加 GPT 成员',
  invite_gpt_member_existing: '添加 GPT 成员（已在 Team）',
  invite_gpt_member_lookup: '添加 GPT 成员（查询失败）',
  invite_gpt_member_cache_refresh: '添加 GPT 成员（刷新缓存）',
  create_tg_member_pairing_code: '生成成员 TG 绑定码',
  create_access_token: '生成兑换码',

  // Self-service redemption
  self_service_lookup: '自助兑换：查询成员',
  self_service_invite: '自助兑换：邀请',
  self_service_cache_refresh: '自助兑换：刷新缓存',
  self_service_redeem: '自助兑换',
  self_service_invite_reconciled: '自助兑换：核对确认',
  self_service_invite_interrupted: '自助兑换：中断待确认',
  self_service_invite_admin_confirmed: '自助兑换：管理员确认',
  self_service_invite_admin_released: '自助兑换：管理员退回',
  self_service_renew: '自助续期',
  redeem_no_premium_seat: '兑换失败：没有 Premium 空位',
  redeem_seat_type_mismatch: '兑换失败：席位类型不符',

  // Scheduler
  auto_kick: '到期自动移出',
  auto_revoke_invite: '到期撤回邀请',
  auto_kick_job_error: '到期移出任务出错',
  data_sync: '定时同步',
  data_sync_display_degraded: '定时同步（部分展示数据缺失）',
  data_sync_suspended: '暂停自动同步',
  data_sync_resumed: '恢复自动同步',
  data_sync_job_error: '定时同步任务出错',
  invite_reconciliation: '核对邀请',
  member_detect: '发现外部加入成员',
  member_detect_absent: '标记已离开成员',
  overview_failures_notification: '同步失败通知',
  sync_suspension_notification: '暂停同步通知',
  patrol_job_error: '巡逻任务出错',
  patrol_partial_skip: '巡逻跳过部分 Team',
  tg_summary_job_error: 'TG 汇总任务出错',
  member_watch_invite: '确认邀请生效',
  member_watch_kick: '确认移出生效',
  member_watch_error: '确认操作出错',
  member_watch_job_error: '确认任务出错',
  tg_member_expiry_reminder: 'TG 到期提醒',
  pending_redemption_reconciliation: '核对待确认兑换',

  // Patrol
  // 超员和外部 Premium 成员共用这个 action；detail 里的 reason=premium_outsider / seat_type 说明是哪一种。
  patrol_kick: '巡逻移出',
  patrol_revoke_invite: '巡逻撤回邀请',
  patrol_strict_kick: '严格模式移出',
  patrol_would_kick: '演练：将移出',
  patrol_would_revoke_invite: '演练：将撤回邀请',
  patrol_would_strict_kick: '演练：严格模式将移出',
  patrol_strict_flagged: '严格模式标记可疑成员',
  patrol_strict_batch_guard: '严格模式批量保护',
  patrol_strict_refresh_failed: '严格模式刷新失败',
  patrol_kick_batch_capped: '巡逻移出数已达上限',
  patrol_team_initialize: '巡逻登记新 Team',
  patrol_skip_invalid_entitlement: '巡逻跳过：席位数未知',
  patrol_premium_alert: '巡逻提醒：Premium 席位',

  // Telegram and notifications
  tg_summary: 'TG 汇总推送',
  team_health_alert: 'Team 异常告警',
  team_health_recovery: 'Team 恢复通知',
  update_tg_config: '修改 TG 配置',
  update_tg_operator: '修改 TG 管理员',
  delete_tg_operator: '删除 TG 管理员',
  create_tg_pairing_code: '生成 TG 绑定码',
  delete_tg_pairing_code: '删除 TG 绑定码',

  // Proxies and finance
  create_proxy: '添加代理',
  update_proxy: '修改代理',
  delete_proxy: '删除代理',
  check_proxy: '检测代理',
  update_finance_settings: '修改财务设置',
  update_card_note: '修改卡片备注',
  refresh_fx_rates: '刷新汇率',
};

const TRIGGER_LABELS: Record<string, string> = {
  manual: '手动',
  scheduler: '定时任务',
  patrol: '巡逻',
  auto_refresh: '自动刷新',
  team_health: '健康检查',
  admin: '管理员',
  system: '系统',
};

export type LogTone = 'success' | 'danger' | 'warning' | 'info' | 'neutral';

const RESULT_META: Record<string, { label: string; tone: LogTone }> = {
  success: { label: '成功', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
  error: { label: '失败', tone: 'danger' },
  skipped: { label: '跳过', tone: 'neutral' },
  uncertain: { label: '待确认', tone: 'warning' },
  partial: { label: '部分成功', tone: 'warning' },
  warning: { label: '警告', tone: 'warning' },
  dryrun: { label: '演练', tone: 'info' },
  flagged: { label: '已标记', tone: 'warning' },
  capped: { label: '已限流', tone: 'warning' },
  unchanged: { label: '无变化', tone: 'neutral' },
  superseded: { label: '已被取代', tone: 'neutral' },
};

const TEAM_STATUS_LABELS: Record<string, string> = {
  active: '正常',
  token_expired: 'Session 失效',
  error: '异常',
};

export function logActionLabel(action: string | null | undefined): string {
  if (!action) return '—';
  return ACTION_LABELS[action] ?? action;
}

export function logTriggerLabel(trigger: string | null | undefined): string {
  const key = trigger || 'system';
  return TRIGGER_LABELS[key] ?? key;
}

export function logResultMeta(result: string | null | undefined): { label: string; tone: LogTone } {
  const key = (result || '').toLowerCase();
  return RESULT_META[key] ?? { label: result || '—', tone: 'info' };
}

export function teamStatusLabel(status: string | null | undefined): string {
  if (!status) return '';
  return TEAM_STATUS_LABELS[status] ?? status;
}

// ── search ──────────────────────────────────────────────────────────────────────

// The console words the same act differently in different places (移除成员 button, 移出 in
// patrol labels, 踢出 in 用户管理; 撤销邀请 button vs 撤回邀请 log label). A search for one
// must find the others, or an admin auditing removals can miss some of them.
const SEARCH_SYNONYMS: string[][] = [
  ['移出', '移除', '踢出', '踢'],
  ['撤回', '撤销'],
];

function searchVariants(needle: string): string[] {
  const variants = new Set([needle]);
  for (const group of SEARCH_SYNONYMS) {
    const word = group.find((w) => needle.includes(w));
    if (word) for (const other of group) variants.add(needle.replace(word, other));
  }
  return [...variants];
}

function codesWithLabel(labels: Record<string, string>, needle: string): string[] {
  const variants = searchVariants(needle);
  return Object.entries(labels)
    // A code that itself contains the text is already found by the plain text search.
    .filter(([code, label]) => {
      const text = label.toLowerCase();
      return variants.some((v) => text.includes(v)) && !code.toLowerCase().includes(needle);
    })
    .map(([code]) => code);
}

/**
 * The backend's log search matches stored codes, not the labels shown here, so a search for
 * a visible label ("移出") finds nothing on its own. These are the codes whose label contains
 * the search text: `actions` for an exact `action` filter, `values` (trigger and result
 * codes) for a plain text search.
 */
export function logCodesMatchingLabel(text: string): { actions: string[]; values: string[] } {
  const needle = text.trim().toLowerCase();
  if (!needle) return { actions: [], values: [] };
  const resultLabels = Object.fromEntries(Object.entries(RESULT_META).map(([code, meta]) => [code, meta.label]));
  return {
    actions: codesWithLabel(ACTION_LABELS, needle),
    values: [...new Set([...codesWithLabel(TRIGGER_LABELS, needle), ...codesWithLabel(resultLabels, needle)])],
  };
}

// ── detail ──────────────────────────────────────────────────────────────────────

const BOOL: Record<string, string> = { true: '是', false: '否', '1': '是', '0': '否' };

/** Registry labels (Premium, 其他（raw）); old rows may still say codex / chatgpt. */
function seatLabel(value: string): string {
  if (value === 'codex') return 'Codex';
  if (value === 'chatgpt') return 'ChatGPT';
  return formatSeatTypeLabel(value);
}

/** Python writes booleans as True / False. */
function isTrue(value: string): boolean {
  return BOOL[value.toLowerCase()] === '是';
}

// 「外部加入」= 绕过 TeamBoss 进了 Team 的人，全站同一个叫法（Team 卡片、用户管理、日志）。
const PREMIUM_OUTSIDER = '外部加入，占用 Premium 席位';

const PREMIUM_ALERT_KIND: Record<string, string> = {
  premium_outsider: PREMIUM_OUTSIDER,
  premium_detected_with_record: '外部加入的 Premium 成员，但 TeamBoss 有他的 Premium 记录',
  premium_detected_was_managed: '外部加入的 Premium 成员，但 TeamBoss 以前拉过他或他兑换过',
  premium_unswitched: 'TeamBoss 管理的成员在 Premium 席位上，但不是 TeamBoss 切的',
  unknown_seat_type: '席位类型 TeamBoss 不认识',
};

/** "30d" → "30 天", "12h" → "12 小时", "3m" → "3 分钟", "never" → "永久". */
function durationLabel(value: string): string {
  const match = /^(\d+)([mhd])$/i.exec(value.trim());
  if (!match) return value.toLowerCase() === 'never' ? '永久' : value;
  const unit = { m: '分钟', h: '小时', d: '天' }[match[2].toLowerCase() as 'm' | 'h' | 'd'];
  return `${match[1]} ${unit}`;
}

const SOURCES: Record<string, string> = {
  detected: '外部加入',
  scheduled_data_sync: '定时同步',
  team_sync: '同步 Team',
  manual_token_refresh_all: '手动刷新全部令牌',
};

function timeLabel(value: string): string {
  return formatDateSafe(value, 'MM-dd HH:mm', value);
}

const TRIGGER_DETAIL: Record<string, string> = {
  api_401_retry: '接口 401 后重试',
  scheduled_expiry_refresh: '到期前定时刷新',
};

const ALERT_KEYS: Record<string, string> = {
  chatgpt_auth: '登录失效',
  team_sync: '同步失败',
};

/** key → (value → display). Returning null hides the pair. */
const KEY_FORMATTERS: Record<string, (value: string) => string | null> = {
  delivered_to: (v) => `送达 ${v} 人`,
  timed_out: (v) => (v.toLowerCase() === 'true' ? '等待超时' : '已在 ChatGPT 生效'),
  seat_type: (v) => `席位 ${seatLabel(v)}`,
  from_seat_type: (v) => `原席位 ${seatLabel(v)}`,
  overage_policy: (v) => `超员策略 ${overagePolicyLabel(v)}`,
  previous: (v) => `原为 ${overagePolicyLabel(v)}`,
  policy: (v) => `策略 ${overagePolicyLabel(v)}`,
  overage: (v) => (isTrue(v) ? '超员加购' : null),
  expires_at: (v) => `到期 ${timeLabel(v)}`,
  expires_in: (v) => `时长 ${durationLabel(v)}`,
  allow_overage: (v) => (isTrue(v) ? '已确认加购' : null),
  overage_confirmed: (v) => `已确认加购 ${v}`,
  user_id: () => null,
  request_id: () => null,
  token_use_id: (v) => `兑换记录 #${v}`,
  watch_id: (v) => `任务 #${v}`,
  sent: (v) => `已发送 ${v}`,
  due: (v) => `应发送 ${v}`,
  confirmed: (v) => `确认 ${v}`,
  released: (v) => `退回 ${v}`,
  waiting: (v) => `等待 ${v}`,
  teams: (v) => `${v} 个 Team`,
  teams_protected: (v) => `保护 ${v} 个 Team`,
  grandfathered: (v) => `保留现有成员 ${v}`,
  backfilled: (v) => `补录 ${v}`,
  detected_kept: (v) => `仍在巡逻的外部加入成员 ${v}`,
  patrolled: (v) => `巡逻 ${v} 个 Team`,
  skipped_unrefreshed: (v) => `跳过未刷新 ${v}`,
  refresh_failures: (v) => `刷新失败 ${v}`,
  over_by: (v) => `超员 ${v}`,
  rule: (v) => ({ premium_outsider: PREMIUM_OUTSIDER, over_quota: '超员' } as Record<string, string>)[v] ?? v,
  token_id: (v) => `兑换码 #${v}`,
  grant_expires_in: (v) => `授予 ${durationLabel(v)}`,
  token_ttl: (v) => `兑换有效期 ${durationLabel(v)}`,
  member_seat_type: (v) => `成员席位 ${seatLabel(v)}`,
  pending_seat_type: (v) => `待接受邀请席位 ${seatLabel(v)}`,
  resend: (v) => (isTrue(v) ? '重发邀请' : null),
  teams_checked: (v) => `查了 ${v} 个 Team`,
  deferred: (v) => ({
    seat_changed_after_snapshot: '快照后管理员改过席位，这轮先不移出',
    teamboss_record: 'TeamBoss 有他的记录，这轮先不移出',
  } as Record<string, string>)[v] ?? `推迟：${v}`,
  batch_guard: (v) => ({
    outsiders: '外部成员过多保护',
    premium: 'Premium 批量保护',
    strict: '严格模式批量保护',
  } as Record<string, string>)[v] ?? `批量保护 ${v}`,
  outsiders: (v) => `外部成员 ${v}`,
  premium_kicks: (v) => `本轮已移出 Premium ${v}`,
  kind: (v) => PREMIUM_ALERT_KIND[v] ?? `类型 ${v}`,
  status: (v) => ({ member: '成员', pending: '待接受邀请' } as Record<string, string>)[v] ?? `状态 ${v}`,
  position: (v) => `顺位 ${v}`,
  source: (v) => `来源 ${SOURCES[v] ?? v}`,
  first_seen_at: (v) => `首次发现 ${timeLabel(v)}`,
  candidates: (v) => `候选 ${v}`,
  capped_to: (v) => `限制为 ${v}`,
  count: (v) => `数量 ${v}`,
  team_size: (v) => `Team 人数 ${v}`,
  key: (v) => `类型 ${ALERT_KEYS[v] ?? v}`,
  reminder: () => '重复提醒',
  trigger: (v) => TRIGGER_DETAIL[v] ?? v,
  access_changed: (v) => (v === '1' ? 'Access token 已更新' : 'Access token 未变'),
  session_changed: (v) => (v === '1' ? 'Session 已更新' : null),
  superseded: () => '已被新的刷新取代',
  access_iat: () => null,
  access_exp: (v) => (v === 'unknown' ? null : `令牌到期 ${timeLabel(v)}`),
  kick_enabled: (v) => `自动移出 ${BOOL[v.toLowerCase()] === '是' ? '开' : '关'}`,
  strict_mode_enabled: (v) => `严格模式 ${BOOL[v.toLowerCase()] === '是' ? '开' : '关'}`,
  exempt_team_ids_count: (v) => `受保护 Team ${v} 个`,
  enabled: (v) => `机器人 ${BOOL[v.toLowerCase()] === '是' ? '开' : '关'}`,
  summary_enabled: (v) => `汇总推送 ${BOOL[v.toLowerCase()] === '是' ? '开' : '关'}`,
  summary_interval_minutes: (v) => `汇总间隔 ${v} 分钟`,
  token_changed: () => '更换了 Bot Token',
  bot_changed: () => '更换了 Bot',
  sync_interval_minutes: (v) => `同步间隔 ${v} 分钟`,
  api_concurrency: (v) => `并发 ${v}`,
  expiry_kick_mode: (v) => (v === 'day_end' ? '到期当天结束时移出' : '到期后延迟移出'),
  expiry_kick_delay_hours: (v) => `宽限 ${v} 小时`,
  skip_overage_confirmation: (v) => `跳过超员确认（旧设置） ${isTrue(v) ? '开' : '关'}`,
  ip: (v) => `IP ${v}`,
  failures_remaining: (v) => `剩余尝试 ${v} 次`,
  shared_identity_failures: (v) => `同源失败 ${v} 次`,
  prefix: (v) => `前缀 ${v}`,
  new_name: (v) => `新备注 ${v}`,
  remark_len: () => null,
  note_len: () => null,
  note: (v) => `备注 ${v}`,
  proxy_id: (v) => `代理 #${v}`,
  name: (v) => v,
  url: () => null,
  code_id: (v) => `绑定码 #${v}`,
  card_key: () => null,
  updated_currencies: (v) => `更新 ${v} 种货币`,
  base_currency: (v) => `本位币 ${v}`,
  low_balance_threshold: (v) => `余额提醒阈值 ${v}`,
  reason: (v) => ({
    no_available_seat: '没有空余席位',
    invite: '邀请后',
    kick: '移出后',
    overage_forbidden: '席位已满，禁止超员',
    overage_needs_confirmation: '席位已满，等你确认加购',
    premium_outsider: PREMIUM_OUTSIDER,
    seat_type_mismatch: '兑换码和成员的席位类型不符',
    unknown_member_seat_type: '成员的席位类型 TeamBoss 不认识，未处理',
    unknown_seat_type: '席位类型 TeamBoss 不认识，未处理',
  } as Record<string, string>)[v] ?? v,
  action: (v) => ({ renewed: '已续期', extended: '已延长', renewed_member: '续期成员', renewed_invite: '续期邀请' } as Record<string, string>)[v] ?? v,
  force: () => '强制同步',
  failed: (v) => `失败 ${v}`,
  suspended: (v) => `暂停 ${v}`,
};

const REJECT_REASONS: Record<string, string> = {
  'team is not active': 'Team 不可用',
  'codex is enabled': 'Team 开启了 Codex',
  'member cache is empty or invalid': '成员缓存为空或无效',
  'target is absent from current member cache': '成员已不在当前列表',
  'target is absent from current pending cache': '邀请已不在当前列表',
  'team is not currently over quota': 'Team 当前未超员',
  'persisted source is not detected': '不是外部加入的成员',
  'target is not within the newest over-quota candidates': '不在最新的超员名单内',
  'cached target is missing user_id': '缺少成员 ID',
  'member operation already in progress': '该成员有操作正在进行',
  'member was authorized before destructive action': '移出前该成员已获授权',
  'invite was authorized before destructive action': '撤回前该邀请已获授权',
  'invite missing email': '邀请缺少邮箱',
  'strict mode is disabled': '严格模式未开启',
  'cached target has an expiry record': '成员有到期记录',
  'persisted record has an expiry (not a stray external add)': '成员有到期记录（不是外部加入）',
  'strict kick delay window has not elapsed': '严格模式等待期未到',
};

const PHRASES: Array<[RegExp, (m: RegExpMatchArray) => string]> = [
  [/^Synced (\d+)\/(\d+) teams?(?:; failed=(\d+))?(?:; suspended=(\d+))?$/i, (m) =>
    [`已同步 ${m[1]}/${m[2]} 个 Team`, m[3] && m[3] !== '0' ? `失败 ${m[3]}` : '', m[4] && m[4] !== '0' ? `暂停 ${m[4]}` : '']
      .filter(Boolean).join(' · ')],
  [/^reconciled (\d+) confirmed invite\(s\)$/i, (m) => `核对确认 ${m[1]} 个邀请`],
  [/^detected (\d+) untracked member\(s\)$/i, (m) => `发现 ${m[1]} 个外部加入成员`],
  [/^marked (\d+) absent member\(s\) as kicked$/i, (m) => `${m[1]} 个已离开的成员标记为已移出`],
  [/^sync suspended after (\d+)h of continuous failure; probing every (\d+)h$/i, (m) => `连续失败 ${m[1]} 小时，暂停自动同步，每 ${m[2]} 小时探测一次`],
  [/^sync recovered; suspension lifted$/i, () => '同步已恢复，取消暂停'],
  [/^overview sub-interface failures: (.+)$/i, (m) => `部分接口失败：${m[1]}`],
  [/^display-only failures: (.+)$/i, (m) => `仅展示类接口失败：${m[1]}`],
  [/^patrolled (\d+) freshly synced team\(s\); (\d+) team\(s\) not refreshed this round were left alone$/i, (m) => `巡逻 ${m[1]} 个刚同步的 Team，${m[2]} 个本轮未刷新的 Team 未处理`],
  [/^member snapshot refresh failed$/i, () => '成员快照刷新失败'],
  [/^scheduled$/i, () => '定时'],
  [/^ttl_expired$/i, () => '缓存过期'],
  [/^no_changes$/i, () => '无改动'],
  [/^no_note$/i, () => '无备注'],
  [/^pending invite revoked$/i, () => '已撤回待接受邀请'],
  [/^lookup member by email$/i, () => '按邮箱查找成员'],
  [/^lookup pending invite$/i, () => '查找待接受邀请'],
  [/^member operation already in progress$/i, () => '该成员有操作正在进行'],
  [/^record already kicked or modified$/i, () => '记录已被移出或修改'],
  [/^expires_at updated after initial scan$/i, () => '扫描后到期时间已被修改'],
  [/^missing user_id and email$/i, () => '缺少成员标识'],
  [/^member or invite already absent$/i, () => '成员或邀请已不存在'],
  [/^invalid expires_at$/i, () => '到期时间无效'],
  [/^rejected: (.+)$/i, (m) => `安全检查拦截：${REJECT_REASONS[m[1]] ?? m[1]}`],
];

/** Splits "a=1, b=2" / "a=1; b=2" / "a=1 b=2" into pairs; null if any token isn't key=value. */
function parsePairs(detail: string): Array<[string, string]> | null {
  // "a=1, b=2" and "a=1; b=2" split on the separator; "a=1 b=2" splits only where a new
  // key starts, so a value may contain spaces ("name=HK Proxy").
  const tokens = detail.includes(';') || detail.includes(',')
    ? detail.split(/[;,]\s*/)
    : detail.split(/\s+(?=[a-z_]+=)/i);
  const pairs: Array<[string, string]> = [];
  for (const raw of tokens) {
    const token = raw.trim();
    if (!token) continue;
    const match = /^([a-z_]+)=(.*)$/i.exec(token);
    if (match) {
      pairs.push([match[1], match[2].trim()]);
    } else if (token in KEY_FORMATTERS) {
      pairs.push([token, '']);
    } else {
      return null;
    }
  }
  return pairs.some(([, value]) => value !== '') ? pairs : null;
}

/**
 * Human text for a log detail. Returns `null` when the detail is a diagnostic blob with
 * nothing worth showing beyond the raw text (callers then fall back to the raw value).
 */
export function formatLogDetail(detail: string | null | undefined): string | null {
  if (!detail) return null;
  const text = detail.trim();
  // Token refresh rows append a long diagnostic dump; summarise the useful head only.
  const head = text.split(/;?\s*diag:/)[0].trim();

  for (const [pattern, render] of PHRASES) {
    const match = head.match(pattern);
    if (match) return render(match);
  }

  const pairs = parsePairs(head);
  if (!pairs) return text === head ? text : head || null;

  const parts: string[] = [];
  for (const [key, value] of pairs) {
    const formatter = KEY_FORMATTERS[key];
    if (formatter) {
      const out = formatter(value);
      if (out) parts.push(out);
    } else {
      parts.push(value ? `${key}=${value}` : key);
    }
  }
  return parts.length > 0 ? parts.join(' · ') : null;
}

const TEAM_ID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$|^[0-9a-f]{24,}$/i;

/**
 * Some jobs store a list of team ids as the error ("id1,id2" or "id1: reason; id2: reason").
 * Ids become Team names when the caller knows them, otherwise the usual 8-character short id;
 * any other error text is returned as is.
 */
export function formatLogError(
  message: string | null | undefined,
  teamName: (teamId: string) => string | undefined = () => undefined,
): string | null {
  if (!message) return null;
  const text = message.trim();
  // A structured error stored as a Python dict repr: show its message only.
  const dictMessage = /^\{.*['"]message['"]:\s*['"]([^'"]+)['"].*\}$/s.exec(text);
  if (dictMessage) return dictMessage[1];
  const short = (id: string) => teamName(id) ?? id.slice(0, 8);
  const ids = text.split(/\s*,\s*/);
  if (ids.length > 0 && ids.every((id) => TEAM_ID.test(id))) {
    return `涉及 Team：${ids.map(short).join('、')}`;
  }
  const pairs = text.split(/\s*;\s*/).map((part) => /^([0-9a-f-]{24,}):\s*(.+)$/i.exec(part));
  if (pairs.length > 0 && pairs.every(Boolean)) {
    return pairs.map((m) => `${short(m![1])}：${m![2]}`).join('；');
  }
  return text;
}
