import json
import re
from datetime import datetime
from typing import Any, Optional

from ..chatgpt_limiter import run_chatgpt_call
from ..database import get_db, log_operation
from ..member_cache_service import add_member_watch, fetch_and_cache_members
from ..services.member_expiry import record_confirmed_invite, record_uncertain_invite
from ..services.open_redemptions import (
    find_open_redemption,
    SETTLED_REDEMPTION_RESULTS,
    open_redemption_detail,
    settled_redemption_row_detail,
)
from ..seat_types import DEFAULT_SEAT_TYPE, normalize_overage_policy
from ..services.pricing import seat_price_info
from ..services.overage_policy import (
    load_team_policy,
    proceeds_without_capacity_check,
    refusal_message,
    refusal_reason,
)
from ..services.seat_capacity import (
    SeatCapacityFetchError,
    cached_seat_capacity,
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
# _invite_to_team 返回的错误前缀：带这些前缀、或等于 EMAIL_ALREADY_IN_TEAM 的结果
# 都让这个邮箱就此终止，调用方不能再换 Team 拉，见 _bound_to_team_failure。
MEMBER_LOOKUP_FAILED = "member_lookup_failed:"
INVITE_RESULT_UNCERTAIN = "invite_result_uncertain:"
OPEN_REDEMPTION = "open_redemption:"


class NoGptSeatAvailable(Exception):
    def __init__(self, reason: str = "no_gpt_seat"):
        super().__init__(reason)
        self.reason = reason


class GptInviteFailed(Exception):
    def __init__(self, reason: str, *, team_id: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.team_id = team_id


class ConfirmedOverfillAllowance:
    """一次批量请求里管理员确认过、还能在「超员需确认」的 Team 上加购的个数。

    每个超员邀请发出去之前先扣 1 个（``take``）；只有上游明确拒绝才还回去（``give_back``）。
    结果不明（超时、5xx、读不回来）按已经加购算，不还：邀请可能已经落地、ChatGPT 可能
    已经扣费，不能让下一个邮箱再买一个他没确认的席位。「超员自动」的 Team 不扣这个额度。
    """

    def __init__(self, limit: int):
        self.remaining = max(0, int(limit))

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True

    def give_back(self) -> None:
        self.remaining += 1


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
                      codex_count, chatgpt_count, created_at, active_until, will_renew,
                      seat_capacity_json, overage_policy,
                      billing_period, price_period, billing_currency, billing_symbol,
                      price_per_seat, premium_price_per_seat
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
            # 缓存的分类型容量也参与取小：两种算法不一致时按空位少的那个算，宁可少卖。
            seat_capacity=cached_seat_capacity(team.get("seat_capacity_json")),
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
            "overage_policy": normalize_overage_policy(team.get("overage_policy")),
        })

    candidates.sort(key=lambda item: (-safe_int(item.get("cached_available")), item.get("created_at") or ""))
    return candidates


async def load_gpt_invite_candidates(*, include_full: bool = False) -> list[dict[str, Any]]:
    teams = await _load_active_team_rows()
    caches = await _load_member_caches()
    return await _build_gpt_invite_candidates(teams, caches, include_full=include_full)


def summarize_gpt_candidates(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """候选（含已满的）的缓存空位汇总，批量拉人的 409 里原样给前端。"""
    return {
        "available": sum(safe_int(item.get("cached_available")) for item in candidates),
        "free_team_count": sum(1 for item in candidates if safe_int(item.get("cached_available")) > 0),
        "active_team_count": len(candidates),
    }


async def cached_gpt_capacity_summary() -> dict[str, Any]:
    return summarize_gpt_candidates(await load_gpt_invite_candidates(include_full=True))


def _overfill_order(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """超员时试 Team 的顺序：按建 Team 的先后。

    要超员时候选都已满（空位并列 0），候选顺序本来就是建 Team 的先后；这里显式排一次，
    让问管理员时报的计划（batch_overage_plan）和确认后真正加购的 Team 一致。
    """
    return sorted(candidates, key=lambda item: item.get("created_at") or "")


def confirmed_overage_team_ids(allow_overage: bool, overage_team_ids: Any) -> frozenset[str]:
    """管理员确认过可以超员的「超员需确认」Team：只有带了确认标记时，计划里列出的那些。

    确认绑定在他看到的计划上：没列出的 confirm Team 即使带了确认标记也不超员。
    """
    if not allow_overage or not overage_team_ids:
        return frozenset()
    return frozenset(str(team_id) for team_id in overage_team_ids if team_id)


def overfill_allowed_for_team(team: dict[str, Any], policy: Any, confirmed_ids: frozenset[str]) -> bool:
    """这个 Team 能不能被批量超员：auto 可以；confirm 只在它被列进确认过的计划时；forbid 不行。"""
    return proceeds_without_capacity_check(policy, team.get("id") in confirmed_ids)


def overfill_without_asking(
    candidates: list[dict[str, Any]],
    confirmed_ids: frozenset[str],
    confirmed_seat_limit: int,
) -> int | None:
    """不用再问就能超员加几个席位：有「超员自动」的 Team 不限（None）；否则计划里确认过、
    现在仍是 confirm 的 Team 最多加 ``confirmed_seat_limit`` 个；都没有就是 0。"""
    if any(item.get("overage_policy") == "auto" for item in candidates):
        return None
    if any(
        overfill_allowed_for_team(item, item.get("overage_policy"), confirmed_ids) for item in candidates
    ):
        return max(0, int(confirmed_seat_limit))
    return 0


def batch_overage_plan(candidates: list[dict[str, Any]], extra_seats: int) -> list[dict[str, Any]]:
    """要问管理员时的计划：这 ``extra_seats`` 个席位会加在哪个「超员需确认」的 Team 上。

    确认后的请求里，没有空位的邮箱按 ``_overfill_order`` 逐个找第一个允许超员的 Team，
    所以全部加在排第一的那个 confirm Team 上（会问的时候没有可用的 auto Team）。
    计划里的 Team 改成禁止超员或不在了，它就不再是 confirm 候选，重新问时自然换下一个；
    确认的个数用完时，重新问的还是同一个 Team、只是个数是剩下的邮箱数。
    没有 confirm Team 时返回空列表：不用问，剩下的邮箱没位置。
    """
    if extra_seats <= 0:
        return []
    confirm_teams = [
        item for item in _overfill_order(candidates) if item.get("overage_policy") == "confirm"
    ]
    if not confirm_teams:
        return []
    first = confirm_teams[0]
    return [{
        "team_id": first["id"],
        "team_name": first.get("name") or first["id"],
        "extra_seats": int(extra_seats),
        # 一席 ChatGPT 的价格（每月，年付是月价；这个 Team 的币种，不含税）；未知为 None，界面写「单价未知」。
        "seat_price": seat_price_info(first, DEFAULT_SEAT_TYPE),
    }]


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
    allow_overage: bool = False,
    allowance: ConfirmedOverfillAllowance | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """``check_capacity=True``：只往现拉确认有空位的 Team 里拉。
    ``check_capacity=False``：超员（可能让 ChatGPT 加购扣费），只在锁内现读的超员策略
    允许时才拉——``auto``，或 ``confirm`` 且管理员确认过这个 Team（``allow_overage``，
    批量里 = 它在确认过的计划里）；``forbid`` 永远不超员。``confirm`` 的超员还要先从
    ``allowance`` 里扣 1 个（扣不到就不拉）；``allowance`` 为 None 时不限个数。
    """
    team_id = team["id"]
    policy = normalize_overage_policy(team.get("overage_policy"))
    async with team_invite_lock(team_id):
        # 有对账日后还会给这个邮箱记账的未结兑换就跳过，什么都不写：对账确认那笔兑换
        # 时会在这次写下的到期之上再累加一次兑换码时长（30 天码 + 批量 30 天 = 60 天）。
        # 必须在锁内、任何上游请求之前查：兑换的邀请分支只在同一把 team_invite_lock
        # 里把兑换落到这个 Team、发邀请、记账或锁成 uncertain，查过之后直到本次写完，
        # 已有的兑换不会在这个 Team 上为这个邮箱记账。
        open_redemption = await find_open_redemption(
            team_id,
            email,
            # uncertain 的兑换钉在原 Team，对账确认后在那里占一个席位；原 Team 不是
            # 这个 Team 时照样拉进来，一张码就占了两个席位。
            uncertain_in_any_team=True,
        )
        if open_redemption is not None:
            detail = open_redemption_detail(open_redemption, operation="batch_invite")
            await log_operation(
                team_id,
                action,
                email,
                f"open_redemption token_use_id={open_redemption['token_use_id']} "
                f"result={open_redemption['result']}",
                "skipped",
                detail,
            )
            return None, f"{OPEN_REDEMPTION}{detail}"

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
        else:
            # 候选列表是锁外读的；超员前在锁里现读一次策略，管理员刚改成禁止超员就不加。
            current = await load_team_policy(team_id)
            policy = current.policy
            if not proceeds_without_capacity_check(policy, allow_overage):
                reason = refusal_reason(policy)
                await log_operation(
                    team_id,
                    action,
                    email,
                    f"seat_type=default, policy={policy}, reason={reason}",
                    "skipped",
                    refusal_message(policy=policy, team_name=current.team_name, seat_type="default"),
                )
                return None, f"no_gpt_seat: overage not allowed (policy={policy})"

        # 确认过的个数在发邀请之前就扣掉：之后结果不明也算用掉了，见 ConfirmedOverfillAllowance。
        took_allowance = False
        if not check_capacity and policy == "confirm" and allowance is not None:
            if not allowance.take():
                await log_operation(
                    team_id,
                    action,
                    email,
                    f"seat_type=default, policy={policy}, reason=overage_allowance_used_up",
                    "skipped",
                    "这次确认的加购个数已经用完，没有再超员。",
                )
                return None, "no_gpt_seat: confirmed overage allowance used up"
            took_allowance = True

        result = await run_chatgpt_call(client.invite_member, email, "default")
        # 成败只看 ChatGPTClient 给的定性 _mutation_status，不看有没有 error 键：
        # confirmed 的 2xx 响应体里可能带着 "error": null 之类的字段，按键判失败会让
        # 调用方去下一个 Team 再拉一次，同一个人占两个席位。只有 rejected 是"上游明确
        # 没建邀请"、可以换 Team；没有定性的结果（不是 ChatGPTClient 的返回形状）
        # 和 uncertain 一样不能排除邀请已到 OpenAI，走同一条不明确分支。
        mutation_status = result.get("_mutation_status") if isinstance(result, dict) else None
        upstream_error = result.get("error") if isinstance(result, dict) else None
        if mutation_status == "rejected":
            if took_allowance:
                allowance.give_back()
            error = str(upstream_error or "OpenAI rejected the invite")
            await log_operation(team_id, action, email, "seat_type=default", "failed", error)
            return None, error
        if mutation_status != "confirmed":
            try:
                live_snapshot = await fetch_and_cache_members(team_id, client)
            except Exception:
                live_snapshot = None
            if not _snapshot_contains_email(live_snapshot, email):
                if upstream_error:
                    error = str(upstream_error)
                elif mutation_status == "uncertain":
                    error = "OpenAI invite result is uncertain"
                else:
                    error = f"OpenAI invite result was not classified (_mutation_status={mutation_status!r})"
                await record_uncertain_invite(
                    team_id,
                    "",
                    email,
                    expires_at,
                    source="system",
                    reason=error,
                )
                await log_operation(team_id, action, email, "seat_type=default", "uncertain", error)
                # 邀请可能已经落地：先占住一个 ChatGPT 席位，免得下一个邮箱按旧空位再拉进来。
                await reserve_default_seat(team_id, email)
                return None, f"{INVITE_RESULT_UNCERTAIN}{error}"

        # OpenAI 邀请已在上面成功，本地记录必须最终落地（否则下一轮同步会把
        # 系统自己拉的人误判成外部乱拉的人）——用 record_confirmed_invite 而不是
        # upsert_member_expiry，带重试+兜底，见该函数注释。
        expires_iso = await record_confirmed_invite(team_id, "", email, expires_at)

        await log_operation(
            team_id,
            action,
            email,
            f"seat_type=default, expires_at={expires_iso}, policy={policy}, overage={not check_capacity}",
            "success",
        )

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
            # 落地时锁内现读的策略：批量按它数确认过的超员个数（confirm 计数，auto 不计）。
            "policy": policy,
        }, None


def _is_capacity_error(error: str | None) -> bool:
    return bool(error and error.startswith("no_gpt_seat"))


def _bound_to_team_failure(error: str | None, team: dict[str, Any]) -> GptInviteFailed | None:
    """这个结果是否把邮箱绑定在这个 Team 上，从而必须就此终止、不换下一个 Team。

    * 已在该 Team（成员或待接受邀请）：换 Team 再拉，同一个人就在两个 Team 各占
      一个席位、各有一条到期记录。
    * 该 Team 名单拉不到：无法排除他已在里面，同上，失败关闭。
    * 邀请结果不明确：邀请可能已经到了 OpenAI，只能留在原 Team 等对账。
    * 邮箱有未结兑换：拒绝针对的是邮箱而不是这个 Team。兑换收尾时给它落定的 Team
      记账，这时拉进同一个 Team 多记一次时长、拉进别的 Team 多占一个席位，换 Team
      没有意义。原因文案由 open_redemption_detail 给出。

    没空位、上游明确拒绝等没有远端副作用、也不说明人在该 Team 的失败返回 None，
    调用方照常试下一个 Team。
    """
    if not error:
        return None
    team_id = team.get("id")
    label = team.get("name") or team_id
    if error.startswith(OPEN_REDEMPTION):
        return GptInviteFailed(error[len(OPEN_REDEMPTION):], team_id=team_id)
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


async def _team_with_unresolved_invite(email: str) -> dict[str, Any] | None:
    """这个邮箱有未结清的邀请对账行（``pending_invite_reconciliations.resolved = 0``）
    的 Team，没有返回 None。

    结果不明确的邀请（本模块、后台单个拉人、自助兑换的屏障行）和"远端已确认、本地
    落库失败"的兜底都写这张表：邀请可能已经到了那个 Team，换 Team 再拉就是一人两席。
    行只在两处被结清：调度器同步在那个 Team 的完整现拉名单里看到这个邮箱（成员或
    待接受邀请）；挂着兑换凭据的行（屏障、'extend' 兜底行）随那次兑换的终态一起
    撤掉。管理员邀请（不挂凭据）其实没送达的行不会自己结清，出口是在原 Team 里单独
    拉一次（那条路径不受这里限制），下一轮同步看到人后结清。

    返回 ``id`` / ``name``（行所在的 Team）、``token_use_id``（不挂凭据为 None）和
    ``token_use_result``（那次兑换此刻的 result）。

    已从系统删除的 Team 不算：删 Team 不撤这些行、之后也不再同步它，算上就会让这个
    邮箱永远拉不进别的 Team。
    """
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT r.team_id, t.name, r.token_use_id, atu.result AS token_use_result
               FROM pending_invite_reconciliations r
               JOIN teams t ON t.id = r.team_id
               LEFT JOIN access_token_uses atu ON atu.id = r.token_use_id
               WHERE r.resolved = 0 AND lower(trim(r.email)) = ?
               ORDER BY r.id DESC
               LIMIT 1""",
            ((email or "").strip().lower(),),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "id": row["team_id"],
        "name": row["name"],
        "token_use_id": row["token_use_id"],
        "token_use_result": row["token_use_result"],
    }


async def invite_gpt_member_any_team(
    email: str,
    expires_at: Optional[datetime],
    *,
    allow_overage: bool = False,
    overage_team_ids: Any = None,
    confirm_overfill_budget: int | ConfirmedOverfillAllowance | None = None,
    action: str = "invite_gpt_member",
) -> dict[str, Any]:
    """``allow_overage`` + ``overage_team_ids``：管理员确认过的计划。confirm Team 只有列在
    ``overage_team_ids`` 里才会被超员；auto Team 不用列。没列表 = 空列表。

    ``confirm_overfill_budget``：这次请求里确认过、还能在 confirm Team 上超员的个数。批量路由
    传整个请求共用的 ``ConfirmedOverfillAllowance``（每个超员邀请发出前扣 1 个）；传 int 时
    只在这一次调用里用。用完时 confirm Team 一律不超员，只剩 auto。None = 不限个数（单独调用时）。
    """
    allowance = (
        ConfirmedOverfillAllowance(confirm_overfill_budget)
        if isinstance(confirm_overfill_budget, int)
        else confirm_overfill_budget
    )
    email = (email or "").strip().lower()
    capacity_errors: list[str] = []
    hard_errors: list[tuple[str, str | None]] = []
    teams = await _load_active_team_rows()
    caches = await _load_member_caches()

    holder = _cached_team_holding(teams, caches, email)
    if holder is not None:
        await log_operation(holder["id"], f"{action}_existing", email, EMAIL_ALREADY_IN_TEAM, "skipped")
        raise _bound_to_team_failure(EMAIL_ALREADY_IN_TEAM, holder)

    # 批量结果里"仅重试失败邮箱"会把上次结果不明确的邮箱原样再提交一遍。那次写下的
    # 对账行还没结清时，邀请可能已在原 Team 生效，这里只能跳过、报给管理员，不能
    # 按空位排序换一个 Team 再拉。
    unresolved = await _team_with_unresolved_invite(email)
    if unresolved is not None:
        label = unresolved.get("name") or unresolved["id"]
        # 这行若挂着兑换凭据（屏障 / 兜底行），"确认没送达请单独邀请"会让管理员在退码
        # 之后手动补发，客户还能拿退回的码再兑换一次。这时改用兑换自己的说明：兑换
        # 未结用 open_redemption_detail，已结束用 settled_redemption_row_detail。只有
        # 管理员邀请留下的行（不挂凭据）才让管理员到原 Team 单独邀请。
        open_redemption = await find_open_redemption(
            unresolved["id"], email, uncertain_in_any_team=True
        )
        if open_redemption is not None:
            reason = open_redemption_detail(open_redemption, operation="batch_invite")
        elif unresolved.get("token_use_result") in SETTLED_REDEMPTION_RESULTS:
            reason = settled_redemption_row_detail(
                unresolved["token_use_id"], unresolved.get("token_use_result"), label
            )
        else:
            reason = (
                f"邮箱在 Team {label} 有一次结果未确认的邀请，等待对账，本次跳过、未换 Team 重新邀请。"
                f"邀请若已送达，下一轮同步后会自动确认；确认没送达请在 Team {label} 内单独邀请"
            )
        await log_operation(
            unresolved["id"], action, email, "pending_invite_reconciliation", "skipped", reason
        )
        raise GptInviteFailed(reason, team_id=unresolved["id"])

    candidates = await _build_gpt_invite_candidates(teams, caches, include_full=True)
    # 超员只往策略允许的 Team 去：auto 总是可以；confirm 要管理员确认过、且在他看到的
    # 计划里；forbid 永远不行。
    confirmed_ids = confirmed_overage_team_ids(allow_overage, overage_team_ids)
    if allowance is not None and allowance.remaining <= 0:
        confirmed_ids = frozenset()
    overfill_teams = [
        team for team in _overfill_order(candidates)
        if overfill_allowed_for_team(team, team.get("overage_policy"), confirmed_ids)
    ]

    # 先找真空位。要超员时，缓存显示已满的 Team 也先现拉确认一遍，能不加购就不加购。
    for team in candidates:
        if safe_int(team.get("cached_available")) <= 0 and not overfill_teams:
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

    for team in overfill_teams:
        added, error = await _invite_to_team(
            team,
            email,
            expires_at,
            check_capacity=False,
            action=action,
            cached_snapshot=caches.get(team["id"]),
            # 锁内现读策略时也按「这个 Team 是否在确认过的计划里」判断。
            allow_overage=team["id"] in confirmed_ids,
            allowance=allowance,
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
