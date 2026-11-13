import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-change-me-in-production")
    DATABASE = os.environ.get(
        "DATABASE_PATH",
        str(BASE_DIR / "instance" / "crawl_lab.db"),
    )
    ADMIN_USER = "admin"
    ADMIN_PASSWORD = "123456"
    # 站内账号：急件指定人与撤下人必须是不同账号（“不能由指定人本人撤下”）。
    USERS = {
        "admin": "123456",
        "supervisor": "123456",
    }
    # 后台日期（急件名额按日结算）；测试可通过 config TODAY_OVERRIDE 钉死。
    TODAY_OVERRIDE = os.environ.get("BACKEND_DATE") or None
    RUSH_QUOTA_DEFAULT = 2
    RUSH_YIELD_DEFAULT = 10
    RUSH_POLICY_DEFAULT = "freeze"
