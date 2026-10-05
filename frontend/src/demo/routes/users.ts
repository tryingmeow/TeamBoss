/** People views (owners, all members, display names) and seat usage. */
import { bodyObject, fail, ok, queryFlag, type DemoContext, type DemoResponse, type DemoRoute } from '../http';
import { appendLog } from '../logs';
import { allMembers, owners, resourceUsage } from '../views';

function updateDisplayName(ctx: DemoContext): DemoResponse {
  const body = bodyObject(ctx);
  const email = typeof body.email === 'string' ? body.email.trim() : '';
  if (!email) return fail(400, 'email is required');
  const raw = typeof body.system_display_name === 'string' ? body.system_display_name.trim() : '';
  if (raw.length > 120) return fail(422, [{ msg: 'String should have at most 120 characters' }]);
  const key = email.toLowerCase();
  if (raw) ctx.db.remarks.set(key, raw);
  else ctx.db.remarks.delete(key);
  appendLog(ctx.db, { action: 'update_user_display_name', target_email: email, detail: `new_name=${raw || 'None'}` });
  return ok({ email, system_display_name: raw || null });
}

export const userRoutes: DemoRoute[] = [
  { method: 'GET', pattern: '/api/users/owners', handler: (ctx) => ok(owners(ctx.db, ctx.query.get('q') ?? '')) },
  {
    method: 'GET',
    pattern: '/api/users/members',
    handler: (ctx) =>
      ok(
        allMembers(ctx.db, {
          q: ctx.query.get('q') ?? '',
          status: ctx.query.get('status') ?? '',
          includeOwners: queryFlag(ctx, 'include_owners'),
          teamId: ctx.query.get('team_id') ?? '',
        }),
      ),
  },
  { method: 'PATCH', pattern: '/api/users/display-name', handler: updateDisplayName },
  { method: 'GET', pattern: '/api/resources/usage', handler: (ctx) => ok(resourceUsage(ctx.db, queryFlag(ctx, 'refresh'))) },
];
