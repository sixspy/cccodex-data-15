from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from app.db import get_db
from app.routes.auth import current_user, login_required
from app.services.mock_catalog import CATALOGS
from app.services.rush import (
    POLICY_LABELS,
    RushRuleError,
    active_flag,
    continue_day,
    create_rush_job,
    day_stats,
    designate_rush,
    get_rush_settings,
    job_rush_context,
    save_rush_settings,
    today_str,
    update_rush,
    withdraw_rush,
)
from app.services.runner import run_job

bp = Blueprint("jobs", __name__)


def _list_context(db) -> dict:
    jobs = db.execute("SELECT * FROM crawl_jobs ORDER BY id DESC").fetchall()
    counts = {
        row["job_id"]: row["c"]
        for row in db.execute(
            "SELECT job_id, COUNT(*) AS c FROM crawl_records GROUP BY job_id"
        )
    }
    date = today_str()
    rush_flags = {
        row["job_id"]: row
        for row in db.execute(
            """
            SELECT f.* FROM rush_flags f
            WHERE f.status = 'active' AND f.rush_date = ?
            """,
            (date,),
        )
    }
    runs = {
        row["job_id"]: row
        for row in db.execute(
            "SELECT * FROM rush_runs WHERE rush_date = ?", (date,)
        )
    }
    waitlists = {}
    for row in db.execute(
        """
        SELECT * FROM waitlist_cards
        WHERE rush_date = ? ORDER BY seq, id
        """,
        (date,),
    ):
        waitlists.setdefault(row["job_id"], []).append(row)
    return {
        "jobs": jobs,
        "counts": counts,
        "seeds": sorted(CATALOGS.keys()),
        "stats": day_stats(db, date),
        "rush_flags": rush_flags,
        "runs": runs,
        "waitlists": waitlists,
        "policies": POLICY_LABELS,
    }


@bp.route("/")
@login_required
def list_jobs():
    return render_template("jobs_list.html", **_list_context(get_db()))


@bp.get("/settings")
@login_required
def settings_view():
    db = get_db()
    return render_template(
        "settings.html",
        rush=get_rush_settings(db),
        stats=day_stats(db),
        policies=POLICY_LABELS,
        user=current_user(),
    )


@bp.post("/settings")
@login_required
def save_settings():
    try:
        save_rush_settings(
            request.form.get("rush_quota"),
            request.form.get("rush_yield"),
            request.form.get("rush_policy"),
        )
        flash("急件设置已更新", "ok")
    except RushRuleError as e:
        flash(str(e), "error")
    return redirect(url_for("jobs.settings_view"))


@bp.post("/jobs")
@login_required
def create_job():
    name = request.form.get("name", "").strip()
    seed = request.form.get("seed_path", "").strip()
    is_rush = request.form.get("rush") == "1"
    short_code = request.form.get("rush_short_code", "")
    reason = request.form.get("rush_reason", "")
    policy = request.form.get("rush_policy", "")

    if not name or not seed:
        flash("请填写任务名与种子路径", "error")
        return redirect(url_for("jobs.list_jobs"))
    if seed not in CATALOGS:
        flash("种子路径必须是已注册的 mock 目录", "error")
        return redirect(url_for("jobs.list_jobs"))

    db = get_db()
    if is_rush:
        try:
            create_rush_job(
                name, seed, short_code, reason, current_user(), policy
            )
            flash("任务已创建，急件标记已生效", "ok")
        except RushRuleError as e:
            flash(str(e), "error")
        return redirect(url_for("jobs.list_jobs"))

    db.execute(
        "INSERT INTO crawl_jobs (name, seed_path, status, rush_channel) "
        "VALUES (?, ?, 'draft', 'normal')",
        (name, seed),
    )
    db.commit()
    flash("任务已创建", "ok")
    return redirect(url_for("jobs.list_jobs"))


@bp.post("/jobs/<int:job_id>/run")
@login_required
def run_job_view(job_id: int):
    try:
        summary = run_job(job_id)
        if summary.get("channel") == "rush":
            if summary.get("landed"):
                flash(
                    f"急件优先入账：写入 {summary['inserted']} 条，"
                    f"去重跳过 {summary['skipped_dup']} 条",
                    "ok",
                )
            elif summary.get("frozen"):
                flash(
                    "急件落空：按所选口径冻结候着区，须先撤下急件标记后才能继续",
                    "error",
                )
            else:
                flash(
                    f"急件落空：候着区已改走旁路待继续（{summary['waitlisted']} 条，"
                    "继续后打降级标记）",
                    "error",
                )
        else:
            flash(
                f"运行完成：写入 {summary['inserted']} 条，"
                f"去重跳过 {summary['skipped_dup']} 条；"
                f"让路先收 {summary.get('yielded', 0)} 条、"
                f"候着 {summary.get('waitlisted', 0)} 条",
                "ok",
            )
    except RushRuleError as e:
        flash(str(e), "error")
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:
        flash(f"运行失败: {e}", "error")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.post("/continue")
@login_required
def continue_view():
    try:
        result = continue_day(current_user())
        note = f"候着区继续：收 {result['processed']} 条"
        if result.get("degraded"):
            note += "（降级标记）"
        flash(note, "ok")
    except RushRuleError as e:
        flash(str(e), "error")
    return redirect(url_for("jobs.list_jobs"))


@bp.post("/jobs/<int:job_id>/rush")
@login_required
def designate_view(job_id: int):
    try:
        designate_rush(
            job_id,
            request.form.get("rush_short_code", ""),
            request.form.get("rush_reason", ""),
            current_user(),
            request.form.get("rush_policy", ""),
        )
        flash("急件标记已生效", "ok")
    except RushRuleError as e:
        flash(str(e), "error")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.post("/jobs/<int:job_id>/rush/update")
@login_required
def update_rush_view(job_id: int):
    try:
        update_rush(
            job_id,
            request.form.get("rush_short_code", ""),
            request.form.get("rush_reason", ""),
            current_user(),
            request.form.get("rush_policy", ""),
        )
        flash("急件标记已更新", "ok")
    except RushRuleError as e:
        flash(str(e), "error")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.post("/jobs/<int:job_id>/rush/withdraw")
@login_required
def withdraw_view(job_id: int):
    try:
        withdraw_rush(
            job_id,
            current_user(),
            request.form.get("withdraw_reason", ""),
        )
        flash("急件标记已撤下", "ok")
    except RushRuleError as e:
        flash(str(e), "error")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.get("/jobs/<int:job_id>")
@login_required
def detail(job_id: int):
    db = get_db()
    job = db.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        flash("任务不存在", "error")
        return redirect(url_for("jobs.list_jobs"))
    records = db.execute(
        "SELECT * FROM crawl_records WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()
    rush_ctx = job_rush_context(db, job)
    return render_template(
        "job_detail.html",
        job=job,
        records=records,
        rush=rush_ctx,
        stats=day_stats(db, rush_ctx["date"]),
        policies=POLICY_LABELS,
        active_flag=active_flag(db, job_id, rush_ctx["date"]),
        user=current_user(),
    )
