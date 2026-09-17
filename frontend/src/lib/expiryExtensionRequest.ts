/**
 * Keeps one idempotency key for one unconfirmed "add this duration" intent.
 *
 * A response can be lost after the server commits. Re-submitting the same
 * team/member/duration must therefore use the same key; choosing a different
 * duration deliberately starts a different intent and gets a new key.
 */
export interface ExpiryExtensionIntent {
  teamId: string;
  userId: string;
  duration: string;
}

function identity(intent: ExpiryExtensionIntent): string {
  return `${intent.teamId}\u0000${intent.userId}`;
}

function key(intent: ExpiryExtensionIntent): string {
  return `${identity(intent)}\u0000${intent.duration}`;
}

export class ExpiryExtensionRequestIds {
  private readonly pending = new Map<string, string>();
  private readonly createId: () => string;

  constructor(createId: () => string = () => crypto.randomUUID()) {
    this.createId = createId;
  }

  get(intent: ExpiryExtensionIntent): string {
    const intentKey = key(intent);
    const memberKey = identity(intent);
    for (const pendingKey of this.pending.keys()) {
      if (pendingKey.startsWith(`${memberKey}\u0000`) && pendingKey !== intentKey) {
        this.pending.delete(pendingKey);
      }
    }
    const existing = this.pending.get(intentKey);
    if (existing) return existing;
    const requestId = this.createId();
    this.pending.set(intentKey, requestId);
    return requestId;
  }

  confirm(intent: ExpiryExtensionIntent): void {
    this.pending.delete(key(intent));
  }

  discardMember(teamId: string, userId: string): void {
    const memberKey = `${teamId}\u0000${userId}\u0000`;
    for (const pendingKey of this.pending.keys()) {
      if (pendingKey.startsWith(memberKey)) this.pending.delete(pendingKey);
    }
  }
}
