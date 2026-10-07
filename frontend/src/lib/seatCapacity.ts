/**
 * Seat counts and the cached "is there a free paid seat?" check the UI uses to grey out or
 * confirm an action before it can cost money. The rule is the backend's `billed_free_seats`
 * (app/services/seat_capacity.py); the server re-checks live data and has the last word.
 */
import type { OveragePolicy, SeatType, Team } from '../types';
import { SEAT_TYPES, overagePolicyLabel, parseOveragePolicy, parseSeatType } from './seatType';
import { formatSeatAmountShort, seatChargeText, teamSeatPrice, type SeatPrice } from './seatPrice';

type TeamSeatFields = Pick<Team, 'seats_entitled' | 'seats_in_use' | 'codex_count' | 'chatgpt_count'>;
/** The synced billing fields a gate needs to say what a bought seat costs. */
type TeamPriceFields = Pick<Team,
  'billing_period' | 'billing_currency' | 'billing_symbol' | 'price_per_seat' | 'premium_price_per_seat'>;
/**
 * Older payloads (and some pages) may lack the per-type fields; every reader treats them as unknown.
 * Without the billing fields a gate's price is unknown.
 */
export type TeamCapacityFields = TeamSeatFields &
  Partial<Pick<Team, 'seat_capacity' | 'seat_type_counts' | 'overage_policy' | 'pending_invite_counts'>> &
  Partial<TeamPriceFields>;

function toNumber(value: number | null | undefined): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function capacityEntry(team: Partial<Pick<Team, 'seat_capacity'>>, seatType: SeatType) {
  const entry = team.seat_capacity?.[seatType];
  if (!entry || typeof entry.available !== 'number' || typeof entry.paid !== 'number') return null;
  return entry;
}

export function activeChatGptSeats(
  team: Pick<TeamSeatFields, 'seats_in_use' | 'codex_count' | 'chatgpt_count'>
): number {
  if (typeof team.chatgpt_count === 'number' && Number.isFinite(team.chatgpt_count)) {
    return Math.max(0, team.chatgpt_count);
  }
  return Math.max(0, toNumber(team.seats_in_use) - toNumber(team.codex_count));
}

/**
 * Paid ChatGPT seats: the per-type `paid` count when the workspace reports it, otherwise
 * `seats_entitled`. Once Premium seats are paid, `seats_entitled` may include them, so it
 * would overstate the ChatGPT seats (the finance page's `chatgpt_seats_billed` uses the same rule).
 */
export function chatgptPaidSeats(team: Pick<Team, 'seats_entitled'> & Partial<Pick<Team, 'seat_capacity'>>): number {
  const entry = capacityEntry(team, 'default');
  return entry ? Math.max(0, entry.paid) : Math.max(0, toNumber(team.seats_entitled));
}

/** Premium members and paid Premium seats; null when the Team has neither (nothing to show). */
export function premiumSeatUsage(
  team: Partial<Pick<Team, 'seat_capacity' | 'seat_type_counts'>>
): { inUse: number; paid: number } | null {
  const inUse = Math.max(0, toNumber(team.seat_type_counts?.prolite));
  const paid = Math.max(0, capacityEntry(team, 'prolite')?.paid ?? 0);
  return inUse > 0 || paid > 0 ? { inUse, paid } : null;
}

/** Pending invites per seat type (blank = ChatGPT, as everywhere else). */
export function pendingCountsByType(invites: ReadonlyArray<{ seat_type?: string | null }> | null | undefined): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const invite of invites ?? []) {
    const raw = (invite.seat_type ?? '').trim() || 'default';
    counts[raw] = (counts[raw] ?? 0) + 1;
  }
  return counts;
}

export interface CachedFreeSeats {
  free: number;
  /**
   * Seats held beyond what is paid (in use + pending invites over paid, or the formulas below
   * going negative). When > 0, free is 0 and every UI says 超出 N.
   */
  over: number;
  /** No per-type number for a type that needs one (Premium): counted as full. */
  unknown: boolean;
  /** The workspace reports this type but has no paid seat of it at all (e.g. no Premium yet). */
  noSeats: boolean;
}

/** In use / paid / pending for one billed type, as the Team card shows them. */
function seatCounts(team: TeamCapacityFields, seatType: SeatType, pendingByType: Record<string, number>) {
  const inUse = seatType === 'default'
    ? activeChatGptSeats(team)
    : Math.max(0, toNumber(team.seat_type_counts?.[seatType]));
  const entry = capacityEntry(team, seatType);
  // ChatGPT always has a paid count (seats_entitled at worst); other types only via seat_capacity.
  const paid = seatType === 'default'
    ? chatgptPaidSeats(team)
    : entry ? Math.max(0, entry.paid) : team.seat_capacity ? 0 : null;
  return { inUse, paid, pending: Math.max(0, toNumber(pendingByType[seatType])), entry };
}

/**
 * Free seats of a billed type from cached data:
 * per-type = seat_capacity[T].available − pending T; ChatGPT also takes the old formula
 * (seats_entitled − active ChatGPT − pending ChatGPT) and uses the smaller; Premium without a
 * per-type entry = 0. Non-billed types never need a free seat (returns 0, unknown false).
 * Pending invites hold seats, so in use + pending beyond paid counts as 超出.
 */
export function cachedFreeSeats(
  team: TeamCapacityFields,
  seatType: SeatType,
  pendingByType: Record<string, number> = {}
): CachedFreeSeats {
  if (!SEAT_TYPES[seatType].billed) return { free: 0, over: 0, unknown: false, noSeats: false };
  const { inUse, paid, pending, entry } = seatCounts(team, seatType, pendingByType);
  const perType = entry ? entry.available - pending : null;
  const legacy = seatType === 'default'
    ? toNumber(team.seats_entitled) - activeChatGptSeats(team) - pending
    : null;
  const candidates = [perType, legacy].filter((value): value is number => value !== null);
  // 工作区报了分类型容量、却没有这个类型的已付席位（常见于还没买过 Premium 的 Team）。
  const noSeats = seatType !== 'default' && Boolean(team.seat_capacity) && (!entry || entry.paid === 0);
  const lowest = candidates.length > 0 ? Math.min(...candidates) : null;
  const over = Math.max(0, paid === null ? 0 : inUse + pending - paid, lowest === null ? 0 : -lowest);
  if (lowest === null) return { free: 0, over, unknown: !noSeats, noSeats };
  return { free: over > 0 ? 0 : Math.max(0, lowest), over, unknown: false, noSeats };
}

/** Pending invites per type: the loaded member list when there is one, else the Team's cached counts. */
export function teamPendingCounts(
  team: Partial<Pick<Team, 'pending_invite_counts'>>,
  invites?: ReadonlyArray<{ seat_type?: string | null }> | null
): Record<string, number> {
  if (invites) return pendingCountsByType(invites);
  return team.pending_invite_counts ?? {};
}

/** What a billed seat block on the Team card shows; the same numbers the dialogs decide with. */
export function billedSeatSummary(
  team: TeamCapacityFields,
  seatType: SeatType,
  pendingByType: Record<string, number>
): { inUse: number; paid: number; pending: number; free: number; over: number; unknown: boolean } {
  const { inUse, paid, pending } = seatCounts(team, seatType, pendingByType);
  const { free, over, unknown } = cachedFreeSeats(team, seatType, pendingByType);
  return { inUse, paid: paid ?? 0, pending, free, over, unknown };
}

/** What adding `needed` people to seat type T on this Team would do, per its overage policy. */
export interface SeatGate {
  seatType: SeatType;
  label: string;
  billed: boolean;
  free: number;
  needed: number;
  /** Seats ChatGPT would add (and charge) for this action. */
  extra: number;
  /**
   * One seat of this type per month in this Team (synced Team row; a yearly Team's is its annual-plan
   * price per month); null = unknown or not billed.
   */
  price: SeatPrice | null;
  capacityUnknown: boolean;
  /** The Team has no paid seat of this type at all. */
  noSeats: boolean;
  /** Seats already held beyond paid (see CachedFreeSeats.over). */
  over: number;
  policy: OveragePolicy;
  /** free: enough free seats (or not billed). Otherwise the policy decides. */
  action: 'free' | OveragePolicy;
}

/** The per-seat monthly price of a billed type from the cached Team row; missing fields = unknown. */
function cachedSeatPrice(team: TeamCapacityFields, seatType: SeatType): SeatPrice | null {
  return teamSeatPrice({
    billing_period: team.billing_period ?? null,
    billing_currency: team.billing_currency ?? '',
    billing_symbol: team.billing_symbol ?? null,
    price_per_seat: team.price_per_seat ?? null,
    premium_price_per_seat: team.premium_price_per_seat ?? null,
  }, seatType);
}

export function seatGate(
  team: TeamCapacityFields,
  seatType: SeatType,
  pendingByType: Record<string, number> = {},
  needed = 1
): SeatGate {
  const info = SEAT_TYPES[seatType];
  const policy = parseOveragePolicy(team.overage_policy);
  const want = Math.max(1, needed);
  if (!info.billed) {
    return {
      seatType, label: info.label, billed: false, free: 0, needed: want, extra: 0, price: null,
      capacityUnknown: false, noSeats: false, over: 0, policy, action: 'free',
    };
  }
  const { free, over, unknown, noSeats } = cachedFreeSeats(team, seatType, pendingByType);
  const extra = Math.max(0, want - free);
  return {
    seatType,
    label: info.label,
    billed: true,
    free,
    needed: want,
    extra,
    price: cachedSeatPrice(team, seatType),
    capacityUnknown: unknown,
    noSeats,
    over,
    policy,
    action: extra === 0 ? 'free' : policy,
  };
}

/** Gate for switching an existing member to `target`: none needed when it stays put or is unknown. */
export function seatSwitchGate(
  team: TeamCapacityFields | null | undefined,
  current: string | null | undefined,
  target: SeatType,
  pendingByType: Record<string, number> = {}
): SeatGate | null {
  if (!team || parseSeatType(current) === target) return null;
  return seatGate(team, target, pendingByType, 1);
}

/**
 * Why a seat has to be bought, in the same words the Team card uses (超出 N / 已满 / 空 N):
 * "ChatGPT 席位已超出 2 个" / "ChatGPT 席位已满" / "这个 Team 还没有 Premium 席位" / …
 */
function fullPhrase(gate: SeatGate): string {
  if (gate.over > 0) return `${gate.label} 席位已超出 ${gate.over} 个`;
  if (gate.noSeats) return `暂无 ${gate.label} 席位`;
  if (gate.capacityUnknown) return `未获取到 ${gate.label} 空位，按已满处理`;
  if (gate.free > 0) return `${gate.label} 仅剩 ${gate.free} 个空位`;
  return `${gate.label} 席位已满`;
}

/**
 * "自动加购 3 个席位并扣费，约 +฿2,340 + 税/月（每席 ฿780）"; a yearly Team adds the annual figure
 * and the yearly note (seatChargeText); unknown → 「单价未知，以 ChatGPT 账单为准」.
 */
function charge(count: number, price: SeatPrice | null): string {
  return `自动加购 ${count} 个席位并扣费，${seatChargeText(price, count)}`;
}

/**
 * One line that says what will happen to the operator's money. Null when nothing will be
 * bought. Used as the add-member dialog's hint and the seat menu's tooltip.
 */
export function gateMessage(gate: SeatGate): string | null {
  if (gate.action === 'free') return null;
  const setTo = gate.noSeats && gate.over === 0 ? '且设为' : '设为';
  if (gate.action === 'forbid') {
    return gate.free > 0 && !gate.capacityUnknown
      ? `${fullPhrase(gate)}，${setTo}「${overagePolicyLabel('forbid')}」，最多再加 ${gate.free} 人。`
      : `${fullPhrase(gate)}，${setTo}「${overagePolicyLabel('forbid')}」，禁止自动加购。`;
  }
  if (gate.action === 'confirm') return `${fullPhrase(gate)}，继续将${charge(gate.extra, gate.price)}。提交需确认。`;
  return `${fullPhrase(gate)}，${setTo}「${overagePolicyLabel('auto')}」：提交将${charge(gate.extra, gate.price)}。`;
}

/** The confirm step's text for a 超员需确认 gate: what is about to be bought and what it adds. */
export function gateConfirmText(gate: SeatGate): string {
  return `${fullPhrase(gate)}，继续将${charge(gate.extra, gate.price)}。`;
}

/** Short hint under an option in a seat-switch menu. */
export function gateShortHint(gate: SeatGate | null): string | null {
  if (!gate || gate.action === 'free') return null;
  const full = gate.over > 0
    ? `超出 ${gate.over}`
    : gate.noSeats ? '还没有席位' : gate.capacityUnknown ? '空位未知' : '已满';
  if (gate.action === 'forbid') return `${full} · 禁止超员`;
  if (gate.action === 'confirm') return `${full} · 需确认加购`;
  return `${full} · 会自动加购扣费`;
}

/**
 * Second line under that hint: what the seat it would buy adds per month, compact enough for a
 * narrow menu ("约 +฿780 + 税/月", yearly "约 +฿630 + 税/月 · 年付"). The row's tooltip and the
 * confirm dialog carry the full text (annual figure, yearly note).
 */
export function gateShortPrice(gate: SeatGate | null): string | null {
  if (!gate || (gate.action !== 'confirm' && gate.action !== 'auto')) return null;
  return gate.price ? `约 +${formatSeatAmountShort(gate.price, gate.extra)}` : seatChargeText(null, gate.extra);
}

/**
 * Confirm text after the server found the seat type full while inviting one by one: the live
 * free count is 0, so every remaining email would buy a seat. Quotes that count, not the
 * server's per-request "1 个".
 */
export function liveFullConfirmText({ label, teamName, count, invited, capacityUnknown, reconfirm, price }: {
  label: string;
  teamName?: string;
  /** Emails still to invite; each one buys a seat. */
  count: number;
  /** Already invited in this run. */
  invited: number;
  capacityUnknown: boolean;
  /** The server did not accept the confirmation sent with this email (used up or expired). */
  reconfirm?: boolean;
  /** One seat's monthly price from the server's 409 (`seat_price`); null = unknown. */
  price: SeatPrice | null;
}): string {
  const team = teamName ? `「${teamName}」` : '';
  const done = invited > 0 ? `已邀请 ${invited} 人。` : '';
  const again = reconfirm ? '确认已失效，需重新确认。' : '';
  const full = capacityUnknown
    ? `未获取到${team}的 ${label} 空位，按已满处理，`
    : `${team}${label} 席位已满，`;
  const who = invited > 0 ? `剩余 ${count} 个邮箱` : count > 1 ? `这 ${count} 个邮箱` : '';
  return `${again}${done}${full}${who}继续将${charge(count, price)}。`;
}

/** Confirm text before switching a member into a full billed type. */
export function switchConfirmText(gate: SeatGate): string {
  const lead = gate.over > 0
    ? `${gate.label} 席位已超出 ${gate.over} 个，`
    : gate.noSeats
      ? `暂无 ${gate.label} 席位，`
      : gate.capacityUnknown ? `未获取到 ${gate.label} 空位，按已满处理：` : `${gate.label} 席位已满，`;
  return `${lead}切换将${charge(1, gate.price)}。`;
}

/** "其中 22 个在「Aurora」自动加购并扣费" — the invites that made ChatGPT buy a seat. */
export function overagePurchaseText(added: ReadonlyArray<{ team_name?: string; overage?: boolean }>): string | null {
  const bought = added.filter((item) => item.overage);
  if (bought.length === 0) return null;
  const byTeam = new Map<string, number>();
  for (const item of bought) {
    const name = item.team_name || '未知 Team';
    byTeam.set(name, (byTeam.get(name) ?? 0) + 1);
  }
  if (byTeam.size === 1) return `其中 ${bought.length} 个在「${[...byTeam.keys()][0]}」自动加购并扣费`;
  const parts = [...byTeam].map(([name, count]) => `「${name}」${count} 个`).join('、');
  return `其中 ${bought.length} 个自动加购并扣费：${parts}`;
}
