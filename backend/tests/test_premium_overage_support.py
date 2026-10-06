"""超员策略回归测试共用的替身与临时库（本模块没有用例）。

替身 Team 客户端只回读接口的固定数据，并把每一次调用记下来；写接口（邀请、切换席位）
只记录、返回成功，绝不碰 ChatGPT。用例用 ``client.mutations`` 断言被拒时没有发生任何写。
"""

import _isolation  # noqa: F401  must precede any app import
import asyncio
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app.services.team_locks import release_default_seat_reservation


async def direct_call(func, *args, **kwargs):
    """run_chatgpt_call 的替身：同步直接调用，不进线程池、不做限流 / 刷新。"""
    return func(*args, **kwargs)


class FakeTeamClient:
    """一个 Team 的上游替身。

    * 读：``get_subscription`` / ``get_seat_type_counts`` / ``get_pending_invites``。
      ``fail_reads=True`` 时订阅接口回 ``{"error": ...}``（= 读失败）。
    * 写：``invite_member`` / ``change_seat_type`` 只记录到 ``mutations`` 并回成功。
      其他写接口一律记录并抛错，让误调用的用例当场失败。
    """

    def __init__(
        self,
        *,
        seats_entitled=2,
        counts=None,
        seat_capacity=None,
        pending=(),
        fail_reads=False,
    ):
        self.seats_entitled = seats_entitled
        self.counts = dict(counts if counts is not None else {"default": 0, "usage_based": 0})
        self.seat_capacity = seat_capacity
        self.pending = list(pending)
        self.fail_reads = fail_reads
        self.reads: list[str] = []
        self.mutations: list[tuple] = []

    # ---- 读 ----
    def get_subscription(self):
        self.reads.append("get_subscription")
        if self.fail_reads:
            return {"error": "simulated read failure"}
        sub = {
            "seats_entitled": self.seats_entitled,
            "seats_in_use": sum(self.counts.values()),
        }
        if self.seat_capacity is not None:
            sub["seat_capacity"] = self.seat_capacity
        return sub

    def get_seat_type_counts(self):
        self.reads.append("get_seat_type_counts")
        return {"seat_type_counts": dict(self.counts)}

    def get_pending_invites(self, offset=0, limit=100):
        self.reads.append("get_pending_invites")
        return {"items": list(self.pending), "total": len(self.pending)}

    # ---- 写 ----
    def invite_member(self, email, seat_type="default"):
        self.mutations.append(("invite_member", email, seat_type))
        return {
            "account_invites": [{"email_address": email}],
            "errored_emails": [],
            "_mutation_status": "confirmed",
        }

    def change_seat_type(self, user_id, seat_type):
        self.mutations.append(("change_seat_type", user_id, seat_type))
        return {"id": user_id, "seat_type": seat_type}

    def _forbidden(self, name, *args):
        self.mutations.append((name, *args))
        raise AssertionError(f"unexpected upstream mutation: {name}")

    def remove_member(self, *args):
        self._forbidden("remove_member", *args)

    def revoke_invite(self, *args):
        self._forbidden("revoke_invite", *args)

    def resend_invite(self, *args):
        self._forbidden("resend_invite", *args)

    @property
    def capacity_reads(self) -> int:
        return self.reads.count("get_subscription")


def capacity_entries(**available_by_type):
    """``seat_capacity`` 上游形态：capacity_entries(default=(paid, available), prolite=(...))。"""
    return [
        {"type": seat_type, "paid": paid, "held": 0, "renewal_requested": paid, "available": available}
        for seat_type, (paid, available) in available_by_type.items()
    ]


class TempDbMixin:
    """每个用例一份 init_database() 建的临时库；进程内的席位预留在收尾时清掉。"""

    team_ids: tuple[str, ...] = ()

    def _start_db(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        db_dir_patch = patch.object(app_database, "get_db_dir", return_value=self.tmpdir.name)
        db_dir_patch.start()
        self.addCleanup(db_dir_patch.stop)
        asyncio.run(app_database.init_database())
        self.db_path = app_database.get_db_path()
        self._reserved: list[tuple[str, str]] = []

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def insert_team(
        self,
        team_id,
        *,
        policy="confirm",
        seats_entitled=2,
        members=(),
        pending=(),
        seat_capacity=None,
        created_at="2026-10-01T00:00:00+00:00",
    ):
        active_default = sum(
            1 for m in members if (m.get("seat_type") or "default") == "default"
        )
        codex = sum(1 for m in members if m.get("seat_type") == "usage_based")
        conn = self._conn()
        conn.execute(
            """INSERT INTO teams (id, name, status, owner_email, access_token, device_id,
                                  seats_in_use, seats_entitled, codex_count, chatgpt_count,
                                  active_until, will_renew, created_at, updated_at,
                                  overage_policy, seat_capacity_json)
               VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, NULL, 1, ?, ?, ?, ?)""",
            (
                team_id, f"{team_id}-name", f"owner-{team_id}@example.com", f"tok-{team_id}",
                f"dev-{team_id}", len(members), seats_entitled, codex, active_default,
                created_at, created_at, policy,
                json.dumps(seat_capacity) if seat_capacity is not None else None,
            ),
        )
        conn.execute(
            "INSERT INTO member_cache (team_id, members_json, pending_json, updated_at) VALUES (?, ?, ?, ?)",
            (team_id, json.dumps(list(members)), json.dumps(list(pending)), created_at),
        )
        conn.commit()
        conn.close()

    def set_policy(self, team_id, policy):
        conn = self._conn()
        conn.execute("UPDATE teams SET overage_policy = ? WHERE id = ?", (policy, team_id))
        conn.commit()
        conn.close()

    def logs(self, action=None):
        conn = self._conn()
        if action:
            rows = conn.execute(
                "SELECT team_id, action, target_email, detail, result, error_message "
                "FROM operation_logs WHERE action = ? ORDER BY id",
                (action,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT team_id, action, target_email, detail, result, error_message "
                "FROM operation_logs ORDER BY id"
            ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def track_reservation(self, team_id, email):
        """登记一个可能被预留的 (team, email)，收尾时释放，避免泄漏到别的测试模块。"""
        self._reserved.append((team_id, email))
        self.addCleanup(lambda: asyncio.run(release_default_seat_reservation(team_id, email)))

