"""运行期配置，全部可通过环境变量覆盖（详见 README）。"""
import os


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


DB_PATH = os.environ.get("DB_PATH", "/data/redact.db")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-secret-change-me")
SESSION_TTL_DAYS = _int("SESSION_TTL_DAYS", 7)
PORT = _int("PORT", 8000)

# 首次启动且数据库中没有任何用户时创建的默认账号。
SEED_AUTHOR_USER = os.environ.get("SEED_AUTHOR_USER", "author")
SEED_AUTHOR_PASS = os.environ.get("SEED_AUTHOR_PASS", "author123")
SEED_REVIEWER1_USER = os.environ.get("SEED_REVIEWER1_USER", "reviewer1")
SEED_REVIEWER1_PASS = os.environ.get("SEED_REVIEWER1_PASS", "review123")
SEED_REVIEWER2_USER = os.environ.get("SEED_REVIEWER2_USER", "reviewer2")
SEED_REVIEWER2_PASS = os.environ.get("SEED_REVIEWER2_PASS", "review123")

MAX_TITLE_LEN = 200
MAX_CONTENT_LEN = 1_000_000
