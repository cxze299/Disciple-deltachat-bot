"""管理员密钥的安全生成、保存与验证。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 600_000
SALT_BYTES = 32


def create_key_record(secret: str, iterations: int = ITERATIONS) -> dict:
    """生成可保存的加盐哈希记录，不保留明文密钥。"""
    if len(secret) < 10:
        raise ValueError("管理员密钥至少需要 10 个字符。")
    salt = os.urandom(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, iterations)
    return {
        "algorithm": ALGORITHM,
        "iterations": iterations,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(digest).decode("ascii"),
    }


def verify_key(secret: str, record: dict) -> bool:
    """使用恒定时间比较验证密钥；无效配置统一返回 False。"""
    try:
        if record.get("algorithm") != ALGORITHM:
            return False
        iterations = int(record["iterations"])
        salt = base64.b64decode(record["salt"], validate=True)
        expected = base64.b64decode(record["hash"], validate=True)
        actual = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except (KeyError, TypeError, ValueError):
        return False


def load_key_record(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def save_key_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
