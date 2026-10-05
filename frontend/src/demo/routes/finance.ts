/** Finance overview, spend trend, invoices and finance settings. */
import { findTeam } from '../db';
import { bodyObject, fail, ok, type DemoContext, type DemoResponse, type DemoRoute } from '../http';
import { appendLog } from '../logs';
import { FX_RATES } from '../seed';
import { isoAt } from '../time';
import { financeOverview, financeTrends, invoicesFor } from '../views';

function patchFinanceSettings(ctx: DemoContext): DemoResponse {
  const body = bodyObject(ctx);
  const updates: Record<string, string> = {};
  if (typeof body.base_currency === 'string' && body.base_currency.trim()) {
    const currency = body.base_currency.trim().toUpperCase();
    if (!FX_RATES[currency]) return fail(400, `Currency ${currency} not supported`);
    ctx.db.finance.base_currency = currency;
    updates.finance_base_currency = currency;
  }
  if (body.low_balance_threshold !== undefined && body.low_balance_threshold !== null) {
    const threshold = Number(body.low_balance_threshold);
    if (!Number.isFinite(threshold) || threshold < 0) return fail(400, 'low_balance_threshold must be >= 0');
    ctx.db.finance.low_balance_threshold = threshold;
    updates.finance_low_balance_threshold = String(threshold);
  }
  if (Object.keys(updates).length) {
    appendLog(ctx.db, {
      action: 'update_finance_settings',
      detail: Object.entries(updates).map(([k, v]) => `${k}=${v}`).join(', '),
    });
  }
  return ok({ status: 'ok', updated: updates });
}

function patchCardNote(ctx: DemoContext): DemoResponse {
  const body = bodyObject(ctx);
  const last4 = typeof body.card_last4 === 'string' ? body.card_last4.trim() : '';
  if (!last4) return fail(400, 'card_last4 is required');
  const brand = typeof body.card_brand === 'string' ? body.card_brand.trim().toLowerCase() : '';
  const note = typeof body.note === 'string' ? body.note.trim() : '';
  if (note.length > 80) return fail(400, 'note must be 80 characters or fewer');
  const key = `${brand}:${last4}`;
  if (note) ctx.db.finance.cardNotes.set(key, note);
  else ctx.db.finance.cardNotes.delete(key);
  appendLog(ctx.db, { action: 'update_card_note', detail: `card_key=${key}, note_len=${note.length}` });
  return ok({ status: 'ok', card_key: key, note });
}

function refreshFx(ctx: DemoContext): DemoResponse {
  ctx.db.finance.fx_updated_at = isoAt(Date.now());
  const updated = Object.keys(FX_RATES).length;
  appendLog(ctx.db, { action: 'refresh_fx_rates', detail: `updated_currencies=${updated}` });
  return ok({ status: 'ok', updated_currencies: updated, fx_updated_at: ctx.db.finance.fx_updated_at });
}

export const financeRoutes: DemoRoute[] = [
  { method: 'GET', pattern: '/api/finance/overview', handler: (ctx) => ok(financeOverview(ctx.db)) },
  {
    method: 'GET',
    pattern: '/api/finance/trends',
    handler: (ctx) => ok(financeTrends(ctx.db, Number(ctx.query.get('days') ?? 90))),
  },
  {
    method: 'GET',
    pattern: '/api/finance/invoices/:teamId',
    handler: (ctx) => {
      const record = findTeam(ctx.db, ctx.params.teamId);
      return record ? ok(invoicesFor(record)) : fail(404, 'Team not found');
    },
  },
  { method: 'PATCH', pattern: '/api/finance/settings', handler: patchFinanceSettings },
  { method: 'PATCH', pattern: '/api/finance/card-note', handler: patchCardNote },
  { method: 'POST', pattern: '/api/finance/fx/refresh', handler: refreshFx },
];
