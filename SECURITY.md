# 安全说明

请勿提交以下内容：

- `.env`
- `sites.json` 或 `runtime/config/sites.json`
- `data/`、`runtime/data/`
- Delta Chat 账号目录
- 邮箱密码、应用密码、管理员密钥

这些路径已加入 `.gitignore` 和 `.dockerignore`。管理员密钥文件只保存 PBKDF2 加盐哈希，但仍应作为私密运行数据备份和保管。

发现安全问题时，请不要公开包含真实邮箱、邀请链接、聊天内容或服务器地址的日志。
