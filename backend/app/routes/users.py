import json
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..database import get_sessions_dir, get_db, log_operation
from ..member_cache_service import fetch_and_cache_members, get_cached_members
from ..seat_types import normalize_seat_type, seat_type_label
from ..services.member_expiry import build_expiry_view, get_kick_policy
from ..services.team_clients import get_team_client
from ..services.user_display_names import load_display_name_map, set_display_name
from ..services.subscription_status import subscription_status_display


router = APIRouter(prefix="/api/users", tags=["users"])


class UserDisplayNameUpdate(BaseModel):
    email: str
    system_display_name: Optional[str] = Field(default=None, max_length=120)


def _attach_display_name(row: dict, display_names: dict[str, str]) -> dict:
    email_key = _normalize_email(row.get("email"))
    row["system_display_name"] = display_names.get(email_key)
    return row


def _normalize_email(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _parse_session_file(team_id: str) -> dict:
    path = os.path.join(get_sessions_dir(), f"{team_id}.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, str):
            data = json.loads(data)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _days_remaining(active_until: Optional[str]) -> Optional[int]:
    if not active_until:
        return None
    try:
        until = datetime.fromisoformat(active_until.replace("Z", "+00:00"))
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        delta = until.astimezone(timezone.utc) - datetime.now(timezone.utc)
        return max(delta.days, 0)
    except Exception:
        return None


def _billing_cycle(team: dict) -> dict:
    will_renew = bool(team.get("will_renew", 1))
    return {
        "active_start": team.get("active_start"),
        "active_until": team.get("active_until"),
        "days_remaining": _days_remaining(team.get("active_until")),
        "will_renew": will_renew,
        "subscription_status": subscription_status_display(
            team.get("active_until"), will_renew, team.get("last_full_sync_at")
        ),
    }


async def _load_teams() -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM teams ORDER BY created_at DESC")
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def _load_cache(team: dict, refresh: bool, errors: list[dict]) -> Optional[dict]:
    cache = None if refresh else await get_cached_members(team["id"])
    if cache is not None:
        return cache

    try:
        client = await get_team_client(team["id"])
        snapshot = await fetch_and_cache_members(team["id"], client)
        return {
            "members": snapshot["members"],
            "pending_invites": snapshot["pending_invites"],
            "updated_at": snapshot.get("cached_at"),
        }
    except Exception as exc:
        errors.append({
            "team_id": team["id"],
            "team_name": team.get("name"),
            "error": str(getattr(exc, "detail", exc)),
        })
        return await get_cached_members(team["id"])


def _owner_from_cache(cache: Optional[dict], owner_email: str) -> Optional[dict]:
    if not cache:
        return None
    owner_email = _normalize_email(owner_email)
    for member in cache.get("members", []):
        if member.get("is_owner") or member.get("role") == "account-owner":
            return member
        if owner_email and _normalize_email(member.get("email")) == owner_email:
            return member
    return None


def _matches_query(row: dict, q: Optional[str], fields: tuple[str, ...]) -> bool:
    if not q:
        return True
    needle = q.strip().lower()
    if not needle:
        return True
    return any(needle in str(row.get(field) or "").lower() for field in fields)


@router.get("/owners")
async def list_owners(
    q: Optional[str] = Query(None),
    refresh: bool = Query(False),
):
    teams = await _load_teams()
    items: list[dict] = []
    errors: list[dict] = []
    display_names = await load_display_name_map()

    for team in teams:
        cache = await _load_cache(team, refresh, errors)
        session = _parse_session_file(team["id"])
        session_user = session.get("user") if isinstance(session.get("user"), dict) else {}
        cached_owner = _owner_from_cache(cache, team.get("owner_email") or "")

        email = (
            team.get("owner_email")
            or (cached_owner or {}).get("email")
            or session_user.get("email")
            or ""
        )
        name = (
            (cached_owner or {}).get("name")
            or session_user.get("name")
            or session_user.get("email")
            or ""
        )

        row = {
            "email": email,
            "name": name,
            "team_id": team["id"],
            "team_name": team.get("name"),
            "user_id": (cached_owner or {}).get("id") or (cached_owner or {}).get("user_id") or "",
            "seat_type": normalize_seat_type((cached_owner or {}).get("seat_type")),
            "card_last4": team.get("card_last4"),
            "card_brand": team.get("card_brand"),
            "billing_cycle": _billing_cycle(team),
            "active_start": team.get("active_start"),
            "active_until": team.get("active_until"),
            "team_status": team.get("status"),
            "is_codex_enabled": team.get("is_codex_enabled", False),
        }
        row = _attach_display_name(row, display_names)
        if _matches_query(row, q, ("email", "name", "team_name", "card_last4", "card_brand", "system_display_name")):
            items.append(row)

    return {"items": items, "total": len(items), "errors": errors}


@router.patch("/display-name")
async def update_user_display_name(body: UserDisplayNameUpdate):
    email = (body.email or "").strip()
    if not email:
        await log_operation(None, "update_user_display_name", None, None, "failed", "Email is required")
        raise HTTPException(status_code=400, detail="email is required")
    try:
        system_display_name = await set_display_name(email, body.system_display_name)
        new_name = body.system_display_name or "(cleared)"
        await log_operation(None, "update_user_display_name", email, f"new_name={new_name}", "success")
        return {
            "email": email,
            "system_display_name": system_display_name,
        }
    except ValueError as exc:
        await log_operation(None, "update_user_display_name", email, None, "failed", str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        await log_operation(None, "update_user_display_name", email, None, "failed", str(exc))
        raise


async def _load_expiry_rows() -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM member_expiry ORDER BY created_at DESC")
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def _load_tg_binding_map() -> dict[str, dict]:
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT email, username, paired_at
               FROM tg_member_bindings
               WHERE disabled = 0"""
        )
        rows = await cursor.fetchall()
    return {
        _normalize_email(row["email"]): {
            "bound": True,
            "username": row["username"],
            "paired_at": row["paired_at"],
        }
        for row in rows
        if _normalize_email(row["email"])
    }


def _tg_binding_payload(email: str, bindings: dict[str, dict]) -> dict:
    return bindings.get(_normalize_email(email), {"bound": False, "username": None, "paired_at": None})


def _expiry_maps(expiry_rows: list[dict]) -> tuple[dict, dict]:
    """Index only live expiry records for current member/invite payloads.

    ``expiry_rows`` deliberately still includes kicked records: the caller uses
    those later to build its historical ``kicked`` output.  A re-invited member
    can share a user id with that archived record while their live expiry is
    currently keyed only by email, so allowing the archive into ``by_user``
    would make the current row display the old expiry.
    """
    by_user: dict[tuple[str, str], dict] = {}
    by_email: dict[tuple[str, str], dict] = {}
    for row in expiry_rows:
        if row.get("kicked") != 0:
            continue
        team_id = row.get("team_id") or ""
        user_id = row.get("user_id") or ""
        email = _normalize_email(row.get("email"))
        if user_id and (team_id, user_id) not in by_user:
            by_user[(team_id, user_id)] = row
        if email and (team_id, email) not in by_email:
            by_email[(team_id, email)] = row
    return by_user, by_email


def _member_expiry_payload(team_id: str, user_id: str, email: str, fallback_expires_at: Optional[str], by_user: dict, by_email: dict, policy: dict) -> dict:
    row = by_user.get((team_id, user_id)) or by_email.get((team_id, _normalize_email(email)))
    expires_at = row.get("expires_at") if row else fallback_expires_at
    view = build_expiry_view(expires_at, policy)
    view["expiry_id"] = row.get("id") if row else None
    view["kicked"] = bool(row.get("kicked")) if row else False
    view["kicked_at"] = row.get("kicked_at") if row else None
    view["kick_source"] = row.get("kick_source") if row else None
    view["first_seen_at"] = row.get("first_seen_at") if row else None
    view["source"] = row.get("source") if row else None
    return view


def _member_actions(team_id: str, user_id: str, email: str, status_value: str) -> dict:
    if status_value == "joined":
        return {
            "kick": f"/api/teams/{team_id}/members/{user_id}",
            "change_seat": f"/api/teams/{team_id}/members/{user_id}/seat",
            "set_expiry": f"/api/teams/{team_id}/members/{user_id}/expiry",
            "extend_expiry": f"/api/teams/{team_id}/members/{user_id}/expiry/extend",
            "remove_expiry": f"/api/teams/{team_id}/members/{user_id}/expiry",
        }
    if status_value == "pending":
        return {
            "kick": f"/api/teams/{team_id}/invites/{email}",
            "revoke_invite": f"/api/teams/{team_id}/invites/{email}",
        }
    return {}


@router.get("/members")
async def list_members(
    q: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    team_id: Optional[str] = Query(None),
    include_owners: bool = Query(False),
    refresh: bool = Query(False),
):
    teams = await _load_teams()
    if team_id:
        teams = [team for team in teams if team["id"] == team_id]

    policy = await get_kick_policy()
    expiry_rows = await _load_expiry_rows()
    by_user, by_email = _expiry_maps(expiry_rows)
    display_names = await load_display_name_map()
    tg_bindings = await _load_tg_binding_map()
    errors: list[dict] = []
    items: list[dict] = []
    current_keys: set[tuple[str, str]] = set()

    for team in teams:
        cache = await _load_cache(team, refresh, errors)
        if not cache:
            continue

        owner_email = _normalize_email(team.get("owner_email"))
        owner = _owner_from_cache(cache, team.get("owner_email") or "")
        owner_name = (owner or {}).get("name") or team.get("owner_email") or ""

        for member in cache.get("members", []):
            email = _normalize_email(member.get("email"))
            user_id = member.get("id") or member.get("user_id") or ""
            if not include_owners and (member.get("is_owner") or email == owner_email):
                continue

            if user_id:
                current_keys.add((team["id"], user_id))
            if email:
                current_keys.add((team["id"], email))
            row = {
                "status": "joined",
                "status_label": "已加入",
                "team_id": team["id"],
                "team_name": team.get("name"),
                "owner_email": team.get("owner_email"),
                "owner_name": owner_name,
                # include_owners=true 时 Owner 行也会进来，调用方要能把它们分出去，
                # 不能靠 email == owner_email 各自再判一遍。
                "is_owner": bool(member.get("is_owner")) or email == owner_email,
                "user_id": user_id,
                "invite_id": None,
                "email": member.get("email") or "",
                "name": member.get("name"),
                "role": member.get("role"),
                "seat_type": normalize_seat_type(member.get("seat_type")),
                "created_time": member.get("created_time"),
                "is_codex_enabled": team.get("is_codex_enabled", False),
            }
            row["expiry"] = _member_expiry_payload(
                team["id"], user_id, row["email"], member.get("expires_at"), by_user, by_email, policy
            )
            row["actions"] = _member_actions(team["id"], user_id, row["email"], row["status"])
            items.append(_attach_display_name(row, display_names))

        for invite in cache.get("pending_invites", []):
            email = invite.get("email") or ""
            email_key = _normalize_email(email)
            if email_key:
                current_keys.add((team["id"], email_key))
            row = {
                "status": "pending",
                "status_label": "待接受",
                "team_id": team["id"],
                "team_name": team.get("name"),
                "owner_email": team.get("owner_email"),
                "owner_name": owner_name,
                "is_owner": False,
                "user_id": "",
                "invite_id": invite.get("id") or "",
                "email": email,
                "name": invite.get("name"),
                "role": invite.get("role"),
                "seat_type": normalize_seat_type(invite.get("seat_type")),
                "created_time": invite.get("created_time"),
                "is_codex_enabled": team.get("is_codex_enabled", False),
            }
            row["expiry"] = _member_expiry_payload(
                team["id"], "", row["email"], invite.get("expires_at"), by_user, by_email, policy
            )
            row["actions"] = _member_actions(team["id"], "", row["email"], row["status"])
            items.append(_attach_display_name(row, display_names))

    team_lookup = {team["id"]: team for team in teams}
    for expiry in expiry_rows:
        if not expiry.get("kicked"):
            continue
        team = team_lookup.get(expiry.get("team_id"))
        if not team:
            continue
        user_id = expiry.get("user_id") or ""
        email = expiry.get("email") or ""
        email_key = _normalize_email(email)
        if (user_id and (team["id"], user_id) in current_keys) or (email_key and (team["id"], email_key) in current_keys):
            continue

        row = {
            "status": "kicked",
            "status_label": "已踢出",
            "team_id": team["id"],
            "team_name": team.get("name"),
            "owner_email": team.get("owner_email"),
            "owner_name": team.get("owner_email") or "",
            "is_owner": False,
            "user_id": user_id,
            "invite_id": None,
            "email": email,
            "name": None,
            "role": None,
            "seat_type": None,
            "created_time": expiry.get("created_at"),
            "is_codex_enabled": team.get("is_codex_enabled", False),
            "expiry": {
                **build_expiry_view(expiry.get("expires_at"), policy),
                "expiry_id": expiry.get("id"),
                "kicked": True,
                "kicked_at": expiry.get("kicked_at"),
                "kick_source": expiry.get("kick_source"),
                "first_seen_at": expiry.get("first_seen_at"),
                "source": expiry.get("source"),
            },
            "actions": {},
        }
        items.append(_attach_display_name(row, display_names))

    for item in items:
        # 显示名走注册表；已踢记录没有席位类型，保持 None。
        item["seat_type_label"] = (
            seat_type_label(item["seat_type"]) if item.get("seat_type") else None
        )
        item["tg_binding"] = _tg_binding_payload(item.get("email") or "", tg_bindings)

    if status_filter:
        wanted = status_filter.strip().lower()
        items = [item for item in items if item["status"] == wanted]
    items = [
        item for item in items
        if _matches_query(item, q, ("email", "name", "team_name", "owner_email", "owner_name", "seat_type", "seat_type_label", "status_label", "system_display_name"))
    ]
    items.sort(key=lambda item: (item.get("team_name") or "", item.get("email") or "", item.get("status") or ""))

    return {"items": items, "total": len(items), "kick_policy": policy, "errors": errors}
