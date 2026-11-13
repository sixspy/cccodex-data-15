"""急件通道 / 常件候着区 / 全站当日急件名额 / 落空处理口径。

实现口径（与设置页、列表卡片、详情页文案一致）：

* 急件标记是“任务级指定 + 站级按日名额”的组合，不是单任务上的一个勾：
  指定时必须同时写急件编码与原因，空编码或空原因直接失败。
* 名额按**后台日期**结算（``today_str`` 默认取服务器当日日期；测试用
  ``app.config["TODAY_OVERRIDE"]`` 钉死）。当日生效中的急件标记数达到名额
  后，新的指定失败并提示“名额已满”。
* 指定急件时即选定落空处理口径，当日锁定、禁止改口；设置页登记站级口径。
  点继续时，当日锁定的落空口径与设置页登记值不一致，直接挡下并记录
  “处理不对”。
* 落空 = 急件通道开跑后一条都没入账（来源为空 / 来源不可用）。
  - ``freeze``：冻结候着区，直到该急件标记被撤下；冻结期间不能继续。
  - ``degrade``：候着区改走旁路按常件继续，写入的候着记录打降级标记。
* 撤下急件标记必须写原因，且不能由指定人本人撤下。
* 当日有未收束的候着卡片（未被冻结）时，不能修改急件标记，也不能撤下。
* 名额争用用 ``BEGIN IMMEDIATE`` 串行化“查已用 + 写入”：两个任务同时争
  最后一个名额时只有一个成功，失败原因记入 rush_attempts 供详情页展示。
"""

import json
import sqlite3
from datetime import datetime

from flask import current_app

from app.db import get_db, immediate_transaction

POLICY_FREEZE = "freeze"
POLICY_DEGRADE = "degrade"

POLICY_LABELS = {
    POLICY_FREEZE: "冻结候着区（直到撤下急件标记）",
    POLICY_DEGRADE: "旁路按常件继续（打降级标记）",
}

DEFAULT_QUOTA = 2
DEFAULT_YIELD = 10  # 常件先收条数；默认覆盖小目录，基线行为不变
DEFAULT_POLICY = POLICY_FREEZE


class RushRuleError(ValueError):
    """急件通道规则拒绝；消息直接面向用户。"""


def today_str() -> str:
    """后台日期。测试可钉死：``app.config['TODAY_OVERRIDE']``。"""
    override = current_app.config.get("TODAY_OVERRIDE")
    if override:
        return str(override)
    return datetime.now().strftime("%Y-%m-%d")


def _coerce_int(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def get_rush_settings(db=None) -> dict:
    db = db or get_db()
    stored = {
        row["key"]: row["value"]
        for row in db.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'rush_%'"
        )
    }
    quota = _coerce_int(
        stored.get("rush_quota"),
        _coerce_int(current_app.config.get("RUSH_QUOTA_DEFAULT"), DEFAULT_QUOTA),
    )
    yield_count = _coerce_int(
        stored.get("rush_yield"),
        _coerce_int(current_app.config.get("RUSH_YIELD_DEFAULT"), DEFAULT_YIELD),
    )
    policy = stored.get(
        "rush_policy",
        current_app.config.get("RUSH_POLICY_DEFAULT", DEFAULT_POLICY),
    )
    if policy not in POLICY_LABELS:
        policy = DEFAULT_POLICY
    return {
        "quota": max(0, quota),
        "yield_count": max(0, yield_count),
        "policy": policy,
        "policy_label": POLICY_LABELS[policy],
    }


def save_rush_settings(quota, yield_count, policy, db=None) -> dict:
    policy = (policy or "").strip()
    if policy not in POLICY_LABELS:
        raise RushRuleError("落空处理口径只能登记为 冻结 或 降级")
    quota_i = _coerce_int(quota, -1)
    yield_i = _coerce_int(yield_count, -1)
    if quota_i < 0 or yield_i < 0:
        raise RushRuleError("名额与让路条数必须是非负整数")
    db = db or get_db()
    with immediate_transaction() as tx:
        for key, value in (
            ("rush_quota", str(quota_i)),
            ("rush_yield", str(yield_i)),
            ("rush_policy", policy),
        ):
            tx.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value),
            )
    return get_rush_settings(db)


def ensure_day(db, date: str):
    """取当日站级状态；首次出现时快照设置页登记口径。"""
    row = db.execute(
        "SELECT * FROM rush_days WHERE rush_date = ?", (date,)
    ).fetchone()
    if row is not None:
        return row
    policy = get_rush_settings(db)["policy"]
    db.execute(
        "INSERT OR IGNORE INTO rush_days (rush_date, policy) VALUES (?, ?)",
        (date, policy),
    )
    return db.execute(
        "SELECT * FROM rush_days WHERE rush_date = ?", (date,)
    ).fetchone()


def get_day(db, date: str):
    return db.execute(
        "SELECT * FROM rush_days WHERE rush_date = ?", (date,)
    ).fetchone()


def active_flag(db, job_id: int, date: str | None = None):
    date = date or today_str()
    return db.execute(
        """
        SELECT * FROM rush_flags
        WHERE job_id = ? AND status = 'active' AND rush_date = ?
        """,
        (job_id, date),
    ).fetchone()


def _pending_cards(db, date: str):
    return db.execute(
        """
        SELECT * FROM waitlist_cards
        WHERE rush_date = ? AND status = 'waiting'
        ORDER BY seq, id
        """,
        (date,),
    ).fetchall()


def is_day_unsettled(db, date: str | None = None) -> bool:
    """开跑未收束 = 当日仍有候着卡片未被冻结、未继续。

    冻结是 freeze 口径下的稳定态（“直到撤下急件标记”），不算未收束，
    否则撤下会被规则卡死；撤下后解冻，卡片重新成为未收束。
    """
    date = date or today_str()
    day = get_day(db, date)
    if day is not None and day["frozen"]:
        return False
    return bool(_pending_cards(db, date))


def _validate_code_reason(short_code: str, reason: str) -> tuple[str, str]:
    short_code = (short_code or "").strip()
    reason = (reason or "").strip()
    if not short_code:
        raise RushRuleError("急件编码不能为空")
    if not reason:
        raise RushRuleError("急件原因不能为空")
    return short_code, reason


def _quota_status(db, date: str, settings: dict | None = None) -> tuple[int, int]:
    """返回 (当日生效名额, 当日已用)；已用即当日生效中的急件标记条数。"""
    settings = settings or get_rush_settings(db)
    ensure_day(db, date)
    used = db.execute(
        "SELECT COUNT(*) AS c FROM rush_flags "
        "WHERE rush_date = ? AND status = 'active'",
        (date,),
    ).fetchone()["c"]
    return settings["quota"], used


def _quota_full_message(date: str, quota: int, used: int) -> str:
    return (
        f"名额已满：后台日期 {date} 急件名额 {quota} 条，已用 {used} 条"
    )


def _record_attempt(db, job_id, date, actor, short_code, ok, message="") -> None:
    db.execute(
        """
        INSERT INTO rush_attempts
            (job_id, rush_date, actor, short_code, ok, message)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (job_id, date, actor or "", short_code or "", 1 if ok else 0, message),
    )


def designate_in_tx(
    db,
    job_id: int,
    short_code: str,
    reason: str,
    actor: str,
    policy: str | None = None,
    date: str | None = None,
) -> tuple[bool, str]:
    """在调用方的事务内指定急件标记，并返回 (成功, 失败原因)。"""
    date = date or today_str()
    short_code, reason = _validate_code_reason(short_code, reason)
    existing = db.execute(
        "SELECT id FROM rush_flags WHERE job_id = ? AND status = 'active'",
        (job_id,),
    ).fetchone()
    if existing is not None:
        return False, "该任务已有生效中的急件标记，不能重复指定"
    settings = get_rush_settings(db)
    chosen = (policy or settings["policy"]).strip()
    if chosen not in POLICY_LABELS:
        return False, "落空处理口径只能是 冻结 或 降级"
    ensure_day(db, date)
    quota, used = _quota_status(db, date, settings)
    if used >= quota:
        message = _quota_full_message(date, quota, used)
        _record_attempt(db, job_id, date, actor, short_code, False, message)
        return False, message
    # “一经选定当日禁止改口”：当日本次是第一条急件时，日口径以所选为准；
    # 当日已有生效急件时，后来者必须沿用已锁定的日口径。
    if used == 0:
        db.execute(
            "UPDATE rush_days SET policy = ?, updated_at = datetime('now') "
            "WHERE rush_date = ?",
            (chosen, date),
        )
    else:
        locked = get_day(db, date)["policy"]
        if chosen != locked:
            message = (
                "落空处理口径当日已锁定为 "
                f"{POLICY_LABELS.get(locked, locked)}，禁止改口"
            )
            _record_attempt(db, job_id, date, actor, short_code, False, message)
            return False, message
    db.execute(
        """
        INSERT INTO rush_flags
            (job_id, short_code, reason, designated_by, rush_date,
             status, fallback_policy)
        VALUES (?, ?, ?, ?, ?, 'active', ?)
        """,
        (job_id, short_code, reason, actor or "", date, chosen),
    )
    _record_attempt(db, job_id, date, actor, short_code, True, "急件标记已生效")
    return True, ""


def designate_rush(
    job_id: int,
    short_code: str,
    reason: str,
    actor: str,
    policy: str | None = None,
) -> None:
    """给已存在的任务补指定急件标记（名额争用串行化）。"""
    db = get_db()
    if db.execute(
        "SELECT 1 FROM crawl_jobs WHERE id = ?", (job_id,)
    ).fetchone() is None:
        raise RushRuleError("任务不存在")
    date = today_str()
    failure = ""
    with immediate_transaction() as tx:
        ok, message = designate_in_tx(
            tx, job_id, short_code, reason, actor, policy, date
        )
        if not ok:
            failure = message
    if failure:
        raise RushRuleError(failure)


def create_rush_job_in_tx(
    db,
    name: str,
    seed_path: str,
    short_code: str,
    reason: str,
    actor: str,
    policy: str | None = None,
    date: str | None = None,
) -> tuple[int | None, str]:
    """新建任务并同时指定急件标记；名额争用下只有一个成功。"""
    date = date or today_str()
    short_code, reason = _validate_code_reason(short_code, reason)
    if not name:
        return None, "请填写任务名"
    # 先占名额（失败时不会留下半截任务），再建任务。
    settings = get_rush_settings(db)
    chosen = (policy or settings["policy"]).strip()
    if chosen not in POLICY_LABELS:
        return None, "落空处理口径只能是 冻结 或 降级"
    ensure_day(db, date)
    quota, used = _quota_status(db, date, settings)
    if used >= quota:
        message = _quota_full_message(date, quota, used)
        _record_attempt(db, None, date, actor, short_code, False, message)
        return None, message
    cur = db.execute(
        "INSERT INTO crawl_jobs (name, seed_path, status, rush_channel) "
        "VALUES (?, ?, 'draft', 'rush')",
        (name, seed_path),
    )
    job_id = cur.lastrowid
    ok, message = designate_in_tx(
        db, job_id, short_code, reason, actor, chosen, date
    )
    if not ok:
        db.execute("DELETE FROM crawl_jobs WHERE id = ?", (job_id,))
        return None, message
    return job_id, ""


def create_rush_job(
    name: str,
    seed_path: str,
    short_code: str,
    reason: str,
    actor: str,
    policy: str | None = None,
) -> int:
    db = get_db()
    with immediate_transaction() as tx:
        job_id, failure = create_rush_job_in_tx(
            tx, name, seed_path, short_code, reason, actor, policy, today_str()
        )
        if job_id is None:
            raise RushRuleError(failure)
    return job_id


def update_rush(
    job_id: int, short_code: str, reason: str, actor: str, policy: str | None = None
) -> None:
    db = get_db()
    date = today_str()
    short_code, reason = _validate_code_reason(short_code, reason)
    with immediate_transaction() as tx:
        flag = active_flag(tx, job_id, date)
        if flag is None:
            raise RushRuleError("该任务当日没有生效中的急件标记")
        if is_day_unsettled(tx, date):
            raise RushRuleError("当日开跑未收束，暂不能修改急件标记")
        if policy and policy.strip() != flag["fallback_policy"]:
            raise RushRuleError("落空处理口径当日已选定，禁止改口")
        tx.execute(
            "UPDATE rush_flags SET short_code = ?, reason = ? WHERE id = ?",
            (short_code, reason, flag["id"]),
        )


def withdraw_rush(job_id: int, actor: str, reason: str) -> None:
    reason = (reason or "").strip()
    if not reason:
        raise RushRuleError("撤下急件标记必须写明原因")
    db = get_db()
    date = today_str()
    with immediate_transaction() as tx:
        flag = active_flag(tx, job_id, date)
        if flag is None:
            raise RushRuleError("该任务当日没有生效中的急件标记")
        if actor and actor == flag["designated_by"]:
            raise RushRuleError("不能由指定人本人撤下急件标记")
        if is_day_unsettled(tx, date):
            raise RushRuleError("当日开跑未收束，暂不能撤下急件标记")
        tx.execute(
            """
            UPDATE rush_flags
            SET status = 'withdrawn', withdrawn_by = ?, withdraw_reason = ?,
                withdrawn_at = datetime('now')
            WHERE id = ?
            """,
            (actor or "", reason, flag["id"]),
        )
        # 冻结“直到撤下急件标记”：撤下即解冻候着区，可按常件继续。
        day = get_day(tx, date)
        if day is not None and day["frozen"]:
            tx.execute(
                "UPDATE rush_days SET frozen = 0, updated_at = datetime('now') "
                "WHERE rush_date = ?",
                (date,),
            )
        tx.execute(
            """
            UPDATE rush_runs SET frozen = 0, updated_at = datetime('now')
            WHERE rush_date = ? AND frozen = 1
            """,
            (date,),
        )


def day_stats(db, date: str | None = None) -> dict:
    date = date or today_str()
    settings = get_rush_settings(db)
    day = get_day(db, date)
    used = db.execute(
        "SELECT COUNT(*) AS c FROM rush_flags "
        "WHERE rush_date = ? AND status = 'active'",
        (date,),
    ).fetchone()["c"]
    waiting = len(_pending_cards(db, date))
    policy = day["policy"] if day is not None else settings["policy"]
    return {
        "date": date,
        "quota": settings["quota"],
        "used": used,
        "remaining": max(0, settings["quota"] - used),
        "waiting": waiting,
        "policy": policy,
        "policy_label": POLICY_LABELS.get(policy, policy),
        "settings_policy": settings["policy"],
        "settings_policy_label": settings["policy_label"],
        "yield_count": settings["yield_count"],
        "frozen": bool(day["frozen"]) if day is not None else False,
        "degraded": bool(day["degraded"]) if day is not None else False,
        "unsettled": is_day_unsettled(db, date),
        "mismatch": day["handling_mismatch"] if day is not None else None,
    }


def _insert_record(tx, job_id, item_key, title, payload, channel, degrade) -> bool:
    try:
        tx.execute(
            """
            INSERT INTO crawl_records
                (job_id, item_key, title, payload_json, channel, degrade_flag)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                item_key,
                title,
                json.dumps(payload, ensure_ascii=False),
                channel,
                degrade,
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def _enqueue_waitlist(tx, job, date, items, start_seq) -> int:
    """把条目排进候着区；同任务同日的重复键只保留一张卡片，返回实际条数。"""
    seq = start_seq
    queued = 0
    for it in items:
        seq += 1
        cur = tx.execute(
            """
            INSERT OR IGNORE INTO waitlist_cards
                (job_id, rush_date, seq, title, item_key, payload_json, status)
            VALUES (?, ?, ?, ?, ?, ?, 'waiting')
            """,
            (
                job["id"],
                date,
                seq,
                it["title"],
                it["item_key"],
                json.dumps(it, ensure_ascii=False),
            ),
        )
        if cur.rowcount:
            queued += 1
    return queued


def _fallback_policy_for(db, job, date) -> str:
    flag = active_flag(db, job["id"], date)
    if flag is not None and flag["fallback_policy"] in POLICY_LABELS:
        return flag["fallback_policy"]
    day = get_day(db, date)
    if day is not None:
        return day["policy"]
    return get_rush_settings(db)["policy"]


def start_run(tx, job, items, date: str | None = None) -> dict:
    """把开跑结果写入当前事务。

    * 急件通道：优先入账，全部写 channel='rush'；一条都没入账即落空。
      落空按锁定的口径冻结候着区（freeze）或记录降级待继续（degrade）。
    * 常件通道：先收 ``yield_count`` 条按常件入账，其余进候着区排队。
    """
    date = date or today_str()
    ensure_day(tx, date)
    settings = get_rush_settings(tx)
    flag = active_flag(tx, job["id"], date)
    inserted = skipped_dup = 0
    result = {
        "channel": "normal",
        "inserted": 0,
        "skipped_dup": 0,
        "yielded": 0,
        "waitlisted": 0,
        "landed": True,
        "policy": settings["policy"],
        "frozen": False,
        "degraded": False,
    }

    if flag is not None:
        policy = _fallback_policy_for(tx, job, date)
        result["channel"] = "rush"
        result["policy"] = policy
        # 来源不可用也叫落空：一条都不入账，直接按口径处置。
        source_ok = not any(it.get("source_unavailable") for it in items)
        if source_ok:
            for it in items:
                key = it["item_key"]
                if _insert_record(tx, job["id"], key, it["title"], it, "rush", 0):
                    inserted += 1
                else:
                    skipped_dup += 1
        landed = inserted > 0
        if landed:
            tx.execute(
                """
                INSERT INTO rush_runs
                    (job_id, rush_date, channel, landed, fallback_policy,
                     degrade_flag, frozen, settled, yield_count, waitlist_count,
                     contention_result)
                VALUES (?, ?, 'rush', 1, ?, 0, 0, 1, ?, 0, '急件优先入账')
                ON CONFLICT(job_id, rush_date) DO UPDATE SET
                    channel='rush', landed=1,
                    fallback_policy=excluded.fallback_policy,
                    settled=1, contention_result=excluded.contention_result,
                    updated_at=datetime('now')
                """,
                (job["id"], date, policy, inserted),
            )
            tx.execute(
                "UPDATE crawl_jobs SET status = 'done', "
                "finished_at = datetime('now'), rush_channel = 'rush' "
                "WHERE id = ?",
                (job["id"],),
            )
            queued = 0
        else:
            frozen = 1 if policy == POLICY_FREEZE else 0
            if frozen:
                tx.execute(
                    "UPDATE rush_days SET frozen = 1, "
                    "updated_at = datetime('now') WHERE rush_date = ?",
                    (date,),
                )
            # 落空：来源应收条目全部进候着区等待口径处置。
            queued = _enqueue_waitlist(tx, job, date, items, 0)
            tx.execute(
                """
                INSERT INTO rush_runs
                    (job_id, rush_date, channel, landed, fallback_policy,
                     degrade_flag, frozen, settled, yield_count,
                     waitlist_count, contention_result)
                VALUES (?, ?, 'rush', 0, ?, 0, ?, 0, 0, ?, ?)
                ON CONFLICT(job_id, rush_date) DO UPDATE SET
                    channel='rush', landed=0,
                    fallback_policy=excluded.fallback_policy,
                    frozen=excluded.frozen,
                    waitlist_count=excluded.waitlist_count,
                    settled=0, updated_at=datetime('now')
                """,
                (job["id"], date, policy, frozen, queued, f"急件落空（{policy}）"),
            )
            tx.execute(
                "UPDATE crawl_jobs SET status = 'running', rush_channel = 'rush' "
                "WHERE id = ?",
                (job["id"],),
            )
        result.update(
            inserted=inserted,
            skipped_dup=skipped_dup,
            landed=landed,
            yielded=0,
            waitlisted=queued,
            frozen=not landed and policy == POLICY_FREEZE,
            degraded=not landed and policy == POLICY_DEGRADE,
            settled=landed,
        )
        return result

    # 常件通道
    yield_count = settings["yield_count"]
    to_yield = items[:yield_count]
    to_wait = items[yield_count:]
    for it in to_yield:
        if _insert_record(tx, job["id"], it["item_key"], it["title"], it, "normal", 0):
            inserted += 1
        else:
            skipped_dup += 1
    seq = _enqueue_waitlist(tx, job, date, to_wait, 0)
    tx.execute(
        """
        INSERT INTO rush_runs
            (job_id, rush_date, channel, landed, fallback_policy,
             degrade_flag, frozen, settled, yield_count, waitlist_count,
             contention_result)
        VALUES (?, ?, 'normal', 1, NULL, 0, 0, ?, ?, ?, ?)
        ON CONFLICT(job_id, rush_date) DO UPDATE SET
            channel='normal', settled=excluded.settled,
            yield_count=excluded.yield_count,
            waitlist_count=excluded.waitlist_count,
            contention_result=excluded.contention_result,
            updated_at=datetime('now')
        """,
        (
            job["id"],
            date,
            0 if seq else 1,
            inserted,
            seq,
            "常件先收 %s 条，其余 %s 条入候着区" % (len(to_yield), seq),
        ),
    )
    settled = seq == 0
    tx.execute(
        "UPDATE crawl_jobs SET status = ?, finished_at = ?, rush_channel = 'normal' "
        "WHERE id = ?",
        (
            "done" if settled else "running",
            datetime.now().strftime("%Y-%m-%d %H:%M:%S") if settled else None,
            job["id"],
        ),
    )
    result.update(
        inserted=inserted,
        skipped_dup=skipped_dup,
        yielded=len(to_yield),
        waitlisted=seq,
        landed=True,
        settled=settled,
    )
    return result


def continue_day(actor: str = "") -> dict:
    """继续候着区：先核对落空处理口径，再按口径旁路/常件入账。"""
    db = get_db()
    date = today_str()
    blocked = ""
    result = {"processed": 0, "inserted": 0, "skipped_dup": 0, "degraded": False}
    with immediate_transaction() as tx:
        day = ensure_day(tx, date)
        pending = _pending_cards(tx, date)
        fell = tx.execute(
            """
            SELECT * FROM rush_runs
            WHERE rush_date = ? AND channel = 'rush' AND landed = 0
            ORDER BY id DESC LIMIT 1
            """,
            (date,),
        ).fetchone()
        registered = get_rush_settings(tx)["policy"]
        if fell is not None and fell["fallback_policy"] != registered:
            # 先核对口径：对不上就挡下，不因冻结而漏报“处理不对”。
            mismatch = (
                "处理不对：落空处理口径为 "
                f"{POLICY_LABELS.get(fell['fallback_policy'], fell['fallback_policy'])}，"
                "与设置页登记值 "
                f"{POLICY_LABELS.get(registered, registered)} 不一致"
            )
            tx.execute(
                "UPDATE rush_days SET handling_mismatch = ?, "
                "updated_at = datetime('now') WHERE rush_date = ?",
                (mismatch, date),
            )
            blocked = mismatch
        elif day["frozen"]:
            blocked = "候着区已冻结：须先撤下急件标记后才能继续"
        elif not pending:
            blocked = "当前没有候着中的条目，无需继续"
        else:
            degrade = 0
            if fell is not None and fell["fallback_policy"] == POLICY_DEGRADE:
                degrade = 1
                tx.execute(
                    "UPDATE rush_days SET degraded = 1, updated_at = datetime('now') "
                    "WHERE rush_date = ?",
                    (date,),
                )
            jobs_touched = set()
            for card in pending:
                payload = json.loads(card["payload_json"] or "{}")
                ok = _insert_record(
                    tx,
                    card["job_id"],
                    card["item_key"],
                    card["title"],
                    payload,
                    "normal",
                    degrade,
                )
                tx.execute(
                    "UPDATE waitlist_cards SET status = 'continued' WHERE id = ?",
                    (card["id"],),
                )
                jobs_touched.add(card["job_id"])
                result["processed"] += 1
                result["inserted"] += 1 if ok else 0
                result["skipped_dup"] += 0 if ok else 1
            result["degraded"] = bool(degrade)
            for job_id in jobs_touched:
                tx.execute(
                    "UPDATE crawl_jobs SET status = 'done', finished_at = "
                    "COALESCE(finished_at, datetime('now')) WHERE id = ?",
                    (job_id,),
                )
                tx.execute(
                    """
                    UPDATE rush_runs
                    SET settled = 1, degrade_flag = MAX(degrade_flag, ?),
                        updated_at = datetime('now')
                    WHERE job_id = ? AND rush_date = ?
                    """,
                    (degrade, job_id, date),
                )
            # 落空且来源为空、没有候着卡片的急件任务：按口径收束（degrade 打降级）。
            for run in tx.execute(
                """
                SELECT * FROM rush_runs
                WHERE rush_date = ? AND channel = 'rush' AND landed = 0
                  AND settled = 0
                """,
                (date,),
            ).fetchall():
                job_id = run["job_id"]
                still_waiting = tx.execute(
                    """
                    SELECT 1 FROM waitlist_cards
                    WHERE job_id = ? AND rush_date = ? AND status = 'waiting'
                    """,
                    (job_id, date),
                ).fetchone()
                if still_waiting is not None:
                    continue
                fell_degrade = 1 if run["fallback_policy"] == POLICY_DEGRADE else 0
                tx.execute(
                    """
                    UPDATE rush_runs
                    SET settled = 1, degrade_flag = MAX(degrade_flag, ?),
                        updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (fell_degrade, run["id"]),
                )
                tx.execute(
                    "UPDATE crawl_jobs SET status = 'done', finished_at = "
                    "COALESCE(finished_at, datetime('now')) WHERE id = ?",
                    (job_id,),
                )
        tx.execute(
            "UPDATE rush_days SET updated_at = datetime('now') WHERE rush_date = ?",
            (date,),
        )
    if blocked:
        raise RushRuleError(blocked)
    return result


def job_waitlist(db, job_id: int, date: str | None = None):
    date = date or today_str()
    return db.execute(
        """
        SELECT * FROM waitlist_cards
        WHERE job_id = ? AND rush_date = ?
        ORDER BY seq, id
        """,
        (job_id, date),
    ).fetchall()


def job_rush_context(db, job) -> dict:
    """详情页所需：急标记、降级标记、候着顺序、争用失败原因。"""
    date = today_str()
    flag = db.execute(
        """
        SELECT * FROM rush_flags WHERE job_id = ? ORDER BY id DESC LIMIT 1
        """,
        (job["id"],),
    ).fetchone()
    run = db.execute(
        "SELECT * FROM rush_runs WHERE job_id = ? AND rush_date = ?",
        (job["id"], date),
    ).fetchone()
    cards = job_waitlist(db, job["id"], date)
    attempts = db.execute(
        "SELECT * FROM rush_attempts WHERE job_id = ? ORDER BY id DESC LIMIT 8",
        (job["id"],),
    ).fetchall()
    failure = next((a for a in attempts if not a["ok"]), None)
    flag_today = active_flag(db, job["id"], date)
    channel = None
    if flag_today is not None:
        channel = "rush"
    elif run is not None:
        channel = run["channel"]
    degraded = bool(run is not None and run["degrade_flag"])
    day = get_day(db, date)
    return {
        "date": date,
        "flag": flag,
        "flag_active_today": flag_today is not None,
        "run": run,
        "waitlist": cards,
        "attempts": attempts,
        "failure_message": failure["message"] if failure else None,
        "channel": channel,
        "degraded": degraded,
        "frozen": bool(day is not None and day["frozen"]),
    }
