import threading

import pytest

from app.db import get_db
from app.services.rush import (
    POLICY_DEGRADE,
    POLICY_FREEZE,
    RushRuleError,
    continue_day,
    create_rush_job,
    day_stats,
    save_rush_settings,
    update_rush,
    withdraw_rush,
)
from app.services.runner import run_job


def login(client, user="admin"):
    return client.post(
        "/login",
        data={"username": user, "password": "123456"},
        follow_redirects=True,
    )


def make_normal_job(seed="/mock/catalog/alpha", name="常件"):
    """基线建任务（不走急件），返回 job_id。"""
    db = get_db()
    cur = db.execute(
        "INSERT INTO crawl_jobs (name, seed_path, status, rush_channel) "
        "VALUES (?, ?, 'draft', 'normal')",
        (name, seed),
    )
    db.commit()
    return cur.lastrowid


def make_rush_job(seed, code="R-01", reason="客户当日截止", policy=POLICY_FREEZE, actor="admin"):
    return create_rush_job("急件", seed, code, reason, actor, policy)


def preload_duplicate_records(job_id, seed="/mock/catalog/alpha"):
    """预先写入与来源同键的记录，使开跑时全部去重跳过 → 急件落空但候着区有条目。"""
    from app.services.dedup import normalize_item_key
    from app.services.mock_catalog import CATALOGS

    db = get_db()
    seen = set()
    queued = 0
    for it in CATALOGS[seed]:
        key = normalize_item_key(seed, it["title"])
        if key in seen:
            continue  # alpha 里有一条重复标题，候着区按任务内去重只排一张卡
        seen.add(key)
        db.execute(
            """
            INSERT INTO crawl_records (job_id, item_key, title, payload_json)
            VALUES (?, ?, ?, '{}')
            """,
            (job_id, key, it["title"]),
        )
        queued += 1
    db.commit()
    return queued

# 段 1：名额用尽后新的急件标记失败
def test_quota_exhausted_rejects_new_rush_job(app, client):
    login(client)
    with app.app_context():
        save_rush_settings(2, 10, POLICY_FREEZE)
        create_rush_job("急1", "/mock/catalog/beta", "R-01", "截止", "admin")
        create_rush_job("急2", "/mock/catalog/beta", "R-02", "截止", "admin")
    response = client.post(
        "/jobs",
        data={
            "name": "急3",
            "seed_path": "/mock/catalog/beta",
            "rush": "1",
            "rush_short_code": "R-03",
            "rush_reason": "截止",
            "rush_policy": POLICY_FREEZE,
        },
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)
    assert "名额已满" in body
    with app.app_context():
        db = get_db()
        assert db.execute("SELECT COUNT(*) AS c FROM crawl_jobs").fetchone()["c"] == 2
        db.execute(
            "INSERT INTO crawl_jobs (name, seed_path, status, rush_channel) "
            "VALUES ('普通', '/mock/catalog/beta', 'draft', 'normal')"
        )
        db.commit()
        job_id = db.execute("SELECT id FROM crawl_jobs WHERE name='普通'").fetchone()["id"]
    # 已存在任务补指定也失败
    response = client.post(
        f"/jobs/{job_id}/rush",
        data={
            "rush_short_code": "R-04",
            "rush_reason": "截止",
            "rush_policy": POLICY_FREEZE,
        },
        follow_redirects=True,
    )
    assert "名额已满" in response.get_data(as_text=True)


def test_empty_code_or_reason_rejected(app):
    with app.app_context():
        with pytest.raises(RushRuleError, match="编码不能为空"):
            create_rush_job("x", "/mock/catalog/beta", "  ", "有原因", "admin")
        with pytest.raises(RushRuleError, match="原因不能为空"):
            create_rush_job("x", "/mock/catalog/beta", "R-01", " ", "admin")
        assert get_db().execute("SELECT COUNT(*) AS c FROM crawl_jobs").fetchone()["c"] == 0


# 段 2：急件标记先于常件入账
def test_rush_lands_before_normal(app):
    with app.app_context():
        save_rush_settings(2, 1, POLICY_FREEZE)  # 常件先收 1 条，其余候着
        normal_id = make_normal_job("/mock/catalog/alpha")
        rush_id = make_rush_job("/mock/catalog/beta", "R-01")
        normal_summary = run_job(normal_id)
        rush_summary = run_job(rush_id)
        assert normal_summary["channel"] == "normal"
        assert normal_summary["yielded"] == 1
        assert normal_summary["waitlisted"] == 1
        assert normal_summary["status"] == "running"
        assert rush_summary["channel"] == "rush"
        assert rush_summary["landed"] is True
        assert rush_summary["inserted"] == 2
        db = get_db()
        channels = {
            row["channel"]
            for row in db.execute(
                "SELECT channel FROM crawl_records WHERE job_id = ?", (rush_id,)
            )
        }
        assert channels == {"rush"}
        # 常件仍在候着，急件已入账 → 急件先于常件
        assert day_stats(db)["waiting"] == 1
        assert db.execute(
            "SELECT status FROM crawl_jobs WHERE id = ?", (rush_id,)
        ).fetchone()["status"] == "done"


# 段 3：落空时按所选口径冻结或降级
def test_fallback_freeze_holds_until_withdraw(app):
    with app.app_context():
        save_rush_settings(2, 1, POLICY_FREEZE)
        rush_id = make_rush_job("/mock/catalog/alpha", "R-01", policy=POLICY_FREEZE)
        preload_duplicate_records(rush_id)
        summary = run_job(rush_id)
        assert summary["landed"] is False
        assert summary["frozen"] is True
        assert summary["status"] == "running"
        with pytest.raises(RushRuleError, match="冻结"):
            continue_day("supervisor")
        # 撤下必须写原因，且不能由指定人本人撤下
        with pytest.raises(RushRuleError, match="不能由指定人本人"):
            withdraw_rush(rush_id, "admin", "换口径")
        with pytest.raises(RushRuleError, match="必须写明原因"):
            withdraw_rush(rush_id, "supervisor", "  ")
        withdraw_rush(rush_id, "supervisor", "改走常件")
        # 冻结解除后可继续
        result = continue_day("supervisor")
        assert result["degraded"] is False
        assert result["processed"] == 2
        db = get_db()
        assert db.execute(
            "SELECT status FROM crawl_jobs WHERE id = ?", (rush_id,)
        ).fetchone()["status"] == "done"
        assert day_stats(db)["frozen"] is False


def test_fallback_degrade_continues_with_degrade_flag(app):
    with app.app_context():
        save_rush_settings(2, 1, POLICY_DEGRADE)
        rush_id = make_rush_job("/mock/catalog/flaky", "R-01", policy=POLICY_DEGRADE)
        summary = run_job(rush_id)
        assert summary["landed"] is False
        assert summary["frozen"] is False
        assert summary["degraded"] is True
        result = continue_day("supervisor")
        assert result["degraded"] is True
        assert result["processed"] == 2
        db = get_db()
        rows = db.execute(
            "SELECT channel, degrade_flag FROM crawl_records WHERE job_id = ?",
            (rush_id,),
        ).fetchall()
        assert len(rows) == 2
        assert all(r["degrade_flag"] == 1 for r in rows)
        assert all(r["channel"] == "normal" for r in rows)
        assert day_stats(db)["degraded"] is True


# 段 4：落空处理不一致会被挡下并写“处理不对”
def test_fallback_mismatch_blocked(app):
    with app.app_context():
        save_rush_settings(2, 1, POLICY_DEGRADE)
        rush_id = make_rush_job("/mock/catalog/alpha", "R-01", policy=POLICY_FREEZE)
        preload_duplicate_records(rush_id)
        run_job(rush_id)
        # 设置页登记值改为降级，与当日锁定的冻结口径不一致
        save_rush_settings(2, 1, POLICY_DEGRADE)
        with pytest.raises(RushRuleError, match="处理不对"):
            continue_day("supervisor")
        db = get_db()
        assert "处理不对" in (day_stats(db)["mismatch"] or "")
        # 未处理，候着仍在
        assert day_stats(db)["waiting"] == 2


# 段 5：并发争最后一个名额只有一个成功
def test_concurrent_last_quota_single_winner(app):
    with app.app_context():
        save_rush_settings(1, 10, POLICY_FREEZE)
    results = []

    def attempt(code):
        with app.app_context():
            try:
                create_rush_job("并发", "/mock/catalog/beta", code, "截止", "admin")
                results.append(("ok", code))
            except RushRuleError as e:
                results.append(("fail", str(e)))

    threads = [
        threading.Thread(target=attempt, args=("R-A",)),
        threading.Thread(target=attempt, args=("R-B",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    oks = [r for r in results if r[0] == "ok"]
    fails = [r for r in results if r[0] == "fail"]
    assert len(oks) == 1, results
    assert len(fails) == 1 and "名额已满" in fails[0][1]
    with app.app_context():
        db = get_db()
        active = db.execute(
            "SELECT COUNT(*) AS c FROM rush_flags WHERE status='active'"
        ).fetchone()["c"]
        assert active == 1
        failed_jobs = db.execute(
            "SELECT COUNT(*) AS c FROM crawl_jobs"
        ).fetchone()["c"]
        assert failed_jobs == 1  # 失败方不留半截任务


# 段 6：未收束时改急件 / 撤下都会被拒
def test_unsettled_blocks_update_and_withdraw(app):
    with app.app_context():
        save_rush_settings(2, 1, POLICY_FREEZE)
        normal_id = make_normal_job("/mock/catalog/alpha")
        rush_id = make_rush_job("/mock/catalog/alpha", "R-01")
        run_job(normal_id)  # 常件留 2 条候着 → 未收束
        assert day_stats(get_db())["unsettled"] is True
        with pytest.raises(RushRuleError, match="未收束"):
            update_rush(rush_id, "R-02", "新原因", "admin")
        with pytest.raises(RushRuleError, match="未收束"):
            withdraw_rush(rush_id, "supervisor", "撤下")
        # 收束后可以改
        continue_day("admin")
        update_rush(rush_id, "R-02", "新原因", "admin")
        db = get_db()
        assert db.execute(
            "SELECT short_code FROM rush_flags WHERE job_id=?", (rush_id,)
        ).fetchone()["short_code"] == "R-02"


def test_policy_locked_on_designation_day(app):
    with app.app_context():
        rush_id = make_rush_job("/mock/catalog/beta", "R-01", policy=POLICY_FREEZE)
        with pytest.raises(RushRuleError, match="禁止改口"):
            update_rush(rush_id, "R-01", "原因", "admin", POLICY_DEGRADE)


# 段 7：列表卡片显示急件通道、候着顺序、名额余量
def test_list_card_shows_rush_channel_waitlist_and_quota(app, client):
    login(client)
    with app.app_context():
        save_rush_settings(2, 1, POLICY_FREEZE)
        normal_id = make_normal_job("/mock/catalog/alpha")
        rush_id = make_rush_job("/mock/catalog/beta", "R-01")
        run_job(normal_id)
    body = client.get("/").get_data(as_text=True)
    assert "急件通道" in body
    assert "候着顺序" in body
    assert "名额余量" in body
    assert "今日已用" in body
    assert "当前口径" in body
    assert "R-01" in body
    assert "#1" in body and "排水井巡查台账" in body

    detail = client.get(f"/jobs/{rush_id}").get_data(as_text=True)
    assert "急件标记" in detail
    assert "降级标记" in detail
    assert "候着顺序" in detail
    assert "争用失败原因" in detail


def test_detail_shows_contention_failure(app, client):
    login(client)
    with app.app_context():
        save_rush_settings(1, 10, POLICY_FREEZE)
        create_rush_job("急1", "/mock/catalog/beta", "R-01", "截止", "admin")
    response = client.post(
        "/jobs",
        data={
            "name": "急2",
            "seed_path": "/mock/catalog/beta",
            "rush": "1",
            "rush_short_code": "R-02",
            "rush_reason": "截止",
            "rush_policy": POLICY_FREEZE,
        },
        follow_redirects=True,
    )
    assert "名额已满" in response.get_data(as_text=True)
