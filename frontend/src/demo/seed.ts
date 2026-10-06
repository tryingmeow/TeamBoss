/**
 * Static seed specs for the demo deployment. Everything here is invented:
 * emails live at example.com, card numbers end in 0000/4242, ids are patterned.
 *
 * Each team spec exists to exercise a specific UI state; the comment on the
 * spec says which one.
 */
import type { OveragePolicy, WorkspaceDefaultSeatType } from '../types';
import { DAY, HOUR } from './time';

export interface TeamSpec {
  n: number;
  slug: string;
  name: string;
  remark: string | null;
  status: 'active' | 'token_expired';
  /** Set → `auth_state: 'rejected'` since this many hours ago. */
  authRejectedHoursAgo?: number;
  entitled: number;
  /** ChatGPT (`default`) seat members, owner included. */
  gptMembers: number;
  /** Codex (`usage_based`) seat members. */
  codexMembers: number;
  invites: string[];
  codexEnabled: boolean;
  defaultSeat: WorkspaceDefaultSeatType | null;
  currency: string;
  symbol: string;
  period: 'monthly' | 'yearly';
  /** Monthly price per seat in the native currency; null for yearly billing. */
  price: number | null;
  /** Yearly invoice total (native) for yearly teams. */
  yearlyTotal?: number;
  discount?: { amount: number; periods: number; expiresInDays: number; campaign: string };
  balance: string;
  card: { brand: string; last4: '0000' | '4242' } | null;
  renewsInDays: number;
  /** Length of the current billing period in days. */
  periodDays: number;
  willRenew: boolean;
  proxyId: number | null;
  createdDaysAgo: number;
  sync?: { failingSinceHoursAgo: number; suspendedHoursAgo: number; partialFailures: string[] };
  /** How the newest invoice reconciles against the computed monthly total. */
  invoice: 'match' | 'over' | 'unpaid' | 'yearly';
  /** Expiry offsets (ms from now) forced onto the first dated non-owner members. */
  forcedExpiries?: number[];
  /** Number of newest ChatGPT members that were added outside the panel (`source: detected`). */
  detected?: number;
  /** Overage policy; the backend default is `confirm`. */
  policy?: OveragePolicy;
  /** Paid Premium (`prolite`) seats. `entitled` stays the paid ChatGPT count; seats_entitled adds this on top. */
  premiumPaid?: number;
  /** Premium seat members (not counted in `gptMembers`). */
  premiumMembers?: number;
  /** Members on a seat type TeamBoss does not know (`automation`). */
  unknownMembers?: number;
  /** Mark the Premium members as added outside the panel (`source: detected`). */
  premiumDetected?: boolean;
}

export const TEAM_SPECS: TeamSpec[] = [
  {
    // Healthy flagship: Codex on, promo discount → "还剩 N 次折扣", shared card, proxy.
    n: 1, slug: 'aurora', name: 'Aurora', remark: '主力', status: 'active',
    entitled: 25, gptMembers: 21, codexMembers: 3, invites: ['default', 'usage_based'],
    codexEnabled: true, defaultSeat: 'usage_based',
    currency: 'USD', symbol: '$', period: 'monthly', price: 30,
    discount: { amount: 75, periods: 3, expiresInDays: 65, campaign: 'promo-demo-0001' },
    balance: '0', card: { brand: 'visa', last4: '4242' },
    renewsInDays: 18, periodDays: 30, willRenew: true, proxyId: 1, createdDaysAgo: 240,
    invoice: 'match', forcedExpiries: [6 * HOUR, 26 * HOUR, 2 * DAY + 3 * HOUR],
  },
  {
    // Full (20/20), Codex off, positive credit, last invoice paid more than computed.
    // Policy forbid: the full-Team grey state for ChatGPT and Premium.
    n: 2, slug: 'nebula', name: 'Nebula-02', remark: null, status: 'active', policy: 'forbid',
    entitled: 20, gptMembers: 20, codexMembers: 0, invites: [],
    codexEnabled: false, defaultSeat: 'default',
    currency: 'USD', symbol: '$', period: 'monthly', price: 30,
    balance: '12.5000000000', card: { brand: 'mastercard', last4: '0000' },
    renewsInDays: 9, periodDays: 31, willRenew: true, proxyId: null, createdDaysAgo: 190,
    invoice: 'over', forcedExpiries: [3 * DAY + 2 * HOUR],
  },
  {
    // Renews in 2 days → yellow warning state.
    n: 3, slug: 'tokyo', name: 'Tokyo Studio', remark: '设计组', status: 'active',
    entitled: 12, gptMembers: 10, codexMembers: 2, invites: ['default'],
    codexEnabled: true, defaultSeat: 'default',
    currency: 'USD', symbol: '$', period: 'monthly', price: 30,
    balance: '0', card: { brand: 'visa', last4: '4242' },
    renewsInDays: 2, periodDays: 30, willRenew: true, proxyId: 1, createdDaysAgo: 150,
    invoice: 'match', forcedExpiries: [20 * HOUR, 4 * DAY],
  },
  {
    // THB billing with a symbol; negative balance (credit).
    n: 4, slug: 'bangkok', name: 'Bangkok Hub', remark: null, status: 'active',
    entitled: 8, gptMembers: 7, codexMembers: 0, invites: ['default'],
    codexEnabled: false, defaultSeat: 'default',
    currency: 'THB', symbol: '฿', period: 'monthly', price: 1050,
    balance: '-300.0000000000', card: { brand: 'visa', last4: '0000' },
    renewsInDays: 21, periodDays: 30, willRenew: true, proxyId: null, createdDaysAgo: 80,
    invoice: 'match',
  },
  {
    // EUR, yearly billing → monthly totals are null ("年付，月费暂不计算").
    n: 5, slug: 'berlin', name: 'Berlin Ops', remark: '年付', status: 'active', policy: 'auto',
    entitled: 15, gptMembers: 13, codexMembers: 2, invites: [],
    codexEnabled: true, defaultSeat: 'default',
    currency: 'EUR', symbol: '€', period: 'yearly', price: null, yearlyTotal: 4500,
    balance: '0', card: { brand: 'amex', last4: '0000' },
    renewsInDays: 200, periodDays: 365, willRenew: true, proxyId: null, createdDaysAgo: 170,
    invoice: 'yearly',
  },
  {
    // Over capacity (11/10 ChatGPT) with a member added outside the panel → patrol "over".
    n: 6, slug: 'orion', name: 'Orion Lab', remark: null, status: 'active',
    entitled: 10, gptMembers: 11, codexMembers: 1, invites: ['default'],
    codexEnabled: false, defaultSeat: 'default',
    currency: 'USD', symbol: '$', period: 'monthly', price: 30,
    balance: '0', card: { brand: 'mastercard', last4: '4242' },
    renewsInDays: 25, periodDays: 31, willRenew: true, proxyId: null, createdDaysAgo: 45,
    invoice: 'match', detected: 1,
  },
  {
    // Subscription set to not renew; latest invoice still unpaid.
    n: 7, slug: 'vega', name: 'Vega Labs', remark: '到期停用', status: 'active',
    entitled: 6, gptMembers: 4, codexMembers: 0, invites: ['default'],
    codexEnabled: false, defaultSeat: 'default',
    currency: 'USD', symbol: '$', period: 'monthly', price: 30,
    balance: '0', card: { brand: 'visa', last4: '4242' },
    renewsInDays: 12, periodDays: 30, willRenew: false, proxyId: null, createdDaysAgo: 120,
    invoice: 'unpaid',
  },
  {
    // Sync failing for 3 days, scheduler suspended it → "同步已暂停". Expired member not yet removed.
    n: 8, slug: 'lyra', name: 'Lyra', remark: null, status: 'active',
    entitled: 5, gptMembers: 5, codexMembers: 0, invites: [],
    codexEnabled: false, defaultSeat: 'default',
    currency: 'USD', symbol: '$', period: 'monthly', price: 30,
    balance: '0', card: { brand: 'visa', last4: '0000' },
    renewsInDays: 14, periodDays: 30, willRenew: true, proxyId: null, createdDaysAgo: 210,
    sync: { failingSinceHoursAgo: 74, suspendedHoursAgo: 50, partialFailures: ['subscription', 'balance'] },
    invoice: 'match', forcedExpiries: [-(1 * DAY + 4 * HOUR)],
  },
  {
    // Session expired → card overlay "Session 失效", sorted first.
    n: 9, slug: 'sandbox', name: 'Sandbox', remark: '测试', status: 'token_expired',
    entitled: 5, gptMembers: 3, codexMembers: 0, invites: [],
    codexEnabled: false, defaultSeat: null,
    currency: 'USD', symbol: '$', period: 'monthly', price: 30,
    balance: '0', card: { brand: 'mastercard', last4: '0000' },
    renewsInDays: 16, periodDays: 30, willRenew: true, proxyId: null, createdDaysAgo: 300,
    invoice: 'match',
  },
  {
    // Session answers but its access token was revoked upstream → "登录已失效 · 已持续 N 小时".
    n: 10, slug: 'polaris', name: 'Polaris', remark: null, status: 'active', authRejectedHoursAgo: 5,
    entitled: 8, gptMembers: 6, codexMembers: 1, invites: [],
    codexEnabled: true, defaultSeat: 'default',
    currency: 'USD', symbol: '$', period: 'monthly', price: 30,
    balance: '0', card: { brand: 'amex', last4: '4242' },
    renewsInDays: 11, periodDays: 30, willRenew: true, proxyId: 2, createdDaysAgo: 60,
    invoice: 'match', forcedExpiries: [-(7 * HOUR)],
  },
  {
    // Full ChatGPT (6/6) with policy confirm and no Premium seat: every billed add asks first.
    n: 11, slug: 'atlas', name: 'Atlas-11', remark: '运营组', status: 'active', policy: 'confirm',
    entitled: 6, gptMembers: 5, codexMembers: 1, invites: ['default'],
    codexEnabled: true, defaultSeat: 'default',
    currency: 'USD', symbol: '$', period: 'monthly', price: 30,
    balance: '0', card: { brand: 'visa', last4: '4242' },
    renewsInDays: 13, periodDays: 30, willRenew: true, proxyId: null, createdDaysAgo: 100,
    invoice: 'match',
  },
  {
    // Full ChatGPT (4/4) with policy auto: adds go straight through and ChatGPT charges for the extra seat.
    n: 12, slug: 'helix', name: 'Helix-12', remark: '研发备用', status: 'active', policy: 'auto',
    entitled: 4, gptMembers: 4, codexMembers: 0, invites: [],
    codexEnabled: false, defaultSeat: 'default',
    currency: 'USD', symbol: '$', period: 'monthly', price: 30,
    balance: '0', card: { brand: 'mastercard', last4: '4242' },
    renewsInDays: 20, periodDays: 31, willRenew: true, proxyId: null, createdDaysAgo: 70,
    invoice: 'match',
  },
  {
    // Paid Premium seats (3) with 2 members + 1 pending Premium invite → Premium full; ChatGPT has 2 free.
    // seats_entitled includes the Premium seats, so the legacy free-seat formula would invent free seats.
    n: 13, slug: 'zenith', name: 'Zenith-13', remark: 'Premium 试点', status: 'active', policy: 'confirm',
    entitled: 8, gptMembers: 6, codexMembers: 1, invites: ['prolite'],
    premiumPaid: 3, premiumMembers: 2,
    codexEnabled: true, defaultSeat: 'default',
    currency: 'USD', symbol: '$', period: 'monthly', price: 30,
    balance: '0', card: { brand: 'visa', last4: '4242' },
    renewsInDays: 16, periodDays: 30, willRenew: true, proxyId: 1, createdDaysAgo: 55,
    invoice: 'match', forcedExpiries: [30 * HOUR],
  },
  {
    // ChatGPT full + forbid, one Premium seat still free, patrol-exempt so the Premium outsider stays visible, one member on the unknown `automation` seat,
    // and the Premium member was added outside the panel (patrol alert only).
    n: 14, slug: 'quasar', name: 'Quasar-14', remark: '外包项目', status: 'active', policy: 'forbid',
    // One pending invite on the unknown `automation` seat: resending it answers seat_type_unknown.
    entitled: 5, gptMembers: 5, codexMembers: 1, invites: ['automation'],
    premiumPaid: 2, premiumMembers: 1, unknownMembers: 1, premiumDetected: true,
    codexEnabled: true, defaultSeat: 'default',
    currency: 'USD', symbol: '$', period: 'monthly', price: 30,
    balance: '0', card: { brand: 'amex', last4: '4242' },
    renewsInDays: 8, periodDays: 30, willRenew: true, proxyId: null, createdDaysAgo: 35,
    invoice: 'match',
  },
];

/** People who sit in more than one team (multi-team badge, self-service team choice). */
export const SHARED_MEMBERS: Array<{ email: string; name: string; teams: Array<{ slug: string; pending?: boolean }> }> = [
  { email: 'alice.chen@example.com', name: 'Alice Chen', teams: [{ slug: 'aurora' }, { slug: 'tokyo' }] },
  { email: 'bob.lee@example.com', name: 'Bob Lee', teams: [{ slug: 'nebula' }, { slug: 'orion' }] },
  { email: 'yuki.tanaka@example.com', name: 'Yuki Tanaka', teams: [{ slug: 'tokyo' }, { slug: 'berlin' }] },
  { email: 'sam.taylor@example.com', name: 'Sam Taylor', teams: [{ slug: 'aurora' }, { slug: 'vega', pending: true }] },
];

const FIRST = [
  'carol', 'dave', 'erin', 'frank', 'grace', 'heidi', 'ivan', 'judy', 'kevin', 'lily',
  'mallory', 'nina', 'oscar', 'peggy', 'quinn', 'rupert', 'sybil', 'trent', 'uma', 'victor',
  'wendy', 'xavier', 'yara', 'zane', 'liam', 'mia', 'noah', 'emma', 'lucas', 'sofia',
  'ethan', 'chloe', 'mason', 'ava', 'leo', 'isla', 'owen', 'ruby', 'finn', 'hana',
  'jun', 'mei', 'ken', 'aiko', 'raj', 'priya', 'omar', 'lena', 'hugo', 'clara',
  'felix', 'nora', 'tomas', 'elsa', 'diego', 'lucia', 'arjun', 'zoe', 'max', 'ida',
  'theo', 'vera', 'axel', 'iris', 'milo', 'jade', 'otto', 'alma', 'nils', 'rosa',
  'kai', 'luna', 'ezra', 'nova', 'remy', 'tara', 'abel', 'cora', 'enzo', 'gwen',
  'hiro', 'saki', 'tao', 'lin', 'jin', 'yan', 'wei', 'xin', 'fang', 'ming',
  'bea', 'cal', 'dion', 'eve', 'fay', 'gus', 'hal', 'ines', 'joel', 'kira',
];

const LAST = [
  'wang', 'li', 'zhang', 'liu', 'sato', 'kim', 'park', 'nguyen', 'smith', 'garcia',
  'muller', 'rossi', 'dubois', 'silva', 'patel', 'khan', 'novak', 'cohen', 'berg', 'ito',
  'chan', 'ho', 'lam', 'ng', 'tan', 'lim', 'ong', 'koh', 'yamada', 'suzuki',
];

export interface PoolPerson {
  email: string;
  name: string | null;
}

function cap(word: string): string {
  return word.charAt(0).toUpperCase() + word.slice(1);
}

/** A long, deterministic list of unique fake people; consumers take slices of it. */
export function peoplePool(count: number): PoolPerson[] {
  const used = new Set<string>();
  const people: PoolPerson[] = [];
  for (let i = 0; people.length < count; i += 1) {
    const first = FIRST[i % FIRST.length];
    const round = Math.floor(i / FIRST.length);
    const last = LAST[(i * 7 + round * 3) % LAST.length];
    const style = i % 5;
    let local = style === 3 ? first : style === 4 ? `${first[0]}.${last}` : `${first}.${last}`;
    if (used.has(local)) local = `${local}${round + 2}`;
    if (used.has(local)) continue;
    used.add(local);
    // Some ChatGPT profiles have no display name.
    const name = i % 6 === 2 ? null : `${cap(first)} ${cap(last)}`;
    people.push({ email: `${local}@example.com`, name });
  }
  return people;
}

/** Admin-written remarks (`system_display_name`) for specific people, keyed by email. */
export const REMARKS: Record<string, string> = {
  'alice.chen@example.com': 'Alice · 产品',
  'bob.lee@example.com': '后端 · 阿杰',
  'yuki.tanaka@example.com': '合作方 · 东京',
  'owner-sandbox@example.com': '测试号',
};

/** More remarks, handed out across teams at seed time so every team has a few. */
export const REMARK_POOL = [
  '小王 · 设计', '财务 · 李姐', '前端组', '市场部', '实习生', '外包 · 数据标注', '客服组',
  '老客户 · 续费 3 次', '运营 · 小美', '临时 · 一周', '销售 · 华东', '测试号', '法务',
];

/** Remark on a pending invite, so the invite row's "remark · 待接受" layout shows up. */
export const INVITE_REMARK = '新同事 · 下周入职';

/** Members who paired their Telegram account with the bot at some point. */
export const TG_BOUND: Record<string, { username: string; pairedDaysAgo: number }> = {
  'alice.chen@example.com': { username: 'alice_demo', pairedDaysAgo: 41 },
  'bob.lee@example.com': { username: 'bob_demo', pairedDaysAgo: 20 },
  'carol.wang@example.com': { username: 'carol_demo', pairedDaysAgo: 12 },
  'kevin@example.com': { username: 'kevin_demo', pairedDaysAgo: 33 },
  'yuki.tanaka@example.com': { username: 'yuki_demo', pairedDaysAgo: 8 },
  'emma.garcia@example.com': { username: 'emma_demo', pairedDaysAgo: 3 },
};

/** Static FX table, same keys as the backend's DEFAULT_FX_RATES (1 USD = rate). */
export const FX_RATES: Record<string, number> = {
  USD: 1.0, CNY: 7.2, THB: 36.0, NZD: 1.65, AUD: 1.5, CAD: 1.37,
  GBP: 0.78, EUR: 0.92, JPY: 155.0, SGD: 1.34, HKD: 7.8,
  KRW: 1380.0, INR: 84.0, BRL: 5.6, MXN: 18.5, SEK: 10.5,
  NOK: 10.8, DKK: 6.9, CHF: 0.88, PLN: 4.0, CZK: 23.0,
};

/** Patterned, obviously fake workspace id: `de000001-0000-4000-8000-000000000001`. */
export function teamIdFor(n: number): string {
  const nn = String(n).padStart(2, '0');
  return `de0000${nn}-0000-4000-8000-0000000000${nn}`;
}
