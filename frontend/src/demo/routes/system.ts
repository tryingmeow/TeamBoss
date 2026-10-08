/** Admin auth, health, global settings, proxies and the operation log. */
import type { Settings } from '../../types';
import type { LogsResult, OperationLog } from '../../api/client';
import { findTeam, nextId } from '../db';
import { bodyObject, bodyString, fail, ok, type DemoContext, type DemoResponse, type DemoRoute } from '../http';
import { appendLog, isMemberLog } from '../logs';
import { isoAt } from '../time';

// ── Admin ──

function login(ctx: DemoContext): DemoResponse {
  // Demo mode: any password is accepted.
  appendLog(ctx.db, { action: 'admin_login', detail: 'ip=127.0.0.1' });
  return ok({ status: 'ok', api_key: ctx.db.adminApiKey });
}

function account(ctx: DemoContext): DemoResponse {
  const key = ctx.db.adminApiKey;
  return ok({ api_key: key, api_key_prefix: `${key.slice(0, 8)}...${key.slice(-4)}` });
}

function changePassword(ctx: DemoContext): DemoResponse {
  if (bodyString(ctx, 'new_password').length < 8) {
    return fail(422, [{ msg: 'String should have at least 8 characters' }]);
  }
  const key = `demo-key-${String(nextId(ctx.db)).padStart(6, '0')}`;
  ctx.db.adminApiKey = key;
  const prefix = `${key.slice(0, 8)}...${key.slice(-4)}`;
  appendLog(ctx.db, { action: 'change_admin_password', detail: `api_key_rotated prefix=${prefix}` });
  return ok({ status: 'ok', api_key: key, api_key_prefix: prefix });
}

function rotateApiKey(ctx: DemoContext): DemoResponse {
  const key = `demo-key-${String(nextId(ctx.db)).padStart(6, '0')}`;
  ctx.db.adminApiKey = key;
  const prefix = `${key.slice(0, 8)}...${key.slice(-4)}`;
  appendLog(ctx.db, { action: 'rotate_admin_api_key', detail: `prefix=${prefix}` });
  return ok({ status: 'ok', api_key: key, api_key_prefix: prefix });
}

// ── Settings ──

function getSettings(ctx: DemoContext): DemoResponse {
  const { settings, settingsUpdatedAt } = ctx.db;
  const entry = (value: string) => ({ value, updated_at: settingsUpdatedAt });
  return ok({
    sync_interval_minutes: entry(String(settings.sync_interval_minutes)),
    api_concurrency: entry(String(settings.api_concurrency)),
    expiry_kick_mode: entry(settings.expiry_kick_mode),
    expiry_kick_delay_hours: entry(String(settings.expiry_kick_delay_hours)),
    skip_overage_confirmation: entry('false'),
  });
}

function patchSettings(ctx: DemoContext): DemoResponse {
  const body = bodyObject(ctx);
  const next: Settings = { ...ctx.db.settings };
  const updates: Record<string, string | number> = {};
  const intField = (key: 'sync_interval_minutes' | 'api_concurrency' | 'expiry_kick_delay_hours', min: number, max: number) => {
    if (body[key] === undefined || body[key] === null) return null;
    const value = Number(body[key]);
    if (!Number.isInteger(value) || value < min || value > max) return fail(400, `${key} must be between ${min} and ${max}`);
    next[key] = value;
    updates[key] = value;
    return null;
  };
  const errors = [
    intField('sync_interval_minutes', 5, 60),
    intField('api_concurrency', 1, 10),
    intField('expiry_kick_delay_hours', 0, 720),
  ].filter((r): r is DemoResponse => r !== null);
  if (errors.length) return errors[0];
  if (body.expiry_kick_mode !== undefined) {
    const mode = String(body.expiry_kick_mode);
    if (!['delay_hours', 'day_end', 'day_start'].includes(mode)) return fail(422, [{ msg: 'Invalid expiry_kick_mode' }]);
    next.expiry_kick_mode = mode === 'delay_hours' ? 'delay_hours' : 'day_end';
    updates.expiry_kick_mode = next.expiry_kick_mode;
  }
  // Retired: the per-Team overage policy replaced it. Accepted for old clients, never stored as true.
  if (typeof body.skip_overage_confirmation === 'boolean') {
    next.skip_overage_confirmation = false;
    updates.skip_overage_confirmation = 'false';
  }
  ctx.db.settings = next;
  ctx.db.settingsUpdatedAt = isoAt(Date.now());
  appendLog(ctx.db, {
    action: 'update_settings',
    detail: Object.entries(updates).map(([k, v]) => `${k}=${v}`).join(', ') || null,
  });
  return ok({ status: 'ok', updated: updates });
}

// ── Proxies ──

function createProxy(ctx: DemoContext): DemoResponse {
  const name = bodyString(ctx, 'name').trim();
  const url = bodyString(ctx, 'url').trim();
  if (!name) return fail(400, 'Name required');
  if (!url) return fail(400, 'URL required');
  const id = Math.max(0, ...ctx.db.proxies.map((p) => p.id)) + 1;
  const now = isoAt(Date.now());
  ctx.db.proxies.push({ id, name, url, status: 'unknown', last_check_at: null, created_at: now, updated_at: now });
  appendLog(ctx.db, { action: 'create_proxy', detail: `name=${name}` });
  return ok({ id, name, url, status: 'unknown' });
}

function updateProxy(ctx: DemoContext): DemoResponse {
  const proxy = ctx.db.proxies.find((p) => p.id === Number(ctx.params.proxyId));
  if (!proxy) return fail(404, 'Proxy not found');
  const body = bodyObject(ctx);
  const parts: string[] = [];
  if (typeof body.name === 'string' && body.name.trim()) {
    proxy.name = body.name.trim();
    parts.push(`name=${proxy.name}`);
  }
  if (typeof body.url === 'string' && body.url.trim()) {
    proxy.url = body.url.trim();
    proxy.status = 'unknown';
    parts.push('url=***');
  }
  proxy.updated_at = isoAt(Date.now());
  appendLog(ctx.db, { action: 'update_proxy', detail: parts.join(', ') || `proxy_id=${proxy.id}` });
  return ok();
}

function deleteProxy(ctx: DemoContext): DemoResponse {
  const id = Number(ctx.params.proxyId);
  if (!ctx.db.proxies.some((p) => p.id === id)) return fail(404, 'Proxy not found');
  ctx.db.proxies = ctx.db.proxies.filter((p) => p.id !== id);
  ctx.db.teams.forEach(({ team }) => {
    if (team.proxy_id === id) team.proxy_id = null;
  });
  appendLog(ctx.db, { action: 'delete_proxy', detail: `proxy_id=${id}` });
  return ok();
}

function checkProxy(ctx: DemoContext): DemoResponse {
  const proxy = ctx.db.proxies.find((p) => p.id === Number(ctx.params.proxyId));
  if (!proxy) return fail(404, 'Proxy not found');
  // The seeded backup proxy stays broken so the error state remains visible.
  proxy.status = proxy.id === 2 ? 'error' : 'ok';
  proxy.last_check_at = isoAt(Date.now());
  return ok({ status: proxy.status, last_check_at: proxy.last_check_at });
}

// ── Logs ──

function listLogs(ctx: DemoContext): DemoResponse {
  const { query, db } = ctx;
  const teamId = query.get('team_id');
  const csv = (name: string) =>
    query.getAll(name).flatMap((v) => v.split(',')).map((v) => v.trim()).filter(Boolean);
  const actions = csv('action');
  const qActions = csv('q_action');
  const terms = query.getAll('q').map((v) => v.trim().toLowerCase()).filter(Boolean);
  const scope = query.get('scope');
  const perPage = Math.min(Math.max(Number(query.get('per_page')) || 50, 1), 1000);
  const page = Math.max(Number(query.get('page')) || 1, 1);

  const joined: OperationLog[] = db.logs.map((row) => {
    const team = row.team_id ? findTeam(db, row.team_id)?.team : undefined;
    return {
      ...row,
      team_name: team?.name ?? null,
      team_remark: team?.remark ?? null,
      team_owner_email: team?.owner_email ?? null,
      team_status: team?.status ?? null,
    };
  });

  const filtered = joined.filter((log) => {
    if (teamId && log.team_id !== teamId) return false;
    if (actions.length && !actions.includes(log.action ?? '')) return false;
    if (scope === 'members' && !isMemberLog(log.action)) return false;
    if (!terms.length && !qActions.length) return true;
    if (qActions.includes(log.action ?? '')) return true;
    const haystack = [
      log.team_id, log.action, log.target_email, log.detail, log.result, log.error_message, log.trigger_type,
      log.created_at, log.team_name, log.team_remark, log.team_owner_email, log.team_status,
    ].map((value) => (value ?? '').toLowerCase());
    return terms.some((term) => haystack.some((value) => value.includes(term)));
  });

  const total = filtered.length;
  const result: LogsResult = {
    logs: filtered.slice((page - 1) * perPage, page * perPage),
    total,
    page,
    per_page: perPage,
    total_pages: Math.ceil(total / perPage),
  };
  return ok(result);
}

export const systemRoutes: DemoRoute[] = [
  { method: 'POST', pattern: '/api/admin/login', handler: login },
  { method: 'GET', pattern: '/api/admin/account', handler: account },
  { method: 'PATCH', pattern: '/api/admin/password', handler: changePassword },
  { method: 'POST', pattern: '/api/admin/api-key/rotate', handler: rotateApiKey },
  {
    method: 'GET',
    pattern: '/api/health',
    handler: () => ok({ status: 'healthy', version: 'demo', timestamp: isoAt(Date.now()) }),
  },
  { method: 'GET', pattern: '/api/settings', handler: getSettings },
  { method: 'PATCH', pattern: '/api/settings', handler: patchSettings },
  { method: 'GET', pattern: '/api/proxies', handler: (ctx) => ok(ctx.db.proxies.map((p) => ({ ...p }))) },
  { method: 'POST', pattern: '/api/proxies', handler: createProxy },
  { method: 'PATCH', pattern: '/api/proxies/:proxyId', handler: updateProxy },
  { method: 'DELETE', pattern: '/api/proxies/:proxyId', handler: deleteProxy },
  { method: 'POST', pattern: '/api/proxies/:proxyId/check', handler: checkProxy },
  { method: 'GET', pattern: '/api/logs', handler: listLogs },
];
