/** Teams, members, batch GPT invites and session import/export. */
import type { SeatType, Team } from '../../types';
import type { InviteGptMembersResult, TeamBatchResult } from '../../api/client';
import { findTeam, nextId, recount, type DemoDb, type DemoInvite, type DemoTeam } from '../db';
import { bodyObject, bodyString, fail, ok, queryFlag, type DemoContext, type DemoResponse, type DemoRoute } from '../http';
import { appendLog } from '../logs';
import { DAY, durationMs, isNeverDuration, isoAt } from '../time';
import { availableGptSeats, expiryState, membersData, teamView, workspaceSettings } from '../views';

const AUTH_REJECTED = { code: 'team_auth_rejected', message: '登录已失效，请重新导入' };
const EMAIL_ALREADY_IN_TEAM = '邮箱已在该 Team 中，未重复邀请';
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

function teamOr404(ctx: DemoContext): DemoTeam | DemoResponse {
  return findTeam(ctx.db, ctx.params.teamId) ?? fail(404, 'Team not found');
}

function isResponse(value: DemoTeam | DemoResponse): value is DemoResponse {
  return 'status' in value && typeof value.status === 'number';
}

/** Upstream calls on an unusable team fail the way the backend reports them. */
function upstreamBlocked(record: DemoTeam): DemoResponse | null {
  if (record.team.auth_state === 'rejected') return fail(409, AUTH_REJECTED);
  if (record.team.status === 'token_expired') return fail(502, 'Session 已失效（HTTP 401），请重新导入');
  return null;
}

function normalizeSeat(value: unknown): SeatType {
  const raw = String(value ?? '').trim().toLowerCase();
  return raw === 'usage_based' || raw === 'codex' ? 'usage_based' : 'default';
}

/** `expires_in` → absolute ISO (null for never). Throws a 400-style message on bad input. */
function expiryFromDuration(value: unknown, base = Date.now()): string | null {
  const raw = typeof value === 'string' && value.trim() ? value.trim() : 'never';
  if (isNeverDuration(raw)) return null;
  const ms = durationMs(raw);
  if (ms === null) throw new Error(`Invalid duration: ${raw}. Use 3m, 12h, 7d, 30d or never`);
  return isoAt(base + ms);
}

function touch(record: DemoTeam): void {
  recount(record);
  record.cacheUpdatedAt = isoAt(Date.now());
}

// ── Team list & lifecycle ──

function nextTeamNumber(db: DemoDb): number {
  const used = db.teams
    .map(({ team }) => /^de0000(\d{2})-/.exec(team.id))
    .map((match) => (match ? Number(match[1]) : 0));
  return Math.max(0, ...used) + 1;
}

function newTeamFromSession(db: DemoDb, session: Record<string, unknown>): DemoTeam {
  const now = Date.now();
  const n = nextTeamNumber(db);
  const nn = String(n).padStart(2, '0');
  const user = (session.user ?? {}) as Record<string, unknown>;
  const account = (session.account ?? {}) as Record<string, unknown>;
  const suppliedEmail = typeof user.email === 'string' ? user.email.trim().toLowerCase() : '';
  // Demo mode never shows a pasted real address; only example.com survives.
  const ownerEmail = suppliedEmail.endsWith('@example.com') ? suppliedEmail : `owner-new${nn}@example.com`;
  const name = typeof account.name === 'string' && account.name.trim() ? account.name.trim() : `New Workspace ${nn}`;
  const activeUntil = now + 30 * DAY;
  const team: Team = {
    id: `de0000${nn}-0000-4000-8000-0000000000${nn}`,
    name,
    remark: null,
    owner_email: ownerEmail,
    status: 'active',
    seats_in_use: 0,
    seats_entitled: 5,
    codex_count: 0,
    chatgpt_count: 0,
    is_codex_enabled: false,
    default_seat_type: 'default',
    billing_currency: 'USD',
    billing_symbol: '$',
    billing_period: 'monthly',
    price_per_seat: 30,
    discount_amount: 0,
    discount_duration_num_periods: null,
    discount_expires_at: null,
    discount_quantity_off: null,
    promo_campaign_id: null,
    monthly_subtotal: 150,
    monthly_total: 150,
    balance: '0',
    active_start: isoAt(now),
    active_until: isoAt(activeUntil),
    will_renew: true,
    subscription_status: 'renewing',
    card_last4: '4242',
    card_brand: 'visa',
    days_remaining: 30,
    proxy_id: null,
    last_full_sync_at: isoAt(now),
    last_sync_partial_failures: [],
    auth_state: 'ok',
    auth_state_since: null,
    sync_failing_since: null,
    sync_suspended_at: null,
    cached_member_emails: [],
  };
  const record: DemoTeam = {
    team,
    members: [
      {
        id: `user-new${nn}-00`,
        email: ownerEmail,
        name: `${name} Admin`,
        role: 'account-owner',
        seat_type: 'default',
        is_owner: true,
        created_time: isoAt(now),
        expires_at: null,
        source: null,
        first_seen_at: null,
      },
    ],
    invites: [],
    kicked: [],
    invoices: [],
    cacheUpdatedAt: isoAt(now),
  };
  recount(record);
  return record;
}

/** Re-importing a session clears every failure state on the team. */
function markHealthy(record: DemoTeam): void {
  const now = isoAt(Date.now());
  Object.assign(record.team, {
    status: 'active',
    auth_state: 'ok',
    auth_state_since: null,
    sync_failing_since: null,
    sync_suspended_at: null,
    last_full_sync_at: now,
    last_sync_partial_failures: [],
  } satisfies Partial<Team>);
  record.cacheUpdatedAt = now;
}

function importTeam(ctx: DemoContext): DemoResponse {
  const session = bodyObject(ctx);
  const account = (session.account ?? {}) as Record<string, unknown>;
  const existing = typeof account.id === 'string' ? findTeam(ctx.db, account.id) : undefined;
  const record = existing ?? newTeamFromSession(ctx.db, session);
  if (existing) markHealthy(existing);
  else ctx.db.teams.push(record);
  const proxyId = Number(ctx.query.get('proxy_id'));
  if (Number.isInteger(proxyId) && proxyId > 0) record.team.proxy_id = proxyId;
  appendLog(ctx.db, {
    team_id: record.team.id, action: existing ? 'reimport_team' : 'add_team', target_email: record.team.owner_email,
    detail: 'Team added/updated',
  });
  return ok({ status: 'ok', team_id: record.team.id, name: record.team.name });
}

function reimportTeam(ctx: DemoContext): DemoResponse {
  const record = teamOr404(ctx);
  if (isResponse(record)) return record;
  markHealthy(record);
  const proxyId = Number(ctx.query.get('proxy_id'));
  if (Number.isInteger(proxyId) && proxyId > 0) record.team.proxy_id = proxyId;
  appendLog(ctx.db, {
    team_id: record.team.id, action: 'reimport_team', target_email: record.team.owner_email, detail: 'Team added/updated',
  });
  return ok({ status: 'ok', team_id: record.team.id, name: record.team.name });
}

function syncTeam(ctx: DemoContext): DemoResponse {
  const record = teamOr404(ctx);
  if (isResponse(record)) return record;
  const blocked = upstreamBlocked(record);
  if (blocked) return blocked;
  const force = queryFlag(ctx, 'force');
  if (record.team.sync_suspended_at && force) {
    return fail(502, 'overview sub-interface failures: subscription, balance (HTTP 502 Bad Gateway)');
  }
  // Suspended teams serve their last snapshot; everyone else refreshes.
  const refreshed = force && !record.team.sync_suspended_at;
  if (refreshed) {
    record.cacheUpdatedAt = isoAt(Date.now());
    record.team.last_full_sync_at = record.cacheUpdatedAt;
    appendLog(ctx.db, { team_id: record.team.id, action: 'sync_team', detail: 'force=true' });
  }
  return ok({
    team: teamView(record, true),
    members: membersData(ctx.db, record, !refreshed),
    workspace_settings: workspaceSettings(record, !refreshed),
    cached: !refreshed,
    refreshed,
    reason: refreshed ? 'force' : 'ttl_fresh',
  });
}

function syncAll(ctx: DemoContext): DemoResponse {
  const results: TeamBatchResult['results'] = ctx.db.teams
    .filter(({ team }) => team.status === 'active')
    .map(({ team }) => {
      if (team.auth_state === 'rejected') return { team_id: team.id, status: 'failed', error: AUTH_REJECTED.message };
      if (team.sync_suspended_at) return { team_id: team.id, status: 'failed', error: 'HTTP 502: upstream unavailable' };
      return { team_id: team.id, status: 'ok' };
    });
  const failed = results.filter((r) => r.status === 'failed');
  appendLog(ctx.db, {
    action: 'sync_all', detail: `Synced ${results.length - failed.length}/${results.length} teams; failed=${failed.length}`,
    result: failed.length ? 'failed' : 'success', error_message: failed.map((r) => r.team_id).join('; ') || null,
  });
  return ok({ status: failed.length ? 'partial' : 'ok', results, succeeded: results.length - failed.length, failed: failed.length });
}

function refreshAll(ctx: DemoContext): DemoResponse {
  const results = ctx.db.teams.map(({ team }) =>
    team.status === 'token_expired'
      ? { team_id: team.id, status: 'failed' as const, error: 'Token refresh failed: session expired' }
      : { team_id: team.id, status: 'ok' as const, refresh_result: 'refreshed', access_changed: true, session_changed: true },
  );
  const failed = results.filter((r) => r.status === 'failed').length;
  return ok({ status: failed ? 'partial' : 'ok', results, succeeded: results.length - failed, failed });
}

function refreshTeamToken(ctx: DemoContext): DemoResponse {
  const record = teamOr404(ctx);
  if (isResponse(record)) return record;
  if (record.team.auth_state === 'rejected') return fail(409, AUTH_REJECTED);
  if (record.team.status === 'token_expired') return fail(502, 'Token refresh failed: session expired');
  return ok({ status: 'refreshed', access_changed: true, session_changed: true, token_expires: isoAt(Date.now() + 10 * DAY) });
}

function deleteTeam(ctx: DemoContext): DemoResponse {
  const record = teamOr404(ctx);
  if (isResponse(record)) return record;
  ctx.db.teams = ctx.db.teams.filter((t) => t !== record);
  ctx.db.patrol.exempt_team_ids = ctx.db.patrol.exempt_team_ids.filter((id) => id !== record.team.id);
  appendLog(ctx.db, {
    team_id: record.team.id, action: 'delete_team',
    detail: `Team removed from management; retained ${record.members.filter((m) => m.expires_at).length} member_expiry records for audit trail`,
  });
  return ok();
}

function updateRemark(ctx: DemoContext): DemoResponse {
  const record = teamOr404(ctx);
  if (isResponse(record)) return record;
  const remark = bodyString(ctx, 'remark').trim();
  if (remark.length > 80) return fail(400, 'Remark exceeds 80 characters');
  record.team.remark = remark || null;
  appendLog(ctx.db, { team_id: record.team.id, action: 'update_team_remark', detail: `remark_len=${remark.length}` });
  // The backend's single-team serializer leaves cached_member_emails empty.
  return ok({ ...teamView(record), cached_member_emails: [] });
}

function updateTeamProxy(ctx: DemoContext): DemoResponse {
  const record = teamOr404(ctx);
  if (isResponse(record)) return record;
  const raw = bodyObject(ctx).proxy_id;
  const proxyId = typeof raw === 'number' ? raw : null;
  if (proxyId !== null && !ctx.db.proxies.some((p) => p.id === proxyId)) return fail(400, 'Proxy not found');
  record.team.proxy_id = proxyId;
  appendLog(ctx.db, { team_id: record.team.id, action: 'update_team_proxy', detail: `proxy_id=${proxyId === null ? 'None' : proxyId}` });
  return ok({ status: 'ok', proxy_id: proxyId });
}

function setDefaultSeat(ctx: DemoContext): DemoResponse {
  const record = teamOr404(ctx);
  if (isResponse(record)) return record;
  const blocked = upstreamBlocked(record);
  if (blocked) return blocked;
  const seat = normalizeSeat(bodyObject(ctx).seat_type);
  record.team.default_seat_type = seat;
  record.cacheUpdatedAt = isoAt(Date.now());
  appendLog(ctx.db, { team_id: record.team.id, action: 'change_default_seat_type', detail: `seat_type=${seat}` });
  return ok(workspaceSettings(record, false));
}

// ── Members ──

function inviteMember(ctx: DemoContext): DemoResponse {
  const record = teamOr404(ctx);
  if (isResponse(record)) return record;
  const blocked = upstreamBlocked(record);
  if (blocked) return blocked;
  const body = bodyObject(ctx);
  const email = bodyString(ctx, 'email').trim().toLowerCase();
  if (!EMAIL_RE.test(email)) return fail(422, [{ msg: '邮箱格式无效' }]);
  const seat = normalizeSeat(body.seat_type);
  let expiresAt: string | null;
  try {
    expiresAt = expiryFromDuration(body.expires_in);
  } catch (error) {
    return fail(400, (error as Error).message);
  }
  if (record.members.some((m) => m.email === email) || record.invites.some((i) => i.email === email)) {
    return fail(409, EMAIL_ALREADY_IN_TEAM);
  }
  if (seat === 'default' && body.allow_overage !== true && availableGptSeats(record) <= 0) {
    const activeChatgpt = record.members.filter((m) => m.seat_type === 'default').length;
    const pendingDefault = record.invites.filter((i) => i.seat_type === 'default').length;
    const codex = record.members.length - activeChatgpt;
    return fail(409, {
      code: 'require_overage_confirmation',
      message:
        `ChatGPT 席位不足，需要确认超额添加: active_chatgpt=${activeChatgpt}/${record.team.seats_entitled}, ` +
        `total_in_use=${record.members.length}, codex=${codex}, pending_default=${pendingDefault}, reserved_default=0`,
      capacity: {
        seats_entitled: record.team.seats_entitled,
        seats_in_use_total: record.members.length,
        codex_count: codex,
        active_chatgpt: activeChatgpt,
        pending_default: pendingDefault,
        reserved_default: 0,
        available: 0,
      },
    });
  }
  const now = Date.now();
  record.invites.push({
    id: `invite-demo-${nextId(ctx.db)}`,
    email,
    seat_type: seat,
    created_time: isoAt(now),
    expires_at: expiresAt,
    source: 'system',
    first_seen_at: isoAt(now),
  });
  touch(record);
  appendLog(ctx.db, {
    team_id: record.team.id, action: 'invite_member', target_email: email,
    detail: `seat_type=${seat}, expires_in=${String(body.expires_in ?? 'never')}, allow_overage=${body.allow_overage === true ? 'True' : 'False'}`,
  });
  return ok({ status: 'ok', result: { _mutation_status: 'confirmed' } });
}

function memberOr404(ctx: DemoContext, record: DemoTeam) {
  return record.members.find((m) => m.id === ctx.params.userId);
}

function removeMember(ctx: DemoContext): DemoResponse {
  const record = teamOr404(ctx);
  if (isResponse(record)) return record;
  const blocked = upstreamBlocked(record);
  if (blocked) return blocked;
  const member = memberOr404(ctx, record);
  if (!member) return fail(404, '该成员已不在此 Team，请刷新后重试');
  if (member.is_owner) return fail(400, 'Owner 不能被移除');
  const now = Date.now();
  record.members = record.members.filter((m) => m !== member);
  record.kicked.push({
    expiry_id: nextId(ctx.db),
    user_id: member.id,
    email: member.email,
    expires_at: member.expires_at,
    kicked_at: isoAt(now),
    kick_source: 'admin',
    first_seen_at: member.first_seen_at ?? member.created_time,
    source: member.source ?? 'system',
    created_at: member.created_time,
  });
  touch(record);
  appendLog(ctx.db, { team_id: record.team.id, action: 'remove_member', target_email: member.email, detail: `user_id=${member.id}` });
  return ok();
}

function changeSeat(ctx: DemoContext): DemoResponse {
  const record = teamOr404(ctx);
  if (isResponse(record)) return record;
  const blocked = upstreamBlocked(record);
  if (blocked) return blocked;
  const member = memberOr404(ctx, record);
  if (!member) return fail(404, '该成员已不在此 Team，请刷新后重试');
  const seat = normalizeSeat(bodyObject(ctx).seat_type);
  if (seat === 'usage_based' && !record.team.is_codex_enabled) {
    appendLog(ctx.db, {
      team_id: record.team.id, action: 'change_seat', target_email: member.email,
      detail: `user_id=${member.id}, seat_type=${seat}`, result: 'failed', error_message: 'HTTP 403: Forbidden',
    });
    return fail(502, 'HTTP 403: Forbidden (usage_based seats are not enabled for this workspace)');
  }
  member.seat_type = seat;
  touch(record);
  appendLog(ctx.db, {
    team_id: record.team.id, action: 'change_seat', target_email: member.email, detail: `user_id=${member.id}, seat_type=${seat}`,
  });
  return ok({ status: 'ok', result: { seat_type: seat } });
}

function revokeInvite(ctx: DemoContext): DemoResponse {
  const record = teamOr404(ctx);
  if (isResponse(record)) return record;
  const blocked = upstreamBlocked(record);
  if (blocked) return blocked;
  const email = ctx.params.email.trim().toLowerCase();
  const invite = record.invites.find((i) => i.email === email);
  if (!invite) return fail(502, '邀请不存在或已被接受');
  record.invites = record.invites.filter((i) => i !== invite);
  touch(record);
  appendLog(ctx.db, { team_id: record.team.id, action: 'revoke_invite', target_email: email });
  return ok();
}

function setExpiry(ctx: DemoContext): DemoResponse {
  const record = teamOr404(ctx);
  if (isResponse(record)) return record;
  const member = memberOr404(ctx, record);
  if (!member) return fail(404, '该成员已不在此 Team，请刷新后重试');
  const body = bodyObject(ctx);
  let expiresAt: string | null;
  let detail: string;
  if (typeof body.expires_at === 'string' && body.expires_at) {
    const ms = Date.parse(body.expires_at);
    if (Number.isNaN(ms)) return fail(400, 'expires_at 格式无效，请使用 ISO 时间');
    expiresAt = isoAt(ms);
    detail = `expires_at=${body.expires_at}`;
  } else if (typeof body.expires_in === 'string' && body.expires_in) {
    try {
      expiresAt = expiryFromDuration(body.expires_in);
    } catch (error) {
      return fail(400, (error as Error).message);
    }
    detail = `expires_in=${body.expires_in}`;
  } else {
    return fail(400, 'expires_in 或 expires_at 必填');
  }
  member.expires_at = expiresAt;
  member.source = member.source && member.source !== 'detected' ? member.source : 'admin';
  member.first_seen_at = member.first_seen_at ?? member.created_time;
  record.cacheUpdatedAt = isoAt(Date.now());
  appendLog(ctx.db, { team_id: record.team.id, action: 'set_expiry', target_email: member.email, detail: `user_id=${member.id}, ${detail}` });
  return ok({ status: 'ok', expires_at: expiresAt });
}

function extendExpiry(ctx: DemoContext): DemoResponse {
  const record = teamOr404(ctx);
  if (isResponse(record)) return record;
  const member = memberOr404(ctx, record);
  if (!member) return fail(404, '该成员已不在此 Team，请刷新后重试');
  const body = bodyObject(ctx);
  const duration = String(body.expires_in ?? '').trim();
  const detail = `user_id=${member.id}, expires_in=${duration}, request_id=${String(body.request_id ?? '')}`;
  if (isNeverDuration(duration)) {
    member.expires_at = null;
    member.source = 'admin';
  } else {
    const ms = durationMs(duration);
    if (ms === null) return fail(400, `Invalid duration: ${duration}. Use 3m, 12h, 7d, 30d or never`);
    if (expiryState(member) === 'permanent') {
      appendLog(ctx.db, {
        team_id: record.team.id, action: 'extend_expiry', target_email: member.email, detail,
        result: 'failed', error_message: '永久成员不能增加有限时长',
      });
      return fail(409, '永久成员不能增加有限时长');
    }
    const current = member.expires_at ? Date.parse(member.expires_at) : 0;
    member.expires_at = isoAt(Math.max(current, Date.now()) + ms);
    member.source = member.source && member.source !== 'detected' ? member.source : 'admin';
    member.first_seen_at = member.first_seen_at ?? member.created_time;
  }
  record.cacheUpdatedAt = isoAt(Date.now());
  appendLog(ctx.db, { team_id: record.team.id, action: 'extend_expiry', target_email: member.email, detail });
  return ok({ status: 'ok', expires_at: member.expires_at });
}

function removeExpiry(ctx: DemoContext): DemoResponse {
  const record = teamOr404(ctx);
  if (isResponse(record)) return record;
  const member = memberOr404(ctx, record);
  if (!member) return fail(404, '该成员已不在此 Team，请刷新后重试');
  member.expires_at = null;
  member.source = 'admin';
  record.cacheUpdatedAt = isoAt(Date.now());
  appendLog(ctx.db, { team_id: record.team.id, action: 'remove_expiry', target_email: member.email, detail: `user_id=${member.id}` });
  return ok();
}

// ── Batch GPT invites (toolbar "添加 GPT 成员") ──

function inviteGptMembers(ctx: DemoContext): DemoResponse {
  const body = bodyObject(ctx);
  const raw = [
    ...(Array.isArray(body.emails) ? body.emails : []),
    ...(typeof body.email === 'string' ? [body.email] : []),
  ];
  const emails = Array.from(new Set(raw.map((e) => String(e).trim().toLowerCase()).filter(Boolean)));
  if (emails.length === 0) return fail(400, '邮箱不能为空');
  let expiresAt: string | null;
  try {
    expiresAt = expiryFromDuration(body.expires_in);
  } catch (error) {
    return fail(400, (error as Error).message);
  }

  const candidates = ctx.db.teams.filter(({ team }) => team.status === 'active' && team.auth_state === 'ok' && !team.sync_suspended_at);
  const added: InviteGptMembersResult['added'] = [];
  const failed: InviteGptMembersResult['failed'] = [];
  const remaining: string[] = [];

  emails.forEach((email) => {
    if (!EMAIL_RE.test(email)) {
      failed.push({ email, error: '邮箱格式无效' });
      return;
    }
    const existing = ctx.db.teams.find((t) => t.members.some((m) => m.email === email) || t.invites.some((i) => i.email === email));
    if (existing) {
      failed.push({ email, error: EMAIL_ALREADY_IN_TEAM });
      return;
    }
    const ranked = [...candidates].sort((a, b) => availableGptSeats(b) - availableGptSeats(a));
    let target = ranked.find((t) => availableGptSeats(t) > 0);
    let overage = false;
    if (!target && body.allow_overage === true) {
      target = ranked[0];
      overage = true;
    }
    if (!target) {
      remaining.push(email);
      return;
    }
    const invite: DemoInvite = {
      id: `invite-demo-${nextId(ctx.db)}`,
      email,
      seat_type: 'default',
      created_time: isoAt(Date.now()),
      expires_at: expiresAt,
      source: 'system',
      first_seen_at: isoAt(Date.now()),
    };
    target.invites.push(invite);
    touch(target);
    appendLog(ctx.db, {
      team_id: target.team.id, action: 'invite_gpt_member', target_email: email, detail: `expires_at=${expiresAt ?? 'None'}`,
    });
    added.push({ email, team_id: target.team.id, team_name: target.team.name, expires_at: expiresAt, overage });
  });

  if (remaining.length > 0) {
    const free = candidates.reduce((sum, t) => sum + availableGptSeats(t), 0);
    return fail(409, {
      code: 'require_overage_confirmation',
      message: added.length
        ? `已添加 ${added.length} 个，剩余 ${remaining.length} 个未添加。继续可能产生额外计费。`
        : '空闲 GPT 席位不足，继续可能产生额外计费。',
      capacity: {
        available: free,
        free_team_count: candidates.filter((t) => availableGptSeats(t) > 0).length,
        active_team_count: candidates.length,
      },
      added,
      remaining_emails: remaining,
      failed,
    });
  }
  return ok({ status: 'ok', added, failed, total: emails.length } satisfies InviteGptMembersResult);
}

// ── Sessions ──

function exportSessions(ctx: DemoContext): DemoResponse {
  const now = Date.now();
  return ok(
    ctx.db.teams.map(({ team, members }) => {
      const owner = members.find((m) => m.is_owner);
      return {
        user: { id: owner?.id ?? 'user-demo', name: owner?.name ?? null, email: team.owner_email, image: null },
        expires: isoAt(now + 30 * DAY),
        account: { id: team.id, planType: 'team', structure: 'workspace' },
        accessToken: 'demo-access-token-not-real',
        sessionToken: 'demo-session-token-not-real',
        _team_id: team.id,
      };
    }),
  );
}

function importSessions(ctx: DemoContext): DemoResponse {
  const body = ctx.body;
  const items = Array.isArray(body) ? body : body && typeof body === 'object' ? [body] : [];
  const imported: string[] = [];
  const errors: Array<{ error: string; detail: unknown }> = [];
  items.forEach((item) => {
    if (!item || typeof item !== 'object') {
      errors.push({ error: 'invalid_session', detail: [{ type: 'dict_type', loc: [], msg: 'Input should be a valid dictionary' }] });
      return;
    }
    const session = item as Record<string, unknown>;
    const account = (session.account ?? {}) as Record<string, unknown>;
    const teamId = typeof session._team_id === 'string' ? session._team_id : typeof account.id === 'string' ? account.id : '';
    const existing = teamId ? findTeam(ctx.db, teamId) : undefined;
    const record = existing ?? newTeamFromSession(ctx.db, session);
    if (existing) markHealthy(existing);
    else ctx.db.teams.push(record);
    imported.push(record.team.id);
    appendLog(ctx.db, {
      team_id: record.team.id, action: 'import_session', target_email: record.team.owner_email, detail: 'Team added/updated',
    });
  });
  return ok({ status: 'ok', imported, count: imported.length, errors });
}

export const teamRoutes: DemoRoute[] = [
  { method: 'GET', pattern: '/api/teams', handler: (ctx) => ok(ctx.db.teams.map((t) => teamView(t))) },
  { method: 'POST', pattern: '/api/teams', handler: importTeam },
  { method: 'POST', pattern: '/api/teams/sync', handler: syncAll },
  { method: 'POST', pattern: '/api/teams/refresh-all', handler: refreshAll },
  {
    method: 'GET',
    pattern: '/api/teams/:teamId',
    handler: (ctx) => {
      const record = teamOr404(ctx);
      return isResponse(record) ? record : ok({ ...teamView(record), cached_member_emails: [] });
    },
  },
  { method: 'DELETE', pattern: '/api/teams/:teamId', handler: deleteTeam },
  { method: 'POST', pattern: '/api/teams/:teamId/sync', handler: syncTeam },
  { method: 'POST', pattern: '/api/teams/:teamId/reimport', handler: reimportTeam },
  { method: 'POST', pattern: '/api/teams/:teamId/refresh', handler: refreshTeamToken },
  { method: 'PATCH', pattern: '/api/teams/:teamId/remark', handler: updateRemark },
  { method: 'PATCH', pattern: '/api/teams/:teamId/proxy', handler: updateTeamProxy },
  {
    method: 'GET',
    pattern: '/api/teams/:teamId/members',
    handler: (ctx) => {
      const record = teamOr404(ctx);
      return isResponse(record) ? record : ok(membersData(ctx.db, record));
    },
  },
  {
    method: 'GET',
    pattern: '/api/teams/:teamId/workspace-settings',
    handler: (ctx) => {
      const record = teamOr404(ctx);
      return isResponse(record) ? record : ok(workspaceSettings(record));
    },
  },
  { method: 'POST', pattern: '/api/teams/:teamId/workspace-settings/default-seat-type', handler: setDefaultSeat },
  { method: 'POST', pattern: '/api/teams/:teamId/members/invite', handler: inviteMember },
  { method: 'DELETE', pattern: '/api/teams/:teamId/members/:userId', handler: removeMember },
  { method: 'PATCH', pattern: '/api/teams/:teamId/members/:userId/seat', handler: changeSeat },
  { method: 'PUT', pattern: '/api/teams/:teamId/members/:userId/expiry', handler: setExpiry },
  { method: 'DELETE', pattern: '/api/teams/:teamId/members/:userId/expiry', handler: removeExpiry },
  { method: 'POST', pattern: '/api/teams/:teamId/members/:userId/expiry/extend', handler: extendExpiry },
  { method: 'DELETE', pattern: '/api/teams/:teamId/invites/:email', handler: revokeInvite },
  { method: 'POST', pattern: '/api/gpt-members/invite', handler: inviteGptMembers },
  { method: 'GET', pattern: '/api/sessions/export', handler: exportSessions },
  { method: 'POST', pattern: '/api/sessions/import', handler: importSessions },
];
