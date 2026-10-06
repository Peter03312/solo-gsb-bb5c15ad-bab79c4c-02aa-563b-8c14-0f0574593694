"""脱敏文稿工作台 —— FastAPI 后端。

- 本地账号（作者 / 审阅人），会话 Cookie 登录，无第三方服务
- 正文版本不可变；修改须另建版本，旧版批注不沿用
- 审阅人提议遮盖范围、可撤回自己未裁决的提议；他人逐条裁决
- 预览 / 导出只按已接受提议的并集遮盖
- 全部提议裁决完毕 + 两名不同且非作者的审阅人确认同一份最终预览，作者才能发布
- 所有写操作基于 state_version 乐观并发控制，过期提交返回 409
- 发布与确认在同一事务内校验与落库，保证原子一致
"""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .masking import excerpt, mask_text, u16_len

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.getcwd(), "data", "app.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('author', 'reviewer'))
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users (id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    author_id     INTEGER NOT NULL REFERENCES users (id),
    status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'published')),
    state_version INTEGER NOT NULL DEFAULT 1,   -- 乐观并发控制：每次变更 +1
    epoch         INTEGER NOT NULL DEFAULT 1,   -- 裁决纪元：任何裁决相关变化 +1，使确认失效
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    published_at  TEXT
);
CREATE TABLE IF NOT EXISTS versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents (id),
    seq         INTEGER NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (document_id, seq)
);
CREATE TABLE IF NOT EXISTS proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents (id),
    version_id  INTEGER NOT NULL REFERENCES versions (id),
    reviewer_id INTEGER NOT NULL REFERENCES users (id),
    start_off   INTEGER NOT NULL,
    end_off     INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'accepted', 'rejected', 'withdrawn')),
    decided_by  INTEGER REFERENCES users (id),
    decided_at  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS confirmations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents (id),
    version_id  INTEGER NOT NULL REFERENCES versions (id),
    reviewer_id INTEGER NOT NULL REFERENCES users (id),
    epoch       INTEGER NOT NULL,               -- 确认时的裁决纪元；纪元变化即失效
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (document_id, version_id, reviewer_id)
);
"""

DEFAULT_USERS = [
    {"username": "alice", "password": "alice123", "role": "author"},
    {"username": "bob", "password": "bob123", "role": "reviewer"},
    {"username": "carol", "password": "carol123", "role": "reviewer"},
]


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), 100_000
    ).hex()


class DB:
    """单连接 + 写锁；写操作一律在 BEGIN IMMEDIATE 事务中执行。"""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.lock = threading.RLock()

    def q(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    @contextmanager
    def txn(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()


db = DB(DB_PATH)
with db.lock:
    db.conn.executescript(SCHEMA)
    seed_spec = os.environ.get("SEED_USERS")
    seed_users = json.loads(seed_spec) if seed_spec else DEFAULT_USERS
    for u in seed_users:
        if not db.conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (u["username"],)
        ).fetchone():
            salt = secrets.token_hex(16)
            db.conn.execute(
                "INSERT INTO users (username, password_hash, salt, role) VALUES (?,?,?,?)",
                (u["username"], hash_password(u["password"], salt), salt, u["role"]),
            )
    db.conn.commit()

app = FastAPI(title="脱敏文稿工作台")


@app.exception_handler(RequestValidationError)
async def validation_handler(request, exc):
    return JSONResponse(status_code=400, content={"detail": "请求参数不合法"})


# ---------------------------------------------------------------- 工具

def require_user(request: Request) -> sqlite3.Row:
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(401, "未登录或会话已过期")
    user = db.one(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
        (token,),
    )
    if not user:
        raise HTTPException(401, "未登录或会话已过期")
    return user


def require_role(user, role: str, action: str):
    if user["role"] != role:
        label = "作者" if role == "author" else "审阅人"
        raise HTTPException(403, f"无权限：仅{label}可{action}")


def get_doc(doc_id: int) -> sqlite3.Row:
    doc = db.one("SELECT * FROM documents WHERE id = ?", (doc_id,))
    if not doc:
        raise HTTPException(404, "文稿不存在")
    return doc


def current_version(doc_id: int) -> sqlite3.Row:
    return db.one(
        "SELECT * FROM versions WHERE document_id = ? ORDER BY seq DESC LIMIT 1",
        (doc_id,),
    )


def check_state(doc, expected: int):
    """乐观并发控制：客户端基于过期状态的提交一律拒绝。"""
    if doc["state_version"] != expected:
        raise HTTPException(409, "文档状态已变化（存在并发提交），请刷新页面后重试")


def check_open(doc):
    if doc["status"] != "open":
        raise HTTPException(400, "文稿已发布，不能再修改")


def valid_confirmations(doc, version) -> list:
    """当前版本、当前裁决纪元下的有效确认。"""
    return db.q(
        """SELECT c.*, u.username FROM confirmations c
           JOIN users u ON u.id = c.reviewer_id
           WHERE c.document_id = ? AND c.version_id = ? AND c.epoch = ?""",
        (doc["id"], version["id"], doc["epoch"]),
    )


def proposal_dict(p) -> dict:
    return {
        "id": p["id"],
        "start": p["start_off"],
        "end": p["end_off"],
        "status": p["status"],
        "reviewer": p["reviewer"],
        "reviewer_id": p["reviewer_id"],
        "decider": p["decider"],
        "excerpt": p["excerpt"],
        "created_at": p["created_at"],
    }


def proposals_of(version) -> list:
    rows = db.q(
        """SELECT p.*, u.username AS reviewer, du.username AS decider
           FROM proposals p
           JOIN users u ON u.id = p.reviewer_id
           LEFT JOIN users du ON du.id = p.decided_by
           WHERE p.version_id = ? ORDER BY p.id""",
        (version["id"],),
    )
    out = []
    for p in rows:
        p = dict(p)
        p["excerpt"] = excerpt(version["content"], p["start_off"], p["end_off"])
        out.append(proposal_dict(p))
    return out


def doc_detail(doc, user) -> dict:
    ver = current_version(doc["id"])
    props = proposals_of(ver)
    accepted = [(p["start"], p["end"]) for p in props if p["status"] == "accepted"]
    confs = valid_confirmations(doc, ver)
    stale = db.one(
        """SELECT COUNT(*) AS n FROM confirmations
           WHERE document_id = ? AND version_id = ? AND epoch <> ?""",
        (doc["id"], ver["id"], doc["epoch"]),
    )["n"]
    pending = sum(1 for p in props if p["status"] == "pending")
    confirmers = {c["reviewer_id"] for c in confs if c["reviewer_id"] != doc["author_id"]}

    blockers = []
    if doc["status"] != "open":
        blockers.append("文稿已发布")
    if pending:
        blockers.append(f"还有 {pending} 条提议未裁决")
    if len(confirmers) < 2:
        blockers.append(
            f"确认不足：需要两名不同且非作者的审阅人确认当前最终预览"
            f"（当前 {len(confirmers)} 名）"
        )

    versions = db.q(
        """SELECT v.id, v.seq, v.created_at,
                  (SELECT COUNT(*) FROM proposals p WHERE p.version_id = v.id) AS proposals
           FROM versions v WHERE v.document_id = ? ORDER BY v.seq DESC""",
        (doc["id"],),
    )
    author = db.one("SELECT username FROM users WHERE id = ?", (doc["author_id"],))
    return {
        "id": doc["id"],
        "title": doc["title"],
        "status": doc["status"],
        "state_version": doc["state_version"],
        "author_id": doc["author_id"],
        "author": author["username"],
        "created_at": doc["created_at"],
        "published_at": doc["published_at"],
        "current_version": {
            "id": ver["id"],
            "seq": ver["seq"],
            "content": ver["content"],
            "created_at": ver["created_at"],
            "u16_len": u16_len(ver["content"]),
        },
        "versions": [dict(v) for v in versions],
        "proposals": props,
        "preview": mask_text(ver["content"], accepted),
        "pending_count": pending,
        "confirmations": [
            {"reviewer": c["username"], "reviewer_id": c["reviewer_id"],
             "created_at": c["created_at"]}
            for c in confs
        ],
        "stale_confirmations": stale,
        "can_publish": not blockers,
        "publish_blockers": blockers,
    }


# ---------------------------------------------------------------- 请求模型

class LoginIn(BaseModel):
    username: str
    password: str


class DocIn(BaseModel):
    title: str
    content: str


class VersionIn(BaseModel):
    content: str
    expected_state_version: int


class ProposalIn(BaseModel):
    start: int
    end: int
    expected_state_version: int


class DecideIn(BaseModel):
    decision: str
    expected_state_version: int


class StateIn(BaseModel):
    expected_state_version: int


# ---------------------------------------------------------------- 认证

@app.post("/api/login")
def login(body: LoginIn, request: Request):
    user = db.one("SELECT * FROM users WHERE username = ?", (body.username,))
    if not user or not hmac.compare_digest(
        user["password_hash"], hash_password(body.password, user["salt"])
    ):
        raise HTTPException(401, "用户名或密码错误")
    token = secrets.token_hex(32)
    with db.txn() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, user["id"])
        )
    resp = JSONResponse(
        {"id": user["id"], "username": user["username"], "role": user["role"]}
    )
    resp.set_cookie("session", token, httponly=True, samesite="lax")
    return resp


@app.post("/api/logout")
def logout(request: Request):
    token = request.cookies.get("session")
    if token:
        with db.txn() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("session")
    return resp


@app.get("/api/me")
def me(request: Request):
    user = require_user(request)
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


# ---------------------------------------------------------------- 待办 / 列表

@app.get("/api/todo")
def todo(request: Request):
    user = require_user(request)
    if user["role"] == "reviewer":
        adjudicate = db.q(
            """SELECT d.id AS doc_id, d.title, COUNT(*) AS n
               FROM proposals p
               JOIN documents d ON d.id = p.document_id
               JOIN versions v ON v.id = p.version_id
               WHERE p.status = 'pending' AND p.reviewer_id <> ? AND d.status = 'open'
                 AND v.seq = (SELECT MAX(seq) FROM versions v2 WHERE v2.document_id = d.id)
               GROUP BY d.id ORDER BY d.id DESC""",
            (user["id"],),
        )
        confirm = db.q(
            """SELECT d.id AS doc_id, d.title
               FROM documents d
               JOIN versions v ON v.document_id = d.id
                 AND v.seq = (SELECT MAX(seq) FROM versions v2 WHERE v2.document_id = d.id)
               WHERE d.status = 'open'
                 AND NOT EXISTS (SELECT 1 FROM proposals p
                                 WHERE p.version_id = v.id AND p.status = 'pending')
                 AND NOT EXISTS (SELECT 1 FROM confirmations c
                                 WHERE c.document_id = d.id AND c.version_id = v.id
                                   AND c.reviewer_id = ? AND c.epoch = d.epoch)
               ORDER BY d.id DESC""",
            (user["id"],),
        )
        return {
            "role": "reviewer",
            "adjudicate": [dict(r) for r in adjudicate],
            "confirm": [dict(r) for r in confirm],
        }

    docs = db.q(
        "SELECT * FROM documents WHERE author_id = ? ORDER BY id DESC", (user["id"],)
    )
    waiting, ready, published = [], [], []
    for d in docs:
        if d["status"] == "published":
            published.append({"doc_id": d["id"], "title": d["title"]})
            continue
        ver = current_version(d["id"])
        pending = db.one(
            "SELECT COUNT(*) AS n FROM proposals WHERE version_id = ? AND status = 'pending'",
            (ver["id"],),
        )["n"]
        confs = valid_confirmations(d, ver)
        nconf = len({c["reviewer_id"] for c in confs if c["reviewer_id"] != d["author_id"]})
        if pending:
            waiting.append({"doc_id": d["id"], "title": d["title"], "pending": pending})
        elif nconf >= 2:
            ready.append({"doc_id": d["id"], "title": d["title"]})
        else:
            waiting.append(
                {"doc_id": d["id"], "title": d["title"],
                 "pending": 0, "confirmations": nconf}
            )
    return {"role": "author", "waiting": waiting, "ready": ready, "published": published}


@app.get("/api/documents")
def list_documents(request: Request):
    require_user(request)
    rows = db.q(
        """SELECT d.id, d.title, d.status, d.state_version, d.created_at, d.published_at,
                  u.username AS author,
                  (SELECT MAX(seq) FROM versions v WHERE v.document_id = d.id) AS current_seq
           FROM documents d JOIN users u ON u.id = d.author_id
           ORDER BY d.id DESC"""
    )
    return [dict(r) for r in rows]


@app.post("/api/documents", status_code=201)
def create_document(body: DocIn, request: Request):
    user = require_user(request)
    require_role(user, "author", "创建文稿")
    if not body.title.strip():
        raise HTTPException(400, "标题不能为空")
    if not body.content:
        raise HTTPException(400, "正文不能为空")
    with db.txn() as conn:
        cur = conn.execute(
            "INSERT INTO documents (title, author_id) VALUES (?, ?)",
            (body.title.strip(), user["id"]),
        )
        doc_id = cur.lastrowid
        conn.execute(
            "INSERT INTO versions (document_id, seq, content) VALUES (?, 1, ?)",
            (doc_id, body.content),
        )
    return {"id": doc_id}


# ---------------------------------------------------------------- 文稿详情 / 版本

@app.get("/api/documents/{doc_id}")
def get_document(doc_id: int, request: Request):
    user = require_user(request)
    return doc_detail(get_doc(doc_id), user)


@app.post("/api/documents/{doc_id}/versions", status_code=201)
def create_version(doc_id: int, body: VersionIn, request: Request):
    """修改正文 = 另建不可变新版本；旧版本的提议/确认一律不沿用。"""
    user = require_user(request)
    require_role(user, "author", "新建版本")
    if not body.content:
        raise HTTPException(400, "正文不能为空")
    with db.txn() as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if not doc:
            raise HTTPException(404, "文稿不存在")
        if doc["author_id"] != user["id"]:
            raise HTTPException(403, "无权限：只有该文稿的作者可以新建版本")
        check_open(doc)
        check_state(doc, body.expected_state_version)
        seq = conn.execute(
            "SELECT MAX(seq) AS m FROM versions WHERE document_id = ?", (doc_id,)
        ).fetchone()["m"] + 1
        conn.execute(
            "INSERT INTO versions (document_id, seq, content) VALUES (?,?,?)",
            (doc_id, seq, body.content),
        )
        conn.execute(
            "UPDATE documents SET state_version = state_version + 1, epoch = epoch + 1 WHERE id = ?",
            (doc_id,),
        )
    return {"seq": seq}


@app.get("/api/documents/{doc_id}/versions/{seq}")
def get_version(doc_id: int, seq: int, request: Request):
    """查看不可变历史版本及其当时的批注（不沿用到新版本）。"""
    require_user(request)
    doc = get_doc(doc_id)
    ver = db.one(
        "SELECT * FROM versions WHERE document_id = ? AND seq = ?", (doc_id, seq)
    )
    if not ver:
        raise HTTPException(404, "版本不存在")
    props = proposals_of(ver)
    accepted = [(p["start"], p["end"]) for p in props if p["status"] == "accepted"]
    return {
        "doc_id": doc["id"],
        "title": doc["title"],
        "doc_status": doc["status"],
        "seq": ver["seq"],
        "content": ver["content"],
        "created_at": ver["created_at"],
        "proposals": props,
        "preview": mask_text(ver["content"], accepted),
    }


# ---------------------------------------------------------------- 提议 / 裁决

@app.post("/api/documents/{doc_id}/proposals", status_code=201)
def create_proposal(doc_id: int, body: ProposalIn, request: Request):
    user = require_user(request)
    require_role(user, "reviewer", "提出遮盖提议")
    with db.txn() as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if not doc:
            raise HTTPException(404, "文稿不存在")
        check_open(doc)
        check_state(doc, body.expected_state_version)
        ver = conn.execute(
            "SELECT * FROM versions WHERE document_id = ? ORDER BY seq DESC LIMIT 1",
            (doc_id,),
        ).fetchone()
        n = u16_len(ver["content"])
        if not (0 <= body.start < body.end <= n):
            raise HTTPException(
                400, f"非法选区：范围 [{body.start}, {body.end}) 越界或为空（正文长度 {n}）"
            )
        cur = conn.execute(
            """INSERT INTO proposals (document_id, version_id, reviewer_id, start_off, end_off)
               VALUES (?,?,?,?,?)""",
            (doc_id, ver["id"], user["id"], body.start, body.end),
        )
        conn.execute(
            "UPDATE documents SET state_version = state_version + 1, epoch = epoch + 1 WHERE id = ?",
            (doc_id,),
        )
        pid = cur.lastrowid
    return {"id": pid}


@app.post("/api/proposals/{proposal_id}/withdraw")
def withdraw_proposal(proposal_id: int, body: StateIn, request: Request):
    user = require_user(request)
    require_role(user, "reviewer", "撤回提议")
    with db.txn() as conn:
        p = conn.execute(
            "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        if not p:
            raise HTTPException(404, "提议不存在")
        doc = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (p["document_id"],)
        ).fetchone()
        check_open(doc)
        check_state(doc, body.expected_state_version)
        if p["reviewer_id"] != user["id"]:
            raise HTTPException(403, "无权限：只能撤回自己的提议")
        if p["status"] != "pending":
            raise HTTPException(400, "只能撤回未裁决的提议")
        conn.execute(
            "UPDATE proposals SET status = 'withdrawn' WHERE id = ?", (proposal_id,)
        )
        conn.execute(
            "UPDATE documents SET state_version = state_version + 1, epoch = epoch + 1 WHERE id = ?",
            (doc["id"],),
        )
    return {"ok": True}


@app.post("/api/proposals/{proposal_id}/decide")
def decide_proposal(proposal_id: int, body: DecideIn, request: Request):
    user = require_user(request)
    require_role(user, "reviewer", "裁决提议")
    if body.decision not in ("accept", "reject"):
        raise HTTPException(400, "非法裁决：只能是 accept 或 reject")
    with db.txn() as conn:
        p = conn.execute(
            "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        if not p:
            raise HTTPException(404, "提议不存在")
        doc = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (p["document_id"],)
        ).fetchone()
        check_open(doc)
        check_state(doc, body.expected_state_version)
        if p["reviewer_id"] == user["id"]:
            raise HTTPException(403, "不能裁决自己的提议")
        if p["status"] != "pending":
            raise HTTPException(400, "该提议已被裁决，请刷新查看最新状态")
        conn.execute(
            """UPDATE proposals
               SET status = ?, decided_by = ?, decided_at = datetime('now')
               WHERE id = ?""",
            ("accepted" if body.decision == "accept" else "rejected",
             user["id"], proposal_id),
        )
        conn.execute(
            "UPDATE documents SET state_version = state_version + 1, epoch = epoch + 1 WHERE id = ?",
            (doc["id"],),
        )
    return {"ok": True}


# ---------------------------------------------------------------- 确认 / 发布 / 导出

@app.post("/api/documents/{doc_id}/confirm")
def confirm_document(doc_id: int, body: StateIn, request: Request):
    """审阅人确认当前最终预览；此后任何裁决变化（epoch 变化）都使该确认失效。"""
    user = require_user(request)
    require_role(user, "reviewer", "确认最终预览")
    with db.txn() as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if not doc:
            raise HTTPException(404, "文稿不存在")
        check_open(doc)
        check_state(doc, body.expected_state_version)
        if doc["author_id"] == user["id"]:
            raise HTTPException(403, "无权限：作者不能作为审阅人确认自己的文稿")
        ver = conn.execute(
            "SELECT * FROM versions WHERE document_id = ? ORDER BY seq DESC LIMIT 1",
            (doc_id,),
        ).fetchone()
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM proposals WHERE version_id = ? AND status = 'pending'",
            (ver["id"],),
        ).fetchone()["n"]
        if pending:
            raise HTTPException(
                400, f"还有 {pending} 条提议未裁决，全部裁决后才能确认最终预览"
            )
        conn.execute(
            """INSERT OR REPLACE INTO confirmations (document_id, version_id, reviewer_id, epoch)
               VALUES (?,?,?,?)""",
            (doc_id, ver["id"], user["id"], doc["epoch"]),
        )
        conn.execute(
            "UPDATE documents SET state_version = state_version + 1 WHERE id = ?",
            (doc_id,),
        )
    return {"ok": True}


@app.post("/api/documents/{doc_id}/publish")
def publish_document(doc_id: int, body: StateIn, request: Request):
    """发布：与确认校验在同一事务内完成，保证原子一致。"""
    user = require_user(request)
    require_role(user, "author", "发布文稿")
    with db.txn() as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if not doc:
            raise HTTPException(404, "文稿不存在")
        if doc["author_id"] != user["id"]:
            raise HTTPException(403, "无权限：只有该文稿的作者可以发布")
        check_open(doc)
        check_state(doc, body.expected_state_version)
        ver = conn.execute(
            "SELECT * FROM versions WHERE document_id = ? ORDER BY seq DESC LIMIT 1",
            (doc_id,),
        ).fetchone()
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM proposals WHERE version_id = ? AND status = 'pending'",
            (ver["id"],),
        ).fetchone()["n"]
        if pending:
            raise HTTPException(400, f"还有 {pending} 条提议未裁决，不能发布")
        confs = conn.execute(
            """SELECT reviewer_id FROM confirmations
               WHERE document_id = ? AND version_id = ? AND epoch = ?
                 AND reviewer_id <> ?""",
            (doc_id, ver["id"], doc["epoch"], doc["author_id"]),
        ).fetchall()
        if len({c["reviewer_id"] for c in confs}) < 2:
            raise HTTPException(
                400,
                "确认不足：需要两名不同且非作者的审阅人确认同一份最终预览后才能发布",
            )
        conn.execute(
            """UPDATE documents
               SET status = 'published', published_at = datetime('now'),
                   state_version = state_version + 1
               WHERE id = ?""",
            (doc_id,),
        )
    return {"ok": True}


@app.get("/api/documents/{doc_id}/export")
def export_document(doc_id: int, request: Request):
    """导出：只输出按已接受提议并集遮盖后的文本，绝不包含原文。"""
    require_user(request)
    doc = get_doc(doc_id)
    ver = current_version(doc_id)
    rows = db.q(
        "SELECT start_off, end_off FROM proposals WHERE version_id = ? AND status = 'accepted'",
        (ver["id"],),
    )
    masked = mask_text(ver["content"], [(r["start_off"], r["end_off"]) for r in rows])
    return PlainTextResponse(
        masked,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="redacted-doc-{doc_id}.txt"'
        },
    )


# ---------------------------------------------------------------- 页面

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
