#!/usr/bin/env python3
"""交互式设置门训机器人的管理员验证密钥。"""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path

from admin_key import create_key_record, save_key_record

ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="设置门训机器人管理员密钥（仅保存加盐哈希）")
    parser.add_argument(
        "--file",
        type=Path,
        default=Path(os.getenv("MENXUN_ADMIN_KEY_FILE", ROOT / "data" / "admin-key.json")),
        help="密钥哈希文件路径",
    )
    args = parser.parse_args()

    secret = getpass.getpass("请输入新的管理员密钥（至少 10 个字符）：")
    confirmation = getpass.getpass("请再次输入管理员密钥：")
    if secret != confirmation:
        print("两次输入不一致，未做任何修改。")
        return 1
    try:
        record = create_key_record(secret)
    except ValueError as error:
        print(error)
        return 1
    save_key_record(args.file.resolve(), record)
    print(f"管理员密钥已安全保存到：{args.file.resolve()}")
    print("机器人只保存加盐哈希；请重启机器人，然后私聊发送“管理员验证 你的密钥”。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
