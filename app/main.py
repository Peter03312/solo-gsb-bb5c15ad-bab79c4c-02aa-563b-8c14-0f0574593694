"""脱敏文稿发布工作台 — FastAPI 入口。

并发模型：
- versions.state_rev 是版本裁决状态的乐观锁；任何裁决/撤回/新提议都使其 +1，
  客户端必须携带看到页面时的 state_rev，不一致返回 409 提示刷新。
- 所有写操作在 BEGIN IMMEDIATE 事务中完成；发布在同一事务内重新核验
  HEAD、未决提议、两名非作者审阅人对同一预览哈希的确认，保证原子一致。
"""
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
import sqlite3

from . import config, database, security
from .redaction import apply_mask, preview_hash

app = FastAPI(title="脱敏文稿发布工作台")

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "session"


class APIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message


@app.exception_handler(APIError)
async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.message})


@app.on_event("startup")
def startup() -> None:
    database.init_db()


# ---------- 依赖 ----------

def get_db():
    conn = database.connect()
    try:
        yield conn
    finally:
        conn.close()


def current_user(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> sqlite3.Row:
    payload = security.parse_session_token(request.cookies.get(SESSION_COOKIE))
    if not payload:
        raise APIError(401, "未登录或会话已过期")
    row = conn.execute("SELECT * FROM users WHERE id=?", (payload["uid"],)).fetchone()
    if not row:
        raise APIError(401, "账号不存在，请重新登录")
    return row


def require_role(user: sqlite3.Row, role: str) -> None:
    if user["role"] != role:
        raise APIError(403, "需要" + ("作者" if role == "author" else "审阅人") + "身份")


# ---------- 请求模型 ----------

class LoginIn(BaseModel):
    username: str = Field(min_length=1, max=64)
    password: str = Field(min_length=1, max=256)


class DocumentIn(BaseModel):
    title: str = Field(min_length=1, max=200)
    content: str = Field(min_length=1, max=config.MAX_CONTENT_LEN)


class VersionIn(BaseModel):
    content: str = Field(min_length=1, max=config.MAX_CONTENT_LEN)


class ProposalIn(BaseModel):
    start_cp: int
    end_cp: int
    reason: str = Field(default="", max_length=500)
    expected_rev: int


class DecideIn(BaseModel):
    decision: str = Field(pattern="^(accepted|rejected)$")
    expected_rev: int


class RevIn(BaseModel):
    expected_rev: int


# ---------- 组装返回数据 ----------

def _proposal_flags(p: sqlite3.Row, user: sqlite3.Row, editable: bool) -> dict:
    active = p["status"] == "pending" and p["withdrawn_at"] is None and editable
    is_proposer = p["proposer_id"] == user["id"]
    return {
        "id": p["id"],
        "start_cp": p["start_cp"],
        "end_cp": p["end_cp"],
        "reason": p["reason"],
        "status": "withdrawn" if p["withdrawn_at"] else p["status"],
        "proposer_id": p["proposer_id"],
        "proposer_name": p["proposer_name"],
        "adjudicator_id": p["adjudicator_id"],
        "adjudicator_name": p["adjudicator_name"],
        "created_at": p["created_at"],
        "decided_at": p["decided_at"],
        "withdrawn_at": p["withdrawn_at"],
        "can_withdraw": active and is_proposer and user["role"] == "reviewer",
        # 只能由另一名审阅人裁决，不能裁决自己的提议
        "can_decide": active and not is_proposer and user["role"] == "reviewer",
    }


def build_version_detail(conn: sqlite3.Connection, vid: int, user: sqlite3.Row) -> dict:
    v = conn.execute(
        """SELECT v.*, d.title AS document_title, d.author_id AS document_author_id
           FROM versions v JOIN documents d ON d.id=v.document_id WHERE v.id=?""",
        (vid,),
    ).fetchone()
    if not v:
        raise APIError(404, "版本不存在")

    proposals = conn.execute(
        """SELECT p.*, pu.display_name AS proposer_name,
                  au.display_name AS adjudicator_name
           FROM proposals p
           JOIN users pu ON pu.id=p.proposer_id
           LEFT JOIN users au ON au.id=p.adjudicator_id
           WHERE p.version_id=? ORDER BY p.id""",
        (vid,),
    ).fetchall()

    accepted_ranges = [
        (p["start_cp"], p["end_cp"])
        for p in proposals
        if p["status"] == "accepted" and p["withdrawn_at"] is None
    ]
    pending = [
        p for p in proposals
        if p["status"] == "pending" and p["withdrawn_at"] is None
    ]
    masked = apply_mask(v["content"], accepted_ranges)
    sha = preview_hash(v["content"], accepted_ranges)

    confirmations = conn.execute(
        """SELECT c.id, c.reviewer_id, u.display_name AS reviewer_name,
                  c.preview_sha, c.created_at
           FROM confirmations c JOIN users u ON u.id=c.reviewer_id
           WHERE c.version_id=? ORDER BY c.id""",
        (vid,),
    ).fetchall()
    matching = [
        c for c in confirmations
        if c["preview_sha"] == sha
        and c["reviewer_id"] != v["document_author_id"]
    ]
    distinct_confirmers = len({c["reviewer_id"] for c in matching})
    is_head = conn.execute(
        "SELECT head_version_id FROM documents WHERE id=?", (v["document_id"],)
    ).fetchone()["head_version_id"] == vid
    published = v["published_at"] is not None

    return {
        "id": v["id"],
        "document_id": v["document_id"],
        "document_title": v["document_title"],
        "version_no": v["version_no"],
        "state_rev": v["state_rev"],
        "content": v["content"],
        "content_length": v["content_len"],
        "is_head": is_head,
        "published_at": v["published_at"],
        "created_at": v["created_at"],
        "proposals": [
            _proposal_flags(p, user, is_head and not published)
            for p in proposals
        ],
        "pending_count": len(pending),
        "masked_preview": masked,
        "preview_sha": sha,
        "confirmations": [
            {
                "reviewer_id": c["reviewer_id"],
                "reviewer_name": c["reviewer_name"],
                "matches_current": c["preview_sha"] == sha,
                "created_at": c["created_at"],
            }
            for c in confirmations
        ],
        "distinct_confirmers": distinct_confirmers,
        "my_confirmation": any(c["reviewer_id"] == user["id"] and c["preview_sha"] == sha for c in matching),
        "can_confirm": (
            user["role"] == "reviewer"
            and user["id"] != v["document_author_id"]
            and is_head and not published and len(pending) == 0
        ),
        "can_publish": (
            user["role"] == "author"
            and user["id"] == v["document_author_id"]
            and is_head and not published and len(pending) == 0
            and distinct_confirmers >= 2
        ),
    }


def _lock_version(conn: sqlite3.Connection, vid: int, expected_rev: int) -> sqlite3.Row:
    """在写事务中锁定版本并校验乐观锁。"""
    v = conn.execute(
        """SELECT v.*, d.author_id AS document_author_id, d.head_version_id
           FROM versions v JOIN documents d ON d.id=v.document_id
           WHERE v.id=?""",
        (vid,),
    ).fetchone()
    if not v:
        raise APIError(404, "版本不存在")
    if v["state_rev"] != expected_rev:
        raise APIError(409, "版本状态已变化（可能有其他人刚刚操作），请刷新页面后重试")
    return v


def _bump_and_invalidate(conn: sqlite3.Connection, vid: int) -> None:
    """裁决状态前进一版，并使该版本的全部确认失效（任何裁决变化都使确认失效）。"""
    conn.execute("UPDATE versions SET state_rev=state_rev+1 WHERE id=?", (vid,))
    conn.execute("DELETE FROM confirmations WHERE version_id=?", (vid,))


# ---------- 认证 ----------

@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/api/auth/login")
def login(body: LoginIn, conn: sqlite3.Connection = Depends(get_db)):
    user = conn.execute("SELECT * FROM users WHERE username=?", (body.username,)).fetchone()
    if not user or not security.verify_password(body.password, user["password_hash"]):
        raise APIError(401, "用户名或密码错误")
    token = security.create_session_token(user["id"], user["display_name"])
    resp = JSONResponse({
        "id": user["id"], "username": user["username"],
        "display_name": user["display_name"], "role": user["role"],
    })
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=config.SESSION_TTL_DAYS * 86400,
        httponly=True, samesite="lax", path="/",
    )
    return resp


@app.post("/api/auth/logout")
def logout() -> Response:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/api/auth/me")
def me(user: sqlite3.Row = Depends(current_user)) -> dict:
    return {"id": user["id"], "username": user["username"],
            "display_name": user["display_name"], "role": user["role"]}


# ---------- 文稿与版本 ----------

@app.get("/api/documents")
def list_documents(conn: sqlite3.Connection = Depends(get_db), user: sqlite3.Row = Depends(current_user)):
    rows = conn.execute(
        """SELECT d.id, d.title, d.created_at, d.head_version_id,
                  u.display_name AS author_name,
                  hv.version_no AS head_version_no,
                  hv.published_at AS head_published_at,
                  (SELECT COUNT(*) FROM versions vv WHERE vv.document_id=d.id) AS version_count
           FROM documents d
           JOIN users u ON u.id=d.author_id
           LEFT JOIN versions hv ON hv.id=d.head_version_id
           ORDER BY d.id DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/documents", status_code=201)
def create_document(body: DocumentIn, conn: sqlite3.Connection = Depends(get_db),
                    user: sqlite3.Row = Depends(current_user)):
    require_role(user, "author")
    title = body.title.strip()
    if not title:
        raise APIError(400, "标题不能为空")
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO documents (title, author_id) VALUES (?, ?)",
            (title, user["id"]),
        )
        doc_id = cur.lastrowid
        content = body.content
        cur = conn.execute(
            "INSERT INTO versions (document_id, version_no, content, content_len, created_by)"
            " VALUES (?, 1, ?, ?, ?)",
            (doc_id, content, len(content), user["id"]),
        )
        vid = cur.lastrowid
        conn.execute("UPDATE documents SET head_version_id=? WHERE id=?", (vid, doc_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"id": doc_id, "head_version_id": vid}


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: int, conn: sqlite3.Connection = Depends(get_db),
                 user: sqlite3.Row = Depends(current_user)):
    doc = conn.execute(
        """SELECT d.*, u.display_name AS author_name FROM documents d
           JOIN users u ON u.id=d.author_id WHERE d.id=?""",
        (doc_id,),
    ).fetchone()
    if not doc:
        raise APIError(404, "文稿不存在")
    versions = conn.execute(
        """SELECT v.id, v.version_no, v.state_rev, v.published_at, v.created_at,
                  v.content_len,
                  SUM(CASE WHEN p.status='pending' AND p.withdrawn_at IS NULL THEN 1 ELSE 0 END) AS pending_count,
                  SUM(CASE WHEN p.status='accepted' AND p.withdrawn_at IS NULL THEN 1 ELSE 0 END) AS accepted_count
           FROM versions v LEFT JOIN proposals p ON p.version_id=v.id
           WHERE v.document_id=?
           GROUP BY v.id ORDER BY v.version_no DESC""",
        (doc_id,),
    ).fetchall()
    return {
        "id": doc["id"],
        "title": doc["title"],
        "author_id": doc["author_id"],
        "author_name": doc["author_name"],
        "head_version_id": doc["head_version_id"],
        "created_at": doc["created_at"],
        "versions": [dict(v) for v in versions],
    }


@app.post("/api/documents/{doc_id}/versions", status_code=201)
def create_version(doc_id: int, body: VersionIn, conn: sqlite3.Connection = Depends(get_db),
                   user: sqlite3.Row = Depends(current_user)):
    """正文不可变：修改必须另建版本；旧版本及其批注原样保留，绝不沿用到新版本。"""
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        raise APIError(404, "文稿不存在")
    require_role(user, "author")
    if user["id"] != doc["author_id"]:
        raise APIError(403, "只有文稿作者可以创建新版本")
    conn.execute("BEGIN IMMEDIATE")
    try:
        last = conn.execute(
            "SELECT MAX(version_no) AS m FROM versions WHERE document_id=?", (doc_id,)
        ).fetchone()
        next_no = (last["m"] or 0) + 1
        content = body.content
        cur = conn.execute(
            "INSERT INTO versions (document_id, version_no, content, content_len, created_by)"
            " VALUES (?, ?, ?, ?, ?)",
            (doc_id, next_no, content, len(content), user["id"]),
        )
        vid = cur.lastrowid
        conn.execute("UPDATE documents SET head_version_id=? WHERE id=?", (vid, doc_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"id": vid, "version_no": next_no}


@app.get("/api/versions/{vid}")
def get_version(vid: int, conn: sqlite3.Connection = Depends(get_db),
                user: sqlite3.Row = Depends(current_user)):
    return build_version_detail(conn, vid, user)


# ---------- 遮盖提议与裁决 ----------

@app.post("/api/versions/{vid}/proposals", status_code=201)
def create_proposal(vid: int, body: ProposalIn, conn: sqlite3.Connection = Depends(get_db),
                    user: sqlite3.Row = Depends(current_user)):
    require_role(user, "reviewer")
    if body.start_cp >= body.end_cp:
        raise APIError(400, "非法选区：起点必须在终点之前（不能为空选区）")
    conn.execute("BEGIN IMMEDIATE")
    try:
        v = _lock_version(conn, vid, body.expected_rev)
        if v["head_version_id"] != vid:
            raise APIError(409, "这不是当前版本，批注只能提交到最新版本，请刷新后在新版本上操作")
        if v["published_at"] is not None:
            raise APIError(409, "该版本已发布，不能再添加提议")
        length = v["content_len"]
        if body.start_cp < 0 or body.end_cp > length:
            raise APIError(400, f"非法选区：范围超出正文长度（0–{length} 码位）")
        cur = conn.execute(
            "INSERT INTO proposals (version_id, start_cp, end_cp, reason, proposer_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (vid, body.start_cp, body.end_cp, body.reason.strip(), user["id"]),
        )
        _bump_and_invalidate(conn, vid)
        conn.execute("COMMIT")
        pid = cur.lastrowid
    except APIError:
        conn.execute("ROLLBACK")
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"id": pid}


@app.post("/api/proposals/{pid}/decide")
def decide_proposal(pid: int, body: DecideIn, conn: sqlite3.Connection = Depends(get_db),
                    user: sqlite3.Row = Depends(current_user)):
    require_role(user, "reviewer")
    conn.execute("BEGIN IMMEDIATE")
    try:
        p = conn.execute(
            """SELECT p.*, d.author_id AS doc_author_id FROM proposals p
               JOIN versions v ON v.id=p.version_id
               JOIN documents d ON d.id=v.document_id
               WHERE p.id=?""",
            (pid,),
        ).fetchone()
        if not p:
            raise APIError(404, "提议不存在")
        v = _lock_version(conn, p["version_id"], body.expected_rev)
        if v["head_version_id"] != p["version_id"]:
            raise APIError(409, "该提议属于历史版本，只能处理当前版本上的提议，请刷新页面")
        if v["published_at"] is not None:
            raise APIError(409, "版本已发布，不能再裁决")
        if p["withdrawn_at"] is not None or p["status"] != "pending":
            raise APIError(409, "该提议已有结论或已撤回，请刷新页面")
        if p["proposer_id"] == user["id"]:
            raise APIError(403, "不能裁决自己的提议，须由另一名审阅人裁决")
        conn.execute(
            "UPDATE proposals SET status=?, adjudicator_id=?, decided_at=datetime('now')"
            " WHERE id=?",
            (body.decision, user["id"], pid),
        )
        _bump_and_invalidate(conn, p["version_id"])
        conn.execute("COMMIT")
    except APIError:
        conn.execute("ROLLBACK")
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"ok": True}


@app.post("/api/proposals/{pid}/withdraw")
def withdraw_proposal(pid: int, body: RevIn, conn: sqlite3.Connection = Depends(get_db),
                      user: sqlite3.Row = Depends(current_user)):
    """提议人可撤回自己“尚未裁决”的提议；已裁决的提议不可撤回。"""
    require_role(user, "reviewer")
    conn.execute("BEGIN IMMEDIATE")
    try:
        p = conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        if not p:
            raise APIError(404, "提议不存在")
        v = _lock_version(conn, p["version_id"], body.expected_rev)
        if v["head_version_id"] != p["version_id"]:
            raise APIError(409, "该提议属于历史版本，请刷新页面")
        if v["published_at"] is not None:
            raise APIError(409, "版本已发布，不能撤回")
        if p["proposer_id"] != user["id"]:
            raise APIError(403, "只能撤回自己的提议")
        if p["withdrawn_at"] is not None or p["status"] != "pending":
            raise APIError(409, "提议已裁决，不能撤回；请刷新页面")
        conn.execute(
            "UPDATE proposals SET withdrawn_at=datetime('now') WHERE id=?", (pid,)
        )
        _bump_and_invalidate(conn, p["version_id"])
        conn.execute("COMMIT")
    except APIError:
        conn.execute("ROLLBACK")
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"ok": True}


# ---------- 确认与发布 ----------

@app.post("/api/versions/{vid}/confirm")
def confirm_version(vid: int, body: RevIn, conn: sqlite3.Connection = Depends(get_db),
                    user: sqlite3.Row = Depends(current_user)):
    require_role(user, "reviewer")
    conn.execute("BEGIN IMMEDIATE")
    try:
        v = _lock_version(conn, vid, body.expected_rev)
        if user["id"] == v["document_author_id"]:
            raise APIError(403, "作者不能确认最终预览")
        if v["head_version_id"] != vid:
            raise APIError(409, "只能确认当前版本，请刷新页面")
        if v["published_at"] is not None:
            raise APIError(409, "该版本已发布")
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM proposals"
            " WHERE version_id=? AND status='pending' AND withdrawn_at IS NULL",
            (vid,),
        ).fetchone()["c"]
        if pending:
            raise APIError(409, f"仍有 {pending} 条提议未裁决，无法确认最终预览")
        ranges = conn.execute(
            "SELECT start_cp, end_cp FROM proposals"
            " WHERE version_id=? AND status='accepted' AND withdrawn_at IS NULL",
            (vid,),
        ).fetchall()
        sha = preview_hash(v["content"], [(r["start_cp"], r["end_cp"]) for r in ranges])
        # 自己的确认幂等：不影响他人确认，也不改变 state_rev。
        conn.execute(
            "INSERT INTO confirmations (version_id, reviewer_id, preview_sha)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(version_id, reviewer_id) DO UPDATE SET preview_sha=excluded.preview_sha,"
            " created_at=datetime('now')",
            (vid, user["id"], sha),
        )
        conn.execute("COMMIT")
    except APIError:
        conn.execute("ROLLBACK")
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return build_version_detail(conn, vid, user)


@app.post("/api/versions/{vid}/publish")
def publish_version(vid: int, body: RevIn, conn: sqlite3.Connection = Depends(get_db),
                    user: sqlite3.Row = Depends(current_user)):
    require_role(user, "author")
    conn.execute("BEGIN IMMEDIATE")
    try:
        v = _lock_version(conn, vid, body.expected_rev)
        if user["id"] != v["document_author_id"]:
            raise APIError(403, "只有文稿作者可以发布")
        if v["head_version_id"] != vid:
            raise APIError(409, "只能发布当前版本，请刷新页面")
        if v["published_at"] is not None:
            raise APIError(409, "该版本已经发布")
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM proposals"
            " WHERE version_id=? AND status='pending' AND withdrawn_at IS NULL",
            (vid,),
        ).fetchone()["c"]
        if pending:
            raise APIError(409, f"仍有 {pending} 条提议未裁决，不能发布")
        ranges = conn.execute(
            "SELECT start_cp, end_cp FROM proposals"
            " WHERE version_id=? AND status='accepted' AND withdrawn_at IS NULL",
            (vid,),
        ).fetchall()
        sha = preview_hash(v["content"], [(r["start_cp"], r["end_cp"]) for r in ranges])
        # 原子核验：两名【不同】且【非作者】的审阅人确认了【同一份】最终预览。
        n = conn.execute(
            """SELECT COUNT(DISTINCT c.reviewer_id) AS n
               FROM confirmations c JOIN users u ON u.id=c.reviewer_id
               WHERE c.version_id=? AND c.preview_sha=?
                 AND u.role='reviewer' AND c.reviewer_id != ?""",
            (vid, sha, user["id"]),
        ).fetchone()["n"]
        if n < 2:
            raise APIError(409, f"确认不足：需要两名非作者审阅人确认同一份最终预览（当前 {n} 名）")
        masked = apply_mask(v["content"], [(r["start_cp"], r["end_cp"]) for r in ranges])
        conn.execute(
            "INSERT INTO publications (version_id, document_id, masked_text, published_by)"
            " VALUES (?, ?, ?, ?)",
            (vid, v["document_id"], masked, user["id"]),
        )
        conn.execute("UPDATE versions SET published_at=datetime('now') WHERE id=?", (vid,))
        conn.execute("COMMIT")
    except APIError:
        conn.execute("ROLLBACK")
        raise
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        raise APIError(409, "该版本已发布，请刷新页面")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return build_version_detail(conn, vid, user)


# ---------- 导出 / 发布记录 / 待办 ----------

@app.get("/api/versions/{vid}/export")
def export_version(vid: int, conn: sqlite3.Connection = Depends(get_db),
                   user: sqlite3.Row = Depends(current_user)):
    """导出永远只包含遮盖后的文本（接受范围的并集），任何情况下都不返回原文。"""
    v = conn.execute(
        """SELECT v.*, d.title AS document_title FROM versions v
           JOIN documents d ON d.id=v.document_id WHERE v.id=?""",
        (vid,),
    ).fetchone()
    if not v:
        raise APIError(404, "版本不存在")
    if v["published_at"] is None:
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM proposals"
            " WHERE version_id=? AND status='pending' AND withdrawn_at IS NULL",
            (vid,),
        ).fetchone()["c"]
        if pending:
            raise APIError(409, "仍有未裁决提议，最终遮盖文本尚未确定，暂不能导出")
    ranges = conn.execute(
        "SELECT start_cp, end_cp FROM proposals"
        " WHERE version_id=? AND status='accepted' AND withdrawn_at IS NULL",
        (vid,),
    ).fetchall()
    masked = apply_mask(v["content"], [(r["start_cp"], r["end_cp"]) for r in ranges])
    filename = f"document-{v['document_id']}-v{v['version_no']}-masked.txt"
    return PlainTextResponse(
        masked,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/publications")
def list_publications(conn: sqlite3.Connection = Depends(get_db),
                      user: sqlite3.Row = Depends(current_user)):
    rows = conn.execute(
        """SELECT p.id, p.document_id, p.version_id, p.created_at,
                  d.title AS document_title, v.version_no,
                  u.display_name AS published_by_name
           FROM publications p
           JOIN documents d ON d.id=p.document_id
           JOIN versions v ON v.id=p.version_id
           JOIN users u ON u.id=p.published_by
           ORDER BY p.id DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/todos")
def todos(conn: sqlite3.Connection = Depends(get_db), user: sqlite3.Row = Depends(current_user)):
    result = {
        "awaiting_my_decision": [],
        "my_open_proposals": [],
        "awaiting_my_confirmation": [],
        "ready_to_publish": [],
    }
    if user["role"] == "reviewer":
        result["awaiting_my_decision"] = [dict(r) for r in conn.execute(
            """SELECT p.id AS proposal_id, p.version_id, p.document_id AS doc_id,
                      d.title AS document_title, v.version_no, p.start_cp, p.end_cp,
                      pu.display_name AS proposer_name, v.state_rev
               FROM proposals p
               JOIN versions v ON v.id=p.version_id
               JOIN documents d ON d.id=v.document_id
               JOIN users pu ON pu.id=p.proposer_id
               WHERE p.status='pending' AND p.withdrawn_at IS NULL
                 AND p.proposer_id != ? AND d.head_version_id=v.id
               ORDER BY p.id""",
            (user["id"],),
        ).fetchall()]
        result["my_open_proposals"] = [dict(r) for r in conn.execute(
            """SELECT p.id AS proposal_id, p.version_id, v.document_id AS doc_id,
                      d.title AS document_title, v.version_no, p.start_cp, p.end_cp,
                      v.state_rev
               FROM proposals p
               JOIN versions v ON v.id=p.version_id
               JOIN documents d ON d.id=v.document_id
               WHERE p.status='pending' AND p.withdrawn_at IS NULL AND p.proposer_id=?
                 AND d.head_version_id=v.id
               ORDER BY p.id""",
            (user["id"],),
        ).fetchall()]
        rows = conn.execute(
            """SELECT v.id AS version_id, v.document_id AS doc_id, d.title AS document_title,
                      v.version_no, v.state_rev, v.content
               FROM versions v JOIN documents d ON d.id=v.document_id
               WHERE d.head_version_id=v.id AND v.published_at IS NULL
                 AND NOT EXISTS (SELECT 1 FROM proposals p
                                 WHERE p.version_id=v.id AND p.status='pending'
                                   AND p.withdrawn_at IS NULL)
                 AND NOT EXISTS (SELECT 1 FROM confirmations c
                                 WHERE c.version_id=v.id AND c.reviewer_id=?
                                   AND c.reviewer_id != d.author_id)
               ORDER BY v.id""",
            (user["id"],),
        ).fetchall()
        items = []
        for r in rows:
            ranges = conn.execute(
                "SELECT start_cp, end_cp FROM proposals"
                " WHERE version_id=? AND status='accepted' AND withdrawn_at IS NULL",
                (r["version_id"],),
            ).fetchall()
            sha = preview_hash(r["content"], [(x["start_cp"], x["end_cp"]) for x in ranges])
            item = {k: r[k] for k in r.keys() if k != "content"}
            item["confirmer_count"] = _count_confirmers(conn, r["version_id"], sha)
            items.append(item)
        result["awaiting_my_confirmation"] = items
    else:
        result["ready_to_publish"] = [dict(r) for r in conn.execute(
            """SELECT v.id AS version_id, v.document_id AS doc_id, d.title AS document_title,
                      v.version_no, v.state_rev
               FROM versions v JOIN documents d ON d.id=v.document_id
               WHERE d.head_version_id=v.id AND v.published_at IS NULL
                 AND d.author_id=?
                 AND NOT EXISTS (SELECT 1 FROM proposals p
                                 WHERE p.version_id=v.id AND p.status='pending'
                                   AND p.withdrawn_at IS NULL)
               ORDER BY v.id""",
            (user["id"],),
        ).fetchall()]
        for r in result["ready_to_publish"]:
            ranges = conn.execute(
                "SELECT start_cp, end_cp FROM proposals"
                " WHERE version_id=? AND status='accepted' AND withdrawn_at IS NULL",
                (r["version_id"],),
            ).fetchall()
            vrow = conn.execute("SELECT content FROM versions WHERE id=?", (r["version_id"],)).fetchone()
            sha = preview_hash(vrow["content"], [(x["start_cp"], x["end_cp"]) for x in ranges])
            r["confirmer_count"] = _count_confirmers(conn, r["version_id"], sha)
    return result


def _count_confirmers(conn: sqlite3.Connection, vid: int, sha: str) -> int:
    return conn.execute(
        """SELECT COUNT(DISTINCT c.reviewer_id) AS c FROM confirmations c
           JOIN users u ON u.id=c.reviewer_id
           WHERE c.version_id=? AND c.preview_sha=? AND u.role='reviewer'""",
        (vid, sha),
    ).fetchone()["c"]


# ---------- 前端页面 ----------

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/app.js")
def app_js():
    return FileResponse(STATIC_DIR / "app.js", media_type="application/javascript")


@app.get("/styles.css")
def styles_css():
    return FileResponse(STATIC_DIR / "styles.css", media_type="text/css")
