/**
 * The demo deployment's in-memory state. Built once per page load from the
 * seed specs, anchored to the install-time clock, then mutated by handlers.
 *
 * Shapes mirror the backend tables closely enough that the view functions in
 * `views.ts` can derive every API response the way the backend does.
 */
import type { CodeSeatType, Settings, Team } from '../types';
import type {
  AccessTokenListItem,
  FinanceInvoiceRow,
  Proxy,
  TgCodesResponse,
  TgConfigResponse,
  TgUsersResponse,
} from '../api/client';
import {
  INVITE_REMARK,
  REMARKS,
  REMARK_POOL,
  SHARED_MEMBERS,
  TEAM_SPECS,
  TG_BOUND,
  peoplePool,
  teamIdFor,
  type PoolPerson,
  type TeamSpec,
} from './seed';
import { DAY, HOUR, MINUTE, isoAt, seededRandom } from './time';
import { buildLogs, type DemoLogRow } from './logs';
import { teamMonthlyCost } from './views';

export type MemberSource = 'system' | 'detected' | 'self_service' | 'admin';
export type KickSource = 'auto_expire' | 'admin' | 'detected' | 'patrol';

export interface DemoMember {
  id: string;
  email: string;
  name: string | null;
  role: 'account-owner' | 'standard-user';
  /** Raw upstream seat type: a registry value or an unknown one such as `automation`. */
  seat_type: string;
  is_owner: boolean;
  created_time: string;
  /** Local expiry record. `source === null` means no record exists ("unmanaged"). */
  expires_at: string | null;
  source: MemberSource | null;
  first_seen_at: string | null;
}

export interface DemoInvite {
  id: string;
  email: string;
  /** Raw upstream seat type; may be an unknown one such as `automation`. */
  seat_type: string;
  created_time: string;
  expires_at: string | null;
  source: MemberSource | null;
  first_seen_at: string | null;
}

export interface DemoKicked {
  expiry_id: number;
  user_id: string;
  email: string;
  expires_at: string | null;
  kicked_at: string;
  kick_source: KickSource;
  first_seen_at: string;
  source: MemberSource;
  created_at: string;
}

export interface DemoTeam {
  team: Team;
  /** Paid seats per billed type; `seat_capacity` and `seats_entitled` are derived from these. */
  paid: { default: number; prolite: number };
  members: DemoMember[];
  invites: DemoInvite[];
  kicked: DemoKicked[];
  invoices: FinanceInvoiceRow[];
  cacheUpdatedAt: string;
}

export interface DemoAccessToken extends AccessTokenListItem {
  seat_type: CodeSeatType;
  /** Full token string. The real backend only keeps a hash; the demo keeps it so lookups work. */
  token: string;
}

export interface DemoTokenUse {
  id: number;
  token_id: number;
  email: string;
  team_id: string | null;
  team_name: string | null;
  user_id: string | null;
  action: string;
  result: 'success' | 'failed' | 'notice' | 'uncertain' | 'pending';
  error_message: string | null;
  expires_at: string | null;
  created_at: string;
}

export interface DemoDb {
  now: number;
  teams: DemoTeam[];
  remarks: Map<string, string>;
  tgBindings: Map<string, { username: string; paired_at: string }>;
  settings: Settings;
  settingsUpdatedAt: string;
  adminApiKey: string;
  proxies: Array<Proxy & { updated_at: string | null }>;
  patrol: { kick_enabled: boolean; baseline_at: string | null; exempt_team_ids: string[]; strict_mode_enabled: boolean };
  tg: { config: TgConfigResponse; users: TgUsersResponse['users']; codes: TgCodesResponse['codes'] };
  tokens: DemoAccessToken[];
  tokenUses: DemoTokenUse[];
  finance: {
    base_currency: string;
    low_balance_threshold: number;
    fx_updated_at: string | null;
    cardNotes: Map<string, string>;
  };
  /** Raw operation_logs rows, newest first; team columns are joined at read time. */
  logs: DemoLogRow[];
  seq: number;
}

export function nextId(db: DemoDb): number {
  db.seq += 1;
  return db.seq;
}

export function findTeam(db: DemoDb, teamId: string): DemoTeam | undefined {
  return db.teams.find((record) => record.team.id === teamId);
}

/** Seed-time lookup by spec slug; throws because a missing seed team is a programming error. */
export function findTeamBySlug(db: DemoDb, slug: string): DemoTeam {
  const spec = TEAM_SPECS.find((s) => s.slug === slug);
  const record = spec ? findTeam(db, teamIdFor(spec.n)) : undefined;
  if (!record) throw new Error(`demo seed: unknown team ${slug}`);
  return record;
}

/** Recomputes the cached seat counts, capacity and member emails after a membership change. */
export function recount(record: DemoTeam): void {
  const { team, members, invites, paid } = record;
  team.seats_in_use = members.length;
  team.codex_count = members.filter((m) => m.seat_type === 'usage_based').length;
  team.chatgpt_count = members.filter((m) => m.seat_type === 'default').length;
  const counts: Record<string, number> = { default: 0, usage_based: 0, automation: 0, prolite: 0 };
  members.forEach((m) => {
    counts[m.seat_type] = (counts[m.seat_type] ?? 0) + 1;
  });
  team.seat_type_counts = counts;
  // Like the upstream subscription, `available` is paid minus active members of the type; pending
  // invites are subtracted separately by whoever computes free seats.
  team.seat_capacity = {
    default: { paid: paid.default, available: Math.max(0, paid.default - counts.default), renewal_requested: paid.default },
    prolite: { paid: paid.prolite, available: Math.max(0, paid.prolite - counts.prolite), renewal_requested: paid.prolite },
  };
  team.seats_entitled = paid.default + paid.prolite;
  team.cached_member_emails = Array.from(
    new Set([...members, ...invites].map((m) => m.email.trim().toLowerCase())),
  );
}

function pad2(n: number): string {
  return String(n).padStart(2, '0');
}

/** Hands out pool people in order, skipping anyone already placed explicitly. */
function personCursor(pool: PoolPerson[], reserved: Set<string>) {
  let index = 0;
  return (): PoolPerson => {
    while (index < pool.length && reserved.has(pool[index].email)) index += 1;
    const person = pool[index];
    index += 1;
    if (!person) throw new Error('demo seed: people pool exhausted');
    return person;
  };
}

function buildTeam(spec: TeamSpec, now: number, nextPerson: () => PoolPerson): DemoTeam {
  const rand = seededRandom(spec.n * 7919);
  const id = teamIdFor(spec.n);
  const createdAt = now - spec.createdDaysAgo * DAY;
  const ownerEmail = `owner-${spec.slug}@example.com`;

  const members: DemoMember[] = [
    {
      id: `user-${spec.slug}-00`,
      email: ownerEmail,
      name: `${spec.name} Admin`,
      role: 'account-owner',
      seat_type: 'default',
      is_owner: true,
      created_time: isoAt(createdAt),
      expires_at: null,
      source: null,
      first_seen_at: null,
    },
  ];

  const shared = SHARED_MEMBERS.filter((p) => p.teams.some((t) => t.slug === spec.slug && !t.pending));
  const seats: string[] = [
    ...Array<string>(spec.gptMembers - 1).fill('default'),
    ...Array<string>(spec.codexMembers).fill('usage_based'),
    ...Array<string>(spec.premiumMembers ?? 0).fill('prolite'),
    ...Array<string>(spec.unknownMembers ?? 0).fill('automation'),
  ];
  // Spread Codex seats through the list instead of bunching them at the end.
  for (let i = seats.length - 1; i > 0; i -= 1) {
    const j = Math.floor(rand() * (i + 1));
    [seats[i], seats[j]] = [seats[j], seats[i]];
  }
  // The newest ChatGPT seats are the externally added ones. Pick them from the ChatGPT seats
  // only: after the shuffle the last seat can be a Codex one, which would leave none.
  const chatgptSeats = seats.flatMap((seat, index) => (seat === 'default' ? [index] : []));
  const detectedSeats = new Set(spec.detected ? chatgptSeats.slice(-spec.detected) : []);
  const forced = [...(spec.forcedExpiries ?? [])];

  seats.forEach((seat, index) => {
    const person = index < shared.length ? { email: shared[index].email, name: shared[index].name } : nextPerson();
    const isDetected = detectedSeats.has(index) || (Boolean(spec.premiumDetected) && seat === 'prolite');
    let joinedAt = isDetected
      ? now - 40 * MINUTE
      : createdAt + Math.floor(rand() * Math.max(1, now - createdAt - DAY));
    // Every other team had someone join this week, so the logs have invite → accepted pairs.
    if (!isDetected && index === shared.length && spec.n % 2 === 1) {
      joinedAt = now - Math.round(spec.n * 0.55 * DAY);
    }

    let expiresAt: number | null = null;
    let source: MemberSource | null = null;
    if (isDetected) {
      source = 'detected';
    } else if (forced.length > 0 && index >= shared.length) {
      expiresAt = now + forced.shift()!;
      source = 'system';
    } else {
      const r = rand();
      if (r < 0.28) {
        source = 'system'; // permanent grant
      } else if (r < 0.36) {
        source = null; // predates the panel, never managed
      } else {
        expiresAt = now + (4 + Math.floor(rand() * 76)) * DAY + Math.floor(rand() * 20) * HOUR;
        source = rand() < 0.72 ? 'system' : 'self_service';
      }
    }

    members.push({
      id: `user-${spec.slug}-${pad2(index + 1)}`,
      email: person.email,
      name: person.name,
      role: 'standard-user',
      seat_type: seat,
      is_owner: false,
      created_time: isoAt(joinedAt),
      expires_at: expiresAt === null ? null : isoAt(expiresAt),
      source,
      first_seen_at: source === null ? null : isoAt(joinedAt + 2 * MINUTE),
    });
  });

  // Alice keeps a dated grant in both of her teams so self-service renewal has something to extend.
  members.forEach((m) => {
    if (m.email === 'alice.chen@example.com') {
      m.expires_at = isoAt(now + (spec.slug === 'aurora' ? 12 : 40) * DAY + 3 * HOUR);
      m.source = 'self_service';
    }
  });

  members.sort((a, b) => {
    if (a.is_owner !== b.is_owner) return a.is_owner ? -1 : 1;
    return a.created_time.localeCompare(b.created_time);
  });

  const invites: DemoInvite[] = spec.invites.map((seat, index) => {
    const person = nextPerson();
    const sentAt = now - (2 + index * 19) * HOUR;
    return {
      id: `invite-${spec.slug}-${pad2(index + 1)}`,
      email: person.email,
      seat_type: seat,
      created_time: isoAt(sentAt),
      expires_at: isoAt(sentAt + 30 * DAY),
      source: 'system',
      first_seen_at: isoAt(sentAt),
    };
  });
  SHARED_MEMBERS.filter((p) => p.teams.some((t) => t.slug === spec.slug && t.pending)).forEach((p, index) => {
    const sentAt = now - 9 * HOUR;
    invites.push({
      id: `invite-${spec.slug}-s${index + 1}`,
      email: p.email,
      seat_type: 'default',
      created_time: isoAt(sentAt),
      expires_at: isoAt(sentAt + 7 * DAY),
      source: 'self_service',
      first_seen_at: isoAt(sentAt),
    });
  });

  const activeUntil = now + spec.renewsInDays * DAY + 5 * HOUR;
  const activeStart = activeUntil - spec.periodDays * DAY;
  // Like the real API, the Team's active_start is when the subscription began (the first
  // period boundary after the Team was created), not the start of the current period.
  const periodMs = spec.periodDays * DAY;
  const subscriptionStart = activeStart - Math.max(0, Math.floor((activeStart - createdAt) / periodMs)) * periodMs;
  const discountAmount = spec.discount?.amount ?? 0;

  let lastFullSync = now - (3 + (spec.n % 9)) * MINUTE;
  if (spec.sync) lastFullSync = now - spec.sync.failingSinceHoursAgo * HOUR;
  if (spec.status === 'token_expired') lastFullSync = now - 4 * DAY - 2 * HOUR;
  if (spec.authRejectedHoursAgo) lastFullSync = now - spec.authRejectedHoursAgo * HOUR - 11 * MINUTE;

  const team: Team = {
    id,
    name: spec.name,
    remark: spec.remark,
    owner_email: ownerEmail,
    status: spec.status,
    seats_in_use: 0,
    seats_entitled: spec.entitled + (spec.premiumPaid ?? 0),
    codex_count: 0,
    chatgpt_count: 0,
    is_codex_enabled: spec.codexEnabled,
    default_seat_type: spec.defaultSeat,
    billing_currency: spec.currency,
    billing_symbol: spec.symbol,
    billing_period: spec.period,
    price_per_seat: spec.price,
    // Prices are monthly rates for the Team's actual billing period.
    premium_price_per_seat: spec.premiumPrice ?? null,
    discount_amount: discountAmount,
    discount_duration_num_periods: spec.discount?.periods ?? null,
    discount_expires_at: spec.discount ? isoAt(now + spec.discount.expiresInDays * DAY) : null,
    discount_quantity_off: null,
    promo_campaign_id: spec.discount?.campaign ?? null,
    // Filled from the paid seats below (teamMonthlyCost, the same rule teamView applies on read).
    monthly_subtotal: null,
    monthly_total: null,
    balance: spec.balance,
    active_start: isoAt(subscriptionStart),
    active_until: isoAt(activeUntil),
    will_renew: spec.willRenew,
    subscription_status: spec.willRenew ? 'renewing' : 'nonrenewing',
    card_last4: spec.card?.last4 ?? null,
    card_brand: spec.card?.brand ?? null,
    days_remaining: Math.max(0, Math.floor((activeUntil - now) / DAY)),
    proxy_id: spec.proxyId,
    last_full_sync_at: isoAt(lastFullSync),
    last_sync_partial_failures: spec.sync?.partialFailures ?? [],
    auth_state: spec.authRejectedHoursAgo ? 'rejected' : 'ok',
    auth_state_since: spec.authRejectedHoursAgo ? isoAt(now - spec.authRejectedHoursAgo * HOUR) : null,
    sync_failing_since: spec.sync ? isoAt(now - spec.sync.failingSinceHoursAgo * HOUR) : null,
    sync_suspended_at: spec.sync ? isoAt(now - spec.sync.suspendedHoursAgo * HOUR) : null,
    cached_member_emails: [],
    overage_policy: spec.policy ?? 'confirm',
    seat_capacity: null,
    seat_type_counts: {},
  };

  const record: DemoTeam = {
    team,
    paid: { default: spec.entitled, prolite: spec.premiumPaid ?? 0 },
    members,
    invites,
    kicked: [],
    invoices: buildInvoices(spec, activeStart, activeUntil, createdAt),
    cacheUpdatedAt: isoAt(lastFullSync),
  };
  recount(record);
  const cost = teamMonthlyCost(team);
  team.monthly_subtotal = cost.monthly_subtotal;
  team.monthly_total = cost.monthly_total;
  team.period_subtotal = cost.period_subtotal;
  team.period_total = cost.period_total;
  return record;
}

function money(value: number): string {
  return value.toFixed(2);
}

function buildInvoices(spec: TeamSpec, activeStart: number, activeUntil: number, createdAt: number): FinanceInvoiceRow[] {
  const rows: FinanceInvoiceRow[] = [];
  const add = (
    index: number,
    start: number,
    end: number,
    status: string,
    due: number,
    paid: number,
    description: string,
  ) => {
    const nnnn = String(index).padStart(4, '0');
    rows.push({
      invoice_id: `in_demo_${spec.slug}_${nnnn}`,
      number: `DEMO-${spec.slug.toUpperCase()}-${nnnn}`,
      status,
      currency: spec.currency,
      amount_due: due,
      amount_paid: paid,
      period_start: isoAt(start),
      period_end: isoAt(end),
      description,
      hosted_invoice_url: `https://invoice.example.com/demo/${spec.slug}-${nnnn}`,
    });
  };

  // Unknown current prices do not manufacture a zero-cost or partial invoice.
  if (spec.price === null || ((spec.premiumPaid ?? 0) > 0 && spec.premiumPrice === undefined)) return rows;
  if (spec.period === 'yearly') {
    const standard = spec.price * spec.entitled * 12;
    const premium = (spec.premiumPrice ?? 0) * (spec.premiumPaid ?? 0) * 12;
    const total = Math.max(0, standard - (spec.discount?.amount ?? 0)) + premium;
    add(1, activeStart, activeUntil, 'paid', total, total,
      `${spec.entitled} × ChatGPT Business (at ${spec.symbol}${money(spec.price * 12)} / year)`
      + ((spec.premiumPaid ?? 0) > 0 ? ` + ${spec.premiumPaid} × Premium (at ${spec.symbol}${money((spec.premiumPrice ?? 0) * 12)} / year)` : ''));
    return rows;
  }

  const price = spec.price ?? 0;
  // Each paid seat type contributes its known price.
  const premiumCharge = spec.premiumPrice && spec.premiumPaid ? spec.premiumPrice * spec.premiumPaid : 0;
  const premiumLine = premiumCharge
    ? ` + ${spec.premiumPaid} × ChatGPT Business Premium (at ${spec.symbol}${money(spec.premiumPrice ?? 0)} / month)`
    : '';
  const line = (seats: number) => `${seats} × ChatGPT Business (at ${spec.symbol}${money(price)} / month)${premiumLine}`;
  const fullTotal = price * spec.entitled + premiumCharge;
  let index = 0;
  for (let k = 5; k >= 0; k -= 1) {
    const start = activeStart - k * spec.periodDays * DAY;
    if (start < createdAt - DAY) continue;
    const end = start + spec.periodDays * DAY;
    index += 1;
    const latest = k === 0;
    if (spec.slug === 'nebula') {
      // Grew from 16 to 20 seats mid-period: the latest charge includes proration.
      if (latest) add(index, start, end, 'paid', 640, 640, `${line(20)} + proration for 4 added seats`);
      else add(index, start, end, 'paid', price * 16, price * 16, line(16));
    } else if (spec.slug === 'vega' && latest) {
      add(index, start, end, 'open', fullTotal, 0, line(spec.entitled));
    } else if (spec.discount && latest) {
      const discounted = Math.max(0, price * spec.entitled - (spec.discount?.amount ?? 0)) + premiumCharge;
      add(index, start, end, 'paid', discounted, discounted, `${line(spec.entitled)} · promo -${spec.symbol}${money(spec.discount?.amount ?? 0)}`);
    } else {
      add(index, start, end, 'paid', fullTotal, fullTotal, line(spec.entitled));
    }
    if (spec.slug === 'aurora' && k === 2) {
      // A duplicate draft that was voided; renders dimmed in the invoice sub-table.
      index += 1;
      add(index, start, end, 'void', fullTotal, 0, line(spec.entitled));
    }
  }
  // Newest first, like ORDER BY COALESCE(period_end, created_at) DESC.
  return rows.reverse().slice(0, 6);
}

/** Members who left: auto-expired, kicked by an admin, removed upstream, or by patrol. */
const KICKED_SPECS: Array<{ slug: string; source: KickSource; kickedHoursAgo: number; grantedDaysBefore: number }> = [
  { slug: 'aurora', source: 'auto_expire', kickedHoursAgo: 14, grantedDaysBefore: 30 },
  { slug: 'aurora', source: 'auto_expire', kickedHoursAgo: 61, grantedDaysBefore: 7 },
  { slug: 'nebula', source: 'admin', kickedHoursAgo: 30, grantedDaysBefore: 30 },
  { slug: 'tokyo', source: 'auto_expire', kickedHoursAgo: 96, grantedDaysBefore: 30 },
  { slug: 'bangkok', source: 'detected', kickedHoursAgo: 120, grantedDaysBefore: 90 },
  { slug: 'orion', source: 'patrol', kickedHoursAgo: 49, grantedDaysBefore: 0 },
  { slug: 'vega', source: 'auto_expire', kickedHoursAgo: 150, grantedDaysBefore: 30 },
  { slug: 'berlin', source: 'admin', kickedHoursAgo: 8, grantedDaysBefore: 360 },
  // A Premium outsider removed by patrol (Zenith is not exempt); the log row carries seat_type=prolite.
  { slug: 'zenith', source: 'patrol', kickedHoursAgo: 20, grantedDaysBefore: 0 },
];

export function createDemoDb(now: number): DemoDb {
  const reserved = new Set(SHARED_MEMBERS.map((p) => p.email));
  const nextPerson = personCursor(peoplePool(200), reserved);

  const teams = TEAM_SPECS.map((spec) => buildTeam(spec, now, nextPerson));

  let expiryId = 500;
  KICKED_SPECS.forEach((k) => {
    const spec = TEAM_SPECS.find((s) => s.slug === k.slug)!;
    const record = teams.find((t) => t.team.id === teamIdFor(spec.n))!;
    const person = nextPerson();
    const kickedAt = now - k.kickedHoursAgo * HOUR;
    const firstSeen = kickedAt - Math.max(k.grantedDaysBefore, 1) * DAY;
    expiryId += 1;
    record.kicked.push({
      expiry_id: expiryId,
      user_id: `user-${k.slug}-k${expiryId - 500}`,
      email: person.email,
      expires_at: k.source === 'auto_expire' ? isoAt(kickedAt - 4 * MINUTE) : isoAt(kickedAt + 9 * DAY),
      kicked_at: isoAt(kickedAt),
      kick_source: k.source,
      first_seen_at: isoAt(firstSeen),
      source: k.source === 'patrol' || k.source === 'detected' ? 'detected' : 'system',
      created_at: isoAt(firstSeen),
    });
  });

  const remarks = new Map(Object.entries(REMARKS));
  const sharedEmails = new Set(SHARED_MEMBERS.map((p) => p.email));
  let remarkIndex = 0;
  [2, 7].forEach((position) => {
    teams.forEach((record) => {
      const candidates = record.members.filter((m) => !m.is_owner && !sharedEmails.has(m.email));
      const target = candidates[position];
      if (target && remarkIndex < REMARK_POOL.length) remarks.set(target.email, REMARK_POOL[remarkIndex++]);
    });
  });
  const firstInvite = teams[0].invites[0];
  if (firstInvite) remarks.set(firstInvite.email, INVITE_REMARK);
  const tgBindings = new Map(
    Object.entries(TG_BOUND).map(([email, b]) => [email, { username: b.username, paired_at: isoAt(now - b.pairedDaysAgo * DAY) }]),
  );

  const db: DemoDb = {
    now,
    teams,
    remarks,
    tgBindings,
    settings: {
      sync_interval_minutes: 15,
      api_concurrency: 4,
      expiry_kick_mode: 'delay_hours',
      expiry_kick_delay_hours: 0,
      skip_overage_confirmation: false,
    },
    settingsUpdatedAt: isoAt(now - 12 * DAY),
    adminApiKey: 'demo-key',
    proxies: [
      {
        id: 1,
        name: 'Tokyo Residential',
        url: 'http://demo:demo@proxy-tokyo.example.com:8080',
        status: 'ok',
        last_check_at: isoAt(now - 22 * MINUTE),
        created_at: isoAt(now - 140 * DAY),
        updated_at: isoAt(now - 22 * MINUTE),
      },
      {
        id: 2,
        name: 'Backup SOCKS5',
        url: 'socks5://proxy-backup.example.com:1080',
        status: 'error',
        last_check_at: isoAt(now - 2 * HOUR - 7 * MINUTE),
        created_at: isoAt(now - 58 * DAY),
        updated_at: isoAt(now - 2 * HOUR - 7 * MINUTE),
      },
    ],
    patrol: {
      kick_enabled: true,
      baseline_at: isoAt(now - 19 * DAY - 3 * HOUR),
      exempt_team_ids: [teamIdFor(4), teamIdFor(7), teamIdFor(14)],
      strict_mode_enabled: false,
    },
    tg: {
      config: {
        enabled: false,
        token_set: false,
        bot_username: null,
        polling: false,
        summary_enabled: false,
        summary_interval_minutes: 60,
        summary_last_sent_at: isoAt(now - 3 * DAY - 15 * HOUR),
      },
      users: [
        { id: 3, chat_id: '100000003', username: 'night_shift_demo', note: '夜班', disabled: true, paired_at: isoAt(now - 26 * DAY) },
        { id: 2, chat_id: '100000002', username: 'ops_lead_demo', note: '值班', disabled: false, paired_at: isoAt(now - 47 * DAY) },
        { id: 1, chat_id: '100000001', username: 'owner_demo', note: '', disabled: false, paired_at: isoAt(now - 93 * DAY) },
      ],
      codes: [
        {
          id: 3, code: 'DEMX7K2P', note: '新同事', expires_at: isoAt(now + 20 * HOUR),
          used_by_chat_id: null, used_at: null, disabled: false, created_at: isoAt(now - 4 * HOUR),
        },
        {
          id: 2, code: 'DEMB9T3W', note: '夜班', expires_at: isoAt(now - 25 * DAY),
          used_by_chat_id: '100000003', used_at: isoAt(now - 26 * DAY), disabled: false, created_at: isoAt(now - 26 * DAY - 2 * HOUR),
        },
        {
          id: 1, code: 'DEMH4N8R', note: '', expires_at: isoAt(now - 46 * DAY),
          used_by_chat_id: '100000002', used_at: isoAt(now - 47 * DAY), disabled: false, created_at: isoAt(now - 47 * DAY - HOUR),
        },
      ],
    },
    tokens: [],
    tokenUses: [],
    finance: {
      base_currency: 'USD',
      low_balance_threshold: 0,
      fx_updated_at: isoAt(now - 6 * HOUR - 12 * MINUTE),
      cardNotes: new Map([
        ['visa:4242', '公司主卡'],
        ['visa:0000', '泰国备用卡'],
        ['amex:0000', '欧洲子公司'],
      ]),
    },
    logs: [],
    seq: 10_000,
  };

  seedAccessTokens(db);
  db.logs = buildLogs(db);
  return db;
}

/** Redeem codes in every state, plus the redemption rows behind them. */
function seedAccessTokens(db: DemoDb): void {
  const { now } = db;
  const aurora = findTeamBySlug(db, 'aurora');
  const tokyo = findTeamBySlug(db, 'tokyo');
  const nebula = findTeamBySlug(db, 'nebula');
  const orion = findTeamBySlug(db, 'orion');
  const zenith = findTeamBySlug(db, 'zenith');
  const premiumMember = zenith.members.find((m) => m.seat_type === 'prolite' && !m.is_owner) ?? zenith.members[1];

  type TokenSeed = {
    grant: string;
    ttlDays: number | null;
    note: string | null;
    seatType?: CodeSeatType;
    createdDaysAgo: number;
    used?: { email: string; team: DemoTeam; daysAgo: number; action: string; result?: DemoTokenUse['result'] };
    disabled?: boolean;
  };
  const seeds: TokenSeed[] = [
    { grant: '30d', ttlDays: 30, note: '月卡 · 渠道 A', createdDaysAgo: 62,
      used: { email: 'alice.chen@example.com', team: aurora, daysAgo: 60, action: 'invited' } },
    { grant: '30d', ttlDays: 30, note: '月卡 · 渠道 A', createdDaysAgo: 33,
      used: { email: 'alice.chen@example.com', team: aurora, daysAgo: 31, action: 'renewed_member' } },
    { grant: '90d', ttlDays: 7, note: '季卡', createdDaysAgo: 20,
      used: { email: 'bob.lee@example.com', team: orion, daysAgo: 19, action: 'invited' } },
    { grant: '30d', ttlDays: 7, note: null, createdDaysAgo: 4,
      used: { email: 'alice.chen@example.com', team: tokyo, daysAgo: 3, action: 'renewed_member' } },
    { grant: '7d', ttlDays: 1, note: '体验 7 天', createdDaysAgo: 2,
      used: { email: nebula.members[nebula.members.length - 1].email, team: nebula, daysAgo: 2, action: 'invited' } },
    { grant: '30d', ttlDays: 7, note: '月卡 · 渠道 B', createdDaysAgo: 1,
      used: { email: 'nina.k@example.com', team: tokyo, daysAgo: 0.03, action: 'invite_pending', result: 'uncertain' } },
    { grant: '30d', ttlDays: 7, note: '月卡 · 渠道 B', createdDaysAgo: 1 },
    { grant: '30d', ttlDays: 30, note: '月卡 · 渠道 A', createdDaysAgo: 0.4 },
    { grant: '90d', ttlDays: 30, note: '季卡', createdDaysAgo: 0.2 },
    { grant: 'never', ttlDays: null, note: '内部 · 永久', createdDaysAgo: 12 },
    { grant: '7d', ttlDays: 1, note: '体验 7 天', createdDaysAgo: 9 },
    { grant: '30d', ttlDays: 7, note: '误发，已作废', createdDaysAgo: 6, disabled: true },
    { grant: '360d', ttlDays: 7, note: '年卡', createdDaysAgo: 15 },
    // Premium codes: one already redeemed into Zenith, two unused (one has nowhere to go while Zenith is full).
    { grant: '30d', ttlDays: 30, note: 'Premium 月卡', seatType: 'prolite', createdDaysAgo: 10,
      used: { email: premiumMember.email, team: zenith, daysAgo: 8, action: 'invited' } },
    { grant: '30d', ttlDays: 14, note: 'Premium 月卡', seatType: 'prolite', createdDaysAgo: 0.5 },
    { grant: '90d', ttlDays: 30, note: 'Premium 季卡 · 内部', seatType: 'prolite', createdDaysAgo: 0.3 },
  ];

  seeds.forEach((seed, index) => {
    const id = index + 1;
    const nnnn = String(id).padStart(4, '0');
    const token = `atm_demo${nnnn}-not-a-real-token-${nnnn}`;
    const createdAt = now - seed.createdDaysAgo * DAY;
    const usedAt = seed.used ? now - seed.used.daysAgo * DAY : null;
    db.tokens.push({
      id,
      token,
      token_prefix: token.slice(0, 12),
      seat_type: seed.seatType ?? 'default',
      grant_expires_in: seed.grant,
      token_expires_at: seed.ttlDays === null ? null : isoAt(createdAt + seed.ttlDays * DAY),
      max_uses: 1,
      used_count: seed.used ? 1 : 0,
      note: seed.note,
      disabled: Boolean(seed.disabled),
      created_at: isoAt(createdAt),
      last_used_at: usedAt === null ? null : isoAt(usedAt),
    });
    if (seed.used && usedAt !== null) {
      const member = seed.used.team.members.find((m) => m.email === seed.used!.email);
      const grantMs = seed.grant === 'never' ? null : Number.parseInt(seed.grant, 10) * DAY;
      db.tokenUses.push({
        id: 100 + id,
        token_id: id,
        email: seed.used.email,
        team_id: seed.used.team.team.id,
        team_name: seed.used.team.team.name,
        user_id: member?.id ?? null,
        action: seed.used.action,
        result: seed.used.result ?? 'success',
        error_message: seed.used.result === 'uncertain' ? 'Read timed out after invite request (upstream 504)' : null,
        expires_at: seed.used.result === 'uncertain' || grantMs === null ? null : isoAt(usedAt + grantMs),
        created_at: isoAt(usedAt),
      });
    }
  });

  // Alice once redeemed while sitting in two teams and was asked to pick one.
  db.tokenUses.push({
    id: 99,
    token_id: 4,
    email: 'alice.chen@example.com',
    team_id: null,
    team_name: null,
    user_id: null,
    action: 'renew_multi_team_prompt',
    result: 'notice',
    error_message: null,
    expires_at: null,
    created_at: isoAt(now - 3 * DAY - 2 * MINUTE),
  });
}
