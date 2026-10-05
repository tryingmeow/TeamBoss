/** Over-seat patrol and the Telegram bot. */
import type { TgCodeCreateResult, TgMemberCodeCreateResult } from '../../api/client';
import { findTeam, recount, type DemoDb } from '../db';
import { bodyObject, fail, ok, type DemoContext, type DemoResponse, type DemoRoute } from '../http';
import { appendLog } from '../logs';
import { DAY, isoAt } from '../time';
import { patrolStatus } from '../views';

// ── Patrol ──

function patchPatrolSettings(ctx: DemoContext): DemoResponse {
  const body = bodyObject(ctx);
  const { patrol } = ctx.db;
  const parts: string[] = [];
  if (body.kick_enabled === true) {
    return fail(409, '开启时必须通过“豁免现有成员并开启自动踢人”安全入口');
  }
  if (typeof body.kick_enabled === 'boolean') {
    patrol.kick_enabled = body.kick_enabled;
    parts.push(`kick_enabled=${body.kick_enabled ? 'True' : 'False'}`);
  }
  if (Array.isArray(body.exempt_team_ids)) {
    patrol.exempt_team_ids = body.exempt_team_ids.map(String).filter((id) => findTeam(ctx.db, id));
    parts.push(`exempt_team_ids_count=${patrol.exempt_team_ids.length}`);
  }
  if (typeof body.strict_mode_enabled === 'boolean') {
    patrol.strict_mode_enabled = body.strict_mode_enabled;
    parts.push(`strict_mode_enabled=${body.strict_mode_enabled ? 'True' : 'False'}`);
  }
  appendLog(ctx.db, { action: 'update_patrol_settings', detail: parts.join(', ') || 'no_changes' });
  return ok({
    status: 'ok',
    kick_enabled: patrol.kick_enabled,
    exempt_team_ids: [...patrol.exempt_team_ids],
    strict_mode_enabled: patrol.strict_mode_enabled,
  });
}

/** Teams the scheduler could not refresh this round; patrol leaves them alone. */
function unrefreshed(db: DemoDb): string[] {
  return db.teams
    .filter(({ team }) => team.status === 'active' && (team.sync_suspended_at || team.auth_state === 'rejected'))
    .map(({ team }) => `${team.id}: ${team.sync_suspended_at ? 'sync suspended' : 'team_auth_rejected'}`);
}

function runPatrol(ctx: DemoContext): DemoResponse {
  const { db } = ctx;
  const requestedDry = bodyObject(ctx).dry_run !== false;
  const dryRun = requestedDry || !db.patrol.kick_enabled || !db.patrol.baseline_at;
  const skipped = unrefreshed(db);
  const skippedIds = new Set(skipped.map((s) => s.split(':')[0]));
  const events: Array<Record<string, unknown>> = [];
  let kicked = 0;
  let wouldKick = 0;

  patrolStatus(db).teams.forEach((status) => {
    if (status.risk !== 'over' || skippedIds.has(status.team_id)) return;
    const record = findTeam(db, status.team_id);
    if (!record) return;
    if (db.patrol.exempt_team_ids.includes(status.team_id)) {
      events.push({ team_id: status.team_id, team_name: status.name, action: 'exempt_skip', over_by: status.over_by, detected_count: status.detected_over.length });
      return;
    }
    const selected = status.detected_over.slice(0, Math.min(status.over_by, 10));
    selected.forEach((cand, i) => {
      const reason =
        `over_by=${status.over_by} position=${i + 1}/${selected.length} seat_type=${cand.seat_type} ` +
        `source=detected first_seen_at=${cand.first_seen_at}`;
      const base = { team_id: status.team_id, team_name: status.name, email: cand.email, user_id: cand.user_id, over_by: status.over_by, position: i + 1, reason };
      if (dryRun) {
        wouldKick += 1;
        events.push({ ...base, action: 'would_kick', result: 'dryrun' });
        appendLog(db, { team_id: status.team_id, action: 'patrol_would_kick', target_email: cand.email, detail: reason, result: 'dryrun', trigger_type: 'patrol' });
        return;
      }
      const member = record.members.find((m) => m.id === cand.user_id);
      if (member) {
        record.members = record.members.filter((m) => m !== member);
        record.kicked.push({
          expiry_id: db.seq + 1,
          user_id: member.id,
          email: member.email,
          expires_at: null,
          kicked_at: isoAt(Date.now()),
          kick_source: 'patrol',
          first_seen_at: member.first_seen_at ?? member.created_time,
          source: 'detected',
          created_at: member.created_time,
        });
        db.seq += 1;
        recount(record);
      }
      kicked += 1;
      events.push({ ...base, action: 'kick', result: 'success', error: null });
      appendLog(db, { team_id: status.team_id, action: 'patrol_kick', target_email: cand.email, detail: `user_id=${cand.user_id}`, trigger_type: 'patrol' });
    });
  });

  appendLog(db, {
    action: 'patrol_manual_run',
    detail: `patrolled=${patrolStatus(db).teams.length - skipped.length}, skipped_unrefreshed=${skipped.length}`,
    error_message: skipped.join('; ') || null,
  });
  return ok({
    events,
    kicked,
    would_kick: wouldKick,
    invites_revoked: 0,
    invites_would_revoke: 0,
    strict_kicked: 0,
    strict_would_kick: 0,
    skipped_teams: skipped,
  });
}

function activatePatrol(ctx: DemoContext): DemoResponse {
  const { db } = ctx;
  const watched = db.teams.filter(({ team }) => team.status === 'active' && !team.is_codex_enabled);
  const grandfathered = watched.reduce((sum, t) => sum + t.members.filter((m) => !m.is_owner && m.source !== 'detected').length, 0);
  const backfilled = watched.reduce((sum, t) => sum + t.members.filter((m) => m.source === 'detected').length, 0);
  db.patrol.kick_enabled = true;
  db.patrol.baseline_at = isoAt(Date.now());
  appendLog(db, { action: 'activate_patrol', detail: `teams_protected=${watched.length}` });
  return ok({ status: 'ok', kick_enabled: true, grandfathered, backfilled, baseline_at: db.patrol.baseline_at });
}

// ── Telegram ──

const CODE_ALPHABET = '23456789ABCDEFGHJKMNPQRSTUVWXYZ';

function newPairingCode(db: DemoDb): string {
  let n = db.seq * 2654435761;
  let code = 'DEM';
  for (let i = 0; i < 5; i += 1) {
    code += CODE_ALPHABET[Math.abs(n) % CODE_ALPHABET.length];
    n = Math.floor(n / CODE_ALPHABET.length) + 7919 * (i + 1);
  }
  db.seq += 1;
  return code;
}

function createCode(db: DemoDb, note: string): TgCodeCreateResult {
  const now = Date.now();
  const code: TgCodeCreateResult = {
    id: Math.max(0, ...db.tg.codes.map((c) => c.id)) + 1,
    code: newPairingCode(db),
    note,
    expires_at: isoAt(now + DAY),
    used_by_chat_id: null,
    used_at: null,
    disabled: false,
    created_at: isoAt(now),
  };
  db.tg.codes.unshift(code);
  return code;
}

function patchTgConfig(ctx: DemoContext): DemoResponse {
  const { db } = ctx;
  const body = bodyObject(ctx);
  const config = db.tg.config;
  const parts: string[] = [];
  let botChanged = false;
  let pairing: TgCodeCreateResult | null = null;

  if (body.summary_interval_minutes !== undefined) {
    const minutes = Number(body.summary_interval_minutes);
    if (!Number.isInteger(minutes) || minutes < 5 || minutes > 1440) return fail(400, '摘要间隔必须在 5–1440 分钟之间');
  }
  if (typeof body.token === 'string') {
    if (!body.token.trim()) return fail(400, '无法验证机器人 Token，原配置未修改');
    botChanged = !config.token_set;
    config.token_set = true;
    config.bot_username = 'teamboss_demo_bot';
    parts.push('token_changed=true');
    if (botChanged) {
      parts.push('bot_changed=true');
      pairing = createCode(db, '管理员配对');
    }
  }
  if (typeof body.enabled === 'boolean') {
    config.enabled = body.enabled;
    parts.push(`enabled=${body.enabled ? 'True' : 'False'}`);
  }
  if (typeof body.summary_enabled === 'boolean') {
    config.summary_enabled = body.summary_enabled;
    parts.push(`summary_enabled=${body.summary_enabled ? 'True' : 'False'}`);
  }
  if (body.summary_interval_minutes !== undefined) {
    config.summary_interval_minutes = Number(body.summary_interval_minutes);
    parts.push(`summary_interval_minutes=${config.summary_interval_minutes}`);
  }
  config.polling = config.enabled && config.token_set;
  appendLog(db, { action: 'update_tg_config', detail: parts.join(', ') || 'no_changes' });
  return ok({
    status: 'ok',
    token_set: config.token_set,
    bot_changed: botChanged,
    admin_pairing_code: pairing?.code ?? null,
    admin_pairing_expires_at: pairing?.expires_at ?? null,
    summary_enabled: config.summary_enabled,
    summary_interval_minutes: config.summary_interval_minutes,
  });
}

function sendSummary(ctx: DemoContext): DemoResponse {
  const { db } = ctx;
  if (!db.tg.config.enabled || !db.tg.config.token_set) return fail(503, '摘要未送达: bot_disabled');
  const sent = db.tg.users.filter((u) => !u.disabled).length;
  if (sent === 0) return fail(503, '摘要未送达: no_admin_delivery');
  const sentAt = isoAt(Date.now());
  db.tg.config.summary_last_sent_at = sentAt;
  appendLog(db, { action: 'tg_summary', detail: `delivered_to=${sent}`, trigger_type: 'scheduler' });
  return ok({ sent, reason: 'sent', sent_at: sentAt });
}

function patchTgUser(ctx: DemoContext): DemoResponse {
  const user = ctx.db.tg.users.find((u) => u.id === Number(ctx.params.userId));
  if (!user) return fail(404, '用户不存在');
  const disabled = bodyObject(ctx).disabled;
  if (typeof disabled === 'boolean') user.disabled = disabled;
  appendLog(ctx.db, {
    action: 'update_tg_operator',
    detail: typeof disabled === 'boolean' ? `user_id=${user.id}, disabled=${disabled ? 'True' : 'False'}` : `user_id=${user.id}`,
  });
  return ok({ ...user });
}

function deleteTgUser(ctx: DemoContext): DemoResponse {
  const id = Number(ctx.params.userId);
  if (!ctx.db.tg.users.some((u) => u.id === id)) return fail(404, '用户不存在');
  ctx.db.tg.users = ctx.db.tg.users.filter((u) => u.id !== id);
  appendLog(ctx.db, { action: 'delete_tg_operator', detail: `user_id=${id}` });
  return ok();
}

function deleteTgCode(ctx: DemoContext): DemoResponse {
  const code = ctx.db.tg.codes.find((c) => c.id === Number(ctx.params.codeId));
  if (!code) return fail(404, '配对码不存在');
  code.disabled = true;
  appendLog(ctx.db, { action: 'delete_tg_pairing_code', detail: `code_id=${code.id}` });
  return ok();
}

function createMemberCode(ctx: DemoContext): DemoResponse {
  const { db } = ctx;
  const email = String(bodyObject(ctx).email ?? '').trim().toLowerCase();
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return fail(400, '成员邮箱格式无效');
  const bot = db.tg.config.bot_username;
  if (!db.tg.config.token_set || !bot) return fail(503, '无法取得 TG 机器人地址，请先检查机器人 Token');
  const known = db.teams.some((t) => t.members.some((m) => m.email === email) || t.invites.some((i) => i.email === email));
  if (!known) return fail(409, '该邮箱当前不在待接受或已加入成员中');
  const code = newPairingCode(db);
  const url = `https://t.me/${bot}`;
  appendLog(db, { action: 'create_tg_member_pairing_code', target_email: email });
  const result: TgMemberCodeCreateResult = {
    id: db.seq,
    email,
    code,
    expires_at: isoAt(Date.now() + DAY),
    bot_username: bot,
    bot_url: url,
    command: `/pair ${code}`,
    copy_text: `打开 ${url} ，发送：/pair ${code}`,
    currently_bound: db.tgBindings.has(email),
  };
  return ok(result);
}

export const patrolTgRoutes: DemoRoute[] = [
  { method: 'GET', pattern: '/api/patrol/status', handler: (ctx) => ok(patrolStatus(ctx.db)) },
  { method: 'PATCH', pattern: '/api/patrol/settings', handler: patchPatrolSettings },
  { method: 'POST', pattern: '/api/patrol/run', handler: runPatrol },
  { method: 'POST', pattern: '/api/patrol/activate', handler: activatePatrol },
  { method: 'GET', pattern: '/api/tg/config', handler: (ctx) => ok({ ...ctx.db.tg.config }) },
  { method: 'PATCH', pattern: '/api/tg/config', handler: patchTgConfig },
  { method: 'POST', pattern: '/api/tg/summary', handler: sendSummary },
  { method: 'GET', pattern: '/api/tg/users', handler: (ctx) => ok({ users: ctx.db.tg.users.map((u) => ({ ...u })) }) },
  { method: 'PATCH', pattern: '/api/tg/users/:userId', handler: patchTgUser },
  { method: 'DELETE', pattern: '/api/tg/users/:userId', handler: deleteTgUser },
  { method: 'GET', pattern: '/api/tg/codes', handler: (ctx) => ok({ codes: ctx.db.tg.codes.map((c) => ({ ...c })) }) },
  {
    method: 'POST',
    pattern: '/api/tg/codes',
    handler: (ctx) => {
      const note = String(bodyObject(ctx).note ?? '').trim();
      const code = createCode(ctx.db, note);
      appendLog(ctx.db, { action: 'create_tg_pairing_code', detail: note ? `note=${note}` : 'no_note' });
      return ok({ ...code });
    },
  },
  { method: 'DELETE', pattern: '/api/tg/codes/:codeId', handler: deleteTgCode },
  { method: 'POST', pattern: '/api/tg/member-codes', handler: createMemberCode },
];
