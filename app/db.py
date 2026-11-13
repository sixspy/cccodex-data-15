import sqlite3
from contextlib import contextmanager

from flask import current_app, g


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 5000")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def db_transaction():
    db = get_db()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


@contextmanager
def immediate_transaction():
    """写事务立即加锁，用于急件名额等需要串行化的“检查 + 写入”。

    同一时刻只允许一个连接进入；并发的第二个连接会等待锁释放后再读到
    已提交的占用数，因此最后一个名额只会被一个请求拿到。
    """
    db = get_db()
    if db.in_transaction:
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def init_db(app):
    with app.app_context():
        db = get_db()
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS crawl_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                seed_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS crawl_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                item_key TEXT NOT NULL,
                title TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(job_id, item_key)
            );

            -- 后台按日生效的急件名额 / 让路条数 / 落空口径（设置页登记）。
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- 急件标记：一个任务至多一条“生效中”的标记（partial unique index 兜底）。
            CREATE TABLE IF NOT EXISTS rush_flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                short_code TEXT NOT NULL,
                reason TEXT NOT NULL,
                designated_by TEXT NOT NULL,
                rush_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',  -- active | withdrawn
                fallback_policy TEXT,                   -- freeze | degrade，指定当日锁定
                withdrawn_by TEXT,
                withdraw_reason TEXT,
                withdrawn_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_rush_active_per_job
                ON rush_flags(job_id) WHERE status = 'active';
            CREATE INDEX IF NOT EXISTS ix_rush_date_status
                ON rush_flags(rush_date, status);

            -- 站级按日状态：落空口径快照、冻结、降级、处理不对记录。
            CREATE TABLE IF NOT EXISTS rush_days (
                rush_date TEXT PRIMARY KEY,
                policy TEXT NOT NULL,
                frozen INTEGER NOT NULL DEFAULT 0,
                degraded INTEGER NOT NULL DEFAULT 0,
                handling_mismatch TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- 每个任务当日的通道结果：急件入账 / 常件让路 / 候着区。
            CREATE TABLE IF NOT EXISTS rush_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                rush_date TEXT NOT NULL,
                channel TEXT NOT NULL,                  -- rush | normal
                landed INTEGER NOT NULL DEFAULT 1,     -- 急件是否实际落空
                fallback_policy TEXT,                  -- 当日锁定的落空口径
                degrade_flag INTEGER NOT NULL DEFAULT 0,
                frozen INTEGER NOT NULL DEFAULT 0,
                settled INTEGER NOT NULL DEFAULT 0,
                yield_count INTEGER NOT NULL DEFAULT 0,
                waitlist_count INTEGER NOT NULL DEFAULT 0,
                contention_result TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(job_id, rush_date)
            );

            -- 候着区：常件让路后未收的部分，按 seq 排队等待“继续”。
            CREATE TABLE IF NOT EXISTS waitlist_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                rush_date TEXT NOT NULL,
                seq INTEGER NOT NULL,
                title TEXT NOT NULL,
                item_key TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'waiting',  -- waiting | continued
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(job_id, rush_date, item_key)
            );
            CREATE INDEX IF NOT EXISTS ix_waitlist_date_seq
                ON waitlist_cards(rush_date, seq);

            -- 指定/争用审计：成功与否、名额争用失败原因。
            CREATE TABLE IF NOT EXISTS rush_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                rush_date TEXT NOT NULL,
                actor TEXT NOT NULL,
                short_code TEXT NOT NULL,
                ok INTEGER NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        _ensure_job_columns(db)
        db.commit()


def _ensure_job_columns(db):
    """给已有快照库补急件相关列（幂等）。"""
    cols = {
        row["name"]
        for row in db.execute("PRAGMA table_info(crawl_jobs)")
    }
    for name, ddl in (
        ("rush_channel", "TEXT"),          # 当日实际走的口径，仅作历史展示
        ("rush_via_candidate", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in cols:
            db.execute(f"ALTER TABLE crawl_jobs ADD COLUMN {name} {ddl}")
    rec_cols = {
        row["name"]
        for row in db.execute("PRAGMA table_info(crawl_records)")
    }
    for name, ddl in (
        ("channel", "TEXT NOT NULL DEFAULT 'normal'"),
        ("degrade_flag", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in rec_cols:
            db.execute(f"ALTER TABLE crawl_records ADD COLUMN {name} {ddl}")
