# 更新日志

项目的重要变更按时间倒序记录。

## 2026-10-19 | 移植 Tatotalk 门训业务到 Delta Chat

- 关联 PR：[Tato-menxunbot #1](https://github.com/cxze299/Tato-menxunbot/pull/1)。
- 新增：支持 Cedar 小组 Bot API、增量打卡与取消通知、独立读经状态和近 30 天记录、微读计划查询，以及超级管理员和小组管理员权限。
- 修复：每日灵修按当日计划读取 Markdown 正文或 PDF 页码入口；未配置和读取失败会重试，避免将占位提示当作已发布内容。
- 兼容：保留 Delta Chat 现有账号、成员绑定、通知群、中文命令和管理员群内发布灵修能力；聊天 ID 按平台独立保存。
