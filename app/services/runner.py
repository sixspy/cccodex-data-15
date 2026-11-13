from flask import current_app
from app.db import db_transaction, get_db
from app.services.dedup import normalize_item_key
from app.services.mock_catalog import (
    CATALOGS,
    parse_catalog_html,
    render_catalog_page,
)
from app.services.rush import RushRuleError, start_run, today_str


def _load_items(seed: str) -> list[dict]:
    """抓取并解析来源；来源不可用时回退到登记清单，便于候着区有据可排。"""
    html = render_catalog_page(seed)
    if isinstance(html, tuple):
        # 来源不可用：按站内登记的应收清单生成条目，开跑时必然全部落空。
        return [
            {
                "sku": it["sku"],
                "title": it["title"],
                "seed_path": seed,
                "item_key": normalize_item_key(seed, it["title"]),
                "source_unavailable": True,
            }
            for it in CATALOGS.get(seed, [])
        ]
    items = parse_catalog_html(html, seed)
    for it in items:
        it["item_key"] = normalize_item_key(seed, it["title"])
    return items


def run_job(job_id: int) -> dict:
    db = get_db()
    job = db.execute(
        "SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if job is None:
        raise ValueError("job not found")
    if job["status"] == "running":
        raise ValueError("job already running")
    if job["status"] == "done":
        raise ValueError("job already done")

    seed = job["seed_path"]
    if seed not in CATALOGS:
        with db_transaction() as tx:
            tx.execute(
                "UPDATE crawl_jobs SET status = 'failed', finished_at = datetime('now') "
                "WHERE id = ?",
                (job_id,),
            )
        return {
            "status": "failed",
            "reason": "unknown seed",
            "inserted": 0,
            "skipped_dup": 0,
            "channel": "unknown",
        }

    items = _load_items(seed)
    date = today_str()

    with db_transaction() as tx:
        job = tx.execute(
            "SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if job["status"] == "running":
            raise RushRuleError("job already running（开跑未收束）")
        if job["status"] == "done":
            raise RushRuleError("job already done")
        tx.execute(
            "UPDATE crawl_jobs SET status = 'running', finished_at = NULL WHERE id = ?",
            (job_id,),
        )
        summary = start_run(tx, job, items, date)

    current_app.logger.info(
        "job %s finished channel=%s inserted=%s skipped_dup=%s waitlisted=%s",
        job_id,
        summary["channel"],
        summary["inserted"],
        summary["skipped_dup"],
        summary.get("waitlisted", 0),
    )
    return {
        "status": "done" if summary.get("settled", True) else "running",
        "inserted": summary["inserted"],
        "skipped_dup": summary["skipped_dup"],
        "total_source": len(items),
        "channel": summary["channel"],
        "yielded": summary.get("yielded", 0),
        "waitlisted": summary.get("waitlisted", 0),
        "landed": summary.get("landed", True),
        "policy": summary.get("policy"),
        "frozen": summary.get("frozen", False),
        "degraded": summary.get("degraded", False),
    }
