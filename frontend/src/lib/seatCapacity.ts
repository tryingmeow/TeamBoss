import type { Team } from '../types';

type TeamSeatFields = Pick<Team, 'seats_entitled' | 'seats_in_use' | 'codex_count' | 'chatgpt_count'>;

function toNumber(value: number | null | undefined): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

export function activeChatGptSeats(
  team: Pick<TeamSeatFields, 'seats_in_use' | 'codex_count' | 'chatgpt_count'>
): number {
  if (typeof team.chatgpt_count === 'number' && Number.isFinite(team.chatgpt_count)) {
    return Math.max(0, team.chatgpt_count);
  }
  return Math.max(0, toNumber(team.seats_in_use) - toNumber(team.codex_count));
}

export function availableChatGptSeats(team: TeamSeatFields, pendingDefault = 0): number {
  return Math.max(
    0,
    toNumber(team.seats_entitled) - activeChatGptSeats(team) - toNumber(pendingDefault)
  );
}
