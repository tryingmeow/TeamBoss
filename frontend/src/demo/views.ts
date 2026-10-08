/**
 * Response builders. Each mirrors the backend computation for the same
 * endpoint (see backend/app/routes/*.py) so derived numbers stay consistent
 * across pages after a mutation.
 */
import type { Member, MembersData, PendingInvite, RenewalIdleSeatLine, RenewalIdleSeats, Team, TeamWorkspaceSettings } from '../types';
import type {
  FinanceAlert,
  FinanceDailyTotal,
  FinanceInvoiceSummary,
  FinanceInvoicesResponse,
  FinanceLatestInvoice,
  FinancePaidAmounts,
  FinanceOverview,
  FinanceTeamItem,
  FinanceTimelineItem,
  FinanceTrends,
  FinanceTrendsRow,
  OwnersResult,
  AllMembersResult,
  PatrolStatusResponse,
  UsageData,
  UsageTeamItem,
} from '../api/client';
import type { DemoDb, DemoInvite, DemoMember, DemoTeam } from './db';
import { FX_RATES, TEAM_SPECS, teamIdFor } from './seed';
import { formatCredit } from '../lib/money';
import { DAY, HOUR, dateOnly, isoAt, shanghaiLocal } from './time';

// ── Teams & members ──

/** Pending invites per seat type; a missing seat type counts as ChatGPT (`default`). */
function pendingInviteCounts(record: DemoTeam): Record<string, number> {
  const counts: Record<string, number> = {};
  record.invites.forEach((invite) => {
    const type = invite.seat_type || 'default';
    counts[type] = (counts[type] ?? 0) + 1;
  });
  return counts;
}

const RENEWAL_REMINDER_WINDOW_MS = 3 * DAY;
const BILLED_SEAT_TYPES = ['default', 'prolite'] as const;

/**
 * Mirrors backend services/renewal_reminders.renewal_idle_seats: inside the 3-day renewal window,
 * idle = renewal seats (renewal_requested, else paid) − members − pending invites of that type
 * (an invite without a type holds one of every billed type), floored at 0.
 */
export function renewalIdleSeats(record: DemoTeam, now = Date.now()): RenewalIdleSeats | null {
  const { team } = record;
  if (!team.will_renew || team.sync_suspended_at || !team.active_until) return null;
  const until = Date.parse(team.active_until);
  if (Number.isNaN(until) || until <= now || until > now + RENEWAL_REMINDER_WINDOW_MS) return null;
  const capacity = team.seat_capacity;
  const counts = team.seat_type_counts;
  if (!capacity || Object.keys(counts).length === 0) return null;
  const lines: RenewalIdleSeatLine[] = [];
  BILLED_SEAT_TYPES.forEach((seatType) => {
    const entry = capacity[seatType];
    const inUse = counts[seatType];
    if (!entry || inUse === undefined) return;
    const renewing = entry.renewal_requested === undefined ? entry.paid : entry.renewal_requested;
    if (renewing === null || (renewing === 0 && entry.paid === 0)) return;
    const pending = record.invites.filter((invite) => !invite.seat_type || invite.seat_type === seatType).length;
    lines.push({
      seat_type: seatType,
      paid: entry.paid,
      renewing,
      in_use: inUse,
      pending,
      idle: Math.max(0, renewing - inUse - pending),
    });
  });
  const totalIdle = lines.reduce((sum, line) => sum + line.idle, 0);
  if (totalIdle <= 0) return null;
  return { renews_at: isoAt(until).replace(/\.\d{3}Z$/, 'Z'), total_idle: totalIdle, lines };
}

/** Cost for the current billing cycle, and its monthly equivalent; incomplete pricing stays unknown. */
export function teamMonthlyCost(team: Team) {
  const months = team.billing_period === 'monthly' ? 1 : team.billing_period === 'yearly' ? 12 : null;
  const positive = (value: number | null | undefined) => (months && typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null);
  const price = positive(team.price_per_seat);
  const premiumPrice = positive(team.premium_price_per_seat);
  const chatgptSeats = team.seat_capacity?.default?.paid ?? team.seats_entitled;
  const premiumSeats = team.seat_capacity?.prolite?.paid ?? 0;
  const chatgptSubtotal = chatgptSeats === 0 ? 0 : price === null ? null : price * chatgptSeats;
  const premiumSubtotal = premiumSeats === 0 ? 0 : premiumPrice === null ? null : premiumPrice * premiumSeats;
  const known = months !== null && chatgptSubtotal !== null && premiumSubtotal !== null;
  const appliedDiscount = months !== null && chatgptSubtotal !== null && team.discount_amount !== null
    ? Math.min(Math.max(0, team.discount_amount), chatgptSubtotal * months) : null;
  const periodSubtotal = known ? (chatgptSubtotal + premiumSubtotal) * months : null;
  const periodTotal = periodSubtotal !== null && appliedDiscount !== null ? periodSubtotal - appliedDiscount : null;
  return {
    price_per_seat: price,
    premium_price_per_seat: premiumPrice,
    chatgpt_seats_billed: chatgptSeats,
    premium_seats_paid: premiumSeats,
    premium_subtotal: premiumSubtotal,
    premium_price_source: premiumPrice !== null ? 'upstream' as const : null,
    discount_monthly: appliedDiscount !== null && months !== null ? appliedDiscount / months : null,
    period_subtotal: periodSubtotal,
    period_total: periodTotal,
    monthly_subtotal: periodSubtotal !== null && months !== null ? periodSubtotal / months : null,
    monthly_total: periodTotal !== null && months !== null ? periodTotal / months : null,
  };
}

export function teamView(record: DemoTeam, sortedEmails = false): Team {
  const cost = teamMonthlyCost(record.team);
  const team = {
    ...record.team,
    price_per_seat: cost.price_per_seat,
    premium_price_per_seat: cost.premium_price_per_seat,
    monthly_subtotal: cost.monthly_subtotal,
    monthly_total: cost.monthly_total,
    period_subtotal: cost.period_subtotal,
    period_total: cost.period_total,
    last_sync_partial_failures: [...record.team.last_sync_partial_failures],
    seat_capacity: record.team.seat_capacity
      ? Object.fromEntries(Object.entries(record.team.seat_capacity).map(([k, v]) => [k, { ...v }]))
      : null,
    seat_type_counts: { ...record.team.seat_type_counts },
    pending_invite_counts: pendingInviteCounts(record),
    renewal_idle_seats: renewalIdleSeats(record),
    invoice_count: record.invoices.length,
  };
  team.cached_member_emails = sortedEmails ? [...record.team.cached_member_emails].sort() : [...record.team.cached_member_emails];
  return team;
}

function remarkOf(db: DemoDb, email: string): string | null {
  return db.remarks.get(email.trim().toLowerCase()) ?? null;
}

type MemberRow = Member & { first_seen_at: string | null; source: string | null; status: 'active' };
type InviteRow = PendingInvite & {
  name: null;
  role: string;
  is_owner: false;
  first_seen_at: string | null;
  source: string | null;
  status: 'pending';
};

export function membersData(db: DemoDb, record: DemoTeam, cached = true): MembersData {
  const members: MemberRow[] = record.members.map((m) => ({
    id: m.id,
    email: m.email,
    name: m.name,
    role: m.role,
    seat_type: m.seat_type,
    is_owner: m.is_owner,
    expires_at: m.expires_at,
    first_seen_at: m.first_seen_at,
    source: m.source,
    created_time: m.created_time,
    status: 'active',
    system_display_name: remarkOf(db, m.email),
  }));
  const pending: InviteRow[] = record.invites.map((inv) => ({
    id: inv.id,
    email: inv.email,
    name: null,
    role: 'standard-user',
    seat_type: inv.seat_type,
    is_owner: false,
    expires_at: inv.expires_at,
    first_seen_at: inv.first_seen_at,
    source: inv.source,
    created_time: inv.created_time,
    status: 'pending',
    system_display_name: remarkOf(db, inv.email),
  }));
  return {
    members,
    pending_invites: pending,
    total: members.length + pending.length,
    cached,
    cached_at: record.cacheUpdatedAt,
  };
}

export function workspaceSettings(record: DemoTeam, cached = true): TeamWorkspaceSettings {
  const seat = record.team.default_seat_type ?? 'default';
  return {
    default_seat_type: seat,
    settings: { default_seat_type: seat },
    cached,
    cached_at: record.team.workspace_settings_cached_at ?? null,
  };
}

// ── Expiry & kick policy ──

export interface KickPolicyView {
  mode: 'delay_hours' | 'day_end';
  delay_hours: number;
  timezone: 'Asia/Shanghai';
  label: string;
}

export function kickPolicy(db: DemoDb): KickPolicyView {
  const mode = db.settings.expiry_kick_mode === 'day_end' ? 'day_end' : 'delay_hours';
  const delay = Math.min(Math.max(Math.round(db.settings.expiry_kick_delay_hours) || 0, 0), 720);
  return {
    mode,
    delay_hours: delay,
    timezone: 'Asia/Shanghai',
    label: mode === 'day_end' ? '日末' : delay ? `+${delay}h` : '到期',
  };
}

function effectiveKickAt(expiresMs: number, policy: KickPolicyView): number {
  if (policy.mode === 'day_end') {
    const local = new Date(expiresMs + 8 * HOUR);
    return Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate(), 23, 59) - 8 * HOUR;
  }
  return expiresMs + policy.delay_hours * HOUR;
}

export function expiryView(db: DemoDb, expiresAt: string | null) {
  const policy = kickPolicy(db);
  if (!expiresAt) {
    return {
      expires_at: null,
      expires_at_local: null,
      effective_kick_at: null,
      effective_kick_at_local: null,
      kick_label: '永不',
      kick_display: '永不',
    };
  }
  const ms = Date.parse(expiresAt);
  const kickMs = effectiveKickAt(ms, policy);
  return {
    expires_at: expiresAt,
    expires_at_local: shanghaiLocal(ms),
    effective_kick_at: isoAt(kickMs),
    effective_kick_at_local: shanghaiLocal(kickMs),
    kick_label: policy.label,
    kick_display: `${shanghaiLocal(ms)} (${policy.label})`,
  };
}

/** `dated` / `permanent` / `unmanaged`, as `get_active_expiry_state` classifies a member. */
export function expiryState(row: Pick<DemoMember, 'expires_at' | 'source'>): 'dated' | 'permanent' | 'unmanaged' {
  if (row.expires_at) return 'dated';
  if (row.source === null || row.source === 'detected') return 'unmanaged';
  return 'permanent';
}

function tgBinding(db: DemoDb, email: string) {
  const binding = db.tgBindings.get(email.trim().toLowerCase());
  return binding
    ? { bound: true, username: binding.username, paired_at: binding.paired_at }
    : { bound: false, username: null, paired_at: null };
}

// ── Users page ──

function billingCycle(team: Team) {
  return {
    active_start: team.active_start,
    active_until: team.active_until,
    days_remaining: team.days_remaining,
    will_renew: team.will_renew,
    subscription_status: team.subscription_status,
  };
}

function matchesQuery(row: Record<string, unknown>, q: string, fields: string[]): boolean {
  const needle = q.trim().toLowerCase();
  if (!needle) return true;
  return fields.some((field) => String(row[field] ?? '').toLowerCase().includes(needle));
}

export function owners(db: DemoDb, q: string): OwnersResult {
  const items = db.teams.map(({ team, members }) => {
    const owner = members.find((m) => m.is_owner);
    return {
      email: team.owner_email,
      name: owner?.name ?? '',
      team_id: team.id,
      team_name: team.name,
      user_id: owner?.id ?? '',
      seat_type: owner?.seat_type ?? 'default',
      card_last4: team.card_last4,
      card_brand: team.card_brand,
      billing_cycle: billingCycle(team),
      active_start: team.active_start,
      active_until: team.active_until,
      team_status: team.status,
      is_codex_enabled: team.is_codex_enabled ? 1 : 0,
      system_display_name: remarkOf(db, team.owner_email),
    };
  });
  const filtered = items.filter((row) =>
    matchesQuery(row, q, ['email', 'name', 'team_name', 'card_last4', 'card_brand', 'system_display_name']),
  );
  return { items: filtered, total: filtered.length, errors: [] };
}

function memberActions(teamId: string, userId: string) {
  const base = `/api/teams/${teamId}/members/${userId}`;
  return {
    kick: base,
    change_seat: `${base}/seat`,
    set_expiry: `${base}/expiry`,
    extend_expiry: `${base}/expiry/extend`,
    remove_expiry: `${base}/expiry`,
  };
}

function expiryExtras(row: DemoMember | DemoInvite, expiryId: number) {
  const hasRow = row.source !== null || row.expires_at !== null;
  return {
    expiry_id: hasRow ? expiryId : null,
    kicked: false,
    kicked_at: null,
    kick_source: null,
    first_seen_at: hasRow ? row.first_seen_at : null,
    source: hasRow ? row.source : null,
  };
}

export function allMembers(
  db: DemoDb,
  params: { q: string; status: string; includeOwners: boolean; teamId: string },
): AllMembersResult {
  const items: Array<Record<string, unknown>> = [];
  db.teams.forEach(({ team, members, invites, kicked }, teamIndex) => {
    if (params.teamId && team.id !== params.teamId) return;
    const ownerRow = members.find((m) => m.is_owner);
    const ownerName = ownerRow?.name || team.owner_email || '';
    const codex = team.is_codex_enabled ? 1 : 0;
    const present = new Set<string>();

    members.forEach((m, index) => {
      present.add(m.email.toLowerCase());
      present.add(m.id);
      const isOwner = m.is_owner || m.email.toLowerCase() === team.owner_email.toLowerCase();
      if (isOwner && !params.includeOwners) return;
      items.push({
        status: 'joined',
        status_label: '已加入',
        team_id: team.id,
        team_name: team.name,
        owner_email: team.owner_email,
        owner_name: ownerName,
        is_owner: isOwner,
        user_id: m.id,
        invite_id: null,
        email: m.email,
        name: m.name,
        role: m.role,
        seat_type: m.seat_type,
        created_time: m.created_time,
        is_codex_enabled: codex,
        expiry: { ...expiryView(db, m.expires_at), ...expiryExtras(m, 2000 + teamIndex * 100 + index) },
        actions: memberActions(team.id, m.id),
        system_display_name: remarkOf(db, m.email),
        tg_binding: tgBinding(db, m.email),
      });
    });

    invites.forEach((inv, index) => {
      present.add(inv.email.toLowerCase());
      const inviteUrl = `/api/teams/${team.id}/invites/${encodeURIComponent(inv.email)}`;
      items.push({
        status: 'pending',
        status_label: '待接受',
        team_id: team.id,
        team_name: team.name,
        owner_email: team.owner_email,
        owner_name: ownerName,
        is_owner: false,
        user_id: '',
        invite_id: inv.id,
        email: inv.email,
        name: null,
        role: 'standard-user',
        seat_type: inv.seat_type,
        created_time: inv.created_time,
        is_codex_enabled: codex,
        expiry: { ...expiryView(db, inv.expires_at), ...expiryExtras(inv, 2050 + teamIndex * 100 + index) },
        actions: { kick: inviteUrl, revoke_invite: inviteUrl },
        system_display_name: remarkOf(db, inv.email),
        tg_binding: tgBinding(db, inv.email),
      });
    });

    kicked.forEach((k) => {
      if (present.has(k.email.toLowerCase()) || present.has(k.user_id)) return;
      items.push({
        status: 'kicked',
        status_label: '已踢出',
        team_id: team.id,
        team_name: team.name,
        owner_email: team.owner_email,
        owner_name: team.owner_email,
        is_owner: false,
        user_id: k.user_id,
        invite_id: null,
        email: k.email,
        name: null,
        role: null,
        seat_type: null,
        created_time: k.created_at,
        is_codex_enabled: codex,
        expiry: {
          ...expiryView(db, k.expires_at),
          expiry_id: k.expiry_id,
          kicked: true,
          kicked_at: k.kicked_at,
          kick_source: k.kick_source,
          first_seen_at: k.first_seen_at,
          source: k.source,
        },
        actions: {},
        system_display_name: remarkOf(db, k.email),
        tg_binding: tgBinding(db, k.email),
      });
    });
  });

  const status = params.status.trim().toLowerCase();
  const filtered = items
    .filter((row) => !status || row.status === status)
    .filter((row) =>
      matchesQuery(row, params.q, [
        'email', 'name', 'team_name', 'owner_email', 'owner_name', 'seat_type', 'status_label', 'system_display_name',
      ]),
    )
    .sort((a, b) =>
      String(a.team_name ?? '').localeCompare(String(b.team_name ?? '')) ||
      String(a.email ?? '').localeCompare(String(b.email ?? '')) ||
      String(a.status ?? '').localeCompare(String(b.status ?? '')),
    );
  return { items: filtered, total: filtered.length, kick_policy: kickPolicy(db), errors: [] };
}

// ── Seat usage ──

export function teamSeatUsage(record: DemoTeam) {
  const activeChatgpt = record.members.filter((m) => m.seat_type === 'default').length;
  const codex = record.members.filter((m) => m.seat_type === 'usage_based').length;
  const premium = record.members.filter((m) => m.seat_type === 'prolite').length;
  const pendingDefault = record.invites.filter((i) => i.seat_type === 'default').length;
  const pendingPremium = record.invites.filter((i) => i.seat_type === 'prolite').length;
  return { activeChatgpt, codex, premium, pendingDefault, pendingPremium, inUse: record.members.length };
}

export function resourceUsage(db: DemoDb, refresh: boolean): UsageData {
  let activeTeam = 0;
  let totalGptSeats = 0;
  let inuseGpt = 0;
  let inuseCodex = 0;
  let pendingGpt = 0;
  let freeGpt = 0;
  let freeTeamCount = 0;

  const teams: UsageTeamItem[] = db.teams.map((record) => {
    const { team } = record;
    const usage = teamSeatUsage(record);
    const isActive = team.status === 'active';
    const available = availableGptSeats(record);
    const teamFree = isActive ? available : 0;
    if (isActive) {
      activeTeam += 1;
      totalGptSeats += record.paid.default;
      inuseGpt += usage.activeChatgpt;
      inuseCodex += usage.codex;
      pendingGpt += usage.pendingDefault;
      freeGpt += teamFree;
      if (teamFree > 0) freeTeamCount += 1;
    }
    return {
      team_id: team.id,
      team_name: team.name,
      owner_email: team.owner_email,
      status: team.status,
      is_idle: teamFree > 0,
      seats_entitled: team.seats_entitled,
      seats_in_use: usage.inUse,
      inuse_gpt: isActive ? usage.activeChatgpt : 0,
      inuse_codex: isActive ? usage.codex : 0,
      pending_gpt_invites: isActive ? usage.pendingDefault : 0,
      free_gpt_seats: teamFree,
      card_last4: team.card_last4,
      active_until: team.active_until,
      cache_loaded: true,
    };
  });

  return {
    is_idle: freeGpt > 0,
    total_team: db.teams.length,
    active_team: activeTeam,
    inuse_gpt: inuseGpt,
    inuse_codex: inuseCodex,
    pending_gpt_invites: pendingGpt,
    total_gpt_seats: totalGptSeats,
    free_gpt_seats: freeGpt,
    free_team_count: freeTeamCount,
    refresh,
    teams,
    errors: [],
  };
}

/**
 * Free seats of a billed type: the contract's `billed_free_seats`. Per-type = capacity.available minus
 * pending invites of that type. ChatGPT also has the legacy formula (seats_entitled − active − pending)
 * and takes the smaller of the two, because seats_entitled may include paid Premium seats. Premium
 * without a capacity entry is 0, and non-billed or unknown types are always 0.
 */
export function freeSeats(record: DemoTeam, seatType: string): number {
  if (seatType !== 'default' && seatType !== 'prolite') return 0;
  const usage = teamSeatUsage(record);
  const entry = record.team.seat_capacity?.[seatType];
  const pending = seatType === 'default' ? usage.pendingDefault : usage.pendingPremium;
  const perType = entry ? entry.available - pending : null;
  if (seatType === 'prolite') return perType === null ? 0 : Math.max(0, perType);
  const legacy = record.team.seats_entitled - usage.activeChatgpt - usage.pendingDefault;
  return Math.max(0, perType === null ? legacy : Math.min(perType, legacy));
}

/** Free ChatGPT seats in one team, the number the overage check compares against. */
export function availableGptSeats(record: DemoTeam): number {
  return freeSeats(record, 'default');
}

// ── Patrol ──

export function patrolStatus(db: DemoDb): PatrolStatusResponse & { strict_mode_enabled: boolean } {
  const teams = db.teams
    .filter(({ team }) => team.status === 'active')
    .map((record) => {
      const { team } = record;
      const activeChatgpt = teamSeatUsage(record).activeChatgpt;
      const overBy = Math.max(0, activeChatgpt - team.seats_entitled);
      const risk: 'ok' | 'watch' | 'over' = team.is_codex_enabled ? 'ok' : overBy > 0 ? 'over' : 'watch';
      const detectedOver =
        risk === 'over'
          ? record.members
              .filter((m) => m.source === 'detected' && m.seat_type === 'default')
              .sort((a, b) => b.created_time.localeCompare(a.created_time))
              .map((m) => ({ email: m.email, user_id: m.id, seat_type: m.seat_type, first_seen_at: m.first_seen_at }))
          : [];
      return {
        team_id: team.id,
        name: team.name,
        codex_enabled: team.is_codex_enabled,
        seats_entitled: team.seats_entitled,
        active_chatgpt: activeChatgpt,
        over_by: overBy,
        risk,
        detected_over: detectedOver,
      };
    });
  return {
    kick_enabled: db.patrol.kick_enabled,
    baseline_at: db.patrol.baseline_at,
    sync_interval_minutes: db.settings.sync_interval_minutes,
    exempt_team_ids: [...db.patrol.exempt_team_ids],
    strict_mode_enabled: db.patrol.strict_mode_enabled,
    teams,
  };
}

// ── Finance ──

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}

export function convert(amount: number | null, from: string, to: string): number | null {
  if (amount === null) return null;
  const fromRate = FX_RATES[from.toUpperCase()];
  const toRate = FX_RATES[to.toUpperCase()];
  if (!fromRate || !toRate) return null;
  return round2((amount / fromRate) * toRate);
}

export function cardKey(team: Team): string | null {
  if (!team.card_last4) return null;
  return `${(team.card_brand || '').toLowerCase()}:${team.card_last4}`;
}

function monthlyNative(team: Team): number | null {
  return teamMonthlyCost(team).monthly_total;
}

function utcDay(ms: number): number {
  const d = new Date(ms);
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
}

function latestInvoice(record: DemoTeam, base: string): FinanceLatestInvoice | null {
  const { team } = record;
  const invoice = record.invoices.find((row) => row.status !== 'void' && row.status !== 'draft');
  if (!invoice) return null;
  const status = (invoice.status || '').toLowerCase();
  const display = status === 'paid' ? invoice.amount_paid : invoice.amount_due;
  const native = teamMonthlyCost(team).period_total;
  const currency = invoice.currency || '';
  let reconciliation: FinanceLatestInvoice['reconciliation'] = null;
  let diffNative: number | null = null;
  if (status === 'open' || status === 'uncollectible') {
    reconciliation = 'unpaid';
  } else if (status === 'paid' && display !== null && native !== null && team.billing_currency && currency === team.billing_currency) {
    const diff = round2(display - native);
    const tolerance = Math.max(0.01 * Math.abs(native), 1);
    reconciliation = Math.abs(diff) <= tolerance ? 'match' : diff > 0 ? 'over' : 'under';
    diffNative = diff;
  }
  return {
    invoice_id: invoice.invoice_id,
    status: invoice.status,
    currency: invoice.currency,
    amount_due: invoice.amount_due,
    amount_paid: invoice.amount_paid,
    display_amount: display,
    display_amount_base: convert(display, currency, base),
    period_start: invoice.period_start,
    period_end: invoice.period_end,
    hosted_invoice_url: invoice.hosted_invoice_url,
    reconciliation,
    diff_native: diffNative,
    diff_base: diffNative === null ? null : convert(diffNative, currency, base),
  };
}

/** The per-seat and Premium fields FinanceTeamItem marks optional (for older servers); the demo always sends them. */
export type PremiumFinanceFields = Required<Pick<FinanceTeamItem,
  | 'chatgpt_seats_billed'
  | 'premium_seats_paid'
  | 'price_per_seat_base'
  | 'premium_price_per_seat'
  | 'premium_price_per_seat_base'
  | 'premium_price_source'
  | 'premium_monthly_native'
  | 'premium_monthly_base'
>>;

export function financeOverview(db: DemoDb): FinanceOverview & {
  teams: Array<FinanceTeamItem & PremiumFinanceFields>;
  premium_monthly_base_total: number;
} {
  const base = db.finance.base_currency;
  const threshold = db.finance.low_balance_threshold;
  const today = utcDay(Date.now());

  const cardCounts = new Map<string, number>();
  db.teams.forEach(({ team }) => {
    const key = cardKey(team);
    if (key) cardCounts.set(key, (cardCounts.get(key) ?? 0) + 1);
  });

  let monthlyTotalBase = 0;
  let discountTotalBase = 0;
  let excluded = 0;
  let premiumIncludedBase = 0;
  let lastPaidTotal = 0;
  let lastPaidCount = 0;
  const alerts: FinanceAlert[] = [];
  const timeline: FinanceTimelineItem[] = [];

  const teams: Array<FinanceTeamItem & PremiumFinanceFields> = db.teams.map((record) => {
    const { team } = record;
    const usage = teamSeatUsage(record);
    const key = cardKey(team);
    const cost = teamMonthlyCost(team);
    const native = cost.monthly_total;
    const nativeBase = convert(native, team.billing_currency, base);
    const latest = latestInvoice(record, base);
    const untilMs = team.active_until ? Date.parse(team.active_until) : null;
    const daysLeft = untilMs === null ? null : Math.round((utcDay(untilMs) - today) / DAY);
    // Known Premium costs are included only when the complete Team total is known.
    const premiumNative = cost.premium_price_source === 'upstream' ? cost.premium_subtotal : null;
    const premiumBase = convert(premiumNative, team.billing_currency, base);

    if (team.status === 'active' && team.subscription_status === 'renewing') {
      if (nativeBase === null) {
        excluded += 1;
      } else {
        monthlyTotalBase += nativeBase;
        if (premiumBase !== null) premiumIncludedBase += premiumBase;
      }
      discountTotalBase += convert(cost.discount_monthly, team.billing_currency, base) ?? 0;
    }
    if (latest && (latest.status || '').toLowerCase() === 'paid' && latest.display_amount_base !== null) {
      lastPaidTotal += latest.display_amount_base;
      lastPaidCount += 1;
    }

    const balance = team.balance === null ? null : Number.parseFloat(team.balance);
    if (balance !== null && Number.isFinite(balance) && balance < threshold) {
      alerts.push({
        type: 'low_balance', team_id: team.id, team_name: team.name,
        detail: balance < 0
          ? `Credit 余额为负 · ${formatCredit(balance)}`
          : `Credit 余额 ${formatCredit(balance)} 低于阈值 ${formatCredit(threshold)}`,
      });
    }
    if (team.discount_expires_at) {
      const days = Math.round((utcDay(Date.parse(team.discount_expires_at)) - today) / DAY);
      if (days >= 0 && days <= 14) {
        alerts.push({ type: 'discount_expiring', team_id: team.id, team_name: team.name, detail: `折扣将在 ${days} 天后到期` });
      }
    }
    if (team.status === 'token_expired') {
      alerts.push({ type: 'token_expired', team_id: team.id, team_name: team.name, detail: 'Session 已失效，需要重新导入' });
    }
    if (team.subscription_status === 'expired') {
      alerts.push({ type: 'subscription_expired', team_id: team.id, team_name: team.name, detail: 'Team 订阅已到期' });
    }
    if (latest && (latest.reconciliation === 'over' || latest.reconciliation === 'under')) {
      const word = (latest.diff_native ?? 0) > 0 ? '多' : '少';
      alerts.push({
        type: 'invoice_mismatch', team_id: team.id, team_name: team.name,
        detail: latest.diff_base !== null
          ? `上期实付比推算${word}约 ${base} ${Math.abs(latest.diff_base).toFixed(2)}`
          : `上期实付比推算${word} ${latest.currency} ${Math.abs(latest.diff_native ?? 0).toFixed(2)}`,
      });
    }
    if (latest && latest.reconciliation === 'unpaid') {
      alerts.push({
        type: 'invoice_unpaid', team_id: team.id, team_name: team.name,
        detail: latest.display_amount_base !== null
          ? `上期账单约 ${base} ${latest.display_amount_base.toFixed(2)} 尚未支付`
          : `上期账单 ${latest.currency} ${(latest.display_amount ?? 0).toFixed(2)} 尚未支付`,
      });
    }

    const note = key ? db.finance.cardNotes.get(key) ?? '' : '';
    if (team.status === 'active' && team.subscription_status !== 'expired' && team.active_until) {
      timeline.push({
        date: team.active_until.slice(0, 10),
        team_id: team.id,
        team_name: team.name,
        owner_email: team.owner_email,
        amount_native: cost.period_total,
        currency: team.billing_currency,
        amount_base: convert(cost.period_total, team.billing_currency, base),
        card_last4: team.card_last4,
        card_brand: team.card_brand,
        card_key: key,
        card_note: note,
        card_team_count: key ? cardCounts.get(key) ?? 0 : 0,
        will_renew: team.will_renew ? 1 : 0,
        billing_period: team.billing_period,
      });
    }

    return {
      team_id: team.id,
      name: team.name,
      owner_email: team.owner_email,
      remark: team.remark,
      status: team.status,
      billing_currency: team.billing_currency,
      billing_symbol: team.billing_symbol,
      billing_period: team.billing_period,
      card_last4: team.card_last4,
      card_brand: team.card_brand,
      card_key: key,
      card_note: note,
      card_team_count: key ? cardCounts.get(key) ?? 0 : 0,
      price_per_seat: cost.price_per_seat,
      price_per_seat_base: convert(cost.price_per_seat, team.billing_currency, base),
      seats_entitled: team.seats_entitled,
      chatgpt_seats_billed: cost.chatgpt_seats_billed,
      premium_seats_paid: cost.premium_seats_paid,
      premium_price_per_seat: cost.premium_price_per_seat,
      premium_price_per_seat_base: convert(cost.premium_price_per_seat, team.billing_currency, base),
      premium_price_source: cost.premium_price_source,
      premium_monthly_native: premiumNative,
      premium_monthly_base: premiumBase,
      seats_in_use: usage.inUse,
      chatgpt_in_use: usage.activeChatgpt,
      codex_count: usage.codex,
      is_codex_enabled: team.is_codex_enabled ? 1 : 0,
      discount_amount: team.discount_amount,
      monthly_total_native: native,
      period_total_native: cost.period_total,
      period_total_base: convert(cost.period_total, team.billing_currency, base),
      monthly_total_base: nativeBase,
      balance: team.balance,
      active_until: team.active_until,
      days_left: daysLeft,
      will_renew: team.will_renew ? 1 : 0,
      subscription_status: team.subscription_status,
      latest_invoice: latest,
    };
  });

  timeline.sort((a, b) => a.date.localeCompare(b.date));

  return {
    base_currency: base,
    fx_updated_at: db.finance.fx_updated_at,
    low_balance_threshold: threshold,
    monthly_total_base: round2(monthlyTotalBase),
    premium_monthly_base_total: round2(premiumIncludedBase),
    discount_total_base: round2(discountTotalBase),
    excluded_teams_count: excluded,
    last_paid_total_base: lastPaidCount > 0 ? round2(lastPaidTotal) : null,
    last_paid_count: lastPaidCount,
    teams,
    timeline,
    alerts,
  };
}

/**
 * Daily billing snapshots. History is reconstructed from the current state by
 * undoing a few dated events (teams joining, seat growth, a promo starting, a
 * team switching to non-renewing), plus a small THB/USD drift. Two days about
 * five months ago are missing on purpose, so the 180/365-day views show a gap
 * (the default 90-day view stays continuous).
 */
export function financeTrends(db: DemoDb, daysParam: number): FinanceTrends {
  const base = db.finance.base_currency;
  const days = Math.min(Math.max(Math.round(daysParam) || 90, 1), 365);
  const today = utcDay(Date.now());
  const FIRST_SNAPSHOT_DAYS_AGO = 200;
  const MISSING = new Set([150, 151]);

  const rows: FinanceTrendsRow[] = [];
  const daily: FinanceDailyTotal[] = [];

  const nativeOn = (record: DemoTeam, daysAgo: number): number | null => {
    const { team } = record;
    const spec = TEAM_SPECS.find((s) => teamIdFor(s.n) === team.id);
    let native = monthlyNative(team);
    if (native === null) return null;
    const price = team.price_per_seat ?? 0;
    if (spec?.slug === 'nebula' && daysAgo > 12) native = price * 16;
    if (spec?.slug === 'tokyo' && daysAgo > 6) native = price * 10;
    if (spec?.slug === 'aurora' && daysAgo > 21) native = price * team.seats_entitled;
    return native;
  };
  const countsOn = (record: DemoTeam, daysAgo: number): boolean => {
    const { team } = record;
    const slug = TEAM_SPECS.find((s) => teamIdFor(s.n) === team.id)?.slug;
    if (slug === 'vega') return daysAgo > 3; // switched to non-renewing 3 days ago
    if (slug === 'sandbox') return daysAgo > 4; // session expired 4 days ago
    return team.status === 'active' && team.subscription_status === 'renewing';
  };

  for (let daysAgo = Math.min(days - 1, FIRST_SNAPSHOT_DAYS_AGO); daysAgo >= 0; daysAgo -= 1) {
    if (MISSING.has(daysAgo)) continue;
    const date = dateOnly(today - daysAgo * DAY);
    let total = 0;
    db.teams.forEach((record) => {
      const spec = TEAM_SPECS.find((s) => teamIdFor(s.n) === record.team.id);
      if (spec && daysAgo > spec.createdDaysAgo) return; // not managed yet, no snapshot row
      const native = nativeOn(record, daysAgo);
      let nativeBase = convert(native, record.team.billing_currency, base);
      if (nativeBase !== null && record.team.billing_currency === 'THB') {
        nativeBase = round2(nativeBase * (1 + 0.012 * Math.sin(daysAgo / 9)));
      }
      rows.push({
        snapshot_date: date,
        team_id: record.team.id,
        billing_currency: record.team.billing_currency,
        monthly_total_native: native,
        monthly_total_base: nativeBase,
        balance: record.team.balance,
      });
      if (countsOn(record, daysAgo) && nativeBase !== null) total += nativeBase;
    });
    daily.push({ date, total_base: round2(total) });
  }

  return { days, base_currency: base, rows, daily_total_base: daily };
}

function paidAmounts(totals: Map<string, number>, base: string): FinancePaidAmounts {
  const amounts = [...totals.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([currency, amount]) => ({ currency, amount: round2(amount) }));
  const converted = amounts.map((item) => convert(item.amount, item.currency, base));
  const known = converted.length > 0 && converted.every((value) => value !== null);
  return { amounts, base: known ? round2(converted.reduce<number>((sum, value) => sum + (value ?? 0), 0)) : null };
}

/**
 * Mirrors backend routes/finance.team_invoice_summary: only `paid` invoices are summed, per currency;
 * the demo rows carry no created_at, so the period start stands in for the charge date.
 */
function invoiceSummary(record: DemoTeam, base: string): FinanceInvoiceSummary {
  const recentSince = Date.now() - 30 * DAY;
  const total = new Map<string, number>();
  const recent = new Map<string, number>();
  let paidCount = 0;
  record.invoices.forEach((row) => {
    const currency = (row.currency || '').toUpperCase();
    if ((row.status || '').toLowerCase() !== 'paid' || !currency || row.amount_paid === null) return;
    paidCount += 1;
    total.set(currency, (total.get(currency) ?? 0) + row.amount_paid);
    const chargedAt = row.period_start ? Date.parse(row.period_start) : NaN;
    if (!Number.isNaN(chargedAt) && chargedAt >= recentSince) {
      recent.set(currency, (recent.get(currency) ?? 0) + row.amount_paid);
    }
  });
  const latest = latestInvoice(record, base);
  return {
    base_currency: base,
    invoice_count: record.invoices.length,
    paid_count: paidCount,
    paid_total: paidAmounts(total, base),
    paid_last_30_days: paidAmounts(recent, base),
    latest_invoice: latest && {
      invoice_id: latest.invoice_id,
      status: latest.status,
      currency: latest.currency,
      display_amount: latest.display_amount,
      display_amount_base: latest.display_amount_base,
      period_start: latest.period_start,
      period_end: latest.period_end,
      hosted_invoice_url: latest.hosted_invoice_url,
    },
  };
}

export function invoicesFor(db: DemoDb, record: DemoTeam, limit = 6): FinanceInvoicesResponse {
  return {
    team_id: record.team.id,
    invoices: record.invoices.slice(0, Math.max(1, Math.min(100, limit))).map((row) => ({ ...row })),
    summary: invoiceSummary(record, db.finance.base_currency),
  };
}
