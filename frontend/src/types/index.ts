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
  default_seat_type: SeatType | null;
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
}

export type SeatType = 'default' | 'usage_based';

export interface TeamWorkspaceSettings {
  default_seat_type: SeatType;
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
}

export type ToastType = 'success' | 'error';
export type ShowToast = (text: string, type?: ToastType) => void;
