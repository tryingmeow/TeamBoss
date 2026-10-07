/** Teams, members, batch GPT invites and session import/export. */
import type { OveragePolicy, Team, WorkspaceDefaultSeatType } from '../../types';
import type { InviteGptMembersResult, TeamBatchResult } from '../../api/client';
import { groupSeatCosts, seatChargeText, teamSeatPrice } from '../../lib/seatPrice';
import { findTeam, nextId, recount, type DemoDb, type DemoInvite, type DemoTeam } from '../db';
import { bodyObject, bodyString, fail, ok, queryFlag, type DemoContext, type DemoResponse, type DemoRoute } from '../http';
import { appendLog } from '../logs';
import { DAY, durationMs, isNeverDuration, isoAt } from '../time';
import { isOverage, isRegistrySeat, overageGate, parseConfirmation, parseSeat, policyOf, seatLabel } from '../overage';
import { availableGptSeats, expiryState, membersData, teamMonthlyCost, teamView, workspaceSettings } from '../views';

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

const SEAT_INVALID = [{ msg: "seat_type 必须是 'default' / 'usage_based' / 'prolite'" }];
const NO_PLACE = '没位置，未邀请';
const CONFIRMATION_INVALID = [{ msg: 'overage_confirmation 需要 confirmation_id（16–64 位字母数字 _ -）、seat_type 和 1–100 的 seat_limit' }];

/** Workspace default invite seat: Premium can never be the default (the backend answers 422). */
function parseWorkspaceDefault(value: unknown): WorkspaceDefaultSeatType | null {
  const seat = parseSeat(value);
  return seat === 'default' || seat === 'usage_based' ? seat : null;
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
    overage_policy: 'confirm',
    seat_capacity: null,
    seat_type_counts: {},
  };
  const record: DemoTeam = {
    team,
    paid: { default: 5, prolite: 0 },
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

function setOveragePolicy(ctx: DemoContext): DemoResponse {
  const record = teamOr404(ctx);
  if (isResponse(record)) return record;
  const value = bodyObject(ctx).overage_policy;
  if (value !== 'forbid' && value !== 'confirm' && value !== 'auto') {
    return fail(422, [{ msg: "overage_policy 必须是 'forbid' / 'confirm' / 'auto'" }]);
  }
  const previous = record.team.overage_policy;
  record.team.overage_policy = value as OveragePolicy;
  appendLog(ctx.db, {
    team_id: record.team.id, action: 'set_overage_policy', detail: `overage_policy=${value}, previous=${previous}`,
  });
  return ok(teamView(record));
}

function setDefaultSeat(ctx: DemoContext): DemoResponse {
  const record = teamOr404(ctx);
  if (isResponse(record)) return record;
  const blocked = upstreamBlocked(record);
  if (blocked) return blocked;
  const seat = parseWorkspaceDefault(bodyObject(ctx).seat_type);
  if (!seat) return fail(422, SEAT_INVALID);
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
  const seat = parseSeat(body.seat_type);
  if (!seat) return fail(422, SEAT_INVALID);
  const confirmation = parseConfirmation(body.overage_confirmation);
  if (confirmation === 'invalid') return fail(422, CONFIRMATION_INVALID);
  let expiresAt: string | null;
  try {
    expiresAt = expiryFromDuration(body.expires_in);
  } catch (error) {
    return fail(400, (error as Error).message);
  }
  const pending = record.invites.find((i) => i.email === email);
  if (pending && !isRegistrySeat(pending.seat_type)) {
    // Resending an invite whose seat type TeamBoss does not know is never attempted.
    return fail(409, {
      code: 'seat_type_unknown',
      message: `该邀请的席位类型是「${seatLabel(pending.seat_type)}」，TeamBoss 不会重发或改动这类席位。`,
      seat_type: pending.seat_type,
    });
  }
  if (record.members.some((m) => m.email === email) || pending) {
    return fail(409, EMAIL_ALREADY_IN_TEAM);
  }
  // 旧前端的 allow_overage 不再算确认（超员需确认的 Team 满了照样 409）。
  const gate = overageGate({
    db: ctx.db, record, seatType: seat, confirmation, operation: 'invite',
    logAction: 'invite_member', targetEmail: email,
  });
  if (gate.refused) return gate.refused;
  const overage = isOverage(record, seat);
  const policy = policyOf(record);
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
    detail:
      `seat_type=${seat}, expires_in=${String(body.expires_in ?? 'never')}` +
      (seat === 'usage_based' ? '' : `, policy=${policy}, overage=${overage ? 'True' : 'False'}`) +
      (gate.confirmed ? `, overage_confirmed=${gate.confirmed.used}/${gate.confirmed.limit}` : ''),
  });
  return ok({ status: 'ok', result: { _mutation_status: 'confirmed' }, overage, policy });
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
  const body = bodyObject(ctx);
  const seat = parseSeat(body.seat_type);
  if (!seat) return fail(422, SEAT_INVALID);
  const confirmation = parseConfirmation(body.overage_confirmation);
  if (confirmation === 'invalid') return fail(422, CONFIRMATION_INVALID);
  const policy = policyOf(record);
  const from = member.seat_type;
  if (from !== 'default' && from !== 'usage_based' && from !== 'prolite') {
    return fail(409, {
      code: 'seat_type_unknown',
      message: `该成员的席位类型是「${seatLabel(from)}」，TeamBoss 不会切换或移除这类席位。`,
    });
  }
  if (seat === from) return ok({ status: 'ok', result: { seat_type: seat }, unchanged: true, overage: false, policy });
  if (seat === 'usage_based' && !record.team.is_codex_enabled) {
    appendLog(ctx.db, {
      team_id: record.team.id, action: 'change_seat', target_email: member.email,
      detail: `user_id=${member.id}, seat_type=${seat}`, result: 'failed', error_message: 'HTTP 403: Forbidden',
    });
    return fail(502, 'HTTP 403: Forbidden (usage_based seats are not enabled for this workspace)');
  }
  const gate = overageGate({
    db: ctx.db, record, seatType: seat, confirmation, operation: 'seat_switch',
    logAction: 'change_seat', targetEmail: member.email, logPrefix: `user_id=${member.id}, `,
  });
  if (gate.refused) return gate.refused;
  const overage = isOverage(record, seat);
  member.seat_type = seat;
  touch(record);
  appendLog(ctx.db, {
    team_id: record.team.id, action: 'change_seat', target_email: member.email,
    detail:
      `user_id=${member.id}, seat_type=${seat}, from_seat_type=${from}, policy=${policy}` +
      (seat === 'usage_based' ? '' : `, overage=${overage ? 'True' : 'False'}`) +
      (gate.confirmed ? `, overage_confirmed=${gate.confirmed.used}/${gate.confirmed.limit}` : ''),
  });
  return ok({ status: 'ok', result: { seat_type: seat }, overage, policy });
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
  const allowOverage = body.allow_overage === true;
  const added: InviteGptMembersResult['added'] = [];
  const failed: InviteGptMembersResult['failed'] = [];
  const remaining: string[] = [];

  // Overfill never touches `forbid` Teams; `confirm` Teams only after the operator confirmed.
  // The confirmation is bound to the plan the operator saw: only the listed `confirm` Teams may be overfilled.
  const listedIds = new Set(
    Array.isArray(body.overage_team_ids) ? body.overage_team_ids.filter((v): v is string => typeof v === 'string') : [],
  );
  const overfillNow = candidates.filter((t) => {
    const policy = policyOf(t);
    return policy === 'auto' || (allowOverage && policy === 'confirm' && listedIds.has(t.team.id));
  });
  // Confirm Teams that could take the leftovers but were not confirmed (all of them before the first ask).
  const unconfirmed = candidates.filter((t) => policyOf(t) === 'confirm' && !(allowOverage && listedIds.has(t.team.id)));

  // The confirmation also binds the count: at most `overage_seat_limit` overfills on confirm Teams per request.
  const seatLimit = typeof body.overage_seat_limit === 'number' && body.overage_seat_limit > 0 ? Math.floor(body.overage_seat_limit) : 0;
  let confirmOverfills = 0;
  let limitHit = false;

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
    if (!target) {
      // Candidate order; auto Teams are free, confirm Teams only while the confirmed count lasts.
      for (const t of overfillNow) {
        if (policyOf(t) === 'auto') {
          target = t;
          break;
        }
        if (confirmOverfills < seatLimit) {
          target = t;
          confirmOverfills += 1;
          break;
        }
        limitHit = true;
      }
      overage = Boolean(target);
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
      team_id: target.team.id, action: 'invite_gpt_member', target_email: email,
      detail: `expires_at=${expiresAt ?? 'None'}, policy=${policyOf(target)}, overage=${overage ? 'True' : 'False'}`,
    });
    added.push({ email, team_id: target.team.id, team_name: target.team.name, expires_at: expiresAt, overage });
  });

  // Teams a fresh plan can name: the never-confirmed ones, plus the confirmed ones once their count is used up.
  const askTeams = limitHit ? candidates.filter((t) => policyOf(t) === 'confirm') : unconfirmed;
  if (remaining.length > 0 && askTeams.length > 0) {
    const free = candidates.reduce((sum, t) => sum + availableGptSeats(t), 0);
    const first = askTeams[0];
    // Like the server: each plan item carries one ChatGPT seat's monthly price (null = unknown),
    // and cost_totals sums the priced items per currency.
    const plan = [{
      team_id: first.team.id,
      team_name: first.team.name,
      extra_seats: remaining.length,
      seat_price: teamSeatPrice(first.team, 'default'),
    }];
    const { totals } = groupSeatCosts(plan.map((item) => ({ price: item.seat_price, seats: item.extra_seats })));
    return fail(409, {
      code: 'require_overage_confirmation',
      // Same shape as the server's batch message: the UI keeps what comes before 「继续会让」.
      message: (allowOverage ? '你确认过的超员计划已经不成立，需要重新确认。' : '') +
        (added.length
          ? `已添加 ${added.length} 个，剩余 ${remaining.length} 个没有空闲 ChatGPT 席位。`
          : `空闲 ChatGPT 席位只有 ${free} 个，要邀请 ${remaining.length} 个。`) +
        `继续会让 ChatGPT 自动加购 ${remaining.length} 个 ChatGPT 席位并扣费：` +
        plan.map((item) => `「${item.team_name}」${item.extra_seats} 个，${seatChargeText(item.seat_price, item.extra_seats)}`).join('；') +
        '。',
      seat_type: 'default',
      policy: 'confirm',
      operation: 'batch',
      capacity: {
        seat_type: 'default',
        available: free,
        capacity_unknown: false,
        free_team_count: candidates.filter((t) => availableGptSeats(t) > 0).length,
        active_team_count: candidates.length,
      },
      overage_plan: plan,
      extra_seats_total: remaining.length,
      cost_totals: totals,
      added,
      remaining_emails: remaining,
      failed,
    });
  }
  // Whatever is left has no eligible Team (forbid Teams are never overfilled).
  remaining.forEach((email) => failed.push({ email, error: NO_PLACE }));
  return ok({ status: 'ok', added, failed, total: emails.length, no_place_emails: remaining } satisfies InviteGptMembersResult & {
    no_place_emails: string[];
  });
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

/** Fake quote only: no request reaches ChatGPT in demo mode. */
function seatPurchasePreview(ctx: DemoContext): DemoResponse {
  const record = teamOr404(ctx);
  if (isResponse(record)) return record;
  const body = bodyObject(ctx);
  const seat = body.seat_type;
  const additional = body.additional_seats;
  if ((seat !== 'default' && seat !== 'prolite') || typeof additional !== 'number' || !Number.isInteger(additional) || additional <= 0) return fail(422, 'Invalid seat purchase');
  const current = teamMonthlyCost(record.team);
  const months = record.team.billing_period === 'yearly' ? 12 : 1;
  const price = teamSeatPrice(record.team, seat);
  if (!price || current.period_total === null) return fail(502, { code: 'seat_purchase_quote_unavailable', message: '演示：当前 Team 报价不可用' });
  const baseline = { default: record.paid.default, prolite: record.paid.prolite };
  const proposed = { ...baseline, [seat]: baseline[seat] + additional };
  const nextTeam = { ...record.team, seat_capacity: { ...record.team.seat_capacity,
    [seat]: { paid: proposed[seat], available: additional } } };
  const next = teamMonthlyCost(nextTeam);
  if (next.period_total === null) return fail(502, '演示：费用未知');
  return ok({
    currency: record.team.billing_currency,
    minor_unit_exponent: 2,
    quoted_at: isoAt(Date.now()),
    seat_type: seat,
    additional_seats: additional,
    baseline_quantities: baseline,
    proposed_quantities: proposed,
    current_recurring: { period: record.team.billing_period, amount: current.period_total, discount: (current.discount_monthly ?? 0) * months },
    proposed_recurring: { period: record.team.billing_period, amount: next.period_total, discount: (next.discount_monthly ?? 0) * months },
    due_now: { amount: Math.round((next.period_total - current.period_total) * 0.5 * 100) / 100, tax_amount: 0 },
  });
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
  { method: 'POST', pattern: '/api/teams/:teamId/seat-purchase-preview', handler: seatPurchasePreview },
  { method: 'POST', pattern: '/api/teams/:teamId/sync', handler: syncTeam },
  { method: 'POST', pattern: '/api/teams/:teamId/reimport', handler: reimportTeam },
  { method: 'POST', pattern: '/api/teams/:teamId/refresh', handler: refreshTeamToken },
  { method: 'PATCH', pattern: '/api/teams/:teamId/remark', handler: updateRemark },
  { method: 'PATCH', pattern: '/api/teams/:teamId/proxy', handler: updateTeamProxy },
  { method: 'PATCH', pattern: '/api/teams/:teamId/overage-policy', handler: setOveragePolicy },
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
