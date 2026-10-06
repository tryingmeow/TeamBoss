/**
 * Seat counts and the cached "is there a free paid seat?" check the UI uses to grey out or
 * confirm an action before it can cost money. The rule is the backend's `billed_free_seats`
 * (app/services/seat_capacity.py); the server re-checks live data and has the last word.
 */
import type { OveragePolicy, SeatType, Team } from '../types';
import { SEAT_TYPES, overagePolicyLabel, parseOveragePolicy, parseSeatType } from './seatType';

type TeamSeatFields = Pick<Team, 'seats_entitled' | 'seats_in_use' | 'codex_count' | 'chatgpt_count'>;
/** Older payloads (and some pages) may lack the per-type fields; every reader treats them as unknown. */
export type TeamCapacityFields = TeamSeatFields &
  Partial<Pick<Team, 'seat_capacity' | 'seat_type_counts' | 'overage_policy'>>;

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
  /** No per-type number for a type that needs one (Premium): counted as full. */
  unknown: boolean;
}

/**
 * Free seats of a billed type from cached data:
 * per-type = seat_capacity[T].available − pending T; ChatGPT also takes the old formula
 * (seats_entitled − active ChatGPT − pending ChatGPT) and uses the smaller; Premium without a
 * per-type entry = 0. Non-billed types never need a free seat (returns 0, unknown false).
 */
export function cachedFreeSeats(
  team: TeamCapacityFields,
  seatType: SeatType,
  pendingByType: Record<string, number> = {}
): CachedFreeSeats {
  if (!SEAT_TYPES[seatType].billed) return { free: 0, unknown: false };
  const pending = Math.max(0, toNumber(pendingByType[seatType]));
  const entry = capacityEntry(team, seatType);
  const perType = entry ? Math.max(0, entry.available - pending) : null;
  const legacy = seatType === 'default'
    ? Math.max(0, toNumber(team.seats_entitled) - activeChatGptSeats(team) - pending)
    : null;
  const candidates = [perType, legacy].filter((value): value is number => value !== null);
  if (candidates.length === 0) return { free: 0, unknown: true };
  return { free: Math.min(...candidates), unknown: false };
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
  capacityUnknown: boolean;
  policy: OveragePolicy;
  /** free: enough free seats (or not billed). Otherwise the policy decides. */
  action: 'free' | OveragePolicy;
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
    return { seatType, label: info.label, billed: false, free: 0, needed: want, extra: 0, capacityUnknown: false, policy, action: 'free' };
  }
  const { free, unknown } = cachedFreeSeats(team, seatType, pendingByType);
  const extra = Math.max(0, want - free);
  return {
    seatType,
    label: info.label,
    billed: true,
    free,
    needed: want,
    extra,
    capacityUnknown: unknown,
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

function fullPhrase(gate: SeatGate): string {
  if (gate.capacityUnknown) return `读不到 ${gate.label} 空位，按已满处理`;
  if (gate.free > 0) return `${gate.label} 只剩 ${gate.free} 个空位`;
  return `${gate.label} 席位已满`;
}

/**
 * One line that says what will happen to the operator's money. Null when nothing will be
 * bought. Used for the add-member dialog and as the confirm text.
 */
export function gateMessage(gate: SeatGate): string | null {
  if (gate.action === 'free') return null;
  const charge = `ChatGPT 自动加购 ${gate.extra} 个 ${gate.label} 席位并扣费`;
  if (gate.action === 'forbid') {
    return gate.free > 0 && !gate.capacityUnknown
      ? `${fullPhrase(gate)}，这个 Team 设为「${overagePolicyLabel('forbid')}」，最多再加 ${gate.free} 人。`
      : `${fullPhrase(gate)}，这个 Team 设为「${overagePolicyLabel('forbid')}」，不会自动加购。要加人请先在 Team 设置里改超员策略。`;
  }
  if (gate.action === 'confirm') return `${fullPhrase(gate)}，继续会让 ${charge}。`;
  return `${fullPhrase(gate)}，这个 Team 设为「${overagePolicyLabel('auto')}」：提交后 ${charge}。`;
}

/** Short hint under an option in a seat-switch menu. */
export function gateShortHint(gate: SeatGate | null): string | null {
  if (!gate || gate.action === 'free') return null;
  const full = gate.capacityUnknown ? '空位未知' : '已满';
  if (gate.action === 'forbid') return `${full} · 禁止超员`;
  if (gate.action === 'confirm') return `${full} · 需确认加购`;
  return `${full} · 会自动加购扣费`;
}

/** Confirm text before switching a member into a full billed type. */
export function switchConfirmText(gate: SeatGate): string {
  const lead = gate.capacityUnknown ? `读不到 ${gate.label} 空位，按已满处理：` : `${gate.label} 席位已满，`;
  return `${lead}切换到 ${gate.label} 会让 ChatGPT 自动加购 1 个 ${gate.label} 席位并扣费。`;
}
