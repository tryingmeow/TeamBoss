/**
 * Overage-policy enforcement, mirroring contract §3.2 / §3.3: every place a billed seat can be added
 * (single invite, seat switch) runs `overageGate` before it changes anything.
 */
import { seatChargeText, teamSeatPrice } from '../lib/seatPrice';
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

/** `overage_confirmation` of an invite / seat-switch request (see the backend's models.py). */
export interface DemoConfirmation {
  confirmation_id: string;
  seat_type: string;
  seat_limit: number;
}

type ConfirmationStatus = 'missing' | 'used_up' | 'expired' | 'mismatch';

const CONFIRMATION_ID_RE = /^[A-Za-z0-9_-]{16,64}$/;
const CONFIRMATION_TTL_MS = 60 * 60 * 1000;

/** The backend's overage_confirmations table: registered on first use, limit fixed from then on. */
const ledger = new Map<string, { teamId: string; seatType: string; limit: number; used: number; expiresAt: number }>();

/**
 * Missing / null → null; a well-formed object → it; anything else → 'invalid' (the backend answers 422).
 * A bare `allow_overage` is not read here at all: on 超员需确认 it is not a confirmation.
 */
export function parseConfirmation(raw: unknown): DemoConfirmation | null | 'invalid' {
  if (raw === undefined || raw === null) return null;
  if (typeof raw !== 'object') return 'invalid';
  const value = raw as Record<string, unknown>;
  const id = value.confirmation_id;
  const seat = value.seat_type;
  const limit = value.seat_limit;
  if (typeof id !== 'string' || !CONFIRMATION_ID_RE.test(id)) return 'invalid';
  if (typeof seat !== 'string' || !isRegistrySeat(seat)) return 'invalid';
  if (typeof limit !== 'number' || !Number.isInteger(limit) || limit < 1 || limit > 100) return 'invalid';
  return { confirmation_id: id, seat_type: seat, seat_limit: limit };
}

/** Takes one seat from the confirmation, or says why it cannot. */
function consumeConfirmation(
  confirmation: DemoConfirmation | null,
  teamId: string,
  seatType: SeatType,
): { used: number; limit: number } | { status: ConfirmationStatus } {
  if (!confirmation) return { status: 'missing' };
  if (confirmation.seat_type !== seatType) return { status: 'mismatch' };
  const now = Date.now();
  let entry = ledger.get(confirmation.confirmation_id);
  if (!entry) {
    entry = {
      teamId, seatType: confirmation.seat_type, limit: confirmation.seat_limit, used: 0,
      expiresAt: now + CONFIRMATION_TTL_MS,
    };
    ledger.set(confirmation.confirmation_id, entry);
  }
  if (entry.teamId !== teamId || entry.seatType !== seatType) {
    return { status: 'mismatch' };
  }
  if (entry.expiresAt <= now) return { status: 'expired' };
  if (entry.used >= entry.limit) return { status: 'used_up' };
  entry.used += 1;
  return { used: entry.used, limit: entry.limit };
}

/** Same sentences as the backend's overage_policy._CONFIRMATION_RETRY_NOTES. */
function reconfirmNote(status: ConfirmationStatus): string {
  if (status === 'used_up') return '你确认过的加购个数已经用完，需要重新确认。';
  if (status === 'expired') return '上次的确认已过期，需要重新确认。';
  if (status === 'mismatch') return '这次确认对不上这个 Team 或席位类型，需要重新确认。';
  return '';
}

export interface GateInput {
  db: DemoDb;
  record: DemoTeam;
  seatType: SeatType;
  /** The request's parsed `overage_confirmation`. */
  confirmation: DemoConfirmation | null;
  operation: OverageOperation;
  /** The log action of the attempted operation (`invite_member` / `change_seat`). */
  logAction: string;
  targetEmail: string;
  /** Extra detail keys written before `seat_type=` on the refusal log row, e.g. `user_id=...`. */
  logPrefix?: string;
}

export type GateOutcome =
  | { refused: DemoResponse }
  /** May proceed; `confirmed` = the confirmation seat it used (used / limit), null when none was needed. */
  | { refused: null; confirmed: { used: number; limit: number } | null };

/**
 * 超员需确认: a free seat → proceed without touching the confirmation; full → one seat of the
 * confirmation, or a 409 asking again. 禁止超员 refuses when full; 超员自动 always proceeds.
 */
export function overageGate(input: GateInput): GateOutcome {
  const { db, record, seatType, confirmation, operation } = input;
  const pass = { refused: null, confirmed: null } as const;
  if (!BILLED.has(seatType)) return pass;
  const policy = policyOf(record);
  if (policy === 'auto') return pass;
  const capacity = capacityOf(record, seatType);
  if (capacity.available > 0) return pass;

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
    return {
      refused: fail(409, {
        code: 'overage_forbidden',
        message: `「${team.name}」设为禁止超员：${label} 席位已满，不会自动加购。要加人请先在 Team 设置里修改超员策略。`,
        ...base,
      }),
    };
  }
  const use = consumeConfirmation(confirmation, team.id, seatType);
  if ('used' in use) return { refused: null, confirmed: use };
  refusedLog('overage_needs_confirmation');
  // Like the server: one seat of this type per month in this Team, and the message ends with it.
  const seatPrice = teamSeatPrice(team, seatType);
  const money = seatChargeText(seatPrice, 1);
  return {
    refused: fail(409, {
      code: 'require_overage_confirmation',
      message:
        reconfirmNote(use.status) +
        (operation === 'seat_switch'
          ? `切换到 ${label} 会让 ChatGPT 自动加购 1 个 ${label} 席位并扣费，${money}。`
          : `「${team.name}」${label} 席位已满，继续会让 ChatGPT 自动加购 1 个 ${label} 席位并扣费，${money}。`),
      operation,
      confirmation_status: use.status,
      seat_price: seatPrice,
      ...base,
    }),
  };
}

/** Whether an add that passed the gate lands on a full billed type, i.e. ChatGPT will add a seat. */
export function isOverage(record: DemoTeam, seatType: SeatType): boolean {
  return BILLED.has(seatType) && freeSeats(record, seatType) <= 0;
}
