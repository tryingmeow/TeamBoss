import aiosqlite
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from .chatgpt_client import mask_secrets

logger = logging.getLogger(__name__)


def get_db_dir() -> str:
    """获取数据库目录路径。支持 AUTO_TEAM_DATA_DIR 环境变量覆盖。"""
    data_dir = os.getenv("AUTO_TEAM_DATA_DIR")
    if data_dir:
        return data_dir
    # 默认值：backend/data
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def get_db_path() -> str:
    """获取数据库文件路径（app.db）。"""
    return os.path.join(get_db_dir(), "app.db")


def get_sessions_dir() -> str:
    """获取会话目录路径。"""
    return os.path.join(get_db_dir(), "sessions")


# 向后兼容：模块级变量（这些值是在导入时计算的，后续更改环境变量不会影响）
# 新代码应该调用 get_db_path() 等函数以获取最新的配置
DB_DIR = get_db_dir()
DB_PATH = get_db_path()
SESSIONS_DIR = get_sessions_dir()


@asynccontextmanager
async def get_db():
    db = await aiosqlite.connect(get_db_path())
    db.row_factory = aiosqlite.Row
    # WAL is a persistent, on-disk property of the database file (set once,
    # survives across connections/processes), but PRAGMA statements are
    # per-connection, so we still issue them on every connect. busy_timeout
    # makes SQLite retry internally instead of raising "database is locked"
    # immediately when another connection is nested inside an open read.
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=5000")
    try:
        yield db
    finally:
        await db.close()


async def _migrate(db, statement: str) -> None:
    """执行一条幂等的 schema 迁移语句（通常是 ``ALTER TABLE ... ADD COLUMN``）。

    sqlite 的 ``ADD COLUMN`` 没有 ``IF NOT EXISTS``，列已存在时必然报错——这是升级到
    已经跑过这条迁移的库时的预期路径，必须吞掉，不能阻塞启动。但把它写成
    ``except Exception: pass`` 会把"列已存在"之外的所有失败（磁盘满、库被锁、语句
    本身写错了列名/类型……）一起吞掉，迁移悄悄没生效，直到几个版本后在某个完全无关
    的地方炸出一个看不出原因的运行时错误。所以这里把"已存在"之外的失败单独识别出来，
    打一条 warning 日志（带上语句和原始异常），其余情况仍然原样吞掉、不阻塞启动、
    不引入任何 schema 版本门槛。
    """
    try:
        await db.execute(statement)
    except Exception as exc:
        message = str(exc).lower()
        if "duplicate column" in message or "already exists" in message:
            return
        logger.warning("schema 迁移执行失败，已跳过（不阻塞启动）: %s | %s", statement, exc)


async def init_database():
    os.makedirs(get_db_dir(), exist_ok=True)
    os.makedirs(get_sessions_dir(), exist_ok=True)
    os.chmod(get_db_dir(), 0o700)
    os.chmod(get_sessions_dir(), 0o700)

    async with get_db() as db:
        # sqlite creates a new file according to process umask.  The database
        # contains admin credentials and imported sessions, so never rely on a
        # deployment's umask being restrictive.
        os.chmod(get_db_path(), 0o600)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                id TEXT PRIMARY KEY,
                name TEXT,
                remark TEXT,
                owner_email TEXT,
                session_token TEXT,
                access_token TEXT,
                device_id TEXT,
                country_code TEXT,
                token_expires TEXT,
                card_last4 TEXT,
                card_brand TEXT,
                payment_method_id TEXT,
                seats_in_use INTEGER,
                seats_entitled INTEGER,
                codex_count INTEGER DEFAULT 0,
                chatgpt_count INTEGER,
                is_codex_enabled INTEGER DEFAULT 0,
                billing_currency TEXT,
                billing_symbol TEXT,
                billing_period TEXT,
                price_per_seat REAL,
                discount_amount REAL DEFAULT 0,
                discount_duration_num_periods INTEGER,
                discount_expires_at TEXT,
                discount_quantity_off INTEGER,
                promo_campaign_id TEXT,
                balance TEXT,
                active_start TEXT,
                active_until TEXT,
                will_renew INTEGER,
                status TEXT DEFAULT 'active',
                cached_data TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS member_expiry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id TEXT,
                user_id TEXT,
                email TEXT,
                expires_at TEXT,
                auto_kick INTEGER DEFAULT 1,
                kicked INTEGER DEFAULT 0,
                kicked_at TEXT,
                kick_source TEXT,
                first_seen_at TEXT,
                source TEXT DEFAULT 'system',
                created_at TEXT
            )
        """)

        # migrate: add first_seen_at / source columns for existing databases
        await _migrate(db, "ALTER TABLE member_expiry ADD COLUMN first_seen_at TEXT")
        await _migrate(db, "ALTER TABLE member_expiry ADD COLUMN source TEXT DEFAULT 'system'")
        await _migrate(db, "ALTER TABLE member_expiry ADD COLUMN kick_source TEXT")

        # 命中率高的查找：member_expiry 上按 team_id 单独过滤（如巡检拉全量成员）
        # 以及按 team_id + lower(email) 精确定位一条记录（如踢人前查 source/expires_at，
        # 见 services/patrol.py 里若干 "WHERE team_id = ? ... AND lower(email) = ?"）。
        # lower(email) 用表达式索引，因为热路径查询就是这样写谓词的——普通索引
        # 不会被 lower(email) = ? 命中。
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_member_expiry_team_id "
            "ON member_expiry(team_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_member_expiry_lower_email "
            "ON member_expiry(team_id, lower(email))"
        )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS operation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id TEXT,
                action TEXT,
                target_email TEXT,
                detail TEXT,
                result TEXT,
                error_message TEXT,
                trigger_type TEXT,
                created_at TEXT
            )
        """)
        # chatgpt_limiter._cooldown_until 每次 token 刷新都要跑
        # "WHERE team_id=? AND action IN (...) ORDER BY id DESC LIMIT 1"，
        # 无索引时对 operation_logs 全表扫描。
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_operation_logs_team_action_id "
            "ON operation_logs(team_id, action, id)"
        )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            )
        """)

        # 后台密码登录成功过的来源（IPv4 地址 / IPv6 /64）。这些来源不受全站登录
        # 冷却影响，只受按来源的失败锁定；见 routes/admin.py 的 _GlobalLoginBudget。
        # 时间是 Unix 秒；条目数和有效期由 admin.py 维护。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_login_trusted_sources (
                source TEXT PRIMARY KEY,
                first_success_at REAL NOT NULL,
                last_success_at REAL NOT NULL
            )
        """)

        # 成员列表缓存（懒加载：首次打开成员面板时写入）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS member_cache (
                team_id TEXT PRIMARY KEY,
                members_json TEXT DEFAULT '[]',
                pending_json  TEXT DEFAULT '[]',
                updated_at TEXT
            )
        """)

        # 这份快照的上游拉取是什么时候开始的（第一个列表请求发出之前取的 UTC 时间）。
        # 写缓存时只允许开始得更晚的快照覆盖更早的；巡逻的 Premium 否决拿它比对
        # TeamBoss 之后有没有动过这个人的席位。旧行为 NULL = 不知道，按最旧处理。
        await _migrate(db, "ALTER TABLE member_cache ADD COLUMN fetch_started_at TEXT")

        # 持久席位占用：邀请 / 切换已发出、上游空位数还不一定扣掉它的席位。正本说明见
        # services/seat_holds.py。重启不清空，只由对账或上游明确拒绝放掉。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS seat_holds (
                team_id TEXT NOT NULL,
                email TEXT NOT NULL,
                seat_type TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY (team_id, email)
            )
        """)

        # 管理员确认过的加购额度：一次确认（confirmation_id，前端生成）绑定一个 Team、一个
        # 计费席位类型和他看到的加购个数 seat_limit；每次没有确认空位的加席位先扣 1，
        # 只有上游明确拒绝才退回。规则正本见 services/overage_policy.py。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS overage_confirmations (
                confirmation_id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                seat_type TEXT NOT NULL,
                seat_limit INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        """)

        # 成员变动监视任务（邀请/踢人后轮询，直到变动反映到 API）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS member_watch (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                target_email TEXT,
                target_user_id TEXT,
                started_at TEXT,
                expires_at TEXT,
                done INTEGER DEFAULT 0,
                tg_chat_id TEXT,
                tg_message_id INTEGER
            )
        """)

        # member_watch 列迁移（已有库可能缺新列）
        for col, col_type in [("tg_chat_id", "TEXT"), ("tg_message_id", "INTEGER")]:
            await _migrate(db, f"ALTER TABLE member_watch ADD COLUMN {col} {col_type}")

        # 自助加入/续期 token（只存 SHA-256，明文 token 只在生成响应中返回一次）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS access_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT NOT NULL UNIQUE,
                token_prefix TEXT NOT NULL,
                grant_expires_in TEXT NOT NULL,
                token_expires_at TEXT,
                max_uses INTEGER DEFAULT 1,
                used_count INTEGER DEFAULT 0,
                note TEXT,
                disabled INTEGER DEFAULT 0,
                created_at TEXT,
                last_used_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS access_token_uses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id INTEGER,
                email TEXT,
                action TEXT,
                team_id TEXT,
                user_id TEXT,
                expires_at TEXT,
                result TEXT,
                error_message TEXT,
                created_at TEXT
            )
        """)

        # 同一邮箱的兑换必须串行。邀请接口超时/断线时远端结果可能已生效，
        # 这条持久化 claim 会跨进程重启继续锁住邮箱，避免另一张码把同一人
        # 又拉进第二个 Team。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS redemption_email_claims (
                email TEXT PRIMARY KEY,
                token_use_id INTEGER NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
        """)

        # 续期与自动踢人/撤邀请共享的跨线程、跨进程互斥。单纯 asyncio.Lock
        # 无法覆盖 APScheduler 线程，也无法在重启边界保护远端写操作。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS member_operation_claims (
                operation_key TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                email TEXT,
                user_id TEXT,
                operation TEXT NOT NULL,
                owner_token TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        # 管理员手工续期也可能在提交已落库、响应却丢失的边界被重试。把请求收据和
        # 到期写入放进同一事务，重复同一 request_id 只能读回第一次的结果，绝不能
        # 再加一次时长。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_expiry_extension_receipts (
                team_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                email TEXT NOT NULL,
                duration TEXT NOT NULL,
                expires_at TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (team_id, user_id, request_id)
            )
        """)

        # 邀请已在 OpenAI 侧成功、但本地 member_expiry 落库重试多次后仍失败的兜底队列。
        # OpenAI 邀请动作不可回滚，这里必须留痕以便人工/后续流程补齐，绝不能让记录悄悄丢失
        # （否则下一轮同步会把系统自己拉的人误判成外部乱拉的人）。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_invite_reconciliations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id TEXT NOT NULL,
                user_id TEXT,
                email TEXT NOT NULL,
                expires_at TEXT,
                source TEXT NOT NULL,
                reason TEXT,
                resolved INTEGER DEFAULT 0,
                resolved_at TEXT,
                created_at TEXT NOT NULL,
                token_use_id INTEGER,
                kind TEXT DEFAULT 'backfill'
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_invite_reconciliations_resolved "
            "ON pending_invite_reconciliations(resolved, created_at)"
        )

        # migrate: 兑换凭据与行类型。
        # ``token_use_id`` 让这条兜底行和它对应的那次兑换绑定，使"调度器回填"与
        # "兑换对账"共用同一张一次性凭据——两条恢复路径不会把同一张码的时长加两次。
        # ``kind``：'backfill' 是原有语义（远端邀请已确认、本地落库失败，需要按
        # 行内 expires_at 回填）；'barrier' 是结果未定的自助邀请只借这张表挡住巡逻，
        # 调度器不得据此写任何到期时间，结算一律走 reconcile_pending_redemptions。
        for stmt in (
            "ALTER TABLE pending_invite_reconciliations ADD COLUMN token_use_id INTEGER",
            "ALTER TABLE pending_invite_reconciliations ADD COLUMN kind TEXT DEFAULT 'backfill'",
        ):
            await _migrate(db, stmt)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                status TEXT DEFAULT 'unknown',
                last_check_at TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_display_names (
                email TEXT PRIMARY KEY,
                system_display_name TEXT NOT NULL,
                updated_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS billing_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id TEXT NOT NULL,
                snapshot_date TEXT NOT NULL,
                billing_currency TEXT,
                price_per_seat REAL,
                seats_entitled INTEGER,
                seats_in_use INTEGER,
                codex_count INTEGER,
                chatgpt_count INTEGER,
                discount_amount REAL,
                monthly_total REAL,
                balance TEXT,
                active_until TEXT,
                will_renew INTEGER,
                created_at TEXT,
                UNIQUE(team_id, snapshot_date)
            )
        """)

        # Stripe 发票的本地缓存。上游响应里的 customer_email / customer_name /
        # customer_address 等 PII 在入库前就被丢掉（见 services/invoices.py），
        # 这张表只允许出现对账需要的字段。金额已换算成主货币单位。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id TEXT NOT NULL,
                invoice_id TEXT NOT NULL,
                number TEXT,
                status TEXT,
                currency TEXT,
                amount_due REAL,
                amount_paid REAL,
                period_start TEXT,
                period_end TEXT,
                description TEXT,
                hosted_invoice_url TEXT,
                created_at TEXT,
                fetched_at TEXT,
                UNIQUE(team_id, invoice_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS finance_card_notes (
                card_key TEXT PRIMARY KEY,
                card_brand TEXT,
                card_last4 TEXT NOT NULL,
                note TEXT DEFAULT '',
                updated_at TEXT
            )
        """)

        # Telegram 管理员白名单。普通成员走独立的邮箱绑定表，不能访问后台命令。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tg_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL UNIQUE,
                username TEXT,
                note TEXT,
                disabled INTEGER DEFAULT 0,
                paired_at TEXT,
                created_at TEXT
            )
        """)

        # Telegram 配对码（一次性凭码注册；管理员在后台生成后发给使用者）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tg_pairing_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                note TEXT,
                expires_at TEXT,
                used_by_chat_id TEXT,
                used_at TEXT,
                disabled INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)

        # Team health incidents back Telegram alerts with durable de-duplication.
        # An incident stays open across process restarts and is resolved only after
        # a later successful refresh/sync for the same Team and alert category.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS team_health_incidents (
                team_id TEXT NOT NULL,
                alert_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                first_failed_at TEXT,
                last_failed_at TEXT,
                last_error TEXT,
                last_source TEXT,
                failure_count INTEGER NOT NULL DEFAULT 1,
                notified INTEGER NOT NULL DEFAULT 0,
                resolved_at TEXT,
                updated_at TEXT,
                PRIMARY KEY (team_id, alert_key)
            )
        """)

        # 巡逻保护必须按 Team 建立。全局时间只能说明巡逻曾开启过，不能证明
        # 之后新增或重新启用的 Team 已完成现有成员保护。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS patrol_team_baselines (
                team_id TEXT PRIMARY KEY,
                baseline_at TEXT NOT NULL
            )
        """)

        # 早期 TG 机器人曾提供 viewer 只读运维角色。产品现只允许管理员和
        # 已绑定成员使用：删除 viewer 数据，并从两张管理表移除遗留 role 字段。
        tg_user_columns = {
            row["name"]
            for row in await (await db.execute("PRAGMA table_info(tg_users)" )).fetchall()
        }
        if "role" in tg_user_columns:
            await db.execute("DELETE FROM tg_users WHERE COALESCE(role, 'viewer') != 'admin'")
            await db.execute("ALTER TABLE tg_users DROP COLUMN role")

        tg_code_columns = {
            row["name"]
            for row in await (await db.execute("PRAGMA table_info(tg_pairing_codes)" )).fetchall()
        }
        if "role" in tg_code_columns:
            await db.execute(
                "DELETE FROM tg_pairing_codes WHERE COALESCE(role, 'viewer') != 'admin'"
            )
            await db.execute("ALTER TABLE tg_pairing_codes DROP COLUMN role")

        # ChatGPT 成员邮箱与 Telegram chat 的绑定。一个 chat 可绑定多个邮箱；
        # email 唯一，确保同一成员身份只会把提醒发给一个 Telegram 账号。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tg_member_bindings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                chat_id TEXT NOT NULL,
                username TEXT,
                disabled INTEGER DEFAULT 0,
                paired_at TEXT,
                disabled_at TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tg_member_bindings_chat_id "
            "ON tg_member_bindings(chat_id, disabled)"
        )

        # 成员绑定码与管理员权限码分表，避免成员配对时意外获得后台权限。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tg_member_pairing_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL COLLATE NOCASE,
                expires_at TEXT,
                used_by_chat_id TEXT,
                used_at TEXT,
                disabled INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tg_member_pairing_codes_email "
            "ON tg_member_pairing_codes(email, disabled, used_at)"
        )

        # 每个到期周期、每个提醒阶段只允许成功发送一次。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tg_member_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL COLLATE NOCASE,
                team_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                reminder_key TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                UNIQUE(email, team_id, expires_at, reminder_key)
            )
        """)

        await _migrate(db, "ALTER TABLE teams ADD COLUMN proxy_id INTEGER")
        await _migrate(db, "ALTER TABLE teams ADD COLUMN remark TEXT")
        for statement in (
            "ALTER TABLE teams ADD COLUMN is_codex_enabled INTEGER DEFAULT 0",
            "ALTER TABLE teams ADD COLUMN billing_symbol TEXT",
            "ALTER TABLE teams ADD COLUMN discount_amount REAL DEFAULT 0",
            "ALTER TABLE teams ADD COLUMN discount_duration_num_periods INTEGER",
            "ALTER TABLE teams ADD COLUMN discount_expires_at TEXT",
            "ALTER TABLE teams ADD COLUMN discount_quantity_off INTEGER",
            "ALTER TABLE teams ADD COLUMN promo_campaign_id TEXT",
            # 计费周期（monthly/yearly）。历史数据一律未知，不能假定 monthly——
            # 上游按月计算金额前必须先检查这一列，非 monthly/未知一律不计算月费。
            "ALTER TABLE teams ADD COLUMN billing_period TEXT",
            # 同步失败追踪：上一次 overview 同步中失败的子接口列表（JSON，如 ["subscription", "balance"]）
            "ALTER TABLE teams ADD COLUMN last_sync_partial_failures TEXT",
            # 最后一次完整成功的同步时间（所有 overview 子接口都成功）
            "ALTER TABLE teams ADD COLUMN last_full_sync_at TEXT",
            # 官方结算页的 ChatGPT 占用分子来自 seat_type_counts.default。
            # 保留 seats_in_use 作为 ChatGPT+Codex 总占用，避免字段语义混用。
            "ALTER TABLE teams ADD COLUMN chatgpt_count INTEGER",
            # 授权状态，与 status 分开。'rejected' 表示：库里的 access token 已经用不了
            # （业务接口 401，或 JWT exp 已过），而 /api/auth/session 仍然应答却交不出
            # 新的——交回同一个 token，或 200 带 RefreshAccessTokenError。会话已经换
            # 不出新 token，只能重新导入。
            #
            # 这里刻意不动 status：scheduler / patrol / 成员缓存等十余处都按
            # `WHERE status = 'active'` 取 Team，一旦把状态改掉，这个 Team 会直接
            # 掉出定时同步循环，再也不会重试，也就永远无法自愈。
            "ALTER TABLE teams ADD COLUMN auth_state TEXT",
            # 进入 rejected 的时间，用于在界面上显示「已持续 N 小时」。
            "ALTER TABLE teams ADD COLUMN auth_state_since TEXT",
            "ALTER TABLE billing_snapshots ADD COLUMN chatgpt_count INTEGER",
            # 上一次成功拉取发票的时间，发票同步按团队最多一天一次。
            "ALTER TABLE teams ADD COLUMN invoices_synced_at TEXT",
            # 上一次成功拉取纯展示字段（余额/卡号/Team 名/折扣/默认席位/单价）的时间。
            # 这些是周级慢变量，按团队最多 DISPLAY_SYNC_INTERVAL_HOURS 小时一次。
            # patrol 的输入（seats_entitled / 席位计数 / 成员名单）不在此列，仍然每轮实时拉。
            "ALTER TABLE teams ADD COLUMN display_synced_at TEXT",
            # 定时同步挂起：连续失败的起点、挂起时刻、挂起期间上一次探活时刻。
            # 上游吊销 token 之后重试不会有别的结果，只会每天上千次注定 401 的
            # 请求和上百条重复告警；挂起后按低频探活自愈，重新导入会话即恢复。
            "ALTER TABLE teams ADD COLUMN sync_failing_since TEXT",
            "ALTER TABLE teams ADD COLUMN sync_suspended_at TEXT",
            "ALTER TABLE teams ADD COLUMN sync_probe_at TEXT",
            # 一个 Team 第一次被巡逻 grandfather（detected 成员转成 system）的时间；非空 =
            # 已经做过。巡逻自动建基线时只有标记为空才 grandfather（管理员显式开启不看它，
            # 每次都保护）。patrol_team_baselines 会在 token_expired / 重新导入时被删掉重建，
            # 这个标记不会——它只随 teams 行一起消失（删除 Team），重新添加的 Team 才重新算
            # "第一次"。见 services/patrol._protect_team_snapshot_sync。
            "ALTER TABLE teams ADD COLUMN patrol_grandfathered_at TEXT",
        ):
            await _migrate(db, statement)

        # 标记列上线前，凡是当前有巡逻基线的 Team 都已经被 grandfather 过，补上标记，
        # 否则它们下一次 token_expired 恢复、巡逻自动重建基线时会把武装期间检测到的
        # 外部成员再洗白一次。只填 NULL，
        # 重复启动是空操作；当前没有基线的 Team（例如正处于 token_expired）无从判断，
        # 保持 NULL，下一次建立基线时按"第一次"处理。
        await _migrate(
            db,
            """UPDATE teams
               SET patrol_grandfathered_at = (
                   SELECT b.baseline_at FROM patrol_team_baselines b WHERE b.team_id = teams.id
               )
               WHERE patrol_grandfathered_at IS NULL
                 AND EXISTS (
                     SELECT 1 FROM patrol_team_baselines b WHERE b.team_id = teams.id
                 )""",
        )

        # 分类型席位缓存（JSON）：subscription.seat_capacity 解析后的
        # {type: {paid, available}}，以及 seat_type_counts 的原样计数（含未知类型）。
        # 写入规则见 services/seat_capacity.subscription_column_updates / seat_counts_column_updates。
        await _migrate(db, "ALTER TABLE teams ADD COLUMN seat_capacity_json TEXT")
        await _migrate(db, "ALTER TABLE teams ADD COLUMN seat_type_counts_json TEXT")
        # Premium 席位每席每月价格（pricing 响应 currency_config.business_prolite 的 month / year 桶，
        # 不含税，币种同 billing_currency）。写入规则和 price_per_seat 一样，读不到就是 NULL（= 未知）。
        # 老库升级后先是 NULL，下一次展示字段同步时补上。
        await _migrate(db, "ALTER TABLE teams ADD COLUMN premium_price_per_seat REAL")
        # 两个单价取自哪个计费周期的桶（'monthly' / 'yearly'）。只有它和 billing_period 一致时单价
        # 才算数，年付价格永远不会被当成月付价格乘（见 services/pricing.priced_period）。
        # 老行是 NULL：那时只有月付 Team 存单价，NULL + monthly 按月付认，NULL + yearly 不认。
        await _migrate(db, "ALTER TABLE teams ADD COLUMN price_period TEXT")
        await _migrate(db, "ALTER TABLE teams ADD COLUMN discount_start_in_num_periods INTEGER")
        # 计费快照的 monthly_total 和财务页同一算法（services/pricing.team_monthly_cost）：
        # 含真实 Premium 部分，年付 Team 记月均。这几列记下当天的计费周期、Premium 单价和已付席位，
        # 好对得上总额。
        await _migrate(db, "ALTER TABLE billing_snapshots ADD COLUMN premium_price_per_seat REAL")
        await _migrate(db, "ALTER TABLE billing_snapshots ADD COLUMN premium_seats_paid INTEGER")
        await _migrate(db, "ALTER TABLE billing_snapshots ADD COLUMN billing_period TEXT")
        # 兑换码的席位类型（default = ChatGPT，prolite = Premium）。历史码都是 ChatGPT 码。
        await _migrate(
            db, "ALTER TABLE access_tokens ADD COLUMN seat_type TEXT NOT NULL DEFAULT 'default'"
        )

        # 每个 Team 的超员策略（forbid / confirm / auto）。全局开关 skip_overage_confirmation
        # 折进来：只在这一列第一次加上时迁移一次——全局开着的库全部 Team 设成 auto，
        # 否则保持默认 confirm。之后新加的 Team 用列默认值 confirm，全局开关不再生效。
        team_columns = {
            row[1] for row in await (await db.execute("PRAGMA table_info(teams)")).fetchall()
        }
        if "overage_policy" not in team_columns:
            await db.execute(
                "ALTER TABLE teams ADD COLUMN overage_policy TEXT NOT NULL DEFAULT 'confirm'"
            )
            cursor = await db.execute(
                "SELECT value FROM settings WHERE key = 'skip_overage_confirmation'"
            )
            skip_row = await cursor.fetchone()
            if skip_row is not None and str(skip_row[0]).strip().lower() == "true":
                await db.execute("UPDATE teams SET overage_policy = 'auto'")
                # 折进 Team 之后全局开关退役：写回 false，升级前打开的旧页面读到它也不会
                # 再自动带上 allow_overage 跳过确认。
                await db.execute(
                    "UPDATE settings SET value = 'false' WHERE key = 'skip_overage_confirmation'"
                )

        # 历史库先用旧等式回填，保证升级后 API 合同立即可用；下一轮官方
        # seat_type_counts 同步会用 default 字段覆盖为权威值。
        await db.execute(
            """UPDATE teams
               SET chatgpt_count = MAX(
                   0,
                   COALESCE(seats_in_use, 0) - COALESCE(codex_count, 0)
               )
               WHERE chatgpt_count IS NULL"""
        )
        await db.execute(
            """UPDATE billing_snapshots
               SET chatgpt_count = MAX(
                   0,
                   COALESCE(seats_in_use, 0) - COALESCE(codex_count, 0)
               )
               WHERE chatgpt_count IS NULL"""
        )

        # 轮换 API Key 的宽限期已取消（见 security.rotate_admin_api_key）：旧 Key 立即
        # 失效，不再保留。历史库里这两行是没人再读的旧密钥，删掉。
        await db.execute(
            "DELETE FROM settings WHERE key IN "
            "('admin_api_key_previous', 'admin_api_key_previous_expires_at')"
        )

        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            ("sync_interval_minutes", "15", now)
        )
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            ("api_concurrency", "4", now)
        )
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            ("expiry_kick_mode", "delay_hours", now)
        )
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            ("expiry_kick_delay_hours", "0", now)
        )
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            ("skip_overage_confirmation", "false", now)
        )
        # Telegram 机器人 & 巡逻踢人设置。token 明文只存 DB（不入代码/仓库），默认空、需运行时写入。
        for _skey, _sval in (
            ("tg_bot_token", ""),            # 机器人 token（运行时写入）
            ("tg_bot_id", ""),               # getMe 得到的稳定机器人 id，用于识别是否换 Bot
            ("tg_bot_username", ""),         # getMe 得到的用户名缓存，用于成员绑定复制模板
            ("tg_bot_enabled", "0"),         # 机器人长轮询开关
            ("tg_summary_enabled", "0"),     # 自动同步完成后向 TG 管理员推送摘要
            ("tg_summary_interval_minutes", "15"),  # 摘要最短推送间隔
            ("tg_summary_last_sent_at", ""), # 最近一次成功摘要推送时间
            ("patrol_kick_enabled", "0"),    # 巡逻踢人：'0'=空跑演练 '1'=真踢
            ("patrol_baseline_at", ""),      # 祖父基线时间戳；为空时巡逻强制空跑（未保护现有成员前绝不真踢）
            ("patrol_exempt_team_ids", "[]"),# 永不自动踢的 Team id 列表（JSON 数组）
            # 严格模式：忽略超员判定 + Codex 豁免，把所有非系统拉入的人一律列为候选。
            # 独立危险开关，默认关闭；即便开启，仍受基线保护/豁免名单/踢人延迟/批量护栏约束。
            ("patrol_strict_mode_enabled", "0"),
        ):
            await db.execute(
                "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (_skey, _sval, now)
            )

        await db.commit()


async def log_operation(team_id: str, action: str, target_email: str = None,
                        detail: str = None, result: str = None,
                        error_message: str = None, trigger_type: str = "manual"):
    now = datetime.now(timezone.utc).isoformat()
    # detail / error_message 是自由文本，调用方经常直接把异常 str() 塞进来（代理
    # URL 的 user:pass@、token 等都可能混在里面），且这里落库后会经 GET /api/logs
    # 原样返回给前端。统一在写入这一处掩码，调用方不用各自记得脱敏。
    detail = mask_secrets(detail) if detail else detail
    error_message = mask_secrets(error_message) if error_message else error_message
    async with get_db() as db:
        await db.execute(
            """INSERT INTO operation_logs
               (team_id, action, target_email, detail, result, error_message, trigger_type, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (team_id, action, target_email, detail, result, error_message, trigger_type, now)
        )
        await db.commit()
