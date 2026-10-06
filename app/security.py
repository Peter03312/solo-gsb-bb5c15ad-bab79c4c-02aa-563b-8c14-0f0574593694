"""会话 Cookie 与口令哈希。

会话是 HMAC-SHA256 签名的自包含 cookie（本地部署，不依赖第三方服务）：
    base64url(payload).base64url(hmac_sha256(secret, payload))
payload = {"uid": int, "name": str, "exp": epoch}
口令使用 PBKDF2-HMAC-SHA256 加盐哈希，仅存哈希不存明文。
"""
import base64
import hashlib
import hmac
import json
import os
import time

from . import config

_PBKDF2_ROUNDS = 200_000


def hash_password(password: str, salt: str | None = None) -> str:
    """返回 pbkdf2_sha256$rounds$salt$hash 形式的字符串。"""
    if salt is None:
        salt = base64.b64encode(os.urandom(12)).decode("ascii")
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ROUNDS
    ).hex()
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        check = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(rounds)
        ).hex()
        return hmac.compare_digest(check, digest)
    except (ValueError, TypeError):
        return False


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def create_session_token(uid: int, name: str) -> str:
    payload = {
        "uid": uid,
        "name": name,
        "exp": int(time.time()) + config.SESSION_TTL_DAYS * 86400,
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = hmac.new(config.SESSION_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    return f"{body}.{_b64e(sig)}"


def parse_session_token(token: str | None) -> dict | None:
    """校验签名与有效期，成功返回 payload，否则 None。"""
    if not token or token.count(".") != 1:
        return None
    body, sig = token.split(".", 1)
    expected = hmac.new(config.SESSION_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64e(expected), sig):
        return None
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload.get("uid"), int) or payload.get("exp", 0) < time.time():
        return None
    return payload
