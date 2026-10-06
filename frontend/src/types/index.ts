export interface Team {
  id: string;
  name: string;
  remark: string | null;
  owner_email: string;
  status: 'active' | 'token_expired' | 'error';
  seats_in_use: number;
  seats_entitled: number;
  codex_count: number;
  chatgpt_count: number;
  is_codex_enabled: boolean;
  default_seat_type: WorkspaceDefaultSeatType | null;
  billing_currency: string;
  billing_symbol: string | null;
  billing_period: string | null;
  price_per_seat: number | null;
  discount_amount: number;
  discount_duration_num_periods: number | null;
  discount_expires_at: string | null;
  discount_quantity_off: number | null;
  promo_campaign_id: string | null;
  monthly_subtotal: number | null;
  monthly_total: number | null;
  balance: string;
  active_start: string | null;
  active_until: string | null;
  will_renew: boolean;
  subscription_status: 'renewing' | 'nonrenewing' | 'expired' | 'stale';
  card_last4: string | null;
  card_brand: string | null;
  days_remaining: number | null;
  proxy_id: number | null;
  /** 最近一次「全部接口都成功」的同步时间。同步一直失败时它会停在原地不动。 */
  last_full_sync_at: string | null;
  /** 最近一次同步中失败的接口名，例如 ["members", "subscription"]。 */
  last_sync_partial_failures: string[];
  /** 'rejected' = 会话仍能应答，但它交回的 access token 已被上游吊销。 */
  auth_state: 'ok' | 'rejected';
  /** 进入 rejected 的时间，用于显示「已持续 N 小时」。 */
  auth_state_since: string | null;
  /** 连续同步失败的起点。全部接口恢复正常时清空。 */
  sync_failing_since: string | null;
  /** 定时同步被挂起的时刻。非空 = 已停止每轮请求，只按低频探活。 */
  sync_suspended_at: string | null;
  cached_member_emails: string[];
  /** 超员策略：满了时邀请 / 切到计费席位怎么办。 */
  overage_policy: OveragePolicy;
  /** 缓存的分类型容量 {type: {paid, available}}；null = 未知。 */
  seat_capacity: Record<string, SeatCapacityEntry> | null;
  /** 缓存的 seat_type_counts 原样计数（含未知类型）；{} = 未知。 */
  seat_type_counts: Record<string, number>;
  /**
   * 缓存的待接受邀请数 {seat_type: n}（它们也占着席位）。可选：缺失时卡片只在展开、
   * 拉到成员名单后才把待接受算进空位。
   */
  pending_invite_counts?: Record<string, number>;
  /** 只在续费前 3 天内、计费席位还有空闲时才有；其余情况（含数据不全）为 null。 */
  renewal_idle_seats?: RenewalIdleSeats | null;
  /** 本地已同步的发票条数。没有付款卡时，有发票才放「查看账单」入口。 */
  invoice_count?: number;
}

/** 注册表里的席位类型（正本：lib/seatType.ts 与后端 app/seat_types.py）。 */
export type SeatType = 'default' | 'usage_based' | 'prolite';
/** 工作区「默认邀请席位」只允许这两种。 */
export type WorkspaceDefaultSeatType = 'default' | 'usage_based';
/** 兑换码可选的席位类型。 */
export type CodeSeatType = 'default' | 'prolite';

export type OveragePolicy = 'forbid' | 'confirm' | 'auto';

/**
 * 管理员对「超员需确认」Team 的一次加购确认（单个邀请 / 切换席位）。服务端按 confirmation_id
 * 记账：同一个确认最多加购 seat_limit 个 seat_type 席位，用完、过期或对不上就重新问。
 */
export interface OverageConfirmation {
  confirmation_id: string;
  seat_type: SeatType;
  seat_limit: number;
}

export interface SeatCapacityEntry {
  paid: number;
  available: number;
  /** 下个计费周期要续费的席位数。缺 = 上游没给（按 paid）；null = 给了但不可信。 */
  renewal_requested?: number | null;
}

/** 一个计费席位类型在续费前的占用：空闲 = 续费席位 − 在用 − 待接受（不低于 0）。 */
export interface RenewalIdleSeatLine {
  seat_type: string;
  paid: number;
  /** 下个计费周期要续费的席位数（上游 renewal_requested，没给时等于 paid）。 */
  renewing: number;
  in_use: number;
  pending: number;
  idle: number;
}

/** 续费前 3 天内还有没人用的计费席位（后端正本：services/renewal_reminders.py）。 */
export interface RenewalIdleSeats {
  renews_at: string;
  total_idle: number;
  lines: RenewalIdleSeatLine[];
}

export interface TeamWorkspaceSettings {
  default_seat_type: WorkspaceDefaultSeatType;
  settings?: unknown;
  cached?: boolean;
  cached_at?: string | null;
}

export interface Member {
  id: string;
  email: string;
  name: string | null;
  // 管理员在"人员管理"里给这个邮箱写的备注（后端表 user_display_names）。
  // 按邮箱走，不属于某个 Team，同一个人在几个 Team 里看到的是同一条备注。
  system_display_name?: string | null;
  role: string;
  seat_type: string;
  is_owner: boolean;
  expires_at: string | null;
  /**
   * 本地到期记录的来源（system / self_service / detected …）；null = 没有记录。
   * 用来区分到期时间为空的成员是"永久"还是"没记录"，见 lib/expiry 的 noExpiryKind。
   */
  source?: string | null;
  created_time: string | null;
}

export interface PendingInvite {
  id: string;
  email: string;
  system_display_name?: string | null;
  seat_type: string;
  created_time: string;
  expires_at: string | null;
}

export interface MembersData {
  members: Member[];
  pending_invites: PendingInvite[];
  total?: number;
  cached?: boolean;
  cached_at?: string | null;
}

export interface TeamSyncResult {
  team: Team;
  members: MembersData;
  workspace_settings: TeamWorkspaceSettings;
  cached: boolean;
  refreshed: boolean;
  reason: string;
}

export interface Settings {
  sync_interval_minutes: number;
  api_concurrency: number;
  expiry_kick_mode: 'delay_hours' | 'day_end';
  expiry_kick_delay_hours: number;
  /** 已退役：由每个 Team 的 overage_policy 取代，界面不再读写。 */
  skip_overage_confirmation?: boolean;
}

export type ToastType = 'success' | 'error';
export type ShowToast = (text: string, type?: ToastType) => void;
