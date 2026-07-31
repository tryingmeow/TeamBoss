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
  subscription_status: 'renewing' | 'nonrenewing' | 'expired';
  card_last4: string | null;
  card_brand: string | null;
  days_remaining: number | null;
  proxy_id: number | null;
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
  role: string;
  seat_type: string;
  is_owner: boolean;
  expires_at: string | null;
  created_time: string | null;
}

export interface PendingInvite {
  id: string;
  email: string;
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
