/**
 * Redeem codes (admin) and the public self-service redeem/query endpoints.
 *
 * Demo conventions for the public page:
 *   - any token starting with `atm_demo` redeems (invite, or renewal if the email is already in a team);
 *   - any token starting with `atm_multi` asks the user to pick a team first;
 *   - seeded codes (`atm_demo0001…`) keep their real state: used, disabled and expired codes are refused.
 * Unlike production, an email query returns its redemption history without a proof token.
 */
import type {
  AccessTokenResponse,
  MembershipInfo,
  MembershipStatusResult,
  MembershipTeamEntry,
  PendingConfirmationItem,
  RedeemAccessTokenResult,
  RedeemTeamChoice,
  RedemptionHistoryItem,
  TokenQueryInfo,
  TokenUsageInfo,
} from '../../api/client';
import type { CodeSeatType } from '../../types';
import { findTeam, findTeamBySlug, nextId, recount, type DemoAccessToken, type DemoDb, type DemoTeam, type DemoTokenUse } from '../db';
import { bodyObject, bodyString, fail, ok, type DemoContext, type DemoResponse, type DemoRoute } from '../http';
import { appendLog } from '../logs';
import { DAY, HOUR, durationMs, isNeverDuration, isoAt } from '../time';
import { seatLabel } from '../overage';
import { expiryState, freeSeats } from '../views';

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

function normalizeDuration(raw: unknown, fallback: string): string | null {
  const value = typeof raw === 'string' && raw.trim() ? raw.trim().toLowerCase() : fallback;
  if (isNeverDuration(value)) return 'never';
  return durationMs(value) === null ? null : value.replace(/\s+/g, '');
}

function tokenStatus(db: DemoDb, token: DemoAccessToken): { status: TokenQueryInfo['token_status']; label: string } {
  const latest = latestUse(db, token.id);
  if (latest && (latest.result === 'uncertain' || latest.result === 'pending')) {
    return { status: 'pending_confirmation', label: '结果确认中' };
  }
  if (token.used_count > 0) return { status: 'used', label: '已使用' };
  if (token.disabled) return { status: 'disabled', label: '已禁用' };
  if (token.token_expires_at && Date.parse(token.token_expires_at) <= Date.now()) return { status: 'expired', label: '已过期' };
  return { status: 'unused', label: '未使用' };
}

function latestUse(db: DemoDb, tokenId: number): DemoTokenUse | undefined {
  return db.tokenUses
    .filter((use) => use.token_id === tokenId)
    .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
}

// ── Admin: redeem code list ──

function createToken(ctx: DemoContext): DemoResponse {
  const body = bodyObject(ctx);
  const grant = normalizeDuration(body.grant_expires_in, '');
  if (!grant) return fail(400, `Invalid duration: ${String(body.grant_expires_in ?? '')}. Use 3m, 12h, 7d, 30d or never`);
  const ttl = normalizeDuration(body.token_ttl, '7d');
  if (!ttl) return fail(400, `Invalid duration: ${String(body.token_ttl ?? '')}. Use 3m, 12h, 7d, 30d or never`);
  const seatType = bodyObject(ctx).seat_type ?? 'default';
  if (seatType !== 'default' && seatType !== 'prolite') return fail(422, [{ msg: "seat_type 必须是 'default' / 'prolite'" }]);
  const id = Math.max(0, ...ctx.db.tokens.map((t) => t.id)) + 1;
  const nnnn = String(id).padStart(4, '0');
  const token = `atm_demo${nnnn}-not-a-real-token-${nnnn}`;
  const now = Date.now();
  const ttlMs = ttl === 'never' ? null : durationMs(ttl);
  const note = typeof body.note === 'string' && body.note.trim() ? body.note.trim() : null;
  const record: DemoAccessToken = {
    id,
    token,
    token_prefix: token.slice(0, 12),
    seat_type: seatType,
    grant_expires_in: grant,
    token_expires_at: ttlMs === null ? null : isoAt(now + ttlMs),
    max_uses: 1,
    used_count: 0,
    note,
    disabled: false,
    created_at: isoAt(now),
    last_used_at: null,
  };
  ctx.db.tokens.unshift(record);
  const response: AccessTokenResponse & { seat_type: CodeSeatType } = {
    id,
    token,
    token_prefix: record.token_prefix,
    seat_type: seatType,
    grant_expires_in: grant,
    token_expires_at: record.token_expires_at,
    max_uses: 1,
    used_count: 0,
    note,
    created_at: record.created_at,
  };
  return ok(response);
}

function listTokens(ctx: DemoContext): DemoResponse {
  return ok(
    [...ctx.db.tokens]
      .sort((a, b) => b.created_at.localeCompare(a.created_at))
      .map(({ token: _secret, ...item }) => ({ ...item })),
  );
}

function disableToken(ctx: DemoContext): DemoResponse {
  const token = ctx.db.tokens.find((t) => t.id === Number(ctx.params.tokenId));
  if (!token) return fail(404, '兑换码不存在');
  token.disabled = true;
  return ok();
}

function pendingConfirmations(ctx: DemoContext): DemoResponse {
  const { db } = ctx;
  const items: PendingConfirmationItem[] = db.tokenUses
    .filter((use) => use.result === 'uncertain')
    .sort((a, b) => b.id - a.id)
    .map((use) => {
      const token = db.tokens.find((t) => t.id === use.token_id);
      const team = use.team_id ? findTeam(db, use.team_id) : undefined;
      return {
        id: use.id,
        email: use.email,
        action: use.action,
        team_id: use.team_id,
        team_name: team?.team.name ?? use.team_name,
        user_id: use.user_id,
        error_message: use.error_message,
        created_at: use.created_at,
        token_prefix: token?.token_prefix ?? '',
        grant_expires_in: token?.grant_expires_in ?? '30d',
        seen_in_cached_snapshot: Boolean(team?.invites.some((i) => i.email === use.email)),
        cache_updated_at: team?.cacheUpdatedAt ?? null,
      };
    });
  return ok(items);
}

function resolvePending(ctx: DemoContext): DemoResponse {
  const { db } = ctx;
  const use = db.tokenUses.find((u) => u.id === Number(ctx.params.useId));
  if (!use) return fail(404, '兑换记录不存在');
  if (use.result !== 'uncertain') return fail(409, `这笔兑换当前不是结果确认中（${use.result}），无需处理`);
  const outcome = bodyString(ctx, 'outcome');
  const note = bodyString(ctx, 'note');
  const token = db.tokens.find((t) => t.id === use.token_id);
  const team = use.team_id ? findTeam(db, use.team_id) : undefined;
  if (outcome === 'success') {
    const grantMs = token && token.grant_expires_in !== 'never' ? durationMs(token.grant_expires_in) : null;
    const expiresAt = grantMs === null ? null : isoAt(Date.now() + grantMs);
    use.result = 'success';
    use.action = 'invited';
    use.expires_at = expiresAt;
    if (team && !team.invites.some((i) => i.email === use.email) && !team.members.some((m) => m.email === use.email)) {
      team.invites.push({
        id: `invite-demo-${nextId(db)}`,
        email: use.email,
        seat_type: token?.seat_type ?? 'default',
        created_time: use.created_at,
        expires_at: expiresAt,
        source: 'self_service',
        first_seen_at: use.created_at,
      });
      recount(team);
    }
    appendLog(db, {
      team_id: use.team_id, action: 'self_service_invite_admin_confirmed', target_email: use.email,
      detail: `token_use_id=${use.id}, expires_at=${expiresAt ?? 'None'}, note=${note}`, trigger_type: 'admin',
    });
    return ok({ status: 'ok', outcome: 'success', expires_at: expiresAt });
  }
  if (outcome === 'released') {
    use.result = 'failed';
    use.action = 'redeem_admin_released';
    use.error_message = `admin_released: ${note || 'verified absent on OpenAI side'}`;
    if (token) {
      token.used_count = 0;
      token.last_used_at = null;
    }
    appendLog(db, {
      team_id: use.team_id, action: 'self_service_invite_admin_released', target_email: use.email,
      detail: `token_use_id=${use.id}, note=${note}`, trigger_type: 'admin',
    });
    return ok({ status: 'ok', outcome: 'released' });
  }
  return fail(422, [{ msg: "outcome must be 'success' or 'released'" }]);
}

// ── Public: memberships ──

interface Membership {
  record: DemoTeam;
  status: 'joined' | 'pending';
  expires_at: string | null;
  is_owner: boolean;
  expiry_state: 'dated' | 'permanent' | 'unmanaged';
  user_id: string | null;
  public_expiry_state: NonNullable<MembershipTeamEntry['expiry_state']>;
}

function publicExpiryState(row: { expires_at: string | null; source: string | null }): NonNullable<MembershipTeamEntry['expiry_state']> {
  if (row.expires_at) return 'dated';
  if (row.source === 'detected') return 'external';
  if (row.source === null) return 'unrecorded';
  return 'permanent';
}

function membershipsOf(db: DemoDb, email: string): Membership[] {
  const result: Membership[] = [];
  db.teams.forEach((record) => {
    const member = record.members.find((m) => m.email === email);
    if (member) {
      result.push({
        record,
        status: 'joined',
        expires_at: member.expires_at,
        is_owner: member.is_owner,
        expiry_state: expiryState(member),
        user_id: member.id,
        public_expiry_state: publicExpiryState(member),
      });
      return;
    }
    const invite = record.invites.find((i) => i.email === email);
    if (invite) {
      result.push({
        record,
        status: 'pending',
        expires_at: invite.expires_at,
        is_owner: false,
        expiry_state: expiryState(invite),
        user_id: null,
        public_expiry_state: publicExpiryState(invite),
      });
    }
  });
  return result;
}

/** Like the backend, the public choice for an Owner looks exactly like a member with no expiry set. */
function toChoice(m: Membership): RedeemTeamChoice {
  const expiryState = m.is_owner ? 'permanent' : m.expiry_state;
  const blocked = expiryState === 'permanent' ? 'permanent_membership' : null;
  return {
    team_id: m.record.team.id,
    team_name: m.record.team.name,
    status: m.status,
    expires_at: m.is_owner ? null : m.expires_at,
    renewable: blocked === null,
    is_owner: false,
    expiry_state: expiryState,
    blocked_reason: blocked,
  };
}

const NOT_RENEWABLE_DETAIL = '该邮箱不能使用兑换码续期。兑换码未使用，如需处理请联系管理员。';

/** Three plausible teams for an `atm_multi` demo token when the email is not in two teams already. */
function syntheticChoices(db: DemoDb): RedeemTeamChoice[] {
  const now = Date.now();
  const aurora = findTeamBySlug(db, 'aurora').team;
  const tokyo = findTeamBySlug(db, 'tokyo').team;
  const nebula = findTeamBySlug(db, 'nebula').team;
  return [
    { team_id: aurora.id, team_name: aurora.name, status: 'joined', expires_at: isoAt(now + 9 * DAY), renewable: true, is_owner: false, expiry_state: 'dated', blocked_reason: null },
    { team_id: tokyo.id, team_name: tokyo.name, status: 'pending', expires_at: isoAt(now + 2 * DAY + 5 * HOUR), renewable: true, is_owner: false, expiry_state: 'dated', blocked_reason: null },
    { team_id: nebula.id, team_name: nebula.name, status: 'joined', expires_at: null, renewable: false, is_owner: false, expiry_state: 'permanent', blocked_reason: 'permanent_membership' },
  ];
}

function recordUse(db: DemoDb, token: DemoAccessToken | undefined, use: Omit<DemoTokenUse, 'id' | 'token_id' | 'created_at'>): void {
  db.tokenUses.push({ ...use, id: nextId(db), token_id: token?.id ?? 0, created_at: isoAt(Date.now()) });
  if (token && use.result === 'success') {
    token.used_count = 1;
    token.last_used_at = isoAt(Date.now());
  }
}

function redeem(ctx: DemoContext): DemoResponse {
  const { db } = ctx;
  const email = bodyString(ctx, 'email').trim().toLowerCase();
  const tokenText = bodyString(ctx, 'token').trim();
  const teamId = bodyString(ctx, 'team_id').trim();
  if (!EMAIL_RE.test(email)) return fail(400, '邮箱格式无效');
  if (!tokenText) return fail(400, '兑换码不能为空');

  const stored = db.tokens.find((t) => t.token === tokenText);
  const lower = tokenText.toLowerCase();
  const isMulti = !stored && lower.startsWith('atm_multi');
  if (!stored && !isMulti && !lower.startsWith('atm_demo')) return fail(401, '兑换码无效');
  if (stored) {
    const state = tokenStatus(db, stored).status;
    if (state === 'used' || state === 'pending_confirmation') return fail(409, '兑换码已用完');
    if (state === 'disabled') return fail(403, '兑换码已禁用');
    if (state === 'expired') return fail(410, '兑换码已过期');
  }
  const codeSeat: CodeSeatType = stored?.seat_type ?? 'default';
  const grant = stored?.grant_expires_in ?? '30d';
  const grantMs = grant === 'never' ? null : durationMs(grant);

  const memberships = membershipsOf(db, email).filter(({ record }) => record.team.status === 'active');
  const realChoices = memberships.map(toChoice);

  // Team choice: always for `atm_multi`, and for real multi-team emails on seeded codes.
  if (!teamId && (isMulti || (stored && memberships.length > 1))) {
    const choices = realChoices.length > 1 ? realChoices : syntheticChoices(db);
    recordUse(db, stored, {
      email, team_id: null, team_name: null, user_id: null, action: 'renew_multi_team_prompt', result: 'notice',
      error_message: null, expires_at: null,
    });
    const result: RedeemAccessTokenResult = {
      status: 'team_selection_required', action: null, team_id: null, team_name: null, email, expires_at: null,
      message: '该邮箱同时在多个 Team 中，请选择要续期的 Team 后再提交。兑换码未使用。',
      choices,
    };
    return ok(result);
  }

  // Renewal: an explicit team choice, or the email's (first renewable) existing membership.
  const chosenReal = teamId ? memberships.find((m) => m.record.team.id === teamId) : memberships.find((m) => toChoice(m).renewable) ?? memberships[0];
  if (teamId && !chosenReal) {
    const synthetic = syntheticChoices(db).find((c) => c.team_id === teamId);
    if (!synthetic || realChoices.length > 1) return fail(409, '所选 Team 已不在该邮箱的成员列表中，请重新查询后再兑换。兑换码未使用。');
    if (!synthetic.renewable) return fail(409, NOT_RENEWABLE_DETAIL);
    const base = synthetic.expires_at ? Math.max(Date.parse(synthetic.expires_at), Date.now()) : Date.now();
    const expiresAt = grantMs === null ? null : isoAt(base + grantMs);
    const action = synthetic.status === 'joined' ? 'renewed_member' : 'renewed_invite';
    recordUse(db, stored, {
      email, team_id: synthetic.team_id, team_name: synthetic.team_name, user_id: null, action, result: 'success',
      error_message: null, expires_at: expiresAt,
    });
    appendLog(db, { team_id: synthetic.team_id, action: 'self_service_renew', target_email: email, detail: `action=${action}, expires_at=${expiresAt}` });
    return ok({
      status: 'ok', action, team_id: synthetic.team_id, team_name: synthetic.team_name ?? '', email, expires_at: expiresAt, message: '已续期',
    } satisfies RedeemAccessTokenResult);
  }

  if (chosenReal) {
    const { record } = chosenReal;
    if (chosenReal.is_owner || chosenReal.expiry_state === 'permanent') return fail(409, NOT_RENEWABLE_DETAIL);
    const row = chosenReal.status === 'joined'
      ? record.members.find((m) => m.email === email)
      : record.invites.find((i) => i.email === email);
    // A Premium code renews only Premium members; a ChatGPT code renews ChatGPT or Codex members, never Premium.
    const current = row?.seat_type || 'default';
    const matches = codeSeat === 'prolite' ? current === 'prolite' : current === 'default' || current === 'usage_based';
    if (!matches) {
      const message = `兑换码是 ${seatLabel(codeSeat)} 码，你当前是 ${seatLabel(current)} 席位，不能用它续期。`;
      appendLog(db, {
        team_id: record.team.id, action: 'redeem_seat_type_mismatch', target_email: email,
        detail: `seat_type=${codeSeat}, from_seat_type=${current}`, result: 'failed', error_message: message,
      });
      return fail(409, message);
    }
    const base = chosenReal.expires_at ? Math.max(Date.parse(chosenReal.expires_at), Date.now()) : Date.now();
    const expiresAt = grantMs === null ? null : isoAt(base + grantMs);
    if (row) {
      row.expires_at = expiresAt;
      row.source = 'self_service';
      row.first_seen_at = row.first_seen_at ?? row.created_time;
    }
    const action = chosenReal.status === 'joined' ? 'renewed_member' : 'renewed_invite';
    recordUse(db, stored, {
      email, team_id: record.team.id, team_name: record.team.name, user_id: chosenReal.user_id, action, result: 'success',
      error_message: null, expires_at: expiresAt,
    });
    appendLog(db, { team_id: record.team.id, action: 'self_service_renew', target_email: email, detail: `action=${action}, expires_at=${expiresAt}` });
    return ok({
      status: 'ok', action, team_id: record.team.id, team_name: record.team.name, email, expires_at: expiresAt, message: '已续期',
    } satisfies RedeemAccessTokenResult);
  }

  // New member: invite into the usable team with the most free seats of the code's type. Redemption
  // never overfills, whatever the Team's overage policy says.
  const label = seatLabel(codeSeat);
  const target = db.teams
    .filter(({ team }) => team.status === 'active' && team.auth_state === 'ok' && !team.sync_suspended_at && team.will_renew)
    .sort((a, b) => freeSeats(b, codeSeat) - freeSeats(a, codeSeat))
    .find((record) => freeSeats(record, codeSeat) > 0);
  if (!target) {
    if (codeSeat === 'prolite') {
      appendLog(db, {
        action: 'redeem_no_premium_seat', target_email: email, detail: 'seat_type=prolite', result: 'failed',
        error_message: '没有可用 Premium 席位，兑换码未使用',
      });
      return fail(409, '没有可用 Premium 席位，兑换码未使用，请联系管理员');
    }
    appendLog(db, { action: 'self_service_redeem', detail: 'reason=no_available_seat', result: 'failed', error_message: '没有可用 ChatGPT 席位，请联系管理员' });
    return fail(409, `没有可用 ${label} 席位，请联系管理员`);
  }
  const now = Date.now();
  const expiresAt = grantMs === null ? null : isoAt(now + grantMs);
  target.invites.push({
    id: `invite-demo-${nextId(db)}`,
    email,
    seat_type: codeSeat,
    created_time: isoAt(now),
    expires_at: expiresAt,
    source: 'self_service',
    first_seen_at: isoAt(now),
  });
  recount(target);
  recordUse(db, stored, {
    email, team_id: target.team.id, team_name: target.team.name, user_id: null, action: 'invited', result: 'success',
    error_message: null, expires_at: expiresAt,
  });
  appendLog(db, {
    team_id: target.team.id, action: 'self_service_invite', target_email: email,
    detail: codeSeat === 'prolite' ? `seat_type=prolite, expires_at=${expiresAt}` : `expires_at=${expiresAt}`,
  });
  return ok({
    status: 'ok', action: 'invited', team_id: target.team.id, team_name: target.team.name, email, expires_at: expiresAt, message: '已发送邀请',
  } satisfies RedeemAccessTokenResult);
}

function historyFor(db: DemoDb, email: string): RedemptionHistoryItem[] {
  return db.tokenUses
    .filter((use) => use.email === email)
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .slice(0, 20)
    .map((use) => {
      const token = db.tokens.find((t) => t.id === use.token_id);
      return {
        action: use.action,
        result: use.result,
        team_id: use.team_id,
        team_name: use.team_name,
        token_prefix: token?.token_prefix ?? null,
        grant_expires_in: token?.grant_expires_in ?? null,
        expires_at: use.expires_at,
        error_message: use.error_message,
        created_at: use.created_at,
      };
    });
}

function emailQuery(db: DemoDb, email: string): MembershipStatusResult {
  // Like the backend, an Owner's own row never shows up in the anonymous lookup.
  const memberships = membershipsOf(db, email).filter((m) => !m.is_owner);
  const history = historyFor(db, email);
  if (memberships.length === 0) {
    const absent: MembershipInfo & { cache_updated_at: null } = {
      status: 'absent', email, team_id: null, team_name: null, expires_at: null, is_owner: false,
      message: '未找到记录', memberships: [], redemption_history: history, cache_updated_at: null,
    };
    return { query_type: 'email', membership: absent };
  }
  const entries: MembershipTeamEntry[] = memberships.map((m) => ({
    status: m.status,
    team_id: m.record.team.id,
    team_name: m.record.team.name,
    expires_at: m.expires_at,
    is_owner: false,
    expiry_state: m.public_expiry_state,
    cache_updated_at: m.record.cacheUpdatedAt,
  }));
  const first = entries[0];
  const membership: MembershipInfo & { cache_updated_at: string | null } = {
    status: first.status,
    email,
    team_id: first.team_id,
    team_name: first.team_name,
    expires_at: first.expires_at,
    is_owner: first.is_owner,
    message: entries.length > 1 ? `在 ${entries.length} 个 Team 中` : first.status === 'joined' ? '已加入' : '待接受邀请',
    memberships: entries,
    redemption_history: history,
    cache_updated_at: first.cache_updated_at,
  };
  return { query_type: 'email', membership };
}

function usageFor(db: DemoDb, use: DemoTokenUse): TokenUsageInfo {
  const team = use.team_id ? findTeam(db, use.team_id) : undefined;
  const kicked = team?.kicked.find((k) => k.email === use.email);
  let status: 'joined' | 'pending' | 'expired_removed' | 'absent' | 'unknown' = 'unknown';
  if (team?.members.some((m) => m.email === use.email)) status = 'joined';
  else if (team?.invites.some((i) => i.email === use.email)) status = 'pending';
  else if (kicked) status = 'expired_removed';
  else if (team) status = 'absent';
  const labels = { joined: '已加入', pending: '待接受', expired_removed: '已过期移出', absent: '未找到', unknown: '未知' };
  const inFlight = use.result === 'uncertain' || use.result === 'pending';
  return {
    email: use.email,
    email_status: status,
    email_status_label: labels[status],
    team_id: use.team_id,
    team_name: use.team_name,
    user_id: use.user_id,
    action: use.action,
    result: use.result,
    error_message: inFlight ? '结果确认中，兑换码已暂时锁定，请稍后查询' : use.error_message,
    expires_at: use.expires_at,
    kicked_at: status === 'expired_removed' ? kicked?.kicked_at ?? null : null,
    used_at: use.created_at,
  };
}

function tokenQuery(db: DemoDb, query: string): MembershipStatusResult {
  const stored = db.tokens.find((t) => t.token === query) ?? db.tokens.find((t) => t.token_prefix === query);
  if (stored) {
    const state = tokenStatus(db, stored);
    const latest = latestUse(db, stored.id);
    const token: TokenQueryInfo & { seat_type: CodeSeatType; seat_type_label: string } = {
      id: stored.id,
      token_prefix: stored.token_prefix,
      seat_type: stored.seat_type,
      seat_type_label: seatLabel(stored.seat_type),
      token_status: state.status,
      token_status_label: state.label,
      grant_expires_in: stored.grant_expires_in,
      token_expires_at: stored.token_expires_at,
      max_uses: 1,
      used_count: stored.used_count,
      created_at: stored.created_at,
      last_used_at: stored.last_used_at,
    };
    return { query_type: 'token', token, usage: latest && latest.team_id ? usageFor(db, latest) : null };
  }
  const lower = query.toLowerCase();
  if (lower.startsWith('atm_demo') || lower.startsWith('atm_multi')) {
    const now = Date.now();
    return {
      query_type: 'token',
      token: {
        token_prefix: query.slice(0, 12),
        token_status: 'unused',
        token_status_label: '未使用',
        grant_expires_in: '30d',
        token_expires_at: isoAt(now + 7 * DAY),
        max_uses: 1,
        used_count: 0,
        created_at: isoAt(now - HOUR),
        last_used_at: null,
      },
      usage: null,
    };
  }
  return { query_type: 'token', token: { token_status: 'invalid', token_status_label: '无效' }, usage: null };
}

function query(ctx: DemoContext): DemoResponse {
  const raw = bodyString(ctx, 'query').trim();
  if (!raw) return fail(400, '查询内容不能为空');
  if (raw.startsWith('atm_') || ctx.db.tokens.some((t) => t.token === raw)) return ok(tokenQuery(ctx.db, raw));
  const email = raw.toLowerCase();
  if (!EMAIL_RE.test(email)) return fail(400, '邮箱格式无效');
  return ok(emailQuery(ctx.db, email));
}

export const accessTokenRoutes: DemoRoute[] = [
  { method: 'GET', pattern: '/api/access-tokens', handler: listTokens },
  { method: 'POST', pattern: '/api/access-tokens', handler: createToken },
  { method: 'GET', pattern: '/api/access-tokens/pending-confirmations', handler: pendingConfirmations },
  { method: 'POST', pattern: '/api/access-tokens/pending-confirmations/:useId/resolve', handler: resolvePending },
  { method: 'DELETE', pattern: '/api/access-tokens/:tokenId', handler: disableToken },
  { method: 'POST', pattern: '/api/self-service/redeem', handler: redeem },
  { method: 'POST', pattern: '/api/self-service/query', handler: query },
];
