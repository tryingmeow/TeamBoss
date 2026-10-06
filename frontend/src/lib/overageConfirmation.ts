/**
 * The admin's confirmation that ChatGPT may add and charge seats, sent with a single invite or a
 * seat switch on a 超员需确认 Team. One fresh id per confirmation; the server counts its uses and
 * never buys more than `seat_limit` seats with it.
 */
import type { OverageConfirmation, SeatType } from '../types';

/** The server accepts 1..100 seats per confirmation. */
export const MAX_CONFIRMED_SEATS = 100;

function randomId(): string {
  // crypto.randomUUID 只在 HTTPS / localhost 下有，面板可能跑在纯 HTTP 上；getRandomValues 都有。
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

export function newOverageConfirmation(seatType: SeatType, seatLimit: number): OverageConfirmation {
  const limit = Math.min(MAX_CONFIRMED_SEATS, Math.max(1, Math.floor(seatLimit) || 1));
  return { confirmation_id: randomId(), seat_type: seatType, seat_limit: limit };
}
