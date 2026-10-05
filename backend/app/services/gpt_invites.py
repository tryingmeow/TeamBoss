import json
import re
from datetime import datetime
from typing import Any, Optional

from ..chatgpt_limiter import run_chatgpt_call
from ..database import get_db, log_operation
from ..member_cache_service import add_member_watch, fetch_and_cache_members
from ..services.member_expiry import record_confirmed_invite, record_uncertain_invite
from ..services.seat_capacity import (
    SeatCapacityFetchError,
    chatgpt_seat_capacity,
    fetch_live_chatgpt_seat_capacity,
    member_seat_usage_from_members,
    safe_int,
    update_capacity_cache,
)
from ..services.team_clients import get_team_client
from ..services.team_locks import reserve_default_seat, reserved_default_seats, team_invite_lock
from ..services.tg_notify import notify_member_event
from ..services.subscription_status import subscription_status


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
EMAIL_ALREADY_IN_TEAM = "邮箱已在该 Team 中，未重复邀请"
# _invite_to_team 返回的错误前缀：带这两个前缀、或等于 EMAIL_ALREADY_IN_TEAM 的结果
# 都把这个邮箱绑定在当次的 Team 上，调用方不能再换 Team 拉，见 _bound_to_team_failure。
MEMBER_LOOKUP_FAILED = "member_lookup_failed:"
INVITE_RESULT_UNCERTAIN = "invite_result_uncertain:"


class NoGptSeatAvailable(Exception):
    def __init__(self, reason: str = "no_gpt_seat"):
        super().__init__(reason)
        self.reason = reason


class GptInviteFailed(Exception):
    def __init__(self, reason: str, *, team_id: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.team_id = team_id


def normalize_invite_emails(values: list[str]) -> list[str]:
    emails: list[str] = []
    seen: set[str] = set()
    for raw in values:
        email = (raw or "").strip().lower()
        if not email:
            continue
        if not EMAIL_RE.match(email):
            raise ValueError(f"邮箱格式无效: {raw}")
        if email in seen:
            continue
        seen.add(email)
        emails.append(email)
    return emails


def _pending_default_count(cache: dict[str, Any] | None) -> int:
    if not cache:
        return 0
    pending = cache.get("pending_invites") or []
    if not isinstance(pending, list):
        return 0
    return sum(1 for item in pending if isinstance(item, dict) and (item.get("seat_type") or "default") == "default")


def _snapshot_email_match(snapshot: dict[str, Any] | None, email: str) -> tuple[str, dict[str, Any]] | None:
    if not snapshot:
        return None
    email_lower = (email or "").strip().lower()
    for member in snapshot.get("members", []):
        if (member.get("email") or "").strip().lower() == email_lower:
            return "member", member
    for invite in snapshot.get("pending_invites", []):
        if (invite.get("email") or "").strip().lower() == email_lower:
            return "invite", invite
    return None


def _snapshot_contains_email(snapshot: dict[str, Any] | None, email: str) -> bool:
    return _snapshot_email_match(snapshot, email) is not None


async def _load_member_caches() -> dict[str, dict[str, Any]]:
    async with get_db() as db:
        cursor = await db.execute("SELECT team_id, members_json, pending_json, updated_at FROM member_cache")
        rows = await cursor.fetchall()

    caches: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            members = json.loads(row["members_json"] or "[]")
        except Exception:
            members = []
        try:
            pending = json.loads(row["pending_json"] or "[]")
        except Exception:
            pending = []
        caches[row["team_id"]] = {
            "members": members if isinstance(members, list) else [],
            "pending_invites": pending if isinstance(pending, list) else [],
            "updated_at": row["updated_at"],
        }
    return caches


async def _load_active_team_rows() -> list[dict[str, Any]]:
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT id, name, owner_email, seats_in_use, seats_entitled,
                      codex_count, chatgpt_count, created_at, active_until, will_renew
               FROM teams
               WHERE status = 'active'"""
        )
        return [dict(row) for row in await cursor.fetchall()]


async def _build_gpt_invite_candidates(
    teams: list[dict[str, Any]],
    caches: dict[str, dict[str, Any]],
    *,
    include_full: bool = False,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for team in teams:
        if subscription_status(
            team.get("active_until"),
            bool(team.get("will_renew")),
        ) == "expired":
            continue

        cache = caches.get(team["id"])
        seats_in_use = safe_int(team.get("seats_in_use"))
        codex_count = safe_int(team.get("codex_count"))
        chatgpt_count = (
            safe_int(team.get("chatgpt_count"))
            if team.get("chatgpt_count") is not None
            else None
        )
        usage = member_seat_usage_from_members(cache.get("members") if cache else None)
        if usage is not None:
            seats_in_use = usage.seats_in_use_total
            codex_count = usage.codex_count
            chatgpt_count = usage.active_chatgpt

        capacity = chatgpt_seat_capacity(
            seats_entitled=team.get("seats_entitled"),
            seats_in_use=seats_in_use,
            codex_count=codex_count,
            active_chatgpt=chatgpt_count,
            pending_default=_pending_default_count(cache),
        )
        reserved = await reserved_default_seats(team["id"])
        cached_available = max(0, capacity.available - reserved)
        if cached_available <= 0 and not include_full:
            continue
        candidates.append({
            **team,
            "cached_available": cached_available,
            "cached_active_chatgpt": capacity.active_chatgpt,
            "cached_pending_default": capacity.pending_default,
            "reserved_default": reserved,
        })

    candidates.sort(key=lambda item: (-safe_int(item.get("cached_available")), item.get("created_at") or ""))
    return candidates


async def load_gpt_invite_candidates(*, include_full: bool = False) -> list[dict[str, Any]]:
    teams = await _load_active_team_rows()
    caches = await _load_member_caches()
    return await _build_gpt_invite_candidates(teams, caches, include_full=include_full)


async def cached_gpt_capacity_summary() -> dict[str, Any]:
    candidates = await load_gpt_invite_candidates(include_full=True)
    return {
        "available": sum(safe_int(item.get("cached_available")) for item in candidates),
        "free_team_count": sum(1 for item in candidates if safe_int(item.get("cached_available")) > 0),
        "active_team_count": len(candidates),
    }


async def _live_gpt_available(client, team_id: str, *, email: str) -> tuple[bool, str]:
    try:
        capacity, subscription, seat_counts, _pending = await fetch_live_chatgpt_seat_capacity(client)
    except SeatCapacityFetchError as exc:
        return False, str(exc)

    await update_capacity_cache(team_id, subscription, seat_counts)
    reserved = await reserved_default_seats(team_id, exclude_email=email)
    available_after_reservations = capacity.available - reserved
    if available_after_reservations <= 0:
        return (
            False,
            "no_gpt_seat: "
            f"active_chatgpt={capacity.active_chatgpt}/{capacity.seats_entitled}, "
            f"pending_default={capacity.pending_default}, reserved_default={reserved}",
        )
    return True, f"available={available_after_reservations}"


async def _invite_to_team(
    team: dict[str, Any],
    email: str,
    expires_at: Optional[datetime],
    *,
    check_capacity: bool,
    action: str,
    cached_snapshot: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    team_id = team["id"]
    async with team_invite_lock(team_id):
        # 缓存快照只用来提前跳过：缓存里"有"足以不发邀请。
        if _snapshot_contains_email(cached_snapshot, email):
            await log_operation(team_id, f"{action}_existing", email, EMAIL_ALREADY_IN_TEAM, "skipped")
            return None, EMAIL_ALREADY_IN_TEAM

        # 缓存里"没有"却不能当作"不在"的证据：缓存可能是几分钟前的，期间这个人
        # 可能已被邀请/加入。对已在 Team 的人再发邀请，record_confirmed_invite 会拿
        # 这次的有效期去碰他已有的到期记录。所以发邀请前必须在锁内现拉一次名单；
        # 拉不到（含残缺名单）就是未知状态，失败关闭、不发邀请。
        client = await get_team_client(team_id)
        try:
            snapshot = await fetch_and_cache_members(team_id, client)
        except Exception as exc:
            error = str(exc)
            await log_operation(team_id, f"{action}_lookup", email, None, "failed", error)
            return None, f"{MEMBER_LOOKUP_FAILED}{error}"
        if _snapshot_contains_email(snapshot, email):
            await log_operation(team_id, f"{action}_existing", email, EMAIL_ALREADY_IN_TEAM, "skipped")
            return None, EMAIL_ALREADY_IN_TEAM

        if check_capacity:
            ok, reason = await _live_gpt_available(client, team_id, email=email)
            if not ok:
                return None, reason

        result = await run_chatgpt_call(client.invite_member, email, "default")
        mutation_status = result.get("_mutation_status") if isinstance(result, dict) else None
        if mutation_status == "uncertain":
            try:
                live_snapshot = await fetch_and_cache_members(team_id, client)
            except Exception:
                live_snapshot = None
            if _snapshot_contains_email(live_snapshot, email):
                result = {"_mutation_status": "confirmed"}
            else:
                error = str(result.get("error") or "OpenAI invite result is uncertain")
                await record_uncertain_invite(
                    team_id,
                    "",
                    email,
                    expires_at,
                    source="system",
                    reason=error,
                )
                await log_operation(team_id, action, email, "seat_type=default", "uncertain", error)
                return None, f"{INVITE_RESULT_UNCERTAIN}{error}"
        if "error" in result:
            error = result["error"]
            await log_operation(team_id, action, email, "seat_type=default", "failed", error)
            return None, error

        # OpenAI 邀请已在上面成功，本地记录必须最终落地（否则下一轮同步会把
        # 系统自己拉的人误判成外部乱拉的人）——用 record_confirmed_invite 而不是
        # upsert_member_expiry，带重试+兜底，见该函数注释。
        expires_iso = await record_confirmed_invite(team_id, "", email, expires_at)

        await log_operation(team_id, action, email, f"expires_at={expires_iso}", "success")

        try:
            snapshot = await fetch_and_cache_members(team_id, client)
            await add_member_watch(team_id, "invite", target_email=email)
        except Exception as exc:
            await log_operation(team_id, f"{action}_cache_refresh", email, None, "failed", str(exc))
        if not _snapshot_contains_email(snapshot, email):
            await reserve_default_seat(team_id, email)

        await notify_member_event(
            "批量 GPT 拉人",
            team_id,
            email=email,
            source="admin",
            detail=f"expires_at={expires_iso}, overage={not check_capacity}",
        )

        return {
            "email": email,
            "team_id": team_id,
            "team_name": team.get("name") or "",
            "expires_at": expires_iso,
            "overage": not check_capacity,
        }, None


def _is_capacity_error(error: str | None) -> bool:
    return bool(error and error.startswith("no_gpt_seat"))


def _bound_to_team_failure(error: str | None, team: dict[str, Any]) -> GptInviteFailed | None:
    """这个结果是否把邮箱绑定在这个 Team 上，从而必须就此终止、不换下一个 Team。

    * 已在该 Team（成员或待接受邀请）：换 Team 再拉，同一个人就在两个 Team 各占
      一个席位、各有一条到期记录。
    * 该 Team 名单拉不到：无法排除他已在里面，同上，失败关闭。
    * 邀请结果不明确：邀请可能已经到了 OpenAI，只能留在原 Team 等对账。

    没空位、上游明确拒绝等没有远端副作用、也不说明人在该 Team 的失败返回 None，
    调用方照常试下一个 Team。
    """
    if not error:
        return None
    team_id = team.get("id")
    label = team.get("name") or team_id
    if error == EMAIL_ALREADY_IN_TEAM:
        return GptInviteFailed(f"邮箱已在 Team {label} 中，未重复邀请", team_id=team_id)
    if error.startswith(MEMBER_LOOKUP_FAILED):
        detail = error[len(MEMBER_LOOKUP_FAILED):]
        return GptInviteFailed(
            f"拉不到 Team {label} 的成员列表，无法确认邮箱是否已在其中，未邀请: {detail}",
            team_id=team_id,
        )
    if error.startswith(INVITE_RESULT_UNCERTAIN):
        return GptInviteFailed("邀请结果确认中，请先刷新成员列表，勿重复提交", team_id=team_id)
    return None


def _cached_team_holding(
    teams: list[dict[str, Any]],
    caches: dict[str, dict[str, Any]],
    email: str,
) -> dict[str, Any] | None:
    """缓存名单里已有这个邮箱的 Team（成员或待接受邀请），跳过订阅已过期的 Team。

    候选按空位多少排序、已满的 Team 主循环根本不进，所以只在循环里逐个查，会先把
    人拉进排在前面的 Team B，而他其实已在排在后面或已满的 Team A。缓存"有"足以
    不发邀请；缓存"没有"不能当证据，每个 Team 发邀请前仍在锁内现拉。
    """
    for team in teams:
        if subscription_status(team.get("active_until"), bool(team.get("will_renew"))) == "expired":
            continue
        if _snapshot_contains_email(caches.get(team["id"]), email):
            return team
    return None


async def invite_gpt_member_any_team(
    email: str,
    expires_at: Optional[datetime],
    *,
    allow_overage: bool = False,
    action: str = "invite_gpt_member",
) -> dict[str, Any]:
    email = (email or "").strip().lower()
    capacity_errors: list[str] = []
    hard_errors: list[tuple[str, str | None]] = []
    teams = await _load_active_team_rows()
    caches = await _load_member_caches()

    holder = _cached_team_holding(teams, caches, email)
    if holder is not None:
        await log_operation(holder["id"], f"{action}_existing", email, EMAIL_ALREADY_IN_TEAM, "skipped")
        raise _bound_to_team_failure(EMAIL_ALREADY_IN_TEAM, holder)

    candidates = await _build_gpt_invite_candidates(teams, caches, include_full=allow_overage)

    for team in candidates:
        if safe_int(team.get("cached_available")) <= 0 and not allow_overage:
            continue
        added, error = await _invite_to_team(
            team,
            email,
            expires_at,
            check_capacity=True,
            action=action,
            cached_snapshot=caches.get(team["id"]),
        )
        if added:
            return added
        bound = _bound_to_team_failure(error, team)
        if bound is not None:
            raise bound
        if _is_capacity_error(error):
            capacity_errors.append(error or "no_gpt_seat")
        elif error:
            hard_errors.append((error, team.get("id")))

    if allow_overage:
        overage_candidates = candidates or await _build_gpt_invite_candidates(teams, caches, include_full=True)
        for team in overage_candidates:
            added, error = await _invite_to_team(
                team,
                email,
                expires_at,
                check_capacity=False,
                action=action,
                cached_snapshot=caches.get(team["id"]),
            )
            if added:
                return added
            bound = _bound_to_team_failure(error, team)
            if bound is not None:
                raise bound
            if _is_capacity_error(error):
                capacity_errors.append(error or "no_gpt_seat")
            elif error:
                hard_errors.append((error, team.get("id")))

    if hard_errors:
        reason, team_id = hard_errors[-1]
        raise GptInviteFailed(reason, team_id=team_id)

    raise NoGptSeatAvailable(capacity_errors[-1] if capacity_errors else "no_gpt_seat")
