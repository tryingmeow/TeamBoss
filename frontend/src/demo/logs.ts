/**
 * Operation-log fixtures. Every `action` / `trigger_type` / `result` / `detail`
 * shape below is copied from a real backend call site (log_operation,
 * _log_operation_sync, _log_delivery, _insert_operation_log), including the
 * internal-code-looking ones, so the UI's translation layer gets real input.
 */
import type { OperationLog } from '../api/client';
import { findTeamBySlug, type DemoDb, type DemoTeam } from './db';
import { DAY, HOUR, MINUTE, isoAt, shanghaiIso } from './time';

/** One `operation_logs` row. Team name/remark/owner/status are joined when read. */
export type DemoLogRow = Pick<
  OperationLog,
  'id' | 'team_id' | 'action' | 'target_email' | 'detail' | 'result' | 'error_message' | 'trigger_type' | 'created_at'
>;

type LogInput = Omit<DemoLogRow, 'id' | 'created_at'>;

/** Appends a log row for a mutation that just happened in the demo. */
export function appendLog(db: DemoDb, row: Partial<LogInput> & { action: string }): void {
  const id = (db.logs[0]?.id ?? 0) + 1;
  db.logs.unshift({
    id,
    team_id: row.team_id ?? null,
    action: row.action,
    target_email: row.target_email ?? null,
    detail: row.detail ?? null,
    result: row.result ?? 'success',
    error_message: row.error_message ?? null,
    trigger_type: row.trigger_type ?? 'manual',
    created_at: isoAt(Date.now()),
  });
}

/** `/api/logs?scope=members` filter, same set as backend/app/routes/logs.py. */
const MEMBER_ACTIONS = new Set([
  'change_seat', 'create_tg_member_pairing_code', 'patrol_kick', 'patrol_revoke_invite', 'remove_expiry',
  'remove_member', 'revoke_invite', 'set_expiry', 'extend_expiry', 'update_user_display_name',
]);
const MEMBER_ACTION_PREFIXES = [
  'auto_kick', 'auto_revoke_invite', 'invite_', 'member_', 'patrol_strict_', 'patrol_would_', 'self_service_',
];

export function isMemberLog(action: string | null): boolean {
  if (!action) return false;
  return MEMBER_ACTIONS.has(action) || MEMBER_ACTION_PREFIXES.some((prefix) => action.startsWith(prefix));
}

/** Short fake diagnostics in the `format_refresh_diagnostics` layout. */
function refreshDiag(http: number, access: 'present' | 'absent', now: number): string {
  const exp = Math.floor((now + 9 * DAY) / 1000);
  return (
    `diag: http=${http} keys=${http === 200 ? 'accessToken,account,expires,user' : '-'} ` +
    `err=${http === 200 ? '-' : 'RefreshAccessTokenError'} access=${access} ` +
    `resp_iat=${access === 'present' ? Math.floor(now / 1000) : '-'} resp_exp=${access === 'present' ? exp : '-'} ` +
    `fin=0 cur_exp=${exp - 86_400} cur_fin=0 set_cookies=__Secure-next-auth.session-token ` +
    `sc_session=cookie:00aa11bb sc_cleared=0 json_session=- sent_session=22cc33dd ` +
    `sc_eq_json=- sc_eq_sent=0 json_eq_sent=-`
  );
}

export function buildLogs(db: DemoDb): DemoLogRow[] {
  const { now } = db;
  const rows: Array<LogInput & { at: number }> = [];
  const T = (slug: string): DemoTeam => findTeamBySlug(db, slug);
  const id = (slug: string) => T(slug).team.id;
  const log = (minutesAgo: number, row: Partial<LogInput> & { action: string }) => {
    rows.push({
      team_id: row.team_id ?? null,
      action: row.action,
      target_email: row.target_email ?? null,
      detail: row.detail ?? null,
      result: row.result ?? 'success',
      error_message: row.error_message ?? null,
      trigger_type: row.trigger_type ?? 'manual',
      at: now - minutesAgo * MINUTE,
    });
  };
  const H = 60;
  const D = 24 * H;
  const lyra = id('lyra');
  const polaris = id('polaris');
  const sandbox = id('sandbox');

  // ── Scheduled data sync, twice a day, plus the patrol summary when some teams were not refreshed ──
  for (let ago = 7; ago < 7 * D; ago += 12 * H) {
    let detail: string;
    let result = 'success';
    let error: string | null = null;
    let notPatrolled: string[] = [];
    if (ago < 300) {
      detail = 'Synced 7/9 teams; failed=1; suspended=1';
      result = 'failed';
      error = polaris;
      notPatrolled = [lyra, polaris].sort();
    } else if (ago < 50 * H) {
      detail = 'Synced 8/9 teams; failed=0; suspended=1';
      notPatrolled = [lyra];
    } else if (ago < 74 * H) {
      detail = 'Synced 8/9 teams; failed=1; suspended=0';
      result = 'failed';
      error = lyra;
      notPatrolled = [lyra];
    } else if (ago < 4 * D) {
      detail = 'Synced 9/9 teams; failed=0; suspended=0';
    } else {
      detail = 'Synced 10/10 teams; failed=0; suspended=0';
    }
    log(ago, { action: 'data_sync', detail, result, error_message: error, trigger_type: 'scheduler' });
    if (notPatrolled.length > 0) {
      const patrolled = 9 - notPatrolled.length;
      log(ago - 1, {
        action: 'patrol_partial_skip',
        detail: `patrolled ${patrolled} freshly synced team(s); ${notPatrolled.length} team(s) not refreshed this round were left alone`,
        error_message: notPatrolled.join(','),
        trigger_type: 'scheduler',
      });
    }
  }

  // ── Lyra: sub-interfaces failing, then suspended after 24h ──
  [74 * H, 66 * H, 58 * H, 51 * H].forEach((ago) => {
    log(ago, {
      team_id: lyra, action: 'data_sync', detail: 'scheduled', result: 'partial',
      error_message: 'overview sub-interface failures: subscription, balance', trigger_type: 'scheduler',
    });
  });
  log(74 * H - 3, {
    team_id: lyra, action: 'team_health_alert', detail: 'key=team_sync, source=team_sync, delivered_to=2',
    trigger_type: 'team_health',
  });
  log(50 * H, {
    team_id: lyra, action: 'data_sync_suspended',
    detail: 'sync suspended after 24h of continuous failure; probing every 6h', trigger_type: 'scheduler',
  });
  [44 * H, 20 * H + 30].forEach((ago) => {
    log(ago, {
      team_id: lyra, action: 'data_sync', detail: 'member snapshot refresh failed', result: 'failed',
      error_message: 'members: HTTP 502 Bad Gateway', trigger_type: 'scheduler',
    });
  });

  // ── Sandbox: session expired four days ago ──
  log(4 * D, {
    team_id: sandbox, action: 'token_auto_refresh', result: 'failed', trigger_type: 'auto_refresh',
    detail: `trigger=api_401_retry; access_changed=0; ${refreshDiag(401, 'absent', now - 4 * DAY)}`,
    error_message: 'session expired (HTTP 401)',
  });
  log(4 * D - 2, {
    team_id: sandbox, action: 'team_health_alert', detail: 'key=chatgpt_auth, source=team_sync, delivered_to=2',
    trigger_type: 'team_health',
  });

  // ── Polaris: session still answers but the access token was revoked ──
  const polarisAt = now - 5 * HOUR;
  log(5 * H, {
    team_id: polaris, action: 'token_auto_refresh', result: 'unchanged', trigger_type: 'auto_refresh',
    detail:
      `trigger=api_401_retry; access_changed=0; session_changed=0; access_iat=${isoAt(polarisAt - 2 * DAY)}; ` +
      `access_exp=${isoAt(polarisAt + 8 * DAY)}; ${refreshDiag(200, 'present', polarisAt)}`,
  });
  log(5 * H - 2, {
    team_id: polaris, action: 'team_health_alert', detail: 'key=chatgpt_auth, source=team_sync, delivered_to=0',
    result: 'failed', error_message: 'Telegram alert was not delivered', trigger_type: 'team_health',
  });
  log(4 * H + 50, {
    team_id: polaris, action: 'sync_team', result: 'failed',
    error_message: "{'code': 'team_auth_rejected', 'message': '登录已失效，请重新导入'}",
  });

  // ── Proactive token refreshes on healthy teams ──
  (['aurora', 'nebula', 'tokyo', 'bangkok', 'orion', 'berlin'] as const).forEach((slug, i) => {
    const at = now - (8 + i * 23) * HOUR;
    log((8 + i * 23) * H, {
      team_id: id(slug), action: 'token_proactive_refresh', trigger_type: 'auto_refresh',
      detail:
        `trigger=scheduled_expiry_refresh; access_changed=1; session_changed=1; access_iat=${isoAt(at)}; ` +
        `access_exp=${isoAt(at + 10 * DAY)}; ${refreshDiag(200, 'present', at)}`,
    });
  });

  // ── Telegram: daily summaries until the bot was switched off ──
  [6.6, 5.6, 4.6, 3.625].forEach((days) => {
    log(days * D, { action: 'tg_summary', detail: 'delivered_to=2', trigger_type: 'scheduler' });
  });
  [5.5, 4.5].forEach((days) => {
    log(days * D, { action: 'tg_member_expiry_reminder', detail: 'sent=2, due=2', trigger_type: 'scheduler' });
  });
  log(5.1 * D, {
    action: 'create_tg_member_pairing_code', target_email: 'kevin@example.com',
  });
  log(3.4 * D, { action: 'update_tg_config', detail: 'enabled=False, summary_enabled=False' });

  // ── Invites sent from the panel, and the watcher confirming acceptance ──
  db.teams.forEach((record) => {
    record.invites.forEach((inv) => {
      const ago = Math.round((now - Date.parse(inv.created_time)) / MINUTE);
      if (inv.source === 'self_service') {
        log(ago, {
          team_id: record.team.id, action: 'self_service_invite', target_email: inv.email,
          detail: `expires_at=${inv.expires_at}`,
        });
        return;
      }
      log(ago, {
        team_id: record.team.id, action: 'invite_member', target_email: inv.email,
        detail: `seat_type=${inv.seat_type}, expires_in=30d, allow_overage=False`,
      });
      if (ago > 30) {
        log(ago - 30, {
          team_id: record.team.id, action: 'member_watch_invite', target_email: inv.email,
          detail: 'timed_out=True', trigger_type: 'scheduler',
        });
      }
    });
    record.members.forEach((m) => {
      const ago = Math.round((now - Date.parse(m.created_time)) / MINUTE);
      if (m.is_owner || m.source === 'detected' || ago > 7 * D) return;
      log(ago + 12, {
        team_id: record.team.id, action: 'invite_member', target_email: m.email,
        detail: `seat_type=${m.seat_type}, expires_in=${m.expires_at ? '30d' : 'never'}, allow_overage=False`,
      });
      log(ago, {
        team_id: record.team.id, action: 'member_watch_invite', target_email: m.email,
        detail: 'timed_out=False', trigger_type: 'scheduler',
      });
    });
  });

  // ── Members who left: auto-expiry, admin kicks, patrol, upstream removal ──
  db.teams.forEach((record) => {
    record.kicked.forEach((k) => {
      const ago = Math.round((now - Date.parse(k.kicked_at)) / MINUTE);
      if (ago > 7 * D) return;
      const team_id = record.team.id;
      if (k.kick_source === 'auto_expire') {
        log(ago, { team_id, action: 'auto_kick', target_email: k.email, detail: `user_id=${k.user_id}`, trigger_type: 'scheduler' });
      } else if (k.kick_source === 'admin') {
        log(ago, { team_id, action: 'remove_member', target_email: k.email, detail: `user_id=${k.user_id}` });
        log(ago - 4, { team_id, action: 'member_watch_kick', target_email: k.email, detail: 'timed_out=False', trigger_type: 'scheduler' });
      } else if (k.kick_source === 'patrol') {
        log(ago + 31, { team_id, action: 'member_detect', detail: 'detected 1 untracked member(s)', trigger_type: 'scheduler' });
        log(ago, { team_id, action: 'patrol_kick', target_email: k.email, detail: `user_id=${k.user_id}`, trigger_type: 'patrol' });
      } else {
        log(ago, { team_id, action: 'member_detect_absent', detail: 'marked 1 absent member(s) as kicked', trigger_type: 'scheduler' });
      }
    });
  });
  log(3.2 * D, {
    team_id: id('tokyo'), action: 'auto_revoke_invite', target_email: 'guest.tokyo@example.com',
    detail: 'pending invite revoked', trigger_type: 'scheduler',
  });

  // Expired members that could not be removed because their team is unreachable.
  (['lyra', 'polaris'] as const).forEach((slug) => {
    const record = T(slug);
    const expired = record.members.find((m) => m.expires_at && Date.parse(m.expires_at) < now);
    if (!expired) return;
    const ago = Math.round((now - Date.parse(expired.expires_at!)) / MINUTE) - 3;
    log(ago, {
      team_id: record.team.id, action: 'auto_kick', target_email: expired.email, detail: `user_id=${expired.id}`,
      result: 'failed', trigger_type: 'scheduler',
      error_message: slug === 'lyra' ? 'HTTP 502: upstream unavailable' : 'HTTP 401: access token revoked',
    });
  });

  // ── Patrol on Orion Lab: someone was added in ChatGPT directly, a dry run flagged them ──
  const orion = T('orion');
  const newcomer = orion.members.find((m) => m.source === 'detected');
  if (newcomer) {
    log(38, { team_id: orion.team.id, action: 'member_detect', detail: 'detected 1 untracked member(s)', trigger_type: 'scheduler' });
    log(35, {
      team_id: orion.team.id, action: 'patrol_would_kick', target_email: newcomer.email, result: 'dryrun',
      trigger_type: 'patrol',
      detail: `over_by=1 position=1/1 seat_type=default source=detected first_seen_at=${newcomer.first_seen_at}`,
    });
    log(35, {
      action: 'patrol_manual_run', detail: 'patrolled=8, skipped_unrefreshed=1',
      error_message: `${lyra}: sync suspended`,
    });
  }
  log(4.1 * D, { action: 'update_patrol_settings', detail: 'exempt_team_ids_count=2' });

  // ── Admin edits on members ──
  const pick = (slug: string, offset: number) => {
    const list = T(slug).members.filter((m) => !m.is_owner);
    return list[offset % list.length];
  };
  const seatEdits: Array<[string, number, number]> = [['aurora', 4, 26], ['tokyo', 3, 51], ['berlin', 2, 99]];
  seatEdits.forEach(([slug, offset, hoursAgo]) => {
    const m = pick(slug, offset);
    log(hoursAgo * H, {
      team_id: id(slug), action: 'change_seat', target_email: m.email, detail: `user_id=${m.id}, seat_type=${m.seat_type}`,
    });
  });
  const dated = (slug: string) => T(slug).members.filter((m) => !m.is_owner && m.expires_at);
  const tokyoDated = dated('tokyo')[1] ?? dated('tokyo')[0];
  if (tokyoDated) {
    log(30 * H, {
      team_id: id('tokyo'), action: 'set_expiry', target_email: tokyoDated.email,
      detail: `user_id=${tokyoDated.id}, expires_at=${shanghaiIso(Date.parse(tokyoDated.expires_at!))}`,
    });
  }
  [['aurora', 3, 7], ['bangkok', 1, 77]].forEach(([slug, offset, hoursAgo], i) => {
    const m = dated(slug as string)[offset as number] ?? dated(slug as string)[0];
    if (!m) return;
    log((hoursAgo as number) * H, {
      team_id: id(slug as string), action: 'extend_expiry', target_email: m.email,
      detail: `user_id=${m.id}, expires_in=30d, request_id=00000000-0000-4000-8000-0000000000a${i + 1}`,
    });
  });
  const permanent = T('nebula').members.find((m) => !m.is_owner && !m.expires_at && m.source === 'system');
  if (permanent) {
    log(5.3 * D, { team_id: id('nebula'), action: 'remove_expiry', target_email: permanent.email, detail: `user_id=${permanent.id}` });
  }
  log(2.6 * D, { team_id: id('bangkok'), action: 'revoke_invite', target_email: 'typo.bangkok@example.com' });
  const remarked = [...db.remarks.entries()].filter(([email]) => !email.startsWith('owner-')).slice(3, 6);
  remarked.forEach(([email, name], i) => {
    log([1.4, 2.1, 6.3][i] * D, { action: 'update_user_display_name', target_email: email, detail: `new_name=${name}` });
  });
  const auroraMembers = T('aurora').members.filter((m) => !m.is_owner);
  log(5.8 * D, {
    team_id: id('aurora'), action: 'invite_gpt_member', target_email: 'temp.batch1@example.com',
    detail: `expires_at=${isoAt(now - 5.8 * DAY + 7 * DAY)}`,
  });
  log(5.8 * D - 1, {
    team_id: id('aurora'), action: 'invite_gpt_member_existing', target_email: auroraMembers[6]?.email ?? null,
    detail: '邮箱已在该 Team 中，未重复邀请', result: 'skipped',
  });

  // ── Overage policy and Premium (every detail key follows contract §3.8) ──
  log(2.2 * D, { team_id: id('nebula'), action: 'set_overage_policy', detail: 'overage_policy=forbid, previous=confirm' });
  log(3.5 * D, { team_id: id('helix'), action: 'set_overage_policy', detail: 'overage_policy=auto, previous=confirm' });
  log(1.5 * D, { team_id: id('quasar'), action: 'set_overage_policy', detail: 'overage_policy=forbid, previous=auto' });
  log(2.1 * D, {
    team_id: id('nebula'), action: 'invite_member', target_email: 'new.hire@example.com', result: 'skipped',
    detail: 'seat_type=default, policy=forbid, reason=overage_forbidden',
  });
  log(1.3 * D, {
    team_id: id('atlas'), action: 'invite_member', target_email: 'intern.2026@example.com', result: 'skipped',
    detail: 'seat_type=default, policy=confirm, reason=overage_needs_confirmation',
  });
  const quasarCodex = T('quasar').members.find((m) => m.seat_type === 'usage_based');
  if (quasarCodex) {
    log(0.6 * D, {
      team_id: id('quasar'), action: 'change_seat', target_email: quasarCodex.email, result: 'skipped',
      detail: `user_id=${quasarCodex.id}, seat_type=default, from_seat_type=usage_based, allow_overage=False, policy=forbid, reason=overage_forbidden`,
    });
  }
  log(3.0 * D, {
    team_id: id('helix'), action: 'invite_member', target_email: 'extra.seat@example.com',
    detail: 'seat_type=default, expires_in=30d, allow_overage=False, policy=auto, overage=True',
  });
  const zenithPremium = T('zenith').members.find((m) => m.seat_type === 'prolite' && !m.is_owner);
  if (zenithPremium) {
    log(4.4 * D, {
      team_id: id('zenith'), action: 'change_seat', target_email: zenithPremium.email,
      detail: `user_id=${zenithPremium.id}, seat_type=prolite, from_seat_type=default, allow_overage=False, policy=confirm`,
    });
    log(0.9 * D, {
      team_id: id('zenith'), action: 'redeem_seat_type_mismatch', target_email: zenithPremium.email, result: 'failed',
      detail: 'seat_type=default, from_seat_type=prolite',
      error_message: '兑换码是 ChatGPT 码，你当前是 Premium 席位，不能用它续期。',
    });
  }
  log(0.35 * D, {
    action: 'redeem_no_premium_seat', target_email: 'wait.premium@example.com', result: 'failed',
    detail: 'seat_type=prolite', error_message: '没有可用 Premium 席位，兑换码未使用',
  });
  const quasarPremium = T('quasar').members.find((m) => m.seat_type === 'prolite');
  if (quasarPremium) {
    log(35 * H, {
      team_id: id('quasar'), action: 'patrol_premium_alert', target_email: quasarPremium.email,
      detail: `seat_type=prolite, source=detected, user_id=${quasarPremium.id}, delivered_to=2`, trigger_type: 'patrol',
    });
  }

  // ── Self-service redemptions ──
  db.tokenUses.forEach((use) => {
    const ago = Math.round((now - Date.parse(use.created_at)) / MINUTE);
    if (ago > 7 * D || !use.team_id) return;
    if (use.result === 'uncertain') {
      log(ago, {
        team_id: use.team_id, action: 'self_service_invite', target_email: use.email, detail: 'seat_type=default',
        result: 'uncertain', error_message: use.error_message,
      });
    } else if (use.action === 'invited') {
      log(ago, { team_id: use.team_id, action: 'self_service_invite', target_email: use.email, detail: `expires_at=${use.expires_at}` });
    } else {
      log(ago, {
        team_id: use.team_id, action: 'self_service_renew', target_email: use.email,
        detail: `action=${use.action}, expires_at=${use.expires_at}`,
      });
    }
  });
  log(4.2 * D, {
    action: 'self_service_redeem', detail: 'reason=no_available_seat', result: 'failed',
    error_message: '没有可用 ChatGPT 席位，请联系管理员',
  });
  log(5 * D, {
    team_id: id('tokyo'), action: 'self_service_invite_reconciled', target_email: 'late.accept@example.com',
    detail: 'token_use_id=88', trigger_type: 'scheduler',
  });
  log(5 * D - 1, {
    action: 'pending_redemption_reconciliation', detail: 'confirmed=1, released=0, waiting=0', trigger_type: 'scheduler',
  });
  log(3.1 * D, { team_id: id('tokyo'), action: 'invite_reconciliation', detail: 'reconciled 1 confirmed invite(s)', trigger_type: 'scheduler' });

  // ── Panel housekeeping ──
  [0.08, 1.2, 3.3, 6.1].forEach((days) => log(days * D, { action: 'admin_login', detail: 'ip=203.0.113.24' }));
  log(2.4 * D, {
    action: 'admin_login', detail: 'ip=198.51.100.7, failures_remaining=4', result: 'failed', error_message: 'Invalid password',
  });
  log(2.9 * D, { team_id: id('vega'), action: 'update_team_remark', detail: 'remark_len=4' });
  log(5.2 * D, { team_id: id('aurora'), action: 'change_default_seat_type', detail: 'seat_type=usage_based' });
  log(1.8 * D, { action: 'update_card_note', detail: 'card_key=visa:0000, note_len=5' });
  log(6 * H + 12, { action: 'refresh_fx_rates', detail: 'updated_currencies=21' });
  log(2.2 * D, {
    team_id: id('bangkok'), action: 'data_sync_display_degraded', detail: 'display-only failures: payment_methods',
    result: 'warning', trigger_type: 'scheduler',
  });
  log(1.1 * D, {
    action: 'sync_all', detail: 'Synced 8/9 teams; failed=1', result: 'failed', error_message: lyra,
  });
  ([['aurora', 0.3, true], ['tokyo', 2.5, true], ['nebula', 9.5, false], ['orion', 33, true], ['bangkok', 50, false],
    ['berlin', 70, true], ['vega', 101, false], ['aurora', 130, true]] as const).forEach(([slug, hoursAgo, force]) => {
    log(hoursAgo * H, { team_id: id(slug), action: 'sync_team', detail: force ? 'force=true' : 'ttl_expired' });
  });

  rows.sort((a, b) => a.at - b.at);
  return rows
    .map((row, index) => {
      const { at, ...rest } = row;
      return { id: index + 1, ...rest, created_at: isoAt(at) };
    })
    .reverse();
}
