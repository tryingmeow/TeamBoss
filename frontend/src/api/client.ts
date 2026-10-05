import type { MembersData, SeatType, Settings, Team, TeamSyncResult, TeamWorkspaceSettings } from '../types';

const BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '');
const ADMIN_API_KEY_STORAGE = 'auto_team_admin_api_key';

export function getStoredAdminApiKey(): string {
  return window.localStorage.getItem(ADMIN_API_KEY_STORAGE) || '';
}

export function setStoredAdminApiKey(apiKey: string): void {
  window.localStorage.setItem(ADMIN_API_KEY_STORAGE, apiKey);
}

export function clearStoredAdminApiKey(): void {
  window.localStorage.removeItem(ADMIN_API_KEY_STORAGE);
}

export interface OverageCapacity {
  seats_entitled: number;
  active_chatgpt: number;
  available: number;
  free_team_count?: number;
  active_team_count?: number;
}

/** A non-2xx response. `status` lets callers tell an auth rejection from an outage. */
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

/** Team 的 ChatGPT 登录已失效（后端 auth_state 已是 rejected），只能重新导入 session。 */
export class TeamAuthRejectedError extends Error {}

export class OverageConfirmationError extends Error {
  capacity: OverageCapacity;
  remainingEmails: string[];
  added: unknown[];
  failed: unknown[];

  constructor(
    message: string,
    capacity: OverageCapacity,
    remainingEmails: string[] = [],
    added: unknown[] = [],
    failed: unknown[] = []
  ) {
    super(message);
    this.name = 'OverageConfirmationError';
    this.capacity = capacity;
    this.remainingEmails = remainingEmails;
    this.added = added;
    this.failed = failed;
  }
}

function errorMessageFromBody(body: string, fallback: string): string {
  if (!body) return fallback;
  try {
    const parsed = JSON.parse(body);
    if (typeof parsed?.detail === 'string') return parsed.detail;
    if (typeof parsed?.detail?.message === 'string') return parsed.detail.message;
    if (Array.isArray(parsed?.detail)) {
      return parsed.detail
        .map((item: { msg?: string; message?: string }) => item.msg || item.message || String(item))
        .join('; ');
    }
    if (typeof parsed?.message === 'string') return parsed.message;
  } catch {
    return body;
  }
  return body;
}

interface RequestBehavior {
  /** false: on 401 drop the stored key but stay on the page (the caller shows the login form). */
  redirectOnUnauthorized?: boolean;
}

async function request<T>(url: string, options?: RequestInit, behavior: RequestBehavior = {}): Promise<T> {
  const headers = new Headers(options?.headers);
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');

  const apiKey = getStoredAdminApiKey();
  const isPublicRequest =
    url === '/api/admin/login' ||
    url === '/api/health' ||
    url.startsWith('/api/self-service/');
  const sentStoredAdminKey =
    Boolean(apiKey) &&
    !isPublicRequest &&
    !headers.has('Authorization') &&
    !headers.has('X-API-Key');
  if (sentStoredAdminKey) {
    headers.set('Authorization', `Bearer ${apiKey}`);
  }

  const res = await fetch(`${BASE}${url}`, { ...options, headers });
  if (!res.ok) {
    if (res.status === 401 && sentStoredAdminKey) {
      clearStoredAdminApiKey();
      if (behavior.redirectOnUnauthorized !== false) window.location.href = '/admin';
    }
    const body = await res.text();
    if (res.status === 409) {
      try {
        const parsed = JSON.parse(body);
        if (parsed?.detail?.code === 'team_auth_rejected') {
          throw new TeamAuthRejectedError(parsed.detail.message ?? '登录已失效，请重新导入');
        }
        if (parsed?.detail?.code === 'require_overage_confirmation') {
          const capacity = parsed.detail.capacity ?? {};
          throw new OverageConfirmationError(
            parsed.detail.message ?? 'ChatGPT 席位不足',
            {
              seats_entitled: Number(capacity.seats_entitled) || 0,
              active_chatgpt: Number(capacity.active_chatgpt) || 0,
              available: Number(capacity.available) || 0,
              free_team_count: Number(capacity.free_team_count) || 0,
              active_team_count: Number(capacity.active_team_count) || 0,
            },
            Array.isArray(parsed.detail.remaining_emails) ? parsed.detail.remaining_emails : [],
            Array.isArray(parsed.detail.added) ? parsed.detail.added : [],
            Array.isArray(parsed.detail.failed) ? parsed.detail.failed : [],
          );
        }
      } catch (e) {
        if (e instanceof OverageConfirmationError || e instanceof TeamAuthRejectedError) throw e;
      }
    }
    throw new ApiError(errorMessageFromBody(body, `HTTP ${res.status}`), res.status);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export interface AdminLoginResult {
  status: 'ok';
  api_key: string;
}

export interface AdminAccount {
  api_key: string;
  api_key_prefix: string;
}

export interface RotateAdminApiKeyResult {
  status: 'ok';
  api_key: string;
  api_key_prefix: string;
}

export async function loginAdmin(password: string): Promise<AdminLoginResult> {
  return request<AdminLoginResult>('/api/admin/login', {
    method: 'POST',
    body: JSON.stringify({ password }),
  });
}

export async function fetchAdminAccount(): Promise<AdminAccount> {
  return request<AdminAccount>('/api/admin/account');
}

/**
 * Checks the stored admin key without leaving the page. A rejected key is removed and
 * surfaces as an ApiError with status 401/403; any other failure leaves the key alone.
 */
export async function verifyStoredAdminKey(): Promise<AdminAccount> {
  return request<AdminAccount>('/api/admin/account', undefined, { redirectOnUnauthorized: false });
}

export async function changeAdminPassword(data: {
  current_password: string;
  new_password: string;
}): Promise<void> {
  return request<void>('/api/admin/password', {
    method: 'PATCH',
    body: JSON.stringify(data),
  });
}

export async function rotateAdminApiKey(): Promise<RotateAdminApiKeyResult> {
  return request<RotateAdminApiKeyResult>('/api/admin/api-key/rotate', { method: 'POST' });
}

export async function fetchTeams(): Promise<Team[]> {
  return request<Team[]>('/api/teams');
}

export async function fetchTeamMembers(teamId: string, refresh = false): Promise<MembersData> {
  return request<MembersData>(`/api/teams/${teamId}/members${refresh ? '?refresh=true' : ''}`);
}

export async function syncTeam(teamId: string, force = false): Promise<TeamSyncResult> {
  return request<TeamSyncResult>(`/api/teams/${teamId}/sync?force=${force ? 'true' : 'false'}`, {
    method: 'POST',
  });
}

export async function fetchTeamWorkspaceSettings(
  teamId: string,
  refresh = false
): Promise<TeamWorkspaceSettings> {
  return request<TeamWorkspaceSettings>(
    `/api/teams/${teamId}/workspace-settings${refresh ? '?refresh=true' : ''}`
  );
}

export async function updateTeamDefaultSeatType(
  teamId: string,
  seatType: SeatType
): Promise<TeamWorkspaceSettings> {
  return request<TeamWorkspaceSettings>(`/api/teams/${teamId}/workspace-settings/default-seat-type`, {
    method: 'POST',
    body: JSON.stringify({ seat_type: seatType }),
  });
}

export interface TeamImportResult {
  status: 'ok';
  team_id: string;
  name: string;
}

export async function addTeam(sessionJson: unknown, proxyId?: number | null): Promise<TeamImportResult> {
  const qs = proxyId ? `?proxy_id=${proxyId}` : '';
  return request<TeamImportResult>(`/api/teams${qs}`, {
    method: 'POST',
    body: JSON.stringify(sessionJson),
  });
}

export async function reimportTeam(
  teamId: string,
  sessionJson: unknown,
  proxyId?: number | null
): Promise<TeamImportResult> {
  const qs = proxyId ? `?proxy_id=${proxyId}` : '';
  return request<TeamImportResult>(`/api/teams/${teamId}/reimport${qs}`, {
    method: 'POST',
    body: JSON.stringify(sessionJson),
  });
}

export async function deleteTeam(teamId: string): Promise<void> {
  return request<void>(`/api/teams/${teamId}`, { method: 'DELETE' });
}

export async function updateTeamRemark(teamId: string, remark: string): Promise<Team> {
  return request<Team>(`/api/teams/${teamId}/remark`, {
    method: 'PATCH',
    body: JSON.stringify({ remark }),
  });
}

export async function inviteMember(
  teamId: string,
  data: { email: string; seat_type: string; expires_in?: string; allow_overage?: boolean }
): Promise<unknown> {
  return request(`/api/teams/${teamId}/members/invite`, {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export interface InviteGptMembersResult {
  status: 'ok';
  added: Array<{
    email: string;
    team_id: string;
    team_name: string;
    expires_at: string | null;
    overage?: boolean;
  }>;
  failed: Array<{ email: string; error: string }>;
  total: number;
}

export async function inviteGptMembers(data: {
  emails: string[];
  expires_in?: string;
  allow_overage?: boolean;
}): Promise<InviteGptMembersResult> {
  return request<InviteGptMembersResult>('/api/gpt-members/invite', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function removeMember(teamId: string, userId: string): Promise<void> {
  return request<void>(`/api/teams/${teamId}/members/${userId}`, { method: 'DELETE' });
}

export async function changeSeat(teamId: string, userId: string, seatType: string): Promise<void> {
  return request<void>(`/api/teams/${teamId}/members/${userId}/seat`, {
    method: 'PATCH',
    body: JSON.stringify({ seat_type: seatType }),
  });
}

export async function revokeInvite(teamId: string, email: string): Promise<void> {
  return request<void>(`/api/teams/${teamId}/invites/${encodeURIComponent(email)}`, {
    method: 'DELETE',
  });
}

export async function setExpiry(
  teamId: string,
  userId: string,
  expiresIn: string,
  email?: string
): Promise<void> {
  return request<void>(`/api/teams/${teamId}/members/${userId}/expiry`, {
    method: 'PUT',
    body: JSON.stringify({ expires_in: expiresIn, email }),
  });
}

export async function extendMemberExpiry(
  teamId: string,
  userId: string,
  expiresIn: string,
  email: string,
  requestId: string
): Promise<void> {
  return request<void>(`/api/teams/${teamId}/members/${userId}/expiry/extend`, {
    method: 'POST',
    body: JSON.stringify({ expires_in: expiresIn, email, request_id: requestId }),
  });
}

export async function removeExpiry(teamId: string, userId: string): Promise<void> {
  return request<void>(`/api/teams/${teamId}/members/${userId}/expiry`, { method: 'DELETE' });
}

export async function refreshTeam(teamId: string): Promise<void> {
  return request<void>(`/api/teams/${teamId}/refresh`, { method: 'POST' });
}

export interface TeamBatchResult {
  status: 'ok' | 'partial';
  results: Array<{
    team_id: string;
    status: 'ok' | 'failed';
    error?: string;
  }>;
  succeeded?: number;
  failed?: number;
}

export async function refreshAllTeams(): Promise<TeamBatchResult> {
  return request<TeamBatchResult>('/api/teams/refresh-all', { method: 'POST' });
}

export async function syncAllTeams(): Promise<TeamBatchResult> {
  return request<TeamBatchResult>('/api/teams/sync', { method: 'POST' });
}

export async function fetchSettings(): Promise<any> {
  return request<any>('/api/settings');
}

export async function updateSettings(data: Partial<Settings>): Promise<Settings> {
  return request<Settings>('/api/settings', {
    method: 'PATCH',
    body: JSON.stringify(data),
  });
}

export interface OperationLog {
  id: number;
  team_id: string | null;
  team_name: string | null;
  team_remark: string | null;
  team_owner_email: string | null;
  team_status: string | null;
  action: string | null;
  target_email: string | null;
  detail: string | null;
  result: string | null;
  error_message: string | null;
  trigger_type: string | null;
  created_at: string | null;
}

export interface LogsResult {
  logs: OperationLog[];
  total: number;
  page: number;
  per_page: number;
  total_pages: number;
}

export async function fetchLogs(params?: {
  team_id?: string;
  action?: string | string[];
  scope?: 'members';
  /** Repeated terms are ORed: a row matches if any term is a substring of any searched column. */
  q?: string | string[];
  /** Action codes ORed into the `q` search group. */
  q_action?: string[];
  page?: number;
  per_page?: number;
}): Promise<LogsResult> {
  const query = new URLSearchParams();
  if (params?.team_id) query.set('team_id', params.team_id);
  for (const action of [params?.action ?? []].flat()) if (action) query.append('action', action);
  if (params?.scope) query.set('scope', params.scope);
  for (const term of [params?.q ?? []].flat()) if (term.trim()) query.append('q', term.trim());
  for (const code of params?.q_action ?? []) if (code) query.append('q_action', code);
  if (params?.page) query.set('page', String(params.page));
  if (params?.per_page) query.set('per_page', String(params.per_page));
  const qs = query.toString();
  return request<LogsResult>(`/api/logs${qs ? `?${qs}` : ''}`);
}

export interface UsageTeamItem {
  team_id: string;
  team_name: string | null;
  owner_email: string | null;
  status: 'active' | 'token_expired' | 'error' | string;
  is_idle: boolean;
  seats_entitled: number;
  seats_in_use: number;
  inuse_gpt: number;
  inuse_codex: number;
  pending_gpt_invites: number;
  free_gpt_seats: number;
  card_last4: string | null;
  active_until: string | null;
  cache_loaded: boolean;
}

export interface UsageData {
  is_idle: boolean;
  total_team: number;
  active_team: number;
  inuse_gpt: number;
  inuse_codex: number;
  pending_gpt_invites: number;
  total_gpt_seats: number;
  free_gpt_seats: number;
  free_team_count: number;
  refresh: boolean;
  teams: UsageTeamItem[];
  errors: Array<{
    team_id: string;
    team_name: string | null;
    error: string;
  }>;
}

export async function fetchResourceUsage(refresh = false): Promise<UsageData> {
  const qs = refresh ? '?refresh=true' : '?refresh=false';
  return request<UsageData>(`/api/resources/usage${qs}`);
}

export interface OwnersResult {
  items: any[];
  total: number;
  errors: any[];
}

export async function fetchOwners(params?: { q?: string; refresh?: boolean }): Promise<OwnersResult> {
  const search = new URLSearchParams();
  if (params?.q) search.set('q', params.q);
  if (params?.refresh) search.set('refresh', 'true');
  const qs = search.toString();
  return request<OwnersResult>(`/api/users/owners${qs ? `?${qs}` : ''}`);
}

export interface AllMembersResult {
  items: any[];
  total: number;
  kick_policy: any;
  errors: any[];
}

export async function fetchAllMembers(params?: {
  q?: string;
  status?: string;
  refresh?: boolean;
  includeOwners?: boolean;
}): Promise<AllMembersResult> {
  const search = new URLSearchParams();
  if (params?.q) search.set('q', params.q);
  if (params?.status) search.set('status', params.status);
  if (params?.refresh) search.set('refresh', 'true');
  if (params?.includeOwners) search.set('include_owners', 'true');
  const qs = search.toString();
  return request<AllMembersResult>(`/api/users/members${qs ? `?${qs}` : ''}`);
}

export async function updateUserDisplayName(
  email: string,
  systemDisplayName: string | null
): Promise<{ email: string; system_display_name: string | null }> {
  return request<{ email: string; system_display_name: string | null }>('/api/users/display-name', {
    method: 'PATCH',
    body: JSON.stringify({
      email,
      system_display_name: systemDisplayName ?? '',
    }),
  });
}

export async function updateMemberExpiry(teamId: string, userId: string, expiresAt: string): Promise<void> {
  return request<void>(`/api/teams/${teamId}/members/${userId}/expiry`, {
    method: 'PUT',
    body: JSON.stringify({ expires_at: expiresAt }),
  });
}

export async function updateMemberSeat(teamId: string, userId: string, seatType: string): Promise<void> {
  return request<void>(`/api/teams/${teamId}/members/${userId}/seat`, {
    method: 'PATCH',
    body: JSON.stringify({ seat_type: seatType }),
  });
}

export async function kickMember(teamId: string, userId: string): Promise<void> {
  return request<void>(`/api/teams/${teamId}/members/${userId}`, { method: 'DELETE' });
}

export async function kickInvite(teamId: string, email: string): Promise<void> {
  return request<void>(`/api/teams/${teamId}/invites/${encodeURIComponent(email)}`, { method: 'DELETE' });
}

// ── Proxies ──

export interface Proxy {
  id: number;
  name: string;
  url: string;
  status: string;
  last_check_at: string | null;
  created_at: string | null;
}

export async function fetchProxies(): Promise<Proxy[]> {
  return request<Proxy[]>('/api/proxies');
}

export async function createProxy(data: { name: string; url: string }): Promise<Proxy> {
  return request<Proxy>('/api/proxies', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function updateProxy(proxyId: number, data: { name?: string; url?: string }): Promise<void> {
  return request<void>(`/api/proxies/${proxyId}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  });
}

export async function deleteProxy(proxyId: number): Promise<void> {
  return request<void>(`/api/proxies/${proxyId}`, { method: 'DELETE' });
}

export async function checkProxy(proxyId: number): Promise<{ status: string; last_check_at: string }> {
  return request(`/api/proxies/${proxyId}/check`, { method: 'POST' });
}

export async function updateTeamProxy(teamId: string, proxyId: number | null): Promise<void> {
  return request<void>(`/api/teams/${teamId}/proxy`, {
    method: 'PATCH',
    body: JSON.stringify({ proxy_id: proxyId }),
  });
}

export async function exportAllSessions(): Promise<unknown> {
  return request('/api/sessions/export');
}

export async function importSessions(data: unknown): Promise<void> {
  return request<void>('/api/sessions/import', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export type RedeemAccessTokenResult =
  | {
      status: 'ok';
      action: 'invited' | 'renewed_member' | 'renewed_invite';
      team_id: string;
      team_name: string;
      email: string;
      expires_at: string | null;
      message: string;
    }
  | {
      status: 'pending_confirmation';
      action: null;
      team_id: string;
      team_name: string;
      email: string;
      expires_at: null;
      message: string;
    }
  | {
      // 同一邮箱在多个车队：续期落到哪个队必须由用户点，不能替他猜。
      // 兑换码在这一步没有被消耗，用户带上 team_id 重新提交即可。
      status: 'team_selection_required';
      action: null;
      team_id: null;
      team_name: null;
      email: string;
      expires_at: null;
      message: string;
      choices: RedeemTeamChoice[];
    };

export interface RedeemTeamChoice {
  team_id: string;
  team_name: string | null;
  status: 'joined' | 'pending';
  expires_at: string | null;
  /** false 时点了必然 409（这个邮箱在该 Team 不能用兑换码续期），按钮要禁用。 */
  renewable: boolean;
  /** 恒为 false：公开响应不区分 Owner。 */
  is_owner: boolean;
  /** dated=有到期时间；permanent=到期记录里未设置时间（不可续）；unmanaged=本地无到期记录。 */
  expiry_state: 'dated' | 'permanent' | 'unmanaged';
  blocked_reason: string | null;
}

export interface RedemptionHistoryItem {
  action: string;
  result: string;
  team_id: string | null;
  team_name: string | null;
  token_prefix: string | null;
  grant_expires_in: string | null;
  expires_at: string | null;
  error_message: string | null;
  created_at: string;
}

export interface MembershipTeamEntry {
  status: 'joined' | 'pending';
  team_id: string | null;
  team_name: string | null;
  expires_at: string | null;
  is_owner: boolean;
  /** 到期时间为空时的真实含义；缺失（老后端）时不得当永久。 */
  expiry_state?: 'dated' | 'permanent' | 'external' | 'unrecorded';
  cache_updated_at: string | null;
}

// Response body of GET-by-email queries, nested under `membership` in
// MembershipStatusResult — do not flatten these onto the top level.
// Top-level team fields mirror the first team only; the full per-team
// list (one entry per team the email belongs to) is `memberships`.
export interface MembershipInfo {
  status: 'joined' | 'pending' | 'absent';
  email: string;
  team_id: string | null;
  team_name: string | null;
  expires_at: string | null;
  is_owner: boolean;
  message: string;
  memberships: MembershipTeamEntry[];
  redemption_history: RedemptionHistoryItem[];
}

export interface TokenQueryInfo {
  id?: number;
  token_prefix?: string;
  token_status: 'unused' | 'used' | 'pending_confirmation' | 'expired' | 'disabled' | 'invalid';
  token_status_label: string;
  grant_expires_in?: string | null;
  token_expires_at?: string | null;
  max_uses?: number;
  used_count?: number;
  created_at?: string;
  last_used_at?: string | null;
}

export interface TokenUsageInfo {
  email: string;
  email_status?: string;
  email_status_label: string;
  team_id?: string | null;
  team_name?: string | null;
  user_id?: string | null;
  action?: string;
  result?: string;
  error_message?: string | null;
  expires_at: string | null;
  kicked_at?: string | null;
  used_at: string;
}

// Discriminated on `query_type`: email queries nest everything under
// `membership`, token queries nest under `token` / `usage`. Do not read
// membership fields off the top level of this type — see backend
// app/routes/access_tokens.py `query_self_service` / `_query_token`.
export type MembershipStatusResult =
  | { query_type: 'email'; membership: MembershipInfo }
  | { query_type: 'token'; token: TokenQueryInfo; usage: TokenUsageInfo | null };

export async function redeemAccessToken(data: {
  email: string;
  token: string;
  team_id?: string;
}): Promise<RedeemAccessTokenResult> {
  return request<RedeemAccessTokenResult>('/api/self-service/redeem', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function queryMembershipStatus(data: {
  query: string;
  token?: string;
}): Promise<MembershipStatusResult> {
  return request<MembershipStatusResult>('/api/self-service/query', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

// ── Finance ──

export interface FinanceTeamItem {
  team_id: string;
  name: string;
  owner_email: string | null;
  remark: string | null;
  status: string;
  billing_currency: string;
  billing_symbol: string | null;
  billing_period: string | null;
  card_last4: string | null;
  card_brand: string | null;
  card_key: string | null;
  card_note: string;
  card_team_count: number;
  price_per_seat: number | null;
  seats_entitled: number;
  seats_in_use: number;
  chatgpt_in_use: number;
  codex_count: number;
  is_codex_enabled: number;
  discount_amount: number;
  monthly_total_native: number | null;
  monthly_total_base: number | null;
  balance: string | null;
  active_until: string | null;
  days_left: number | null;
  will_renew: number;
  subscription_status: 'renewing' | 'nonrenewing' | 'expired' | 'stale';
  latest_invoice: FinanceLatestInvoice | null;
}

export interface FinanceLatestInvoice {
  invoice_id: string;
  status: string | null;
  currency: string | null;
  amount_due: number | null;
  amount_paid: number | null;
  display_amount: number | null;
  display_amount_base: number | null;
  period_start: string | null;
  period_end: string | null;
  hosted_invoice_url: string | null;
  reconciliation: 'match' | 'over' | 'under' | 'unpaid' | null;
  diff_native: number | null;
  diff_base: number | null;
}

export interface FinanceInvoiceRow {
  invoice_id: string;
  number: string | null;
  status: string | null;
  currency: string | null;
  amount_due: number | null;
  amount_paid: number | null;
  period_start: string | null;
  period_end: string | null;
  description: string | null;
  hosted_invoice_url: string | null;
}

export interface FinanceTimelineItem {
  date: string;
  team_id: string;
  team_name: string;
  owner_email: string | null;
  amount_native: number | null;
  currency: string;
  amount_base: number | null;
  card_last4: string | null;
  card_brand: string | null;
  card_key: string | null;
  card_note: string;
  card_team_count: number;
  will_renew: number;
  billing_period: string | null;
}

export interface FinanceAlert {
  type: 'low_balance' | 'discount_expiring' | 'token_expired' | 'subscription_expired'
    | 'invoice_mismatch' | 'invoice_unpaid';
  team_id: string;
  team_name: string;
  detail: string;
}

export interface FinanceOverview {
  base_currency: string;
  fx_updated_at: string | null;
  low_balance_threshold: number;
  monthly_total_base: number;
  discount_total_base: number;
  excluded_teams_count: number;
  last_paid_total_base: number | null;
  last_paid_count: number;
  teams: FinanceTeamItem[];
  timeline: FinanceTimelineItem[];
  alerts: FinanceAlert[];
}

export interface FinanceTrendsRow {
  snapshot_date: string;
  team_id: string;
  billing_currency: string;
  monthly_total_native: number;
  monthly_total_base: number | null;
  balance: string;
}

export interface FinanceDailyTotal {
  date: string;
  total_base: number;
}

export interface FinanceTrends {
  days: number;
  base_currency: string;
  rows: FinanceTrendsRow[];
  daily_total_base: FinanceDailyTotal[];
}

export interface RefreshFxRatesResult {
  status: string;
  updated_currencies: number;
  fx_updated_at: string;
}

export async function getFinanceOverview(): Promise<FinanceOverview> {
  return request<FinanceOverview>('/api/finance/overview');
}

export async function getFinanceTrends(days: number = 90): Promise<FinanceTrends> {
  return request<FinanceTrends>(`/api/finance/trends?days=${days}`);
}

export async function getFinanceInvoices(teamId: string): Promise<{ team_id: string; invoices: FinanceInvoiceRow[] }> {
  return request<{ team_id: string; invoices: FinanceInvoiceRow[] }>(
    `/api/finance/invoices/${encodeURIComponent(teamId)}`,
  );
}

export async function updateFinanceSettings(body: {
  base_currency?: string;
  low_balance_threshold?: number;
}): Promise<void> {
  return request<void>('/api/finance/settings', {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
}

export async function updateFinanceCardNote(body: {
  card_brand?: string | null;
  card_last4: string;
  note: string;
}): Promise<{ status: string; card_key: string; note: string }> {
  return request<{ status: string; card_key: string; note: string }>('/api/finance/card-note', {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
}

export async function refreshFxRates(): Promise<RefreshFxRatesResult> {
  return request<RefreshFxRatesResult>('/api/finance/fx/refresh', {
    method: 'POST',
  });
}

// ── Patrol ──

export interface PatrolStatusResponse {
  kick_enabled: boolean;
  baseline_at: string | null;
  sync_interval_minutes: number;
  exempt_team_ids: string[];
  teams: Array<{
    team_id: string;
    name: string;
    codex_enabled: boolean;
    seats_entitled: number;
    active_chatgpt: number;
    over_by: number;
    risk: 'ok' | 'watch' | 'over';
    detected_over: Array<{
      email: string;
      user_id: string;
      seat_type: string;
      first_seen_at: string | null;
    }>;
  }>;
}

export interface PatrolRunResult {
  events: unknown[];
  kicked: number;
  would_kick: number;
}

export interface PatrolActivationResult {
  status: string;
  kick_enabled: boolean;
  grandfathered: number;
  backfilled: number;
  baseline_at: string;
}

export async function fetchPatrolStatus(): Promise<PatrolStatusResponse> {
  return request<PatrolStatusResponse>('/api/patrol/status');
}

export async function updatePatrolSettings(body: {
  kick_enabled?: boolean;
  exempt_team_ids?: string[];
}): Promise<void> {
  return request<void>('/api/patrol/settings', {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
}

export async function runPatrol(body: { dry_run?: boolean }): Promise<PatrolRunResult> {
  return request<PatrolRunResult>('/api/patrol/run', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function activatePatrol(): Promise<PatrolActivationResult> {
  return request<PatrolActivationResult>('/api/patrol/activate', {
    method: 'POST',
  });
}

// ── TG Bot ──

export interface TgConfigResponse {
  enabled: boolean;
  token_set: boolean;
  bot_username: string | null;
  polling: boolean;
  summary_enabled: boolean;
  summary_interval_minutes: number;
  summary_last_sent_at: string | null;
}

export interface TgUsersResponse {
  users: Array<{
    id: number;
    chat_id: string;
    username: string;
    note: string;
    disabled: boolean;
    paired_at: string;
  }>;
}

export interface TgCodesResponse {
  codes: Array<{
    id: number;
    code: string;
    note: string;
    expires_at: string;
    used_by_chat_id: string | null;
    used_at: string | null;
    disabled: boolean;
    created_at: string;
  }>;
}

export interface TgCodeCreateResult {
  id: number;
  code: string;
  note: string;
  expires_at: string;
  used_by_chat_id: string | null;
  used_at: string | null;
  disabled: boolean;
  created_at: string;
}

export interface TgMemberCodeCreateResult {
  id: number;
  email: string;
  code: string;
  expires_at: string;
  bot_username: string;
  bot_url: string;
  command: string;
  copy_text: string;
  currently_bound: boolean;
}

export async function fetchTgConfig(): Promise<TgConfigResponse> {
  return request<TgConfigResponse>('/api/tg/config');
}

export async function updateTgConfig(body: {
  enabled?: boolean;
  token?: string;
  summary_enabled?: boolean;
  summary_interval_minutes?: number;
}): Promise<{
  status: string;
  token_set: boolean;
  bot_changed: boolean;
  admin_pairing_code: string | null;
  admin_pairing_expires_at: string | null;
}> {
  return request('/api/tg/config', {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
}

export async function sendTgSummary(): Promise<{ sent: number; reason: string; sent_at: string }> {
  return request<{ sent: number; reason: string; sent_at: string }>('/api/tg/summary', {
    method: 'POST',
  });
}

export async function fetchTgUsers(): Promise<TgUsersResponse> {
  return request<TgUsersResponse>('/api/tg/users');
}

export async function updateTgUser(
  id: number,
  body: { disabled?: boolean }
): Promise<void> {
  return request<void>(`/api/tg/users/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
}

export async function deleteTgUser(id: number): Promise<void> {
  return request<void>(`/api/tg/users/${id}`, {
    method: 'DELETE',
  });
}

export async function fetchTgCodes(): Promise<TgCodesResponse> {
  return request<TgCodesResponse>('/api/tg/codes');
}

export async function createTgCode(body: {
  note?: string;
}): Promise<TgCodeCreateResult> {
  return request<TgCodeCreateResult>('/api/tg/codes', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function createTgMemberCode(email: string): Promise<TgMemberCodeCreateResult> {
  return request<TgMemberCodeCreateResult>('/api/tg/member-codes', {
    method: 'POST',
    body: JSON.stringify({ email }),
  });
}

export async function deleteTgCode(id: number): Promise<void> {
  return request<void>(`/api/tg/codes/${id}`, {
    method: 'DELETE',
  });
}

// Access Tokens Management

export interface AccessTokenListItem {
  id: number;
  token_prefix: string;
  grant_expires_in: string;
  token_expires_at: string | null;
  max_uses: number;
  used_count: number;
  note: string | null;
  disabled: boolean;
  created_at: string;
  last_used_at: string | null;
}

export interface AccessTokenResponse {
  id: number;
  token: string;
  token_prefix: string;
  grant_expires_in: string;
  token_expires_at: string | null;
  max_uses: number;
  used_count: number;
  note: string | null;
  created_at: string;
}

export async function listAccessTokens(): Promise<AccessTokenListItem[]> {
  return request<AccessTokenListItem[]>('/api/access-tokens');
}

export async function createAccessToken(body: {
  grant_expires_in: string;
  token_ttl?: string;
  note?: string;
}): Promise<AccessTokenResponse> {
  return request<AccessTokenResponse>('/api/access-tokens', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function disableAccessToken(tokenId: number): Promise<{ status: string }> {
  return request<{ status: string }>(`/api/access-tokens/${tokenId}`, {
    method: 'DELETE',
  });
}

export interface PendingConfirmationItem {
  id: number;
  email: string;
  action: string | null;
  team_id: string | null;
  team_name: string | null;
  user_id: string | null;
  error_message: string | null;
  created_at: string;
  token_prefix: string;
  grant_expires_in: string;
  seen_in_cached_snapshot: boolean;
  cache_updated_at: string | null;
}

export async function listPendingConfirmations(): Promise<PendingConfirmationItem[]> {
  return request<PendingConfirmationItem[]>('/api/access-tokens/pending-confirmations');
}

export async function resolvePendingConfirmation(
  tokenUseId: number,
  outcome: 'success' | 'released',
  note?: string,
): Promise<{ status: string; outcome: string; expires_at?: string | null }> {
  return request(`/api/access-tokens/pending-confirmations/${tokenUseId}/resolve`, {
    method: 'POST',
    body: JSON.stringify({ outcome, note }),
  });
}
