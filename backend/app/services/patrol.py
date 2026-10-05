"""巡逻踢人引擎 — 自动发现并清理超额占用的"检测到"成员。

最高优先级：绝不误踢付费成员。宁可漏踢也不可错踢。

设计要点：
- 只在以下条件全部成立时才会把某个成员当作"候选":
    1. Team is_codex_enabled == 0（codex 开的 Team 完全跳过踢人，只算风险）
    2. Team id 不在 settings.patrol_exempt_team_ids（豁免名单）里
    3. member_cache 有数据且非空（冷启动/缓存缺失一律跳过整队，绝不动手）
    4. active_chatgpt > seats_entitled（over_by > 0，否则最多是 watch，不踢；
       seats_entitled 不是正整数 = 席位数未知，该 Team 本轮不做超员踢人）
    5. member.seat_type == 'default' 且 member.is_owner is False
    6. member.source == 'detected'（唯一硬规则；'system'/'self_service'/None 永不踢，
       管理员开启自动踢人时会先把当时的现有成员全部保护起来；巡逻自动给 Team 建立
       基线时只有该 Team 第一次才保护，token_expired 恢复/重新导入后的自动重建不保护
       ——见 _protect_team_snapshot_sync）
  最多踢 over_by 个，按 first_seen_at（缺失则退回 created_time）新→旧排序，优先踢最新混进来的。
- dry-run 判定：effective_dry_run = dry_run 参数 OR settings.patrol_kick_enabled != '1'。
- 真正执行踢人只能通过 `_patrol_kick()` 这一个函数：进函数先重新校验候选资格
  （source/is_owner/seat_type），任一不满足直接拒绝、不踢 —— 纵深防御，防止上游逻辑
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
"""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from ..chatgpt_client import ChatGPTClient
from ..chatgpt_limiter import run_chatgpt_call_sync
from ..database import get_db_path
from ..tg_format import detail_card
from .member_expiry import compute_effective_kick_at, normalize_kick_mode
from .seat_capacity import member_seat_usage_from_members, positive_seat_count
from .tg_member_bindings import deactivate_member_binding_if_inactive_sync
from .tg_commands import sync_email_chat_commands_sync
from .tg_notify import notify_admins_sync
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

def select_kick_candidates(members: Any) -> list[dict]:
    """从成员列表里筛出"可踢候选"，按 first_seen_at（退回 created_time）新→旧排序。

    唯一硬规则：source == 'detected'。另外叠加 seat_type == 'default' 且
    is_owner is False（owner/codex 席位永不触碰）。不做任何时间戳门槛判断——
    现有成员会在管理员开启自动踢人时（以及巡逻自动纳入一个从未建过基线的 Team 时）
    一次性转成 source='system' 保护住，所以"只踢 detected"就等价于"只踢那之后从系统外
    混进来的人"（包括 Team token_expired 期间混进来、恢复后自动重建基线的人）。
    """
    if not isinstance(members, list):
        return []

    candidates = []
    for m in members:
        if not isinstance(m, dict):
            continue
        if m.get("seat_type") != "default":
            continue
        if m.get("is_owner") is not False:
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
    """
    if not isinstance(pending, list):
        return []

    candidates = []
    for p in pending:
        if not isinstance(p, dict):
            continue
        if p.get("source") != "detected":
            continue
        candidates.append(p)

    candidates.sort(key=lambda p: p.get("first_seen_at") or p.get("created_time") or "", reverse=True)
    return candidates


def select_strict_kick_candidates(members: Any) -> list[dict]:
    """严格模式候选：忽略 seat_type / 超员判定 / Codex 豁免，只要求：

    - 非 owner
    - source == 'detected'（系统自己拉的人 source 是 'system'/'self_service'，永不进候选）
    - 没有到期记录（expires_at 为空）—— 有 expires_at 说明这个人被系统主动追踪/授权过
      （管理员手动设置到期时间、自助续期等都会把 source 一并改写，但这里额外再挡一层，
      不完全依赖 source 字段各处写入逻辑的正确性，属于纵深防御）

    比 select_kick_candidates 更宽（含 usage_based/Codex 席位），因为严格模式的判断
    标准是"这个人是不是系统自己拉进来的"，与席位类型/是否超员无关。
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


def strict_kick_batch_limit(team_size: int) -> int:
    """"别一次踢一片"阈值：3 人和团队总人数一半，取更小的那个。

    团队越小越保守（比如 4 人的队，阈值是 2 而不是 3），大队封顶 3，
    不随团队规模线性放大——超过这个数基本可以认定是数据异常而非真的巧合。
    """
    return min(3, team_size // 2)


def strict_kick_batch_guard_exceeded(candidate_count: int, team_size: int) -> bool:
    return candidate_count > strict_kick_batch_limit(team_size)


# 非严格模式的绝对保险丝：无论 over_by 算成多少，单轮最多踢这么多人。
# over_by 一旦因上游数据异常被抬高，这道闸挡住"一趟踢光整队"；超出的下一轮再处理。
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


def _patrol_kick(conn: sqlite3.Connection, client: ChatGPTClient, team_id: str,
                  member: dict, kick_source: str = "patrol") -> tuple[bool, Optional[str]]:
    """巡逻踢人的唯一入口。纵深防御：即便调用方选错了候选，这里也会再拦一次。

    校验通过后，复用 auto_kick_job 的原语：client.remove_member -> 标记 kicked -> 记日志。
    返回 (是否踢成功, 失败原因或 None)。
    """
    email = (member.get("email") or "").strip().lower()
    user_id = member.get("id") or member.get("user_id") or ""

    # 所有真踢条件集中在这里。上游筛选结果一律不被信任，避免其他入口或旧缓存绕过安全规则。
    settings = _read_patrol_settings(conn)
    reject_reason = _armed_team_gate_reject(conn, team_id, settings)
    if reject_reason:
        _log_operation_sync(team_id, "patrol_kick", email, reject_reason, "failed", "safety gate rejected")
        return False, reject_reason

    team = conn.execute(
        "SELECT status, is_codex_enabled, seats_entitled FROM teams WHERE id = ?",
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
        "SELECT members_json FROM member_cache WHERE team_id = ?", (team_id,)
    ).fetchone()
    try:
        cached_members = json.loads(cache_row["members_json"]) if cache_row and cache_row["members_json"] else []
    except Exception:
        cached_members = []
    if not isinstance(cached_members, list) or not cached_members:
        reason = "rejected: member cache is empty or invalid"
        _log_operation_sync(team_id, "patrol_kick", email, reason, "failed", "safety gate rejected")
        return False, reason

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
    if cached_member.get("seat_type") != "default":
        reason = f"rejected: seat_type={cached_member.get('seat_type')!r} != 'default'"
        _log_operation_sync(team_id, "patrol_kick", email, reason, "failed", "safety gate rejected")
        return False, reason

    allowed = select_kick_candidates(cached_members)[:status["over_by"]]
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

        result = run_chatgpt_call_sync(client.remove_member, user_id)
        if isinstance(result, dict) and "error" in result:
            _log_operation_sync(team_id, "patrol_kick", email, f"user_id={user_id}", "failed", result["error"])
            return False, result["error"]

        _mark_member_kicked_sync(conn, team_id, kick_source, user_id, email)
        _log_operation_sync(team_id, "patrol_kick", email, f"user_id={user_id}", "success")
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

def _fetch_all_api_items_sync(method, *fallback_keys: str, limit: int = 100, max_items: int = 10000):
    """分页拉取 API 列表的同步小工具，供严格模式"动手前强制实时刷新"使用。

    自成一体、不依赖 scheduler.py，与本文件顶部docstring里"自成一体"的原则一致。
    """
    items: list = []
    offset = 0
    while offset < max_items:
        data = run_chatgpt_call_sync(method, offset=offset, limit=limit)
        if isinstance(data, dict) and "error" in data:
            return None, data["error"]
        page_items: list = []
        found_list = False
        for key in ("items",) + fallback_keys:
            candidate = data.get(key) if isinstance(data, dict) else None
            if isinstance(candidate, list):
                page_items = candidate
                found_list = True
                break
        if not found_list:
            # 200 但响应体没有任何可识别的成员/邀请列表（网关 JSON 挑战、契约漂移、
            # 代理注入）。绝不能当成"这队空了"——那样严格模式会拿空快照去踢人。
            # fail closed，让调用方跳过整队。
            return None, "unrecognized member/invite response structure"
        items.extend(page_items)
        total = data.get("total") if isinstance(data, dict) else None
        short_page = len(page_items) < limit
        if isinstance(total, int):
            if len(items) >= total:
                break
            if short_page:
                # total 已知却提前收到短页 = 响应被截断，未见到的人不能判为缺席。
                return None, "truncated member/invite response before reported total"
        elif short_page:
            break
        offset += limit
    return items, None


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

    members_items, m_err = _fetch_all_api_items_sync(client.get_members, "users")
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

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO member_cache (team_id, members_json, pending_json, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(team_id) DO UPDATE SET
               members_json = excluded.members_json,
               pending_json = excluded.pending_json,
               updated_at   = excluded.updated_at""",
        (team_id, json.dumps(cached_members, ensure_ascii=False),
         json.dumps(cached_pending, ensure_ascii=False), now),
    )
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
            "SELECT id, name, is_codex_enabled, seats_entitled, access_token, device_id, proxy_id "
            "FROM teams WHERE status = 'active'"
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
                elif strict_candidates and strict_kick_batch_guard_exceeded(len(strict_candidates), team_size):
                    # "别一次踢一片"：数量异常多，很可能是数据 half-broken 导致的误判，
                    # 宁可漏踢也不能误清一个队——只报警，不做任何自动处理。
                    notify_admins_sync(detail_card(
                        f"🚨 严格模式异常：陌生成员数量过多 · {name}",
                        [
                            f"👥 疑似陌生成员：{len(strict_candidates)} / 团队共 {team_size} 人",
                            "🛑 数量超过安全阈值，怀疑是数据异常，已跳过自动处理，请人工核查",
                        ],
                    ))
                    _log_operation_sync(
                        team_id, "patrol_strict_batch_guard", None,
                        f"count={len(strict_candidates)} team_size={team_size}", "failed",
                    )
                    events.append({
                        "team_id": team_id, "team_name": name, "action": "strict_batch_guard",
                        "count": len(strict_candidates), "team_size": team_size,
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
                                fresh_candidates = select_strict_kick_candidates(fresh_members)
                                fresh_now = datetime.now(timezone.utc)
                                fresh_ready = [
                                    c for c in fresh_candidates
                                    if _is_strict_candidate_ready(c.get("first_seen_at"), mode, delay_hours, fresh_now)
                                ]
                                # 刷新后复核一次批量护栏——刷新可能暴露出更严重的数据异常。
                                if strict_kick_batch_guard_exceeded(len(fresh_candidates), len(fresh_members)):
                                    notify_admins_sync(detail_card(
                                        f"🚨 严格模式异常（刷新后复核）· {name}",
                                        [
                                            f"👥 疑似陌生成员：{len(fresh_candidates)} / 团队共 {len(fresh_members)} 人",
                                            "🛑 数量超过安全阈值，已跳过自动处理，请人工核查",
                                        ],
                                    ))
                                    _log_operation_sync(
                                        team_id, "patrol_strict_batch_guard", None,
                                        f"post_refresh count={len(fresh_candidates)} team_size={len(fresh_members)}",
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
            candidates = select_kick_candidates(members)
            selected = candidates[:over_by]

            # 绝对保险丝：单轮踢人数封顶，挡住 over_by 因数据异常被抬高导致的"一趟踢光"。
            # 命中说明这轮的超额判定不正常，记一条日志让人来查，超出的部分留给下一轮。
            if len(selected) > NON_STRICT_KICK_ABS_CAP:
                _log_operation_sync(
                    team_id, "patrol_kick_batch_capped", None,
                    f"over_by={over_by} candidates={len(candidates)} capped_to={NON_STRICT_KICK_ABS_CAP}",
                    "capped",
                )
                selected = selected[:NON_STRICT_KICK_ABS_CAP]

            insufficient_note = None
            if len(candidates) < over_by:
                insufficient_note = (
                    f"超额 {over_by}，仅 {len(candidates)} 个外部成员可移除，剩余需人工核查"
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
                events.append({
                    "team_id": team_id, "team_name": name, "email": email, "user_id": user_id,
                    "action": "kick", "result": "success" if ok else "failed", "over_by": over_by,
                    "position": position, "reason": reason, "error": err,
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
