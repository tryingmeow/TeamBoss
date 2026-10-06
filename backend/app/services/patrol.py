"""巡逻踢人引擎 — 自动发现并清理超额占用的"检测到"成员。

最高优先级：绝不误踢付费成员。宁可漏踢也不可错踢。

设计要点：
- 只在以下条件全部成立时才会把某个成员当作"候选":
    1. Team is_codex_enabled == 0（codex 开的 Team 完全跳过踢人，只算风险）
    2. Team id 不在 settings.patrol_exempt_team_ids（豁免名单）里
    3. member_cache 有数据且非空（冷启动/缓存缺失一律跳过整队，绝不动手）
    4. active_chatgpt > seats_entitled（over_by > 0，否则最多是 watch，不踢；
       seats_entitled 不是正整数 = 席位数未知，该 Team 本轮不做超员踢人）
    5. member.seat_type == 'default' 且不是 Owner（is_owner is False，且邮箱不等于 teams.owner_email，
       不分大小写——和 scheduler 同步时认 Owner 的规则相同；两条踢人路径、候选筛选和 _patrol_kick 都认这两样）
    6. member.source == 'detected'（唯一硬规则；'system'/'self_service'/None 永不踢，
       管理员开启自动踢人时会先把当时的现有成员全部保护起来；巡逻自动给 Team 建立
       基线时只有该 Team 第一次才保护，token_expired 恢复/重新导入后的自动重建不保护
       ——见 _protect_team_snapshot_sync）
    7. TeamBoss 没有正在管他、也不是因为他不在名单里才没在管、他也没在这个 Team 兑换过
       （member_expiry 里 system / self_service 的行：开着的，或被同步因"名单里不见了"关掉的
       kick_source='detected'；成功 / 待定 / 处理中的兑换）。付费成员掉出一次完整名单后再出现，会是
       一条新的 detected 记录；这样的人不踢，只进限频提醒。到期踢掉、管理员移出的老用户之后从
       TeamBoss 外面再进来，是普通外部成员（_teamboss_managed_history_sync）
  按 first_seen_at（缺失则退回 created_time）新→旧排序，先取最新的 over_by 个，再去掉第 7 条保护的人；
  空出的名额不往后补（select_over_quota_kick_candidates_sync），所以最多踢 over_by 个。over_by 的
  算法不变。超员踢人不套 Premium 的"外部成员过多"护栏（小 Team 上它会拦下每一次正当的超员踢人），
  数量由 over_by 和单轮封顶 NON_STRICT_KICK_ABS_CAP 管着。
- dry-run 判定：effective_dry_run = dry_run 参数 OR settings.patrol_kick_enabled != '1'。
- 真正执行踢人只能通过 `_patrol_kick()` 这一个函数：进函数先重新校验候选资格
  （source/is_owner/owner_email/seat_type），任一不满足直接拒绝、不踢 —— 纵深防御，防止上游逻辑
  有 bug 时仍然误踢。
- 实时踢除完全复用 scheduler.auto_kick_job 的机制：ChatGPTClient.remove_member ->
  mark_member_kicked（此处用与 services/member_expiry.mark_member_kicked 完全等价的
  同步 SQL，因为本函数运行在没有事件循环的调度线程里）-> _log_operation_sync。
- 本模块自成一体（同步 sqlite3，不依赖运行中的 event loop），可以直接被
  scheduler.py（APScheduler 线程）和 routes/patrol.py（经 asyncio.to_thread）复用。

在上述"超员才踢"的基础逻辑之外，本模块还提供两项跟随巡逻武装状态生效的能力：

- **陌生 pending invite 自动撤销**：只要某个 team 处于"监控中"（已武装 patrol_
  kick_enabled=='1' + 全局 baseline 就绪 + 该 team 已完成基线保护 + 未豁免 + active），
  巡逻就会顺带把 source=='detected' 的陌生邀请一并撤销 —— 判定规则与"只踢 detected
  成员"完全对称，无需额外开关（撤错了重发一次即可，风险远低于踢人）。
- **严格模式**（settings.patrol_strict_mode_enabled，默认 '0'，独立危险开关）：开启后
  忽略超员判定和 Codex 豁免，把"所有 team + 非系统拉进来的人"一律列为候选，同时叠加多层
  护栏（见 `_patrol_strict_kick` 与 run_patrol 中的严格模式小节）：基线前的人永不碰、有到期
  记录的人永不碰、复用现有踢人延迟让管理员有反悔窗口、动手前对目标 team 强制实时刷新一次、
  单轮候选量异常多时只报警不动手、动手前再查一次本地记录确认不是系统自己拉的人。

席位类型（注册表 app/seat_types.py）：
- 超员踢人只看 ChatGPT（default）席位，判定公式不变。
- **Premium 外部成员自动踢**（所有者裁决，唯一一条按席位类型新增的踢人路径）：巡逻对该
  Team 生效（总开关开、已建基线、未豁免、没开 Codex——Codex 队和以前一样不踢人）时，
  source == 'detected' 的非 Owner（认法同第 5 条）Premium（prolite）成员不看超员、不看严格模式直接踢——每人
  都是 ChatGPT 自动加购、按月扣费的席位。真踢仍只走 `_patrol_kick`（rule="premium_outsider"），
  除席位类型 / 超员这一关外闸门与超员踢人相同。一个 Team 一轮在这条路和超员踢人上一共最多
  踢 NON_STRICT_KICK_ABS_CAP 个；并套用严格模式的"别一次踢一片"阈值（只有这条路套），但数的是
  这份名单里全部外部成员（不看席位类型、不看严格模式开没开），超了这一轮这个 Team 一个 Premium
  成员都不踢、只提醒。TeamBoss 对这个人在这个 Team 有任何改席位 / 邀请记录（任何结果、任何目标
  席位，含切回 ChatGPT、超时 / 失败、被超员策略拒绝的）或 Premium 兑换，或者超员踢人第 7 条保护
  他，都不踢、只提醒；快照开始拉取之后 TeamBoss 动过他的席位，这一轮不踢。日志沿用 patrol_kick /
  patrol_would_kick / patrol_kick_batch_capped，detail 带 seat_type=prolite、reason=premium_outsider。
- 注册表外的类型（如 automation）任何模式下都不踢、不撤。严格模式不碰 Premium（交给上一条），
  它的"别一次踢一片"护栏仍按全部疑似陌生成员计数。
- 没被处理的 Premium 外部成员（巡逻没开、豁免 / Codex 队、被拦下）、TeamBoss 成员被切到 Premium 但不是
  TeamBoss 切的、注册表外的席位类型：只发 Telegram 提醒（按 team_health_incidents 去重限频）。
  见 premium_seat_findings_sync。
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from ..chatgpt_client import ChatGPTClient
from ..chatgpt_limiter import run_chatgpt_call_sync
from ..database import get_db_path
from ..member_cache_service import snapshot_fetch_started_now, store_member_snapshot_sync
from ..seat_types import (
    DEFAULT_SEAT_TYPE,
    PREMIUM_SEAT_TYPE,
    is_known_seat_type,
    normalize_seat_type,
    seat_type_label,
)
from ..tg_format import detail_card
from .member_expiry import compute_effective_kick_at, normalize_kick_mode
from .seat_capacity import member_seat_usage_from_members, positive_seat_count
from .snapshot_pages import SnapshotPageAccumulator, SnapshotPageError
from .tg_member_bindings import deactivate_member_binding_if_inactive_sync
from .tg_commands import sync_email_chat_commands_sync
from .tg_notify import notify_admins_sync
from .team_health_alerts import close_incident_family_sync, report_team_failure_sync
from .team_locks import member_operation_claim_sync

# 向后兼容：模块级占位符，测试可能会 patch 它
DB_PATH = None


class PatrolActivationError(RuntimeError):
    """开启巡逻前无法建立完整、可信的现有成员快照。"""


# ── 基础 DB 原语（镜像 scheduler.py 的写法，自包含，短连接） ─────────────────

def _get_sync_db() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _log_operation_sync(team_id, action, target_email=None, detail=None,
                         result=None, error_message=None, trigger_type="patrol"):
    try:
        conn = _get_sync_db()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO operation_logs
               (team_id, action, target_email, detail, result, error_message, trigger_type, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (team_id, action, target_email, detail, result, error_message, trigger_type, now)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _get_proxy_url_sync(conn: sqlite3.Connection, proxy_id) -> Optional[str]:
    if not proxy_id:
        return None
    try:
        row = conn.execute("SELECT url FROM proxies WHERE id = ?", (proxy_id,)).fetchone()
        return row["url"] if row else None
    except Exception:
        return None


def _mark_member_kicked_sync(conn: sqlite3.Connection, team_id: str, kick_source: str,
                              user_id: str = "", email: str = "") -> None:
    """与 services/member_expiry.mark_member_kicked 完全等价的同步实现。

    之所以不直接 await 那个 async 版本，是因为本模块运行在调度线程 /
    asyncio.to_thread 的普通线程里，没有可复用的事件循环，且已经持有一个
    同步 sqlite3 连接（与 scheduler.auto_kick_job 的自有 _mark_expiry_done
    是同一种做法）。SQL 语义与 async 版本逐字对齐。
    """
    normalized_email = (email or "").strip().lower()
    normalized_user_id = user_id or ""
    now = datetime.now(timezone.utc).isoformat()

    cursor = conn.execute(
        """UPDATE member_expiry
           SET kicked = 1, kicked_at = ?, kick_source = ?
           WHERE team_id = ?
             AND kicked = 0
             AND ((? != '' AND user_id = ?) OR (? != '' AND lower(email) = ?))""",
        (now, kick_source, team_id,
         normalized_user_id, normalized_user_id,
         normalized_email, normalized_email),
    )
    if cursor.rowcount == 0:
        conn.execute(
            """INSERT INTO member_expiry
               (team_id, user_id, email, expires_at, auto_kick, kicked, kicked_at,
                kick_source, first_seen_at, source, created_at)
               VALUES (?, ?, ?, NULL, 0, 1, ?, ?, ?, 'system', ?)""",
            (team_id, normalized_user_id, normalized_email, now, kick_source, now, now),
        )
    deactivate_member_binding_if_inactive_sync(conn, normalized_email, now_iso=now)
    conn.commit()
    sync_email_chat_commands_sync(normalized_email, conn=conn)


def _read_patrol_settings(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT key, value FROM settings WHERE key IN "
        "('patrol_kick_enabled', 'patrol_baseline_at', 'patrol_exempt_team_ids', "
        " 'patrol_strict_mode_enabled')"
    ).fetchall()
    return {row["key"]: row["value"] for row in rows}


def _read_kick_delay_settings_sync(conn: sqlite3.Connection) -> tuple[str, int]:
    """读取"复用现有的踢人延迟设置"——和到期自动踢人共用同一组 settings。"""
    rows = conn.execute(
        "SELECT key, value FROM settings WHERE key IN "
        "('expiry_kick_mode', 'expiry_kick_delay_hours')"
    ).fetchall()
    settings = {row["key"]: row["value"] for row in rows}
    mode = normalize_kick_mode(settings.get("expiry_kick_mode"))
    try:
        delay_hours = int(settings.get("expiry_kick_delay_hours") or 0)
    except (TypeError, ValueError):
        delay_hours = 0
    delay_hours = min(max(delay_hours, 0), 720)
    return mode, delay_hours


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_exempt_team_ids(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(x) for x in parsed if x is not None]


def _ensure_team_baseline_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS patrol_team_baselines (
               team_id TEXT PRIMARY KEY,
               baseline_at TEXT NOT NULL
           )"""
    )


def _protect_team_snapshot_sync(
    conn: sqlite3.Connection,
    team_id: str,
    now: str,
    *,
    protect_detected: bool,
) -> tuple[int, int, int]:
    """为一个 Team 建立巡逻基线（成员快照必须完整），返回 (grandfathered, backfilled, detected_kept)。

    保护当前成员 = grandfather（把该 Team 未踢的 detected 行转成 system）+ backfill
    （快照里完全没有记录的成员/邀请补一条 system 行）。是否做取决于谁在建基线：
    - protect_detected=True：管理员显式开启自动踢人（activate_patrol_sync，网页和
      Telegram /patrol on 都走这里）。按开启时的承诺保护当前全部成员，每次开启都做。
    - protect_detected=False：巡逻自动建基线（run_patrol 给新 Team、token_expired
      恢复或重新导入后丢了基线的 Team 初始化）。只有该 Team 第一次建基线时才做——
      sync 不管巡逻开没开都会给未知成员建 detected 行，第一次纳入时他们就是现有成员。
      之后的自动重建两样都不做：没人确认过这些人，不能静默把他们洗白成永久成员
      （detected + NULL 到期 = 未授权；system + NULL 到期 = 永久）。已有的 detected 行
      保持 detected，人数作为 detected_kept 返回并记进日志；没有记录的人（快照比同步
      建档新，例如管理端手动刷新写的快照）保持无记录，下一轮同步按 detected 建档。
    teams.patrol_grandfathered_at 记录第一次 grandfather 的时间，非空 = 已经做过。标记跟着
    teams 行走：token_expired 和重新导入只 UPDATE 这一行，标记保留；删除 Team 会删掉整行，
    重新添加的 Team 视为第一次。
    """
    team_row = conn.execute(
        "SELECT patrol_grandfathered_at FROM teams WHERE id = ?", (team_id,)
    ).fetchone()
    if not team_row:
        raise PatrolActivationError(f"{team_id} 不存在")
    first_baseline = not (team_row["patrol_grandfathered_at"] or "").strip()

    cache_row = conn.execute(
        "SELECT members_json, pending_json FROM member_cache WHERE team_id = ?",
        (team_id,),
    ).fetchone()
    if not cache_row:
        raise PatrolActivationError(f"{team_id} 缺少成员快照")
    try:
        members = json.loads(cache_row["members_json"] or "[]")
        pending = json.loads(cache_row["pending_json"] or "[]")
    except Exception as exc:
        raise PatrolActivationError(f"{team_id} 成员快照损坏") from exc
    if not isinstance(members, list) or not members:
        raise PatrolActivationError(f"{team_id} 成员快照为空")
    if not isinstance(pending, list):
        raise PatrolActivationError(f"{team_id} 邀请快照损坏")

    # 自动重建基线（已 grandfather 过的 Team）既不转 detected 也不补 system 行。补行同样是
    # 永久保护：同步看到已有记录就不会再把这个人建成 detected，巡逻永远碰不到他。
    protect_current = protect_detected or first_baseline

    grandfathered = 0
    if protect_current:
        cursor = conn.execute(
            "UPDATE member_expiry SET source = 'system' "
            "WHERE team_id = ? AND source = 'detected' AND kicked = 0",
            (team_id,),
        )
        grandfathered = cursor.rowcount
    if first_baseline:
        conn.execute(
            "UPDATE teams SET patrol_grandfathered_at = ? WHERE id = ?",
            (now, team_id),
        )

    backfilled = 0
    if protect_current:
        tracked_rows = conn.execute(
            "SELECT user_id, email FROM member_expiry WHERE team_id = ? AND kicked = 0",
            (team_id,),
        ).fetchall()
        tracked_ids = {row["user_id"] for row in tracked_rows if row["user_id"]}
        tracked_emails = {
            row["email"].lower() for row in tracked_rows if row["email"]
        }

        for item in members + pending:
            if not isinstance(item, dict) or item.get("is_owner"):
                continue
            user_id = item.get("id") or item.get("user_id") or ""
            email = (item.get("email") or "").strip().lower()
            if user_id in tracked_ids or (email and email in tracked_emails):
                continue
            if not user_id and not email:
                continue
            conn.execute(
                """INSERT INTO member_expiry
                   (team_id, user_id, email, expires_at, auto_kick, kicked,
                    first_seen_at, source, created_at)
                   VALUES (?, ?, ?, NULL, 0, 0, ?, 'system', ?)""",
                (team_id, user_id, email, now, now),
            )
            backfilled += 1
            if user_id:
                tracked_ids.add(user_id)
            if email:
                tracked_emails.add(email)

    source_rows = conn.execute(
        "SELECT user_id, email, source, first_seen_at, expires_at "
        "FROM member_expiry WHERE team_id = ? AND kicked = 0",
        (team_id,),
    ).fetchall()
    source_map: dict[str, sqlite3.Row] = {}
    for row in source_rows:
        if row["user_id"]:
            source_map[str(row["user_id"])] = row
        if row["email"]:
            source_map[str(row["email"]).lower()] = row
    detected_kept = 0
    for item in members + pending:
        if not isinstance(item, dict):
            continue
        source_row = source_map.get(str(item.get("id") or item.get("user_id") or ""))
        if source_row is None:
            source_row = source_map.get((item.get("email") or "").strip().lower())
        if source_row is not None:
            item["source"] = source_row["source"]
            item["first_seen_at"] = source_row["first_seen_at"]
            item["expires_at"] = source_row["expires_at"]
            if source_row["source"] == "detected" and not item.get("is_owner"):
                detected_kept += 1
    conn.execute(
        "UPDATE member_cache SET members_json = ?, pending_json = ? WHERE team_id = ?",
        (
            json.dumps(members, ensure_ascii=False),
            json.dumps(pending, ensure_ascii=False),
            team_id,
        ),
    )
    _ensure_team_baseline_table(conn)
    conn.execute(
        """INSERT INTO patrol_team_baselines (team_id, baseline_at) VALUES (?, ?)
           ON CONFLICT(team_id) DO UPDATE SET baseline_at = excluded.baseline_at""",
        (team_id, now),
    )
    return grandfathered, backfilled, detected_kept


def activate_patrol_sync(expected_team_ids: list[str]) -> dict:
    """原子地保护当前成员并开启巡逻自动踢人。

    只给管理员显式开启用（POST /api/patrol/activate：网页开启对话框、Telegram /patrol on）。
    调用方必须先实时刷新所有 active team。这里会再次验证 active team 集合和缓存，
    只有所有快照都完整时，才在同一事务中把当前成员标记为受信任并打开总开关。
    任一 team 缓存缺失/损坏都会整笔回滚，绝不留下半开启状态。
    """
    conn = _get_sync_db()
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_team_baseline_table(conn)
        active_rows = conn.execute(
            "SELECT id FROM teams WHERE status = 'active' ORDER BY id"
        ).fetchall()
        active_team_ids = [str(row["id"]) for row in active_rows]
        if set(active_team_ids) != {str(team_id) for team_id in expected_team_ids}:
            raise PatrolActivationError("active team 列表已变化，请重试")

        # 先完整验证，验证完成前不修改任何成员来源或开关。
        for team_id in active_team_ids:
            cache_row = conn.execute(
                "SELECT members_json, pending_json FROM member_cache WHERE team_id = ?",
                (team_id,),
            ).fetchone()
            if not cache_row:
                raise PatrolActivationError(f"{team_id} 缺少成员快照")
            try:
                members = json.loads(cache_row["members_json"] or "[]")
                pending = json.loads(cache_row["pending_json"] or "[]")
            except Exception as exc:
                raise PatrolActivationError(f"{team_id} 成员快照损坏") from exc
            if not isinstance(members, list) or not members:
                raise PatrolActivationError(f"{team_id} 成员快照为空")
            if not isinstance(pending, list):
                raise PatrolActivationError(f"{team_id} 邀请快照损坏")
        grandfathered = 0
        backfilled = 0
        for team_id in active_team_ids:
            protected, inserted, _ = _protect_team_snapshot_sync(
                conn, team_id, now, protect_detected=True
            )
            grandfathered += protected
            backfilled += inserted

        for key, value in (
            ("patrol_baseline_at", now),
            ("patrol_kick_enabled", "1"),
        ):
            conn.execute(
                """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                  updated_at = excluded.updated_at""",
                (key, value, now),
            )
        conn.execute(
            """INSERT INTO operation_logs
               (team_id, action, target_email, detail, result, error_message,
                trigger_type, created_at)
               VALUES (NULL, 'patrol_activate', NULL, ?, 'success', NULL, 'manual', ?)""",
            (
                f"teams={len(active_team_ids)}, grandfathered={grandfathered}, "
                f"backfilled={backfilled}",
                now,
            ),
        )
        conn.commit()
        return {
            "status": "ok",
            "kick_enabled": True,
            "grandfathered": grandfathered,
            "backfilled": backfilled,
            "baseline_at": now,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── 纯函数：候选筛选 + 风险分类（无 IO，routes/patrol.py 和 run_patrol 共用） ──

def _normalize_owner_email(owner_email: Any) -> str:
    return str(owner_email or "").strip().lower()


def _is_team_owner_email(member: dict, owner_email: Any) -> bool:
    """成员邮箱等于 teams.owner_email（不分大小写）。上游角色不是 account-owner 的 Owner 条目
    （缓存里 is_owner 为 False）靠这一条认出来，和 scheduler 同步时跳过 Owner 的规则相同。"""
    owner = _normalize_owner_email(owner_email)
    return bool(owner) and str(member.get("email") or "").strip().lower() == owner


def _team_owner_email_sync(conn: sqlite3.Connection, team_id: str) -> str:
    row = conn.execute("SELECT owner_email FROM teams WHERE id = ?", (team_id,)).fetchone()
    return _normalize_owner_email(row["owner_email"]) if row else ""


def select_kick_candidates(members: Any, owner_email: Any = None) -> list[dict]:
    """从成员列表里筛出"可踢候选"，按 first_seen_at（退回 created_time）新→旧排序。

    唯一硬规则：source == 'detected'。另外叠加 seat_type == 'default' 且
    is_owner is False（owner/codex 席位永不触碰）。不做任何时间戳门槛判断——
    现有成员会在管理员开启自动踢人时（以及巡逻自动纳入一个从未建过基线的 Team 时）
    一次性转成 source='system' 保护住，所以"只踢 detected"就等价于"只踢那之后从系统外
    混进来的人"（包括 Team token_expired 期间混进来、恢复后自动重建基线的人）。
    owner_email（teams.owner_email）：邮箱等于它的人同样当 Owner，不进候选。
    """
    if not isinstance(members, list):
        return []

    candidates = []
    for m in members:
        if not isinstance(m, dict):
            continue
        if m.get("seat_type") != DEFAULT_SEAT_TYPE:
            continue
        if m.get("is_owner") is not False or _is_team_owner_email(m, owner_email):
            continue
        if m.get("source") != "detected":
            continue
        candidates.append(m)

    candidates.sort(key=lambda m: m.get("first_seen_at") or m.get("created_time") or "", reverse=True)
    return candidates


def select_premium_kick_candidates(members: Any, owner_email: Any = None) -> list[dict]:
    """Premium 外部成员候选（所有者裁决的踢人路径）：seat_type == 'prolite'、is_owner is False、
    邮箱不等于 owner_email（teams.owner_email，不分大小写）、source == 'detected'。不看超员 /
    严格模式：每人都是 ChatGPT 自动加购、按月扣费的 Premium 席位（豁免 / Codex 队由调用方和
    _patrol_kick 挡住）。排序与 select_kick_candidates 相同（first_seen_at 新→旧）。
    """
    if not isinstance(members, list):
        return []

    candidates = []
    for m in members:
        if not isinstance(m, dict):
            continue
        if normalize_seat_type(m.get("seat_type")) != PREMIUM_SEAT_TYPE:
            continue
        if m.get("is_owner") is not False or _is_team_owner_email(m, owner_email):
            continue
        if m.get("source") != "detected":
            continue
        candidates.append(m)

    candidates.sort(key=lambda m: m.get("first_seen_at") or m.get("created_time") or "", reverse=True)
    return candidates


def select_invite_revoke_candidates(pending: Any) -> list[dict]:
    """从 pending invite 快照里筛出该自动撤销的候选。

    唯一硬规则：source == 'detected'。系统自己发出的邀请 source 是 'system' /
    'self_service'；管理员开启自动踢人时、以及巡逻自动纳入一个从未建过基线的 Team 时
    （_protect_team_snapshot_sync）会把当时已存在的邀请一次性转成 'system'，所以"只撤
    detected"就等价于"只撤那之后新出现的、系统没发过的邀请"——和 select_kick_candidates
    对已入队成员的处理方式完全对称。
    撤邀请风险比踢人低（人还没进来，撤错了重发一次即可），不设 over_by 式数量上限。
    注册表外的席位类型（如 automation）不撤：TeamBoss 对这类席位什么都不做。
    """
    if not isinstance(pending, list):
        return []

    candidates = []
    for p in pending:
        if not isinstance(p, dict):
            continue
        if p.get("source") != "detected":
            continue
        if not is_known_seat_type(p.get("seat_type")):
            continue
        candidates.append(p)

    candidates.sort(key=lambda p: p.get("first_seen_at") or p.get("created_time") or "", reverse=True)
    return candidates


def strict_mode_may_act_on_seat_type(seat_type: Any) -> bool:
    """严格模式能动手的席位类型：注册表里的类型，Premium 除外（Premium 外部成员走自己的路径）。"""
    seat = normalize_seat_type(seat_type)
    return is_known_seat_type(seat) and seat != PREMIUM_SEAT_TYPE


def select_strict_kick_candidates(members: Any) -> list[dict]:
    """严格模式真正可能动手的候选 = select_strict_outsiders 里、席位类型严格模式允许动手的人
    （ChatGPT、Codex）。Premium 外部成员由 Premium 路径处理（不重复踢），注册表外的类型谁都不碰。
    顺序与 select_strict_outsiders 相同。
    """
    return [
        m for m in select_strict_outsiders(members)
        if strict_mode_may_act_on_seat_type(m.get("seat_type"))
    ]


def select_strict_outsiders(members: Any) -> list[dict]:
    """严格模式眼里的疑似陌生成员（不看席位类型 / 超员判定 / Codex 豁免），只要求：

    - 非 owner
    - source == 'detected'（系统自己拉的人 source 是 'system'/'self_service'，永不进候选）
    - 没有到期记录（expires_at 为空）—— 有 expires_at 说明这个人被系统主动追踪/授权过
      （管理员手动设置到期时间、自助续期等都会把 source 一并改写，但这里额外再挡一层，
      不完全依赖 source 字段各处写入逻辑的正确性，属于纵深防御）

    "别一次踢一片"护栏按这份名单计数：护栏若改按缩小后的候选计数，原本被它拦下的
    一队反而会放行，等于多踢。真正动手只从 select_strict_kick_candidates 里挑。
    """
    if not isinstance(members, list):
        return []

    candidates = []
    for m in members:
        if not isinstance(m, dict):
            continue
        if m.get("is_owner") is not False:
            continue
        if m.get("source") != "detected":
            continue
        if m.get("expires_at"):
            continue
        candidates.append(m)

    candidates.sort(key=lambda m: m.get("first_seen_at") or m.get("created_time") or "", reverse=True)
    return candidates


def select_detected_outsiders(members: Any) -> list[dict]:
    """Premium 踢人"别一次踢一片"护栏数的人：非 Owner、source == 'detected' 的全部成员。

    不看席位类型、到期、严格模式开没开：外部成员一下子多到超过阈值，最可能是数据出了问题
    （例如来源记录没对上），这时一个 Premium 成员都不该踢。
    """
    if not isinstance(members, list):
        return []
    return [
        m for m in members
        if isinstance(m, dict) and m.get("is_owner") is False and m.get("source") == "detected"
    ]


def strict_kick_batch_limit(team_size: int) -> int:
    """"别一次踢一片"阈值：3 人和团队总人数一半，取更小的那个。

    团队越小越保守（比如 4 人的队，阈值是 2 而不是 3），大队封顶 3，
    不随团队规模线性放大——超过这个数基本可以认定是数据异常而非真的巧合。
    """
    return min(3, team_size // 2)


def strict_kick_batch_guard_exceeded(candidate_count: int, team_size: int) -> bool:
    return candidate_count > strict_kick_batch_limit(team_size)


# 非严格模式的绝对保险丝：一个 Team 单轮在 Premium 外部成员和超员两条路上合计最多踢这么多人，
# 无论 over_by 算成多少。over_by 一旦因上游数据异常被抬高，这道闸挡住"一趟踢光整队"；超出的
# 下一轮再处理。
# （seats_entitled 为 NULL/0 这类未知值已在 run_patrol 里整段跳过，不会走到这里。）
# 想更激进/更保守改这一个数即可。
NON_STRICT_KICK_ABS_CAP = 10


def _strict_kick_ready_at(first_seen_at_raw: Any, mode: str, delay_hours: int) -> Optional[datetime]:
    first_seen = _parse_iso_datetime(first_seen_at_raw)
    if first_seen is None:
        return None
    return compute_effective_kick_at(first_seen, {"mode": mode, "delay_hours": delay_hours})


def _is_strict_candidate_ready(first_seen_at_raw: Any, mode: str, delay_hours: int, now: datetime) -> bool:
    """first_seen_at 缺失时保守地判定为"未就绪"——没有可信的观察起点，绝不能立即动手。"""
    ready_at = _strict_kick_ready_at(first_seen_at_raw, mode, delay_hours)
    if ready_at is None:
        return False
    return now >= ready_at


def _already_flagged_strict_sync(conn: sqlite3.Connection, team_id: str, email: str) -> bool:
    """本人是否已经在之前某一轮被标记过"严格模式候选，等待期中"——避免每轮重复推送。"""
    if not email:
        return False
    row = conn.execute(
        "SELECT 1 FROM operation_logs WHERE team_id = ? AND action = 'patrol_strict_flagged' "
        "AND target_email = ? LIMIT 1",
        (team_id, email),
    ).fetchone()
    return row is not None


def valid_seats_entitled(value: Any) -> Optional[int]:
    """seats_entitled 只有是正整数才可信，否则返回 None（未知）。

    超员判定是 over_by = active_chatgpt - seats_entitled。把 NULL/0/负数/脏数据当成 0
    会让每个 default 席位都"超员"，巡逻就把所有 detected 成员都当成候选——未知的
    席位数绝不能当成"0 个席位"，只能当成"判断不了"。合法性规则和写库端共用
    seat_capacity.positive_seat_count，两边不能各判各的。
    """
    return positive_seat_count(value)


def classify_team(*, team_id: str, name: str, codex_enabled: bool,
                   seats_entitled: Any, members: Any) -> dict:
    """纯函数：给定 Team + 成员快照，算出风险等级和"会被踢的候选"，不做任何写操作。

    risk：codex 开 = 'ok'（不管超没超）；codex 关且未超 = 'watch'；codex 关且超 = 'over'。
    detected_over 只在 risk == 'over' 时给出（即真正会被 run_patrol 选中踢除的候选）。
    seats_entitled 不是正整数时无法判断是否超员：over_by 记 0、不给任何候选，
    codex 关时 risk 记 'watch'，并以 entitlement_valid=False 标出。
    """
    usage = member_seat_usage_from_members(members if isinstance(members, list) else [])
    active_chatgpt = usage.active_chatgpt if usage is not None else 0
    entitled = valid_seats_entitled(seats_entitled)
    over_by = max(0, active_chatgpt - entitled) if entitled is not None else 0

    if codex_enabled:
        risk = "ok"
    elif over_by > 0:
        risk = "over"
    else:
        risk = "watch"

    detected_over: list[dict] = []
    if risk == "over":
        selected = select_kick_candidates(members)[:over_by]
        detected_over = [
            {
                "email": c.get("email"),
                "user_id": c.get("id") or c.get("user_id"),
                "seat_type": c.get("seat_type"),
                "first_seen_at": c.get("first_seen_at"),
            }
            for c in selected
        ]

    return {
        "team_id": team_id,
        "name": name,
        "codex_enabled": codex_enabled,
        "seats_entitled": entitled or 0,
        "entitlement_valid": entitled is not None,
        "active_chatgpt": active_chatgpt,
        "over_by": over_by,
        "risk": risk,
        "detected_over": detected_over,
    }


# ── 共享前置校验：踢人/撤邀请两条真动手路径都要先过这一关 ────────────────────

def _armed_team_gate_reject(conn: sqlite3.Connection, team_id: str, settings: dict) -> Optional[str]:
    """任何真正会调用 OpenAI 写接口的路径，进入前都必须先过这一关。

    返回 None 表示放行；否则返回拒绝原因字符串，调用方负责写操作日志。
    与原 _patrol_kick 内联版本逐字对齐，只是抽成共享函数供 _patrol_kick /
    _patrol_revoke_invite / _patrol_strict_kick 三处复用，避免同一套护栏漂移出三份。
    """
    if settings.get("patrol_kick_enabled") != "1":
        return "rejected: patrol live kick is disabled"
    if not (settings.get("patrol_baseline_at") or "").strip():
        return "rejected: existing-member protection is not initialized"
    _ensure_team_baseline_table(conn)
    team_baseline = conn.execute(
        "SELECT baseline_at FROM patrol_team_baselines WHERE team_id = ?",
        (team_id,),
    ).fetchone()
    if not team_baseline:
        return "rejected: team existing-member protection is not initialized"
    if team_id in set(parse_exempt_team_ids(settings.get("patrol_exempt_team_ids"))):
        return "rejected: team is exempt"
    return None


# ── 唯一踢人入口：进函数先重新校验一遍资格，任一不满足直接拒绝 ──────────────

def _pending_invite_reconciliation_reject(
    conn: sqlite3.Connection,
    team_id: str,
    user_id: str = "",
    email: str = "",
) -> Optional[str]:
    """Fail closed while a confirmed remote invite lacks its primary record."""
    normalized_user_id = user_id or ""
    normalized_email = (email or "").strip().lower()
    try:
        row = conn.execute(
            """SELECT 1 FROM pending_invite_reconciliations
               WHERE team_id = ? AND resolved = 0
                 AND ((? != '' AND user_id = ?) OR (? != '' AND lower(email) = ?))
               LIMIT 1""",
            (
                team_id,
                normalized_user_id,
                normalized_user_id,
                normalized_email,
                normalized_email,
            ),
        ).fetchone()
    except sqlite3.Error:
        # If the protection state itself cannot be read, destructive patrol
        # actions must stop rather than assume the target is safe to remove.
        return "rejected: invite reconciliation state is unavailable"
    if row:
        return "rejected: confirmed invite reconciliation is still pending"
    return None


KICK_RULE_OVER_QUOTA = "over_quota"
KICK_RULE_PREMIUM_OUTSIDER = "premium_outsider"
# _patrol_kick 的 Premium 规则在 claim 里发现快照已过期时返回的原因（不是失败，下一轮再判）。
PREMIUM_KICK_DEFERRED = "deferred: seat changed in TeamBoss after the member snapshot"
# _patrol_kick 的超员规则在 claim 里发现 TeamBoss 刚有了这个人的拉人 / 兑换记录时返回的原因。
KICK_DEFERRED_TEAMBOSS_RECORD = "deferred: TeamBoss has a record for this member"


def _patrol_kick(conn: sqlite3.Connection, client: ChatGPTClient, team_id: str,
                  member: dict, kick_source: str = "patrol", *,
                  rule: str = KICK_RULE_OVER_QUOTA) -> tuple[bool, Optional[str]]:
    """巡逻踢人的唯一入口。纵深防御：即便调用方选错了候选，这里也会再拦一次。

    rule 决定席位类型这一关：
    - over_quota（默认）：只踢 ChatGPT（default）席位，Team 必须未开 Codex、席位数有效且当前超员，
      目标必须是 select_over_quota_kick_candidates_sync 选出的人（最新的 over_by 个里去掉 TeamBoss
      还在管、因不在名单才没在管、或兑换过的人）。claim 拿到后再查一次记录，刚有了就返回
      KICK_DEFERRED_TEAMBOSS_RECORD，不踢。
    - premium_outsider：只踢 Premium（prolite）席位，不看超员；目标必须是
      select_premium_kick_candidates 里的人，TeamBoss 没有他的记录（任何改席位 / 邀请记录、Premium
      兑换，或上面超员那条的记录，_premium_kick_veto_sync），且这份名单的外部成员数没有异常
      （outsider_batch_guard）。claim 拿到后再查一次：这次判定用的快照开始拉取之后 TeamBoss 动过他的
      席位、或刚有了这样的记录，返回 PREMIUM_KICK_DEFERRED，不踢。
    其余闸门两条规则完全相同：武装 + 基线 + 未豁免、Team active 且没开 Codex、成员缓存里重新定位、
    缓存与持久化来源都是 detected、非 Owner（is_owner is False，且邮箱不是 teams.owner_email）、
    对账屏障、member claim 后再查一次来源和到期。
    校验通过后，复用 auto_kick_job 的原语：client.remove_member -> 标记 kicked -> 记日志。
    返回 (是否踢成功, 失败原因或 None)。
    """
    email = (member.get("email") or "").strip().lower()
    user_id = member.get("id") or member.get("user_id") or ""
    if rule not in (KICK_RULE_OVER_QUOTA, KICK_RULE_PREMIUM_OUTSIDER):
        reason = f"rejected: unknown kick rule {rule!r}"
        _log_operation_sync(team_id, "patrol_kick", email, reason, "failed", "safety gate rejected")
        return False, reason
    premium_rule = rule == KICK_RULE_PREMIUM_OUTSIDER

    # 所有真踢条件集中在这里。上游筛选结果一律不被信任，避免其他入口或旧缓存绕过安全规则。
    settings = _read_patrol_settings(conn)
    reject_reason = _armed_team_gate_reject(conn, team_id, settings)
    if reject_reason:
        _log_operation_sync(team_id, "patrol_kick", email, reject_reason, "failed", "safety gate rejected")
        return False, reject_reason

    team = conn.execute(
        "SELECT status, is_codex_enabled, seats_entitled, owner_email FROM teams WHERE id = ?",
        (team_id,),
    ).fetchone()
    if not team or team["status"] != "active":
        reason = "rejected: team is not active"
        _log_operation_sync(team_id, "patrol_kick", email, reason, "failed", "safety gate rejected")
        return False, reason
    if bool(team["is_codex_enabled"]):
        reason = "rejected: codex is enabled"
        _log_operation_sync(team_id, "patrol_kick", email, reason, "failed", "safety gate rejected")
        return False, reason

    cache_row = conn.execute(
        "SELECT members_json, fetch_started_at FROM member_cache WHERE team_id = ?", (team_id,)
    ).fetchone()
    try:
        cached_members = json.loads(cache_row["members_json"]) if cache_row and cache_row["members_json"] else []
    except Exception:
        cached_members = []
    if not isinstance(cached_members, list) or not cached_members:
        reason = "rejected: member cache is empty or invalid"
        _log_operation_sync(team_id, "patrol_kick", email, reason, "failed", "safety gate rejected")
        return False, reason
    # 这次判定用的快照是什么时候开始拉的（不是写进缓存的时间：卡在半路的刷新写得晚，名单却是
    # 开始时的）。Premium 规则在 claim 里拿它比对 TeamBoss 之后有没有动过席位。
    snapshot_started_at = cache_row["fetch_started_at"]

    # 重新从缓存定位目标，不能信任调用方传入的 source/seat_type/is_owner。
    cached_member = None
    for item in cached_members:
        if not isinstance(item, dict):
            continue
        item_user_id = item.get("id") or item.get("user_id") or ""
        item_email = (item.get("email") or "").strip().lower()
        if (user_id and item_user_id == user_id) or (email and item_email == email):
            cached_member = item
            break
    if cached_member is None:
        reason = "rejected: target is absent from current member cache"
        _log_operation_sync(team_id, "patrol_kick", email, reason, "failed", "safety gate rejected")
        return False, reason

    status = None
    if not premium_rule:
        # 席位数未知时"是否超员"无从判断，绝不能按 0 个席位算成全员超员。
        if valid_seats_entitled(team["seats_entitled"]) is None:
            reason = "rejected: seats_entitled is not a positive integer"
            _log_operation_sync(team_id, "patrol_kick", email, reason, "failed", "safety gate rejected")
            return False, reason

        status = classify_team(
            team_id=team_id,
            name=team_id,
            codex_enabled=False,
            seats_entitled=team["seats_entitled"],
            members=cached_members,
        )
        if status["risk"] != "over" or status["over_by"] <= 0:
            reason = "rejected: team is not currently over quota"
            _log_operation_sync(team_id, "patrol_kick", email, reason, "failed", "safety gate rejected")
            return False, reason

    # source 必须精确等于 detected；system/self_service/None 等全部拒绝。
    if cached_member.get("source") != "detected":
        reason = f"rejected: source={cached_member.get('source')!r} != 'detected'"
        _log_operation_sync(team_id, "patrol_kick", email, reason, "failed", "safety gate rejected")
        return False, reason

    cached_user_id = cached_member.get("id") or cached_member.get("user_id") or ""
    cached_email = (cached_member.get("email") or "").strip().lower()
    reconciliation_reject = _pending_invite_reconciliation_reject(
        conn, team_id, cached_user_id, cached_email
    )
    if reconciliation_reject:
        _log_operation_sync(
            team_id,
            "patrol_kick",
            cached_email or email,
            reconciliation_reject,
            "failed",
            "safety gate rejected",
        )
        return False, reconciliation_reject

    expiry_row = conn.execute(
        """SELECT source FROM member_expiry
            WHERE team_id = ? AND kicked = 0
              AND ((? != '' AND user_id = ?) OR (? != '' AND lower(email) = ?))
            ORDER BY id DESC LIMIT 1""",
        (team_id, cached_user_id, cached_user_id, cached_email, cached_email),
    ).fetchone()
    if not expiry_row or expiry_row["source"] != "detected":
        reason = "rejected: persisted source is not detected"
        _log_operation_sync(team_id, "patrol_kick", cached_email or email, reason, "failed", "safety gate rejected")
        return False, reason
    if cached_member.get("is_owner") is not False:
        reason = f"rejected: is_owner={cached_member.get('is_owner')!r} is not False"
        _log_operation_sync(team_id, "patrol_kick", email, reason, "failed", "safety gate rejected")
        return False, reason
    owner_email = _normalize_owner_email(team["owner_email"])
    if _is_team_owner_email(cached_member, owner_email):
        reason = "rejected: target email is the Team owner_email"
        _log_operation_sync(team_id, "patrol_kick", email, reason, "failed", "safety gate rejected")
        return False, reason
    if premium_rule:
        if normalize_seat_type(cached_member.get("seat_type")) != PREMIUM_SEAT_TYPE:
            reason = f"rejected: seat_type={cached_member.get('seat_type')!r} != 'prolite'"
            _log_operation_sync(team_id, "patrol_kick", email, reason, "failed", "safety gate rejected")
            return False, reason
        if cached_member not in select_premium_kick_candidates(cached_members, owner_email):
            reason = "rejected: target is not a Premium outsider candidate"
            _log_operation_sync(team_id, "patrol_kick", email, reason, "failed", "safety gate rejected")
            return False, reason
        record_veto = _premium_kick_veto_sync(conn, team_id, cached_email, cached_user_id)
        if record_veto:
            reason = f"rejected: {record_veto}"
            _log_operation_sync(team_id, "patrol_kick", email, reason, "failed", "safety gate rejected")
            return False, reason
        # 同一份快照里外部成员多到异常：一个 Premium 成员都不踢（run_patrol 已经不选人，这里再挡一次）。
        guard_tripped, outsider_count = outsider_batch_guard(cached_members)
        if guard_tripped:
            reason = (
                f"rejected: abnormal number of detected outsiders "
                f"({outsider_count} of {len(cached_members)})"
            )
            _log_operation_sync(team_id, "patrol_kick", email, reason, "failed", "safety gate rejected")
            return False, reason
    else:
        if cached_member.get("seat_type") != DEFAULT_SEAT_TYPE:
            reason = f"rejected: seat_type={cached_member.get('seat_type')!r} != 'default'"
            _log_operation_sync(team_id, "patrol_kick", email, reason, "failed", "safety gate rejected")
            return False, reason
        if _teamboss_managed_history_sync(conn, team_id, cached_email, cached_user_id):
            reason = "rejected: TeamBoss placed or sold a seat to this member before"
            _log_operation_sync(team_id, "patrol_kick", email, reason, "failed", "safety gate rejected")
            return False, reason

        # 和 run_patrol 同一份选人：最新的 over_by 个里去掉受保护的人，空出的名额不往后补。
        allowed, _vetoed = select_over_quota_kick_candidates_sync(
            conn, team_id, cached_members, status["over_by"]
        )
        if cached_member not in allowed:
            reason = "rejected: target is not within the newest over-quota candidates"
            _log_operation_sync(team_id, "patrol_kick", email, reason, "failed", "safety gate rejected")
            return False, reason
    # 使用缓存里重新定位出的 id，禁止调用方替换目标。
    user_id = cached_user_id
    email = cached_email or email
    if not user_id:
        reason = "rejected: cached target is missing user_id"
        _log_operation_sync(team_id, "patrol_kick", email, reason, "failed", "safety gate rejected")
        return False, reason

    with member_operation_claim_sync(
        conn,
        team_id,
        email=email,
        user_id=user_id,
        operation="patrol_kick",
    ) as acquired:
        if not acquired:
            reason = "rejected: member operation already in progress"
            _log_operation_sync(team_id, "patrol_kick", email, reason, "failed", "safety gate rejected")
            return False, reason
        fresh_expiry = conn.execute(
            """SELECT source, expires_at FROM member_expiry
               WHERE team_id = ? AND kicked = 0
                 AND ((? != '' AND user_id = ?) OR (? != '' AND lower(email) = ?))
               ORDER BY id DESC LIMIT 1""",
            (team_id, user_id, user_id, email, email),
        ).fetchone()
        if not fresh_expiry or fresh_expiry["source"] != "detected" or fresh_expiry["expires_at"]:
            reason = "rejected: member was authorized before destructive action"
            _log_operation_sync(team_id, "patrol_kick", email, reason, "failed", "safety gate rejected")
            return False, reason
        deferred_because = None
        if premium_rule:
            if _teamboss_seat_change_after_sync(conn, team_id, email, user_id, snapshot_started_at):
                deferred_because = "seat_changed_after_snapshot"
            elif _premium_kick_veto_sync(conn, team_id, email, user_id):
                deferred_because = "teamboss_record"
        elif _teamboss_managed_history_sync(conn, team_id, email, user_id):
            # 超员踢人：claim 期间刚有了 TeamBoss 拉人 / 兑换记录，这一轮不踢；下一轮他不再是候选。
            reason = KICK_DEFERRED_TEAMBOSS_RECORD
            _log_operation_sync(
                team_id, "patrol_kick", email,
                f"user_id={user_id}, reason={KICK_RULE_OVER_QUOTA}, deferred=teamboss_record",
                "skipped",
            )
            return False, reason
        if deferred_because:
            # 快照之后 TeamBoss 动过他的席位（切换超时、切换后刷新失败等），快照里的 prolite
            # 不可信；或者 claim 期间刚有了 TeamBoss 的记录（Premium 记录、拉人 / 兑换记录）。
            # 这一轮不踢，下一轮按新快照判断（有记录的那时不再是候选，进席位提醒）。
            reason = PREMIUM_KICK_DEFERRED
            _log_operation_sync(
                team_id, "patrol_kick", email,
                f"user_id={user_id}, seat_type={PREMIUM_SEAT_TYPE}, "
                f"reason={KICK_RULE_PREMIUM_OUTSIDER}, deferred={deferred_because}",
                "skipped",
            )
            return False, reason

        log_detail = f"user_id={user_id}"
        if premium_rule:
            log_detail += f", seat_type={PREMIUM_SEAT_TYPE}, reason={KICK_RULE_PREMIUM_OUTSIDER}"
        result = run_chatgpt_call_sync(client.remove_member, user_id)
        if isinstance(result, dict) and "error" in result:
            _log_operation_sync(team_id, "patrol_kick", email, log_detail, "failed", result["error"])
            return False, result["error"]

        _mark_member_kicked_sync(conn, team_id, kick_source, user_id, email)
        _log_operation_sync(team_id, "patrol_kick", email, log_detail, "success")
        return True, None


# ── 唯一撤邀请入口：陌生 pending invite 自动撤销，结构与 _patrol_kick 对称 ──────

def _patrol_revoke_invite(conn: sqlite3.Connection, client: ChatGPTClient, team_id: str,
                           invite: dict, kick_source: str = "patrol_invite_revoke") -> tuple[bool, Optional[str]]:
    """陌生 pending invite 撤销的唯一入口。纵深防御与 _patrol_kick 完全对称：
    进函数先重新校验一遍武装状态 + 邀请仍然存在 + 来源仍然是 detected，任一不满足直接拒绝。
    """
    email = (invite.get("email") or "").strip().lower()
    if not email:
        reason = "rejected: invite missing email"
        _log_operation_sync(team_id, "patrol_revoke_invite", email, reason, "failed", "safety gate rejected")
        return False, reason

    settings = _read_patrol_settings(conn)
    reject_reason = _armed_team_gate_reject(conn, team_id, settings)
    if reject_reason:
        _log_operation_sync(team_id, "patrol_revoke_invite", email, reject_reason, "failed", "safety gate rejected")
        return False, reject_reason

    team = conn.execute("SELECT status FROM teams WHERE id = ?", (team_id,)).fetchone()
    if not team or team["status"] != "active":
        reason = "rejected: team is not active"
        _log_operation_sync(team_id, "patrol_revoke_invite", email, reason, "failed", "safety gate rejected")
        return False, reason

    cache_row = conn.execute(
        "SELECT pending_json FROM member_cache WHERE team_id = ?", (team_id,)
    ).fetchone()
    try:
        cached_pending = json.loads(cache_row["pending_json"]) if cache_row and cache_row["pending_json"] else []
    except Exception:
        cached_pending = []
    if not isinstance(cached_pending, list):
        cached_pending = []

    cached_invite = None
    for item in cached_pending:
        if not isinstance(item, dict):
            continue
        if (item.get("email") or "").strip().lower() == email:
            cached_invite = item
            break
    if cached_invite is None:
        reason = "rejected: target is absent from current pending cache"
        _log_operation_sync(team_id, "patrol_revoke_invite", email, reason, "failed", "safety gate rejected")
        return False, reason
    if cached_invite.get("source") != "detected":
        reason = f"rejected: source={cached_invite.get('source')!r} != 'detected'"
        _log_operation_sync(team_id, "patrol_revoke_invite", email, reason, "failed", "safety gate rejected")
        return False, reason
    if not is_known_seat_type(cached_invite.get("seat_type")):
        reason = f"rejected: seat_type={cached_invite.get('seat_type')!r} is not a TeamBoss seat type"
        _log_operation_sync(team_id, "patrol_revoke_invite", email, reason, "failed", "safety gate rejected")
        return False, reason

    reconciliation_reject = _pending_invite_reconciliation_reject(
        conn, team_id, email=email
    )
    if reconciliation_reject:
        _log_operation_sync(
            team_id,
            "patrol_revoke_invite",
            email,
            reconciliation_reject,
            "failed",
            "safety gate rejected",
        )
        return False, reconciliation_reject

    expiry_row = conn.execute(
        """SELECT source FROM member_expiry
            WHERE team_id = ? AND kicked = 0 AND lower(email) = ?
            ORDER BY id DESC LIMIT 1""",
        (team_id, email),
    ).fetchone()
    if not expiry_row or expiry_row["source"] != "detected":
        reason = "rejected: persisted source is not detected"
        _log_operation_sync(team_id, "patrol_revoke_invite", email, reason, "failed", "safety gate rejected")
        return False, reason

    with member_operation_claim_sync(
        conn,
        team_id,
        email=email,
        operation="patrol_revoke_invite",
    ) as acquired:
        if not acquired:
            reason = "rejected: member operation already in progress"
            _log_operation_sync(team_id, "patrol_revoke_invite", email, reason, "failed", "safety gate rejected")
            return False, reason
        fresh_expiry = conn.execute(
            """SELECT source, expires_at FROM member_expiry
               WHERE team_id = ? AND kicked = 0 AND lower(email) = ?
               ORDER BY id DESC LIMIT 1""",
            (team_id, email),
        ).fetchone()
        if not fresh_expiry or fresh_expiry["source"] != "detected" or fresh_expiry["expires_at"]:
            reason = "rejected: invite was authorized before destructive action"
            _log_operation_sync(team_id, "patrol_revoke_invite", email, reason, "failed", "safety gate rejected")
            return False, reason

        result = run_chatgpt_call_sync(client.revoke_invite, email)
        if isinstance(result, dict) and "error" in result:
            _log_operation_sync(team_id, "patrol_revoke_invite", email, None, "failed", result["error"])
            return False, result["error"]

        _mark_member_kicked_sync(conn, team_id, kick_source, "", email)
        _log_operation_sync(team_id, "patrol_revoke_invite", email, None, "success")
        return True, None


# ── 严格模式：动手前强制实时刷新 + 唯一踢人入口 ───────────────────────────────

def _fetch_all_api_items_sync(
    method, *fallback_keys: str, limit: int = 100, max_items: int = 10000, require_items: bool = False
):
    """分页拉取 API 列表的同步小工具，供严格模式"动手前强制实时刷新"使用。返回 (条目, 错误)。

    每一页按 snapshot_pages.SnapshotPageAccumulator 判定（那里是"名单何时完整"的正本）：读不懂、
    条数和 total 对不上、翻到上限还没完，都返回 (None, 原因)，调用方跳过整队、不写缓存、不踢人。
    上游这一页本身报错时，原因沿用上游原样的 error（和以前一样）。成员名单传
    ``require_items=True``：拉完是空的同样不完整（真实名单里至少有 owner）。
    """
    pages = SnapshotPageAccumulator(*fallback_keys, limit=limit, require_items=require_items)
    # 100 条一页、最多 10000 条 = 最多 100 页，与 scheduler 的同步拉取相同。
    max_pages = max(1, (max_items + pages.limit - 1) // pages.limit)
    for _ in range(max_pages):
        data = run_chatgpt_call_sync(method, offset=pages.next_offset, limit=pages.limit)
        try:
            if pages.add(data):
                return pages.items, None
        except SnapshotPageError as exc:
            return None, exc.upstream_error or exc.reason
    # 翻到上限还没结束 = 名单不完整，不能当完整快照写缓存、拿去对账。
    return None, "member/invite list exceeds the paging limit"


def _refresh_team_snapshot_sync(
    conn: sqlite3.Connection, team: sqlite3.Row
) -> tuple[bool, Optional[str], Optional[ChatGPTClient]]:
    """严格模式动手前的强制实时刷新：绝不能拿旧缓存去踢人，人可能刚被手动处理过。

    只重写 member_cache 快照（复用已有 member_expiry 记录补充 source / first_seen_at /
    expires_at），不做新成员检测建档——建档是 scheduler.data_sync_job 的职责，本函数只
    负责"确认当前谁还在"。刷新失败时调用方必须跳过整队，绝不能拿旧数据动手。
    """
    team_id = team["id"]
    proxy_url = _get_proxy_url_sync(conn, team["proxy_id"])
    client = ChatGPTClient(team["access_token"], team_id, team["device_id"], proxy_url=proxy_url)

    fetch_started_at = snapshot_fetch_started_now()
    members_items, m_err = _fetch_all_api_items_sync(client.get_members, "users", require_items=True)
    if m_err:
        return False, m_err, None
    pending_items, p_err = _fetch_all_api_items_sync(client.get_pending_invites, "invites")
    if p_err:
        return False, p_err, None

    expiry_rows = conn.execute(
        "SELECT * FROM member_expiry WHERE team_id = ? AND kicked = 0", (team_id,)
    ).fetchall()
    exp_map: dict[str, sqlite3.Row] = {}
    for er in expiry_rows:
        if er["user_id"]:
            exp_map[er["user_id"]] = er
        if er["email"]:
            exp_map[er["email"].lower()] = er

    cached_members = []
    for m in (members_items or []):
        uid = m.get("id") or m.get("user_id") or ""
        member_email = m.get("email") or ""
        ei = exp_map.get(uid) or exp_map.get(member_email.lower())
        cached_members.append({
            "id": uid, "email": member_email,
            "name": m.get("name"),
            "role": m.get("role", "standard-user"),
            "seat_type": m.get("seat_type", "default"),
            "is_owner": m.get("role") == "account-owner",
            "expires_at": ei["expires_at"] if ei else None,
            "first_seen_at": ei["first_seen_at"] if ei else None,
            "source": ei["source"] if ei else None,
            "created_time": m.get("created_time", m.get("created")),
            "status": "active",
        })

    cached_pending = []
    for inv in (pending_items or []):
        invite_email = inv.get("email_address", inv.get("email", ""))
        ei = exp_map.get((invite_email or "").lower())
        cached_pending.append({
            "id": inv.get("id", ""), "email": invite_email,
            "name": None,
            "role": inv.get("role", "standard-user"),
            "seat_type": inv.get("seat_type", "default"),
            "is_owner": False,
            "expires_at": ei["expires_at"] if ei else None,
            "first_seen_at": ei["first_seen_at"] if ei else None,
            "source": ei["source"] if ei else None,
            "created_time": inv.get("created_time", inv.get("created")),
            "status": "pending",
        })

    # 库里已有一份开始得更晚的快照时不覆盖；调用方随后读到的就是那一份（同样是完整快照）。
    store_member_snapshot_sync(conn, team_id, cached_members, cached_pending, fetch_started_at)
    conn.commit()
    return True, None, client


def _patrol_strict_kick(conn: sqlite3.Connection, client: ChatGPTClient, team_id: str,
                         member: dict, kick_source: str = "patrol_strict") -> tuple[bool, Optional[str]]:
    """严格模式踢人的唯一入口。结构与 _patrol_kick 对称，但额外要求：

    - settings.patrol_strict_mode_enabled == '1'（独立危险开关，必须显式开启）
    - 不检查 codex_enabled / seat_type / 是否超员（严格模式本来就不看这些）
    - 動手前重新实时查一次 member_expiry：source 仍是 detected 且 expires_at 仍为空
      （"再加一道保险"——不完全信任批次开始时的快照，防止期间被系统重新拉入/授权）
    - 重新校验踢人延迟窗口已过（复用 expiry_kick_mode / expiry_kick_delay_hours）
    """
    email = (member.get("email") or "").strip().lower()
    user_id = member.get("id") or member.get("user_id") or ""

    settings = _read_patrol_settings(conn)
    if settings.get("patrol_strict_mode_enabled") != "1":
        reason = "rejected: strict mode is disabled"
        _log_operation_sync(team_id, "patrol_strict_kick", email, reason, "failed", "safety gate rejected")
        return False, reason

    reject_reason = _armed_team_gate_reject(conn, team_id, settings)
    if reject_reason:
        _log_operation_sync(team_id, "patrol_strict_kick", email, reject_reason, "failed", "safety gate rejected")
        return False, reject_reason

    team = conn.execute("SELECT status FROM teams WHERE id = ?", (team_id,)).fetchone()
    if not team or team["status"] != "active":
        reason = "rejected: team is not active"
        _log_operation_sync(team_id, "patrol_strict_kick", email, reason, "failed", "safety gate rejected")
        return False, reason

    cache_row = conn.execute(
        "SELECT members_json FROM member_cache WHERE team_id = ?", (team_id,)
    ).fetchone()
    try:
        cached_members = json.loads(cache_row["members_json"]) if cache_row and cache_row["members_json"] else []
    except Exception:
        cached_members = []
    if not isinstance(cached_members, list) or not cached_members:
        reason = "rejected: member cache is empty or invalid"
        _log_operation_sync(team_id, "patrol_strict_kick", email, reason, "failed", "safety gate rejected")
        return False, reason

    cached_member = None
    for item in cached_members:
        if not isinstance(item, dict):
            continue
        item_user_id = item.get("id") or item.get("user_id") or ""
        item_email = (item.get("email") or "").strip().lower()
        if (user_id and item_user_id == user_id) or (email and item_email == email):
            cached_member = item
            break
    if cached_member is None:
        reason = "rejected: target is absent from current member cache"
        _log_operation_sync(team_id, "patrol_strict_kick", email, reason, "failed", "safety gate rejected")
        return False, reason

    if cached_member.get("is_owner") is not False:
        reason = f"rejected: is_owner={cached_member.get('is_owner')!r} is not False"
        _log_operation_sync(team_id, "patrol_strict_kick", email, reason, "failed", "safety gate rejected")
        return False, reason
    if cached_member.get("source") != "detected":
        reason = f"rejected: source={cached_member.get('source')!r} != 'detected'"
        _log_operation_sync(team_id, "patrol_strict_kick", email, reason, "failed", "safety gate rejected")
        return False, reason
    if cached_member.get("expires_at"):
        reason = "rejected: cached target has an expiry record"
        _log_operation_sync(team_id, "patrol_strict_kick", email, reason, "failed", "safety gate rejected")
        return False, reason
    if not strict_mode_may_act_on_seat_type(cached_member.get("seat_type")):
        reason = f"rejected: strict mode never acts on seat_type={cached_member.get('seat_type')!r}"
        _log_operation_sync(team_id, "patrol_strict_kick", email, reason, "failed", "safety gate rejected")
        return False, reason

    cached_user_id = cached_member.get("id") or cached_member.get("user_id") or ""
    cached_email = (cached_member.get("email") or "").strip().lower()
    reconciliation_reject = _pending_invite_reconciliation_reject(
        conn, team_id, cached_user_id, cached_email
    )
    if reconciliation_reject:
        _log_operation_sync(
            team_id,
            "patrol_strict_kick",
            cached_email or email,
            reconciliation_reject,
            "failed",
            "safety gate rejected",
        )
        return False, reconciliation_reject

    # 再加一道保险：真正踢人前重新实时查一次本地记录，绝不只信巡逻开始时的那份快照。
    expiry_row = conn.execute(
        """SELECT source, expires_at, first_seen_at FROM member_expiry
            WHERE team_id = ? AND kicked = 0
              AND ((? != '' AND user_id = ?) OR (? != '' AND lower(email) = ?))
            ORDER BY id DESC LIMIT 1""",
        (team_id, cached_user_id, cached_user_id, cached_email, cached_email),
    ).fetchone()
    if not expiry_row or expiry_row["source"] != "detected":
        reason = "rejected: persisted source is not detected"
        _log_operation_sync(team_id, "patrol_strict_kick", cached_email or email, reason, "failed", "safety gate rejected")
        return False, reason
    if (expiry_row["expires_at"] or "").strip():
        reason = "rejected: persisted record has an expiry (not a stray external add)"
        _log_operation_sync(team_id, "patrol_strict_kick", cached_email or email, reason, "failed", "safety gate rejected")
        return False, reason

    mode, delay_hours = _read_kick_delay_settings_sync(conn)
    if not _is_strict_candidate_ready(expiry_row["first_seen_at"], mode, delay_hours, datetime.now(timezone.utc)):
        reason = "rejected: strict kick delay window has not elapsed"
        _log_operation_sync(team_id, "patrol_strict_kick", cached_email or email, reason, "failed", "safety gate rejected")
        return False, reason

    user_id = cached_user_id
    email = cached_email or email
    if not user_id:
        reason = "rejected: cached target is missing user_id"
        _log_operation_sync(team_id, "patrol_strict_kick", email, reason, "failed", "safety gate rejected")
        return False, reason

    with member_operation_claim_sync(
        conn,
        team_id,
        email=email,
        user_id=user_id,
        operation="patrol_strict_kick",
    ) as acquired:
        if not acquired:
            reason = "rejected: member operation already in progress"
            _log_operation_sync(team_id, "patrol_strict_kick", email, reason, "failed", "safety gate rejected")
            return False, reason
        fresh_expiry = conn.execute(
            """SELECT source, expires_at FROM member_expiry
               WHERE team_id = ? AND kicked = 0
                 AND ((? != '' AND user_id = ?) OR (? != '' AND lower(email) = ?))
               ORDER BY id DESC LIMIT 1""",
            (team_id, user_id, user_id, email, email),
        ).fetchone()
        if not fresh_expiry or fresh_expiry["source"] != "detected" or fresh_expiry["expires_at"]:
            reason = "rejected: member was authorized before destructive action"
            _log_operation_sync(team_id, "patrol_strict_kick", email, reason, "failed", "safety gate rejected")
            return False, reason

        result = run_chatgpt_call_sync(client.remove_member, user_id)
        if isinstance(result, dict) and "error" in result:
            _log_operation_sync(team_id, "patrol_strict_kick", email, f"user_id={user_id}", "failed", result["error"])
            return False, result["error"]

        _mark_member_kicked_sync(conn, team_id, kick_source, user_id, email)
        _log_operation_sync(team_id, "patrol_strict_kick", email, f"user_id={user_id}", "success")
        return True, None


# ── 席位提醒：Premium / 未知席位，只读、只发提醒（踢人在 run_patrol 的 Premium 小节） ──

PREMIUM_ALERT_KEY_PREFIX = "premium_seat:"
# 同一份名单多久再提醒一次。这不是故障：可能是管理员有意在 ChatGPT 后台开的 Premium，而
# TeamBoss 没有"已知悉"开关，按故障的 6 小时重复会变成关不掉的骚扰。名单一变（多了人、换了
# 人）就是新的一条，立即提醒；名单来回变时，最近提醒过的名单在这个间隔内不再重复。
PREMIUM_ALERT_INTERVAL = timedelta(days=7)
_MANAGED_SOURCES = ("system", "self_service")
# 同步发现一个人不在完整名单里、把他的 member_expiry 行关掉时写的 kick_source（scheduler.data_sync_job）。
_SYNC_ABSENT_KICK_SOURCE = "detected"
_PREMIUM_FINDING_KINDS = (
    "premium_outsider",
    "premium_detected_with_record",
    "premium_detected_was_managed",
    "premium_unswitched",
    "unknown_seat_type",
    "chatgpt_detected_was_managed",
)
_ALERT_NAME_LIMIT = 10


def _mask_email_for_alert(email: Any) -> str:
    """Telegram 里的邮箱脱敏，与 tg_bot._mask_owner_email 同一格式：本地部分前 5 位 + 服务商名。"""
    email = str(email or "").strip()
    if not email or "@" not in email:
        return email or "?"
    local, _, domain = email.partition("@")
    provider = domain.split(".", 1)[0] if domain else ""
    ell = "…" if len(local) > 5 else ""
    return f"{local[:5]}{ell}@{provider}" if provider else f"{local[:5]}{ell}"


def _parse_log_detail(detail: Any) -> dict[str, str]:
    """operation_logs.detail 的 "k=v, k=v" 文本拆成字典；同名键取第一次出现的值。"""
    parsed: dict[str, str] = {}
    for part in str(detail or "").split(", "):
        key, sep, value = part.partition("=")
        key = key.strip()
        if sep and key and key not in parsed:
            parsed[key] = value.strip()
    return parsed


_TEAMBOSS_SEAT_ACTIONS = ("change_seat", "invite_member")
# 超员策略拒绝（契约 §3.3：status=skipped，reason=overage_*）：上游什么都没发生，不算 TeamBoss 定过席位。
_POLICY_REFUSAL_REASONS = ("overage_forbidden", "overage_needs_confirmation")


def _teamboss_seat_logs_sync(
    conn: sqlite3.Connection, team_id: str, email: str, user_id: str
) -> list[dict]:
    """这个 Team + 这个人的 change_seat / invite_member / invite_gpt_member 日志（任何结果），新→旧。

    invite_gpt_member 是成员管理里批量邀请 ChatGPT 成员那条路（services/gpt_invites.py）写的。

    按 target_email（小写）或 detail 里的 user_id=…（change_seat 的 target_email 可能为空）对上。
    每条 {created_at, result, seat_type, refused}；refused = 被超员策略拒绝、上游没动。
    """
    email = (email or "").strip().lower()
    user_id = str(user_id or "")
    if not email and not user_id:
        return []
    rows = conn.execute(
        """SELECT target_email, detail, result, created_at FROM operation_logs
           WHERE team_id = ? AND action IN ('change_seat', 'invite_member', 'invite_gpt_member')
             AND ((? != '' AND lower(target_email) = ?) OR (? != '' AND instr(detail, ?) > 0))
           ORDER BY id DESC LIMIT 200""",
        (team_id, email, email, user_id, f"user_id={user_id}"),
    ).fetchall()
    logs = []
    for row in rows:
        detail = _parse_log_detail(row["detail"])
        same_email = bool(email) and (row["target_email"] or "").strip().lower() == email
        same_user = bool(user_id) and detail.get("user_id") == user_id
        if not (same_email or same_user):
            continue
        logs.append({
            "created_at": str(row["created_at"] or ""),
            "result": row["result"],
            "seat_type": detail.get("seat_type"),
            "refused": row["result"] == "skipped" or detail.get("reason") in _POLICY_REFUSAL_REASONS,
        })
    return logs


def _premium_code_uses_sync(conn: sqlite3.Connection, team_id: str, email: str) -> list[tuple[str, bool]]:
    """这个 Team + 邮箱的兑换（成功 / 待定 / 处理中），新→旧：[(created_at, 是不是 Premium 码)]。"""
    email = (email or "").strip().lower()
    if not email:
        return []
    try:
        rows = conn.execute(
            """SELECT atu.created_at, at.seat_type FROM access_token_uses atu
               JOIN access_tokens at ON at.id = atu.token_id
               WHERE atu.team_id = ? AND lower(atu.email) = ?
                 AND atu.result IN ('success', 'uncertain', 'pending')
               ORDER BY atu.created_at DESC, atu.id DESC LIMIT 50""",
            (team_id, email),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [
        (str(row["created_at"] or ""), normalize_seat_type(row["seat_type"]) == PREMIUM_SEAT_TYPE)
        for row in rows
    ]


def _teamboss_seat_record_sync(conn: sqlite3.Connection, team_id: str, email: str, user_id: str) -> bool:
    """TeamBoss 动过这个人在这个 Team 的席位没有（Premium 踢人的否决条件，宁可漏踢）。

    任何一条都算，不看先后、不看结果、不看目标席位类型：
    - 任何 change_seat / invite_member / invite_gpt_member 记录：成功、失败、待定、被超员策略拒绝都算；切到 ChatGPT 也算。
      管理员动过这个人就是有意的安排。切换的成功日志先于切换后的名单刷新落库，上游名单可能还停在
      Premium，下一份快照照样列着 prolite；光看"快照开始之后有没有改过"挡不住这种情况。
    - Premium 兑换码在这个 Team 给这个邮箱的兑换（成功 / 待定 / 处理中）。
    """
    if _teamboss_seat_logs_sync(conn, team_id, email, user_id):
        return True
    return any(is_premium for _, is_premium in _premium_code_uses_sync(conn, team_id, email))


def _teamboss_managed_history_sync(conn: sqlite3.Connection, team_id: str, email: str, user_id: str) -> bool:
    """TeamBoss 有没有正在管、或者因为名单里不见了才没在管这个人（两条踢人路径共用的否决条件）。

    同步发现一个人不在完整名单里会把他的记录关掉（kicked=1、kick_source='detected'）；他再出现时是
    一条新的 detected 记录、没有到期时间，光看当前记录就像外人。所以这个 Team + 这个人（邮箱或
    user_id）下面任何一样都算：
    - member_expiry 里 source 为 system / self_service、还开着的行；
    - 同样来源、被同步因为"名单里不见了"关掉的行（kick_source='detected'）；
    - access_token_uses 里成功 / 待定 / 处理中的兑换（任何席位类型）。
    到期踢掉（auto_expire）、管理员移出（admin）、巡逻移出、踢人时补写的审计行都不算：服务已经结束，
    这个人之后从 TeamBoss 外面再进来，就是普通的外部成员。
    读不出来按"有"处理（宁可漏踢）。
    """
    email = (email or "").strip().lower()
    user_id = str(user_id or "")
    if not email and not user_id:
        return False
    try:
        managed_row = conn.execute(
            f"""SELECT 1 FROM member_expiry
               WHERE team_id = ?
                 AND source IN ({", ".join("?" for _ in _MANAGED_SOURCES)})
                 AND ((? != '' AND user_id = ?) OR (? != '' AND lower(email) = ?))
                 AND (kicked = 0 OR kick_source = ?)
               LIMIT 1""",
            (team_id, *_MANAGED_SOURCES, user_id, user_id, email, email, _SYNC_ABSENT_KICK_SOURCE),
        ).fetchone()
        if managed_row:
            return True
        redeemed_row = conn.execute(
            """SELECT 1 FROM access_token_uses
               WHERE team_id = ? AND result IN ('success', 'uncertain', 'pending')
                 AND ((? != '' AND lower(email) = ?) OR (? != '' AND user_id = ?))
               LIMIT 1""",
            (team_id, email, email, user_id, user_id),
        ).fetchone()
    except sqlite3.Error:
        return True
    return redeemed_row is not None


def _premium_kick_veto_sync(conn: sqlite3.Connection, team_id: str, email: str, user_id: str) -> Optional[str]:
    """Premium 外部成员不能踢的记录原因（没有返回 None）。候选筛选、_patrol_kick 的 claim 前后都用它。"""
    if _teamboss_seat_record_sync(conn, team_id, email, user_id):
        return "TeamBoss has a seat or invite record for this member"
    if _teamboss_managed_history_sync(conn, team_id, email, user_id):
        return "TeamBoss placed or sold a seat to this member before"
    return None


def select_over_quota_kick_candidates_sync(
    conn: sqlite3.Connection, team_id: str, members: Any, over_by: int
) -> tuple[list[dict], list[dict]]:
    """超员踢人这一轮要踢的人，返回 (要踢的人, 因 TeamBoss 记录剔除的人)。

    先按 select_kick_candidates 的顺序（first_seen_at 新→旧，邮箱是 teams.owner_email 的人当 Owner、
    不算候选）取最新的 over_by 个，再去掉 TeamBoss
    正在管、或因名单里不见了才没在管的人（_teamboss_managed_history_sync）。被剔除的人空出来的名额
    不往后补：更老的外部成员本来就在额度之内，不能因为别人受保护就轮到他。所以踢的人数只会比
    over_by 少，不会多。
    run_patrol 选人（真踢和空跑）和 _patrol_kick 的闸门都用这一份，两边对"这一轮能踢谁"理解一致。
    """
    selected: list[dict] = []
    vetoed: list[dict] = []
    if not isinstance(over_by, int) or over_by <= 0:
        return selected, vetoed
    owner_email = _team_owner_email_sync(conn, team_id)
    for member in select_kick_candidates(members, owner_email)[:over_by]:
        email = member.get("email") or ""
        user_id = member.get("id") or member.get("user_id") or ""
        if _teamboss_managed_history_sync(conn, team_id, email, user_id):
            vetoed.append(member)
        else:
            selected.append(member)
    return selected, vetoed


def outsider_batch_guard(members: Any) -> tuple[bool, int]:
    """Premium 踢人的"别一次踢一片"护栏：(这份名单是否异常, 外部成员数)。

    数的是全部非 Owner、source == 'detected' 的成员（select_detected_outsiders，不看席位类型、
    不看严格模式开没开），阈值同 strict_kick_batch_guard_exceeded。异常时这一轮这个 Team 一个
    Premium 外部成员都不踢、只提醒。
    超员踢人不套这道护栏：生产上的 Team 只有 1–4 人、1–2 个席位，Owner 也算人数，阈值
    min(3, 人数 // 2) 会把每一次正当的超员踢人都拦下；超员踢人有 over_by 和单轮封顶
    NON_STRICT_KICK_ABS_CAP 管着数量。
    """
    count = len(select_detected_outsiders(members))
    team_size = len(members) if isinstance(members, list) else 0
    return strict_kick_batch_guard_exceeded(count, team_size), count


def _teamboss_set_premium_sync(conn: sqlite3.Connection, team_id: str, email: str, user_id: str) -> bool:
    """TeamBoss 最近一次给这个人定席位时，定的是不是 Premium（只用于 TeamBoss 成员的提醒）。

    看 change_seat / invite_member / invite_gpt_member 记录（被超员策略拒绝、没带目标席位的不算）和 Premium 兑换码兑换，
    两边取时间最新的一条：是 Premium = TeamBoss 切的；不是 Premium，或者一条都没有 = 不是 TeamBoss
    切的。不设时间窗口：operation_logs 不做清理，加窗口只会让 TeamBoss 自己切的 Premium 成员过了
    窗口后被误报。
    """
    latest: Optional[tuple[str, bool]] = None
    for log in _teamboss_seat_logs_sync(conn, team_id, email, user_id):
        if log["refused"] or not log["seat_type"]:
            continue
        latest = (log["created_at"], normalize_seat_type(log["seat_type"]) == PREMIUM_SEAT_TYPE)
        break
    uses = _premium_code_uses_sync(conn, team_id, email)
    if uses and (latest is None or uses[0][0] > latest[0]):
        latest = uses[0]
    return bool(latest and latest[1])


def _teamboss_seat_change_after_sync(
    conn: sqlite3.Connection, team_id: str, email: str, user_id: str, snapshot_started_at: Any
) -> bool:
    """快照开始拉取（member_cache.fetch_started_at）之后 TeamBoss 有没有动过这个人的席位（任何结果都算）。

    比如刚切回 ChatGPT、切换后的刷新失败，快照还写着 prolite；或者一次刷新先读到 prolite、卡在
    半路，管理员这时切回 ChatGPT，它才写回缓存。比的是开始时间，不是写入时间。开始时间为空
    （旧数据）或读不出、日志时间读不出，都按"动过"处理（宁可这一轮不踢）。
    """
    snapshot = _parse_iso_datetime(snapshot_started_at)
    if snapshot is None:
        return True
    for log in _teamboss_seat_logs_sync(conn, team_id, email, user_id):
        logged_at = _parse_iso_datetime(log["created_at"])
        if logged_at is None or logged_at >= snapshot:
            return True
    return False


def premium_seat_findings_sync(
    conn: sqlite3.Connection,
    team_id: str,
    members: Any,
    pending: Any,
    *,
    outsiders_tracked: bool,
    pending_outsiders_revoked: bool,
    handled_ids: Iterable[str] = (),
    over_quota_vetoed: Iterable[dict] = (),
) -> list[dict]:
    """找出要提醒管理员的席位（只读，不动手），每条 {kind, email, user_id, seat_type, source, status}：

    - premium_outsider：本轮没被移除的外部 Premium 成员 / 外部 Premium 邀请（source=detected）：巡逻
      没开、Team 豁免或开了 Codex、被安全规则拦下或接口失败。只在该 Team 建过巡逻基线后才算（outsiders_tracked）：
      基线之前的 detected 也包括没被保护过的老成员。本轮已移除、或因快照过期推迟到下一轮的
      （handled_ids，邮箱小写或 user_id），和本轮会被巡逻撤掉的外部邀请（pending_outsiders_revoked）
      不算：前者有自己的通知或下一轮再判，后者有撤销通知。
    - premium_detected_with_record：来源是 detected，但 TeamBoss 动过他在这个 Team 的席位（任何
      change_seat / invite_member / invite_gpt_member 记录，或 Premium 兑换码兑换，_teamboss_seat_record_sync）。管理员
      动过他就是有意的安排，不自动踢，交给管理员。
    - premium_detected_was_managed：来源是 detected，但 TeamBoss 还在管他、或因为名单里不见了才没在管
      他、或他在这个 Team 兑换过（_teamboss_managed_history_sync，常见于付费成员掉出名单后又回来）。
      不自动踢，交给管理员。
    - premium_unswitched：TeamBoss 管理的成员 / 邀请（source=system/self_service）在 Premium 席位上，
      但 TeamBoss 最近一次给他定的席位不是 Premium（_teamboss_set_premium_sync）。
    - unknown_seat_type：注册表外的席位类型（如 automation）。TeamBoss 对这些人什么都不做（包括到期
      踢人），所以列给管理员；外部的同样要求 outsiders_tracked。
    - chatgpt_detected_was_managed：超员 Team 上落在最新 over_by 个里、本来这一轮要踢，但因为
      _teamboss_managed_history_sync 没踢的人（调用方传入 over_quota_vetoed，见
      select_over_quota_kick_candidates_sync）。不踢，交给管理员。
    Owner（上游角色是 account-owner，或邮箱等于 teams.owner_email）、没有来源记录（source 为空）的人不算。
    """
    handled = {str(x) for x in handled_ids}
    owner_email = _team_owner_email_sync(conn, team_id)
    findings: list[dict] = []
    for status, items in (("member", members), ("pending", pending)):
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or item.get("is_owner"):
                continue
            if _is_team_owner_email(item, owner_email):
                continue
            source = item.get("source")
            seat = normalize_seat_type(item.get("seat_type"))
            email = (item.get("email") or "").strip().lower()
            user_id = str(item.get("id") or item.get("user_id") or "") if status == "member" else ""
            if not email and not user_id:
                continue
            if source == "detected":
                if not outsiders_tracked:
                    continue
                if seat == PREMIUM_SEAT_TYPE:
                    if status == "pending" and pending_outsiders_revoked:
                        continue
                    if status == "member" and (email or user_id) in handled:
                        continue
                    if status == "member" and _teamboss_seat_record_sync(conn, team_id, email, user_id):
                        kind = "premium_detected_with_record"
                    elif status == "member" and _teamboss_managed_history_sync(conn, team_id, email, user_id):
                        kind = "premium_detected_was_managed"
                    else:
                        kind = "premium_outsider"
                elif not is_known_seat_type(seat):
                    kind = "unknown_seat_type"
                else:
                    continue
            elif source in _MANAGED_SOURCES:
                if seat == PREMIUM_SEAT_TYPE:
                    if _teamboss_set_premium_sync(conn, team_id, email, user_id):
                        continue
                    kind = "premium_unswitched"
                elif not is_known_seat_type(seat):
                    kind = "unknown_seat_type"
                else:
                    continue
            else:
                continue
            findings.append({
                "kind": kind,
                "email": email,
                "user_id": user_id,
                "seat_type": seat,
                "source": source,
                "status": status,
            })
    if outsiders_tracked:
        for item in over_quota_vetoed:
            if not isinstance(item, dict):
                continue
            email = (item.get("email") or "").strip().lower()
            user_id = str(item.get("id") or item.get("user_id") or "")
            if not email and not user_id:
                continue
            findings.append({
                "kind": "chatgpt_detected_was_managed",
                "email": email,
                "user_id": user_id,
                "seat_type": normalize_seat_type(item.get("seat_type")),
                "source": item.get("source"),
                "status": "member",
            })
    return findings


def _premium_findings_digest(findings: list[dict]) -> str:
    """名单指纹：同一批人（不分成员 / 邀请，接受邀请不算新情况）得到同一个告警 key。"""
    keys = sorted({f"{f['kind']}:{f['email'] or f['user_id']}" for f in findings})
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()[:16]


def _premium_alert_card(
    name: str, findings: list[dict], is_reminder: bool, unhandled_reason: str = ""
) -> str:
    def names(items: list[dict], *, with_seat: bool = False, mark_pending: bool = True) -> str:
        labels = []
        for f in items[:_ALERT_NAME_LIMIT]:
            label = _mask_email_for_alert(f["email"]) if f["email"] else f"用户 {f['user_id'][:12]}"
            extras = []
            if with_seat:
                extras.append(seat_type_label(f["seat_type"]))
            if mark_pending and f["status"] == "pending":
                extras.append("邀请未接受")
            labels.append(label + (f"（{'，'.join(extras)}）" if extras else ""))
        text = "、".join(labels)
        if len(items) > _ALERT_NAME_LIMIT:
            text += f" 等 {len(items)} 人"
        return text

    outsider_members = [f for f in findings if f["kind"] == "premium_outsider" and f["status"] == "member"]
    outsider_invites = [f for f in findings if f["kind"] == "premium_outsider" and f["status"] == "pending"]
    with_record = [f for f in findings if f["kind"] == "premium_detected_with_record"]
    was_managed = [f for f in findings if f["kind"] == "premium_detected_was_managed"]
    unswitched = [f for f in findings if f["kind"] == "premium_unswitched"]
    unknown = [f for f in findings if f["kind"] == "unknown_seat_type"]
    chatgpt_managed = [f for f in findings if f["kind"] == "chatgpt_detected_was_managed"]
    has_premium = bool(outsider_members or outsider_invites or with_record or was_managed or unswitched)

    rows = []
    if outsider_members:
        rows.append("👤 外部成员占用 Premium 席位：" + names(outsider_members))
    if outsider_invites:
        rows.append("📨 外部 Premium 邀请（未接受）：" + names(outsider_invites, mark_pending=False))
    if (outsider_members or outsider_invites) and unhandled_reason:
        rows.append(unhandled_reason)
    if with_record:
        rows.append(
            "🔎 来源记录是外部加入，但 TeamBoss 给他开过 Premium、改过他的席位或邀请过他，没有自动移除："
            + names(with_record)
        )
    if was_managed:
        rows.append(
            "🔎 来源记录是外部加入，但 TeamBoss 以前拉过他或他兑换过（可能是老用户重新进来），没有自动移除："
            + names(was_managed)
        )
    if unswitched:
        rows.append("🔀 TeamBoss 成员被切到 Premium，但不是 TeamBoss 切的：" + names(unswitched))
    if unknown:
        rows.append("❔ TeamBoss 不认识的席位类型：" + names(unknown, with_seat=True))
    if chatgpt_managed:
        rows.append(
            "🔎 Team 超员，但这些外部加入的 ChatGPT 成员 TeamBoss 以前拉过或他兑换过（可能是老用户重新进来），"
            "巡逻没有移除：" + names(chatgpt_managed)
        )
    rows.append("🛑 以上的人 TeamBoss 没有移除、没有撤邀请、没有改席位")
    if has_premium:
        rows.append("💡 Premium 席位单独计费：请到 ChatGPT 后台确认是否需要，不需要就手动移出或改回 ChatGPT 席位")
    if unknown:
        rows.append("💡 不认识的席位类型 TeamBoss 一律不处理（包括到期踢人），请到 ChatGPT 后台人工确认")
    if chatgpt_managed:
        rows.append("💡 请在成员列表核对他们的到期时间；确实不该在的，手动移出")
    rows.append(f"🔕 名单不变时 {PREMIUM_ALERT_INTERVAL.days} 天内不再重复提醒")

    if has_premium:
        title = "🔁 Premium 席位提醒（仍存在）" if is_reminder else "💎 Premium 席位提醒"
    elif unknown:
        title = "🔁 未知席位类型提醒（仍存在）" if is_reminder else "❔ 未知席位类型提醒"
    else:
        title = "🔁 超员未移除提醒（仍存在）" if is_reminder else "🔎 超员未移除提醒"
    return detail_card(f"{title} · {name}", rows)


def _sent_count(value: Any) -> int:
    """notify 的返回值折成"送达几个管理员"；测试替身可能返回 None。"""
    if isinstance(value, bool):
        return int(value)
    return value if isinstance(value, int) else 0


def _report_premium_seat_findings_sync(
    team_id: str, name: str, findings: list[dict], *, unhandled_reason: str = ""
) -> Optional[dict]:
    """把一个 Team 本轮的发现交给 team_health_incidents 去重限频后发 Telegram，并记 patrol_premium_alert。

    告警 key = 前缀 + 名单指纹；名单变了、或清空了，旧 key 静默关掉（不发"恢复"）。
    patrol_premium_alert 每人一行，只在真发出去、或这份名单第一次出现（没配 Telegram 也留痕）时写，
    不会每轮都写。
    """
    if not findings:
        close_incident_family_sync(
            team_id, PREMIUM_ALERT_KEY_PREFIX, forget_after=PREMIUM_ALERT_INTERVAL
        )
        return None

    alert_key = PREMIUM_ALERT_KEY_PREFIX + _premium_findings_digest(findings)
    close_incident_family_sync(
        team_id,
        PREMIUM_ALERT_KEY_PREFIX,
        keep_alert_key=alert_key,
        forget_after=PREMIUM_ALERT_INTERVAL,
    )
    summary = ", ".join(
        f"{kind}={sum(1 for f in findings if f['kind'] == kind)}" for kind in _PREMIUM_FINDING_KINDS
    )
    outcome = report_team_failure_sync(
        team_id,
        alert_key,
        summary,
        source="patrol",
        notify_interval=PREMIUM_ALERT_INTERVAL,
        render=lambda is_reminder: _premium_alert_card(
            name, findings, is_reminder, unhandled_reason
        ),
        # 运行时再取模块里的 notify_admins_sync：巡逻的所有 Telegram 通知走同一个出口。
        notify=lambda text: _sent_count(notify_admins_sync(text)),
    )
    delivered = int(outcome.get("notified") or 0)
    if delivered > 0 or outcome.get("failure_count") == 1:
        for f in findings:
            _log_operation_sync(
                team_id,
                "patrol_premium_alert",
                f["email"] or None,
                f"kind={f['kind']}, seat_type={f['seat_type']}, source={f['source']}, "
                f"status={f['status']}, delivered_to={delivered}",
                "success" if delivered > 0 else "skipped",
                None if delivered > 0 else str(outcome.get("reason") or "not_delivered"),
            )
    return outcome


# ── 主入口 ───────────────────────────────────────────────────────────────

def run_patrol(
    dry_run: bool,
    allow_team_ids: Iterable[str],
    *,
    skip_over_quota_team_ids: Iterable[str] = (),
) -> dict:
    """跑一轮巡逻。返回 {"events": [...], "kicked": int, "would_kick": int, ...}。

    events 里每条要么是某个候选的踢人/预演动作记录，要么是豁免 Team 的
    "exempt_skip" 摘要（豁免 Team 从不实际处理候选，只上报供人工核查）。
    每个 risk == 'over' 的 Team（无论 dry-run 还是真踢、无论是否豁免）都会
    经 notify_admins_sync 推一条摘要给 TG 管理员。

    ``allow_team_ids`` 是**白名单**，必传：只有本轮刚刚成功刷新过快照的 team 才
    会被巡逻，其余一律不碰。这是故意选的方向——黑名单（"把本轮失败的 team 传进
    来跳过"）失败时是敞开的：调用方任何一次提前 return / 异常 / 忘记传参，都会
    交出一个空集合，于是巡逻拿着每个 team 的陈旧缓存全量开工，按冻住的席位数判
    超员、按冻住的名单挑人。白名单失败时是关闭的：拿不到就什么都不做。

    白名单已经隐含了"同步失败的 team 不巡逻"和"挂起的 team 不巡逻"，调用方不需要
    再额外传排除集合；同理，这里也不需要任何缓存新鲜度检查。

    ``skip_over_quota_team_ids``：白名单里这些 team 的成员快照是本轮新拉的，但库里
    的 seats_entitled 本轮没经上游确认（订阅接口报错、没给出正整数），只是上一轮
    留下的值。超员踢人是唯一拿它当分母的一段，这些 team 只跳过这一段；撤陌生邀请、
    严格模式不看席位数，照常执行。调用方把一个 team 放进白名单却漏放进这里，等于拿
    未确认的旧席位数判超员，所以两者必须在同一处决定（scheduler 的白名单判定）。
    """
    events: list[dict] = []
    kicked = 0
    would_kick = 0
    invites_revoked = 0
    invites_would_revoke = 0
    strict_kicked = 0
    strict_would_kick = 0
    conn: Optional[sqlite3.Connection] = None
    allow_ids = {str(x) for x in (allow_team_ids or ())}
    skip_over_quota_ids = {str(x) for x in (skip_over_quota_team_ids or ())}

    try:
        conn = _get_sync_db()
        settings = _read_patrol_settings(conn)
        kick_enabled = settings.get("patrol_kick_enabled") == "1"
        exempt_ids = set(parse_exempt_team_ids(settings.get("patrol_exempt_team_ids")))
        strict_mode_enabled = settings.get("patrol_strict_mode_enabled") == "1"

        # 三重总闸：显式 dry-run、真踢开关关闭、或尚未完成现有成员保护，任一成立都只空跑。
        baseline_ready = bool((settings.get("patrol_baseline_at") or "").strip())
        effective_dry_run = bool(dry_run) or not kick_enabled or not baseline_ready

        teams = conn.execute(
            "SELECT id, name, is_codex_enabled, seats_entitled, access_token, device_id, proxy_id, "
            "owner_email FROM teams WHERE status = 'active'"
        ).fetchall()

        _ensure_team_baseline_table(conn)
        initialized_team_ids = {
            str(row["team_id"])
            for row in conn.execute("SELECT team_id FROM patrol_team_baselines").fetchall()
        }

        for team in teams:
            team_id = team["id"]
            name = team["name"] or team_id
            codex_enabled = bool(team["is_codex_enabled"])
            # 未知（NULL/0/负数/非整数）时为 None，超员踢人整段跳过，见下方超员小节。
            seats_entitled = valid_seats_entitled(team["seats_entitled"])

            # 不在本轮白名单里的 team（同步失败、已挂起、或调用方压根没刷新过它）：
            # 完全不碰，不初始化、不判断、不通知。
            if str(team_id) not in allow_ids:
                continue

            # 巡逻已开启后新增/首次启用的 Team 必须单独建立基线。建立基线的这一轮
            # 永远不处理候选；快照不完整则继续跳过，宁可漏踢也不误踢。这是自动路径
            # （包括 token_expired 恢复、重新导入后的重建），不是管理员确认：只有从未
            # 建过基线的 Team 才保护现有成员，见 _protect_team_snapshot_sync。
            if kick_enabled and baseline_ready and team_id not in initialized_team_ids:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    grandfathered, backfilled, detected_kept = _protect_team_snapshot_sync(
                        conn,
                        team_id,
                        datetime.now(timezone.utc).isoformat(),
                        protect_detected=False,
                    )
                    conn.commit()
                    initialized_team_ids.add(str(team_id))
                    _log_operation_sync(
                        team_id,
                        "patrol_team_initialize",
                        None,
                        f"grandfathered={grandfathered}, backfilled={backfilled}, "
                        f"detected_kept={detected_kept}",
                        "success",
                    )
                except Exception as exc:
                    conn.rollback()
                    _log_operation_sync(
                        team_id,
                        "patrol_team_initialize",
                        None,
                        None,
                        "failed",
                        str(exc),
                    )
                continue

            # 冷启动/缓存缺失守卫：绝不对没有新鲜成员数据的 Team 动手。
            cache_row = conn.execute(
                "SELECT members_json, pending_json FROM member_cache WHERE team_id = ?", (team_id,)
            ).fetchone()
            if not cache_row or not cache_row["members_json"]:
                continue
            try:
                members = json.loads(cache_row["members_json"])
            except Exception:
                continue
            if not isinstance(members, list) or not members:
                continue
            try:
                pending = json.loads(cache_row["pending_json"] or "[]")
            except Exception:
                pending = []
            if not isinstance(pending, list):
                pending = []

            is_exempt = team_id in exempt_ids
            team_baseline_ready = team_id in initialized_team_ids

            # ── Premium 外部成员：自动踢（所有者裁决，巡逻唯一的新踢人路径）──────────
            # 每个外部 Premium 成员都是 ChatGPT 自动加购、按月扣费的席位，所以不看超员、不看
            # 严格模式。豁免和以前一样生效：豁免 Team、开了 Codex 的 Team 都不踢，只进席位提醒。
            # 动手只走 _patrol_kick(rule="premium_outsider")，那里的闸门（武装、基线、来源、Owner、
            # 对账屏障、member claim、claim 后复查）和超员踢人完全相同。
            # 本轮已移除、或推迟到下一轮的人：都不进下面的席位提醒。
            premium_handled_ids: set[str] = set()
            premium_guard_reason = ""
            # 本轮这个 Team 已经动过（或空跑里会动）的人数，Premium 和超员两条路合计不超过
            # NON_STRICT_KICK_ABS_CAP。真踢时请求发出去之前就算上，结果不明也算；被闸门拦下、
            # 推迟的没发请求，不算。
            round_kicks_used = 0
            # 这一段出错（例如读不了记录）只跳过本 Team 的 Premium 处理，不拖垮整轮巡逻。
            try:
                if team_baseline_ready and not is_exempt and not codex_enabled:
                    premium_outsiders = select_premium_kick_candidates(members, team["owner_email"])
                    # "别一次踢一片"：这份名单里的外部成员（不分席位类型，不看严格模式开没开）多到
                    # 超过严格模式同一个阈值，就当数据出了问题，这一轮这个 Team 一个 Premium 成员都
                    # 不踢、只提醒。超员踢人照它自己的规则走。
                    outsider_count = len(select_detected_outsiders(members))
                    if premium_outsiders and strict_kick_batch_guard_exceeded(
                        outsider_count, len(members)
                    ):
                        premium_guard_reason = (
                            f"🚨 外部成员数量异常（{outsider_count} / 团队共 {len(members)} 人），"
                            "怀疑数据异常，本轮 Premium 成员一个都没移除，请人工核查"
                        )
                        _log_operation_sync(
                            team_id, "patrol_kick_batch_capped", None,
                            f"reason=premium_outsider, batch_guard=outsiders, "
                            f"outsiders={outsider_count}, candidates={len(premium_outsiders)}, "
                            f"team_size={len(members)}, capped_to=0",
                            "capped",
                        )
                        events.append({
                            "team_id": team_id, "team_name": name, "action": "premium_batch_guard",
                            "guard": "outsiders", "count": outsider_count,
                            "premium_count": len(premium_outsiders), "team_size": len(members),
                        })
                        premium_outsiders = []
                    # TeamBoss 有记录的人（改过他的席位 / 邀请过他、卖过 Premium、还在管或因不在名单
                    # 才没在管、兑换过）不进候选，交给席位提醒。
                    premium_candidates = [
                        c for c in premium_outsiders
                        if not _premium_kick_veto_sync(
                            conn, team_id, c.get("email") or "", c.get("id") or c.get("user_id") or ""
                        )
                    ]
                    premium_selected = premium_candidates[:NON_STRICT_KICK_ABS_CAP]
                    if len(premium_candidates) > NON_STRICT_KICK_ABS_CAP:
                        _log_operation_sync(
                            team_id, "patrol_kick_batch_capped", None,
                            f"reason=premium_outsider candidates={len(premium_candidates)} "
                            f"capped_to={NON_STRICT_KICK_ABS_CAP}",
                            "capped",
                        )
                    premium_client = None
                    premium_kicked_emails: list[str] = []
                    premium_failed_emails: list[str] = []
                    for position, cand in enumerate(premium_selected, start=1):
                        email = cand.get("email") or ""
                        user_id = cand.get("id") or cand.get("user_id") or ""
                        reason = (
                            f"reason=premium_outsider position={position}/{len(premium_selected)} "
                            f"seat_type={cand.get('seat_type')} source={cand.get('source')} "
                            f"first_seen_at={cand.get('first_seen_at')}"
                        )
                        if effective_dry_run:
                            # 空跑只记日志、计数；Telegram 走下面限频的席位提醒，不每轮推一条。
                            _log_operation_sync(team_id, "patrol_would_kick", email, reason, "dryrun")
                            would_kick += 1
                            round_kicks_used += 1
                            events.append({
                                "team_id": team_id, "team_name": name, "email": email, "user_id": user_id,
                                "action": "would_kick", "result": "dryrun", "rule": "premium_outsider",
                                "position": position, "reason": reason,
                            })
                            continue
                        if premium_client is None:
                            proxy_url = _get_proxy_url_sync(conn, team["proxy_id"])
                            premium_client = ChatGPTClient(
                                team["access_token"], team_id, team["device_id"], proxy_url=proxy_url
                            )
                        round_kicks_used += 1
                        ok, err = _patrol_kick(
                            conn, premium_client, team_id, cand,
                            kick_source="patrol_premium", rule="premium_outsider",
                        )
                        deferred = not ok and err == PREMIUM_KICK_DEFERRED
                        if not ok and (deferred or str(err or "").startswith("rejected:")):
                            round_kicks_used -= 1
                        if ok:
                            kicked += 1
                            premium_kicked_emails.append(email)
                        elif not deferred:
                            premium_failed_emails.append(f"{email}（{err}）")
                        if ok or deferred:
                            premium_handled_ids.add((email or "").strip().lower() or str(user_id))
                        events.append({
                            "team_id": team_id, "team_name": name, "email": email, "user_id": user_id,
                            "action": "kick",
                            "result": "success" if ok else ("deferred" if deferred else "failed"),
                            "rule": "premium_outsider", "position": position,
                            "reason": reason, "error": err,
                        })
                    if premium_kicked_emails or premium_failed_emails:
                        rows = ["💎 判定依据：系统外加入、占用 Premium 席位（ChatGPT 按月单独扣费）、非 Owner"]
                        if premium_kicked_emails:
                            rows.append("✅ 已移除：" + "、".join(premium_kicked_emails))
                        if premium_failed_emails:
                            rows.append("⚠️ 处理失败：" + "、".join(premium_failed_emails))
                        notify_admins_sync(detail_card(f"🚨 巡逻移除 Premium 外部成员 · {name}", rows))
            except Exception as exc:
                _log_operation_sync(
                    team_id, "patrol_kick", None, "reason=premium_outsider", "failed", str(exc),
                )

            # ── 席位提醒：没被处理的 Premium / 未知席位，只提醒 ──────────────────
            # 空跑也提醒（和本函数里其他巡逻通知一致）；同一份名单按 PREMIUM_ALERT_INTERVAL 限频。
            # 出任何错都只记日志，绝不影响下面的撤邀请 / 严格模式 / 超员处理。
            try:
                # 超员 Team 上本来这一轮要踢、因为 TeamBoss 记录没踢的人，也走这条限频提醒。
                # 条件和下面超员小节真正选人时一致（豁免 / Codex / 席位数未确认的 Team 不选人）。
                over_quota_vetoed: list[dict] = []
                if (
                    not is_exempt
                    and not codex_enabled
                    and seats_entitled is not None
                    and str(team_id) not in skip_over_quota_ids
                ):
                    alert_status = classify_team(
                        team_id=team_id, name=name, codex_enabled=codex_enabled,
                        seats_entitled=seats_entitled, members=members,
                    )
                    if alert_status["risk"] == "over":
                        _selected, over_quota_vetoed = select_over_quota_kick_candidates_sync(
                            conn, team_id, members, alert_status["over_by"]
                        )
                premium_findings = premium_seat_findings_sync(
                    conn, team_id, members, pending,
                    outsiders_tracked=team_baseline_ready,
                    # 本轮真撤的外部邀请不进提醒：撤销有自己的通知，人也没进来。
                    pending_outsiders_revoked=(
                        team_baseline_ready and not is_exempt and not effective_dry_run
                    ),
                    handled_ids=premium_handled_ids,
                    over_quota_vetoed=over_quota_vetoed,
                )
                if is_exempt:
                    unhandled_reason = "🛡️ Team 已豁免巡逻，外部 Premium 成员不会被自动移除"
                elif codex_enabled:
                    unhandled_reason = "🛡️ Team 开了 Codex，巡逻不在这类 Team 踢人，外部 Premium 成员不会被自动移除"
                elif premium_guard_reason:
                    unhandled_reason = premium_guard_reason
                elif effective_dry_run:
                    unhandled_reason = "⏸️ 巡逻自动踢人没开，外部 Premium 成员不会被自动移除"
                else:
                    unhandled_reason = "⚠️ 本轮没能自动移除（被安全规则拦下或接口失败），请人工确认"
                premium_outcome = _report_premium_seat_findings_sync(
                    team_id, name, premium_findings, unhandled_reason=unhandled_reason
                )
                if premium_outcome and premium_outcome.get("notified"):
                    events.append({
                        "team_id": team_id, "team_name": name, "action": "premium_alert",
                        "count": len(premium_findings),
                    })
            except Exception as exc:
                _log_operation_sync(team_id, "patrol_premium_alert", None, None, "failed", str(exc))

            # ── 陌生 pending invite 自动撤销：跟随"监控中"状态，不看超员/codex ──
            # "监控中" = 已武装（全局 + 该 team 均已完成基线保护）+ 该 team 未豁免 + active。
            # 建立基线时该保护的邀请早已被 grandfather 成 source='system'，
            # 所以这里不需要再单独判断时间戳。
            if team_baseline_ready:
                invite_candidates = select_invite_revoke_candidates(pending)
                if invite_candidates:
                    if is_exempt:
                        rows = [
                            "📨 陌生邀请：" + "、".join(c.get("email") or "?" for c in invite_candidates),
                            "🛡️ 处理状态：Team 已豁免，未自动处理",
                        ]
                        notify_admins_sync(detail_card(f"🛡️ 巡逻发现陌生邀请 · {name}", rows))
                        events.append({
                            "team_id": team_id, "team_name": name, "action": "exempt_skip_invite",
                            "count": len(invite_candidates),
                        })
                    else:
                        revoked_emails: list[str] = []
                        failed_invite_emails: list[str] = []
                        would_revoke_emails: list[str] = []
                        invite_client = None
                        for inv in invite_candidates:
                            inv_email = (inv.get("email") or "").strip().lower()
                            reason = (
                                f"source={inv.get('source')} first_seen_at={inv.get('first_seen_at')}"
                            )
                            if effective_dry_run:
                                _log_operation_sync(team_id, "patrol_would_revoke_invite", inv_email, reason, "dryrun")
                                would_revoke_emails.append(inv_email)
                                invites_would_revoke += 1
                                events.append({
                                    "team_id": team_id, "team_name": name, "email": inv_email,
                                    "action": "would_revoke_invite", "result": "dryrun", "reason": reason,
                                })
                                continue

                            if invite_client is None:
                                proxy_url = _get_proxy_url_sync(conn, team["proxy_id"])
                                invite_client = ChatGPTClient(
                                    team["access_token"], team_id, team["device_id"], proxy_url=proxy_url
                                )
                            ok, err = _patrol_revoke_invite(conn, invite_client, team_id, inv)
                            if ok:
                                revoked_emails.append(inv_email)
                                invites_revoked += 1
                            else:
                                failed_invite_emails.append(f"{inv_email}（{err}）")
                            events.append({
                                "team_id": team_id, "team_name": name, "email": inv_email,
                                "action": "revoke_invite", "result": "success" if ok else "failed",
                                "reason": reason, "error": err,
                            })

                        if effective_dry_run and would_revoke_emails:
                            notify_admins_sync(detail_card(
                                f"🧪 巡逻空跑 · 陌生邀请 · {name}",
                                ["🧪 空跑候选：" + "、".join(would_revoke_emails)],
                            ))
                        elif revoked_emails or failed_invite_emails:
                            rows = []
                            if revoked_emails:
                                rows.append("✅ 已撤销：" + "、".join(revoked_emails))
                            if failed_invite_emails:
                                rows.append("⚠️ 撤销失败：" + "、".join(failed_invite_emails))
                            notify_admins_sync(detail_card(f"🚫 巡逻自动撤销陌生邀请 · {name}", rows))

            # ── 严格模式：所有 team（含 Codex）+ 非系统拉入的人 + 不看超员 ────────
            # 独立危险开关，默认关闭；必须先有该 team 的基线快照才生效。
            if strict_mode_enabled and team_baseline_ready:
                # 护栏按全部疑似陌生成员计数（不分席位类型，和以前一样）；只从允许动手的
                # 席位类型里挑候选。Premium 外部成员走上面的 Premium 路径，未知类型只进席位提醒。
                strict_outsiders = select_strict_outsiders(members)
                strict_candidates = select_strict_kick_candidates(members)
                team_size = len(members)

                if strict_candidates and is_exempt:
                    emails = [c.get("email") or "?" for c in strict_candidates]
                    notify_admins_sync(detail_card(
                        f"🛡️ 严格模式发现疑似陌生成员 · {name}",
                        [f"👤 成员：{'、'.join(emails)}", "🛡️ 处理状态：Team 已豁免，未自动处理"],
                    ))
                    events.append({
                        "team_id": team_id, "team_name": name, "action": "strict_exempt_skip",
                        "count": len(strict_candidates),
                    })
                elif strict_candidates and strict_kick_batch_guard_exceeded(len(strict_outsiders), team_size):
                    # "别一次踢一片"：数量异常多，很可能是数据 half-broken 导致的误判，
                    # 宁可漏踢也不能误清一个队——只报警，不做任何自动处理。
                    notify_admins_sync(detail_card(
                        f"🚨 严格模式异常：陌生成员数量过多 · {name}",
                        [
                            f"👥 疑似陌生成员：{len(strict_outsiders)} / 团队共 {team_size} 人",
                            "🛑 数量超过安全阈值，怀疑是数据异常，已跳过自动处理，请人工核查",
                        ],
                    ))
                    _log_operation_sync(
                        team_id, "patrol_strict_batch_guard", None,
                        f"count={len(strict_outsiders)} team_size={team_size}", "failed",
                    )
                    events.append({
                        "team_id": team_id, "team_name": name, "action": "strict_batch_guard",
                        "count": len(strict_outsiders), "team_size": team_size,
                    })
                elif strict_candidates:
                    mode, delay_hours = _read_kick_delay_settings_sync(conn)
                    now_dt = datetime.now(timezone.utc)
                    newly_flagged = []
                    ready_candidates = []
                    for cand in strict_candidates:
                        cand_email = (cand.get("email") or "").strip().lower()
                        if _is_strict_candidate_ready(cand.get("first_seen_at"), mode, delay_hours, now_dt):
                            ready_candidates.append(cand)
                        elif not _already_flagged_strict_sync(conn, team_id, cand_email):
                            newly_flagged.append(cand)

                    if newly_flagged:
                        rows = []
                        for cand in newly_flagged:
                            cand_email = (cand.get("email") or "").strip().lower()
                            ready_at = _strict_kick_ready_at(cand.get("first_seen_at"), mode, delay_hours)
                            ready_at_txt = ready_at.isoformat() if ready_at else "未知（缺少 first_seen_at，将不会自动处理）"
                            rows.append(f"👤 {cand_email}：预计 {ready_at_txt} 起可处理")
                            _log_operation_sync(
                                team_id, "patrol_strict_flagged", cand_email,
                                f"first_seen_at={cand.get('first_seen_at')}", "flagged",
                            )
                            events.append({
                                "team_id": team_id, "team_name": name, "email": cand_email,
                                "action": "strict_flagged",
                            })
                        rows.append("🔎 判定依据：非系统邀请、无到期记录、非 Owner")
                        rows.append("💡 如为误判，请尽快豁免该 Team 或人工处理")
                        notify_admins_sync(detail_card(f"🕒 严格模式检测到疑似陌生成员 · {name}", rows))

                    if ready_candidates:
                        if effective_dry_run:
                            would_emails = []
                            for cand in ready_candidates:
                                cand_email = (cand.get("email") or "").strip().lower()
                                _log_operation_sync(
                                    team_id, "patrol_would_strict_kick", cand_email,
                                    f"first_seen_at={cand.get('first_seen_at')}", "dryrun",
                                )
                                strict_would_kick += 1
                                would_emails.append(cand_email)
                                events.append({
                                    "team_id": team_id, "team_name": name, "email": cand_email,
                                    "action": "would_strict_kick", "result": "dryrun",
                                })
                            notify_admins_sync(detail_card(
                                f"🧪 严格模式空跑候选 · {name}",
                                ["🧪 空跑候选：" + "、".join(would_emails)],
                            ))
                        else:
                            # 动手之前必须对该 team 强制实时刷新一次，不能拿旧缓存去踢人。
                            refresh_ok, refresh_err, strict_client = _refresh_team_snapshot_sync(conn, team)
                            if not refresh_ok:
                                notify_admins_sync(detail_card(
                                    f"⚠️ 严格模式实时刷新失败 · {name}",
                                    [f"❌ 错误：{refresh_err}", "🛑 本轮跳过该 Team 的严格模式处理"],
                                ))
                                _log_operation_sync(
                                    team_id, "patrol_strict_refresh_failed", None, None, "failed", refresh_err,
                                )
                                events.append({
                                    "team_id": team_id, "team_name": name,
                                    "action": "strict_refresh_failed", "error": refresh_err,
                                })
                            else:
                                fresh_row = conn.execute(
                                    "SELECT members_json FROM member_cache WHERE team_id = ?", (team_id,)
                                ).fetchone()
                                try:
                                    fresh_members = json.loads(fresh_row["members_json"]) if fresh_row and fresh_row["members_json"] else []
                                except Exception:
                                    fresh_members = []
                                fresh_outsiders = select_strict_outsiders(fresh_members)
                                fresh_candidates = select_strict_kick_candidates(fresh_members)
                                fresh_now = datetime.now(timezone.utc)
                                fresh_ready = [
                                    c for c in fresh_candidates
                                    if _is_strict_candidate_ready(c.get("first_seen_at"), mode, delay_hours, fresh_now)
                                ]
                                # 刷新后复核一次批量护栏——刷新可能暴露出更严重的数据异常。
                                if strict_kick_batch_guard_exceeded(len(fresh_outsiders), len(fresh_members)):
                                    notify_admins_sync(detail_card(
                                        f"🚨 严格模式异常（刷新后复核）· {name}",
                                        [
                                            f"👥 疑似陌生成员：{len(fresh_outsiders)} / 团队共 {len(fresh_members)} 人",
                                            "🛑 数量超过安全阈值，已跳过自动处理，请人工核查",
                                        ],
                                    ))
                                    _log_operation_sync(
                                        team_id, "patrol_strict_batch_guard", None,
                                        f"post_refresh count={len(fresh_outsiders)} team_size={len(fresh_members)}",
                                        "failed",
                                    )
                                    events.append({
                                        "team_id": team_id, "team_name": name,
                                        "action": "strict_batch_guard_post_refresh",
                                    })
                                else:
                                    kicked_emails = []
                                    failed_strict_emails = []
                                    for cand in fresh_ready:
                                        cand_email = (cand.get("email") or "").strip().lower()
                                        ok, err = _patrol_strict_kick(conn, strict_client, team_id, cand)
                                        if ok:
                                            kicked_emails.append(cand_email)
                                            strict_kicked += 1
                                        else:
                                            failed_strict_emails.append(f"{cand_email}（{err}）")
                                        events.append({
                                            "team_id": team_id, "team_name": name, "email": cand_email,
                                            "action": "strict_kick", "result": "success" if ok else "failed",
                                            "error": err,
                                        })
                                    if kicked_emails or failed_strict_emails:
                                        rows = ["🔎 判定依据：非系统邀请、无到期记录、非 Owner、已过等待期"]
                                        if kicked_emails:
                                            rows.append("✅ 已移除：" + "、".join(kicked_emails))
                                        if failed_strict_emails:
                                            rows.append("⚠️ 处理失败：" + "、".join(failed_strict_emails))
                                        notify_admins_sync(detail_card(f"🔴 严格模式已处理疑似陌生成员 · {name}", rows))

            # ── 超员踢人：唯一依赖 seats_entitled 的一段 ──────────────────────
            # 席位数未知时 over_by 无从计算；按 0 算会把所有 detected 成员都当成超员候选。
            # 本轮跳过该 Team 的超员处理（真踢和空跑都不出候选），只记一条日志；上面的
            # 撤陌生邀请和严格模式不看席位数，照常执行。Codex 开的 Team 本来就不做超员踢人。
            if seats_entitled is None and not codex_enabled:
                _log_operation_sync(
                    team_id, "patrol_skip_invalid_entitlement", None,
                    f"seats_entitled={repr(team['seats_entitled'])[:64]}", "skipped",
                )
                events.append({
                    "team_id": team_id, "team_name": name,
                    "action": "skip_invalid_entitlement",
                })
                continue
            # 库里的值合法但本轮没经上游确认（见 skip_over_quota_team_ids）：同样只跳过超员。
            if str(team_id) in skip_over_quota_ids and not codex_enabled:
                _log_operation_sync(
                    team_id, "patrol_skip_unconfirmed_entitlement", None,
                    f"stored seats_entitled={repr(team['seats_entitled'])[:64]} not confirmed this round",
                    "skipped",
                )
                events.append({
                    "team_id": team_id, "team_name": name,
                    "action": "skip_unconfirmed_entitlement",
                })
                continue

            status = classify_team(
                team_id=team_id, name=name, codex_enabled=codex_enabled,
                seats_entitled=seats_entitled, members=members,
            )
            if status["risk"] != "over":
                continue

            over_by = status["over_by"]
            candidates = select_kick_candidates(members, team["owner_email"])
            # 先取最新的 over_by 个，再去掉 TeamBoss 还在管 / 因不在名单才没在管 / 兑换过的人（上面的
            # 限频提醒里列给管理员）。空出的名额不往后补，所以只会比 over_by 少踢。
            # 超员踢人不套 Premium 的"外部成员过多"护栏（见 outsider_batch_guard）：小 Team 上它会
            # 拦下每一次正当的超员踢人。数量由 over_by 和下面的单轮封顶管着。
            selected, history_vetoed = select_over_quota_kick_candidates_sync(
                conn, team_id, members, over_by
            )
            removable = len(selected)

            # 绝对保险丝：单轮踢人数封顶，挡住 over_by 因数据异常被抬高导致的"一趟踢光"。
            # 本轮 Premium 那段已经动过的人数先扣掉（两条路合计封顶）。命中说明这轮踢得不正常，
            # 记一条日志让人来查，超出的部分留给下一轮。
            over_quota_cap = max(0, NON_STRICT_KICK_ABS_CAP - round_kicks_used)
            if len(selected) > over_quota_cap:
                capped_detail = (
                    f"over_by={over_by} candidates={len(candidates)} capped_to={over_quota_cap}"
                )
                if round_kicks_used:
                    capped_detail += f" premium_kicks={round_kicks_used}"
                _log_operation_sync(
                    team_id, "patrol_kick_batch_capped", None, capped_detail, "capped",
                )
                selected = selected[:over_quota_cap]

            insufficient_note = None
            if removable < over_by:
                insufficient_note = (
                    f"超额 {over_by}，仅 {removable} 个外部成员可移除，剩余需人工核查"
                )

            if is_exempt:
                rows = [
                    f"💺 GPT 席位：{status['active_chatgpt']} / {seats_entitled}（超 {over_by}）",
                    "💻 Codex：关闭 ⏸️",
                    "🛡️ 处理状态：Team 已豁免，未自动处理",
                ]
                if insufficient_note:
                    rows.append(f"👤 人工核查：{insufficient_note}")
                notify_admins_sync(detail_card(f"⚠️ 巡逻发现超员 · {name}", rows))
                events.append({
                    "team_id": team_id,
                    "team_name": name,
                    "action": "exempt_skip",
                    "over_by": over_by,
                    "detected_count": len(candidates),
                })
                continue

            client = None
            if not effective_dry_run:
                proxy_url = _get_proxy_url_sync(conn, team["proxy_id"])
                client = ChatGPTClient(team["access_token"], team_id, team["device_id"], proxy_url=proxy_url)

            kicked_emails: list[str] = []
            would_kick_emails: list[str] = []

            for position, cand in enumerate(selected, start=1):
                email = cand.get("email") or ""
                user_id = cand.get("id") or cand.get("user_id") or ""
                reason = (
                    f"over_by={over_by} position={position}/{len(selected)} "
                    f"seat_type={cand.get('seat_type')} source={cand.get('source')} "
                    f"first_seen_at={cand.get('first_seen_at')}"
                )

                if effective_dry_run:
                    _log_operation_sync(team_id, "patrol_would_kick", email, reason, "dryrun")
                    would_kick_emails.append(email)
                    would_kick += 1
                    events.append({
                        "team_id": team_id, "team_name": name, "email": email, "user_id": user_id,
                        "action": "would_kick", "result": "dryrun", "over_by": over_by,
                        "position": position, "reason": reason,
                    })
                    continue

                ok, err = _patrol_kick(conn, client, team_id, cand, kick_source="patrol")
                if ok:
                    kicked_emails.append(email)
                    kicked += 1
                deferred = not ok and err == KICK_DEFERRED_TEAMBOSS_RECORD
                events.append({
                    "team_id": team_id, "team_name": name, "email": email, "user_id": user_id,
                    "action": "kick",
                    "result": "success" if ok else ("deferred" if deferred else "failed"),
                    "over_by": over_by, "position": position, "reason": reason, "error": err,
                })

            rows = [
                f"💺 GPT 席位：{status['active_chatgpt']} / {seats_entitled}（超 {over_by}）",
                "💻 Codex：关闭 ⏸️",
            ]
            if effective_dry_run:
                if would_kick_emails:
                    rows.append("🧪 空跑候选：" + "、".join(would_kick_emails))
                else:
                    rows.append("🧪 空跑结果：没有安全候选")
            else:
                if kicked_emails:
                    rows.append("✅ 已移除：" + "、".join(kicked_emails))
                else:
                    rows.append("⚠️ 处理结果：没有成员被移除")
            if history_vetoed:
                rows.append(
                    f"🔎 另有 {len(history_vetoed)} 个外部加入的成员 TeamBoss 以前拉过或他兑换过，没有移除"
                )
            if insufficient_note:
                rows.append(f"👤 人工核查：{insufficient_note}")
            notify_admins_sync(detail_card(f"🚨 巡逻发现超员 · {name}", rows))

        conn.close()
        conn = None
    except Exception as e:
        _log_operation_sync(None, "patrol_job_error", None, None, "failed", str(e))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    return {
        "events": events,
        "kicked": kicked,
        "would_kick": would_kick,
        "invites_revoked": invites_revoked,
        "invites_would_revoke": invites_would_revoke,
        "strict_kicked": strict_kicked,
        "strict_would_kick": strict_would_kick,
    }
