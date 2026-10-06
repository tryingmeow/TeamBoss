/**
 * Overage-policy enforcement, mirroring contract §3.2 / §3.3: every place a billed seat can be added
 * (single invite, seat switch) runs `overageGate` before it changes anything.
 */
import type { OveragePolicy, SeatType } from '../types';
import type { DemoDb, DemoTeam } from './db';
import { fail, type DemoResponse } from './http';
import { appendLog } from './logs';
import { freeSeats, teamSeatUsage } from './views';

export const DEMO_SEAT_LABELS: Record<SeatType, string> = {
  default: 'ChatGPT',
  usage_based: 'Codex',
  prolite: 'Premium',
};
const BILLED: ReadonlySet<string> = new Set(['default', 'prolite']);

export function isRegistrySeat(value: string): value is SeatType {
  return value in DEMO_SEAT_LABELS;
}

/** Missing → `default`; anything outside the registry → null (the backend answers 422). */
export function parseSeat(value: unknown): SeatType | null {
  const raw = String(value ?? '').trim().toLowerCase() || 'default';
  return isRegistrySeat(raw) ? raw : null;
}

export function seatLabel(raw: string | null | undefined): string {
  const value = (raw ?? '').trim() || 'default';
  return isRegistrySeat(value) ? DEMO_SEAT_LABELS[value] : `其他（${value}）`;
}

export function policyOf(record: DemoTeam): OveragePolicy {
  const value = record.team.overage_policy;
  return value === 'forbid' || value === 'confirm' || value === 'auto' ? value : 'forbid';
}

function capacityOf(record: DemoTeam, seatType: SeatType) {
  const usage = teamSeatUsage(record);
  const available = freeSeats(record, seatType);
  if (seatType === 'default') {
    return {
      seat_type: seatType,
      available,
      capacity_unknown: false,
      seats_entitled: record.team.seats_entitled,
      seats_in_use_total: record.members.length,
      codex_count: usage.codex,
      active_chatgpt: usage.activeChatgpt,
      pending_default: usage.pendingDefault,
      reserved_default: 0,
    };
  }
  return {
    seat_type: seatType,
    available,
    capacity_unknown: false,
    paid: record.team.seat_capacity?.prolite?.paid ?? 0,
    in_use: usage.premium,
    pending: usage.pendingPremium,
  };
}

export type OverageOperation = 'invite' | 'seat_switch';

export interface GateInput {
  db: DemoDb;
  record: DemoTeam;
  seatType: SeatType;
  allowOverage: boolean;
  operation: OverageOperation;
  /** The log action of the attempted operation (`invite_member` / `change_seat`). */
  logAction: string;
  targetEmail: string;
  /** Extra detail keys written before `seat_type=` on the refusal log row, e.g. `user_id=...`. */
  logPrefix?: string;
}

/** Returns a 409 when the add must be refused or confirmed first; null when it may proceed. */
export function overageGate(input: GateInput): DemoResponse | null {
  const { db, record, seatType, allowOverage, operation } = input;
  if (!BILLED.has(seatType)) return null;
  const policy = policyOf(record);
  if (policy === 'auto' || (policy === 'confirm' && allowOverage)) return null;
  const capacity = capacityOf(record, seatType);
  if (capacity.available > 0) return null;

  const label = DEMO_SEAT_LABELS[seatType];
  const team = record.team;
  const base = { team_id: team.id, team_name: team.name, seat_type: seatType, policy, capacity };
  const refusedLog = (reason: string) =>
    appendLog(db, {
      team_id: team.id, action: input.logAction, target_email: input.targetEmail, result: 'skipped',
      detail: `${input.logPrefix ?? ''}seat_type=${seatType}, policy=${policy}, reason=${reason}`,
    });

  if (policy === 'forbid') {
    refusedLog('overage_forbidden');
    return fail(409, {
      code: 'overage_forbidden',
      message: `「${team.name}」设为禁止超员：${label} 席位已满，不会自动加购。要加人请先在 Team 设置里修改超员策略。`,
      ...base,
    });
  }
  refusedLog('overage_needs_confirmation');
  return fail(409, {
    code: 'require_overage_confirmation',
    message:
      operation === 'seat_switch'
        ? `切换到 ${label} 会让 ChatGPT 自动加购 1 个 ${label} 席位并扣费。`
        : `「${team.name}」${label} 席位已满，继续会让 ChatGPT 自动加购 1 个 ${label} 席位并扣费。`,
    operation,
    ...base,
  });
}

/** Whether an add that passed the gate lands on a full billed type, i.e. ChatGPT will add a seat. */
export function isOverage(record: DemoTeam, seatType: SeatType): boolean {
  return BILLED.has(seatType) && freeSeats(record, seatType) <= 0;
}
