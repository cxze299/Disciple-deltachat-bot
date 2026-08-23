# 门训同行 Delta Chat Bot

[![CI](https://github.com/cxze299/Disciple-deltachat-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/cxze299/Disciple-deltachat-bot/actions/workflows/ci.yml)

面向 Delta Chat / Chatmail 的门训打卡机器人。支持多网站、多群提醒、私聊打卡、补签、取消打卡、状态查询和一次验证的管理员体系。

项目以 Docker Compose 为主要部署方式，适用于群晖 NAS、Linux 服务器、Windows Docker Desktop。

## 功能

- 每天早上提醒灵修和读经，晚上提醒门训学习
- 每天 08:00 自动将当天灵修内容发布到对应群聊
- 灵修、周读物、视频、背经打卡
- 补签和取消打卡，并与网站现有状态保持一致
- 私聊操作后向对应门训群同步按时间排序的名单
- “我的状态”和“群状态”分开查询
- 一套机器人连接多个门训网站和多个 Delta Chat 群
- 管理员密钥验证、身份持久绑定、广播和立即提醒
- NAS HTTPS 临时连接异常自动重试
- Docker 健康检查、日志轮转和 GitHub Actions 测试

## 项目结构

```text
Disciple-deltachat-bot/
├─ menxun_bot.py           机器人主程序
├─ admin_key.py            管理员密钥哈希与验证
├─ set_admin_key.py        管理员密钥设置工具
├─ healthcheck.py          Docker 健康检查
├─ sites.example.json      网站配置模板
├─ Dockerfile
├─ compose.yaml
├─ manage.ps1              Windows 管理工具
├─ manage.sh               Linux / NAS 管理工具
└─ runtime/
   ├─ config/sites.json    实际网站配置，不上传 GitHub
   └─ data/                账号、状态和密钥，不上传 GitHub
```

## 五步部署

### 1. 下载项目

```powershell
git clone https://github.com/cxze299/Disciple-deltachat-bot.git
cd Disciple-deltachat-bot
```

Linux 或 NAS：

```bash
git clone https://github.com/cxze299/Disciple-deltachat-bot.git
cd Disciple-deltachat-bot
chmod +x manage.sh
```

### 2. 构建并生成配置

Windows：

```powershell
.\manage.ps1 setup
```

Linux / NAS：

```bash
./manage.sh setup
```

首次执行会构建镜像，并自动生成：

```text
runtime/config/sites.json
```

### 3. 配置门训网站

编辑 `runtime/config/sites.json`：

```json
{
  "sites": [
    {
      "id": "group1",
      "name": "第一门训小组",
      "url": "https://your-nas.example.com:ports",
      "chat_ids": [],
      "timezone": "Asia/Shanghai",
      "morning_time": "08:30",
      "evening_time": "20:30",
      "enabled": true
    }
  ]
}
```

注意：

- `url` 必须是纯 `http://` 或 `https://` 地址
- 不要填写 Markdown 链接
- `id` 只能使用英文、数字、下划线和短横线
- 一个群 ID 只能属于一个网站
- 暂时不知道群 ID 时先保持 `chat_ids` 为空

### 4. 初始化机器人账号和管理员密钥

Windows：

```powershell
.\manage.ps1 init
.\manage.ps1 admin-key
```

Linux / NAS：

```bash
./manage.sh init
./manage.sh admin-key
```

初始化账号时填写机器人邮箱和邮箱授权码/应用密码。使用自建 Chatmail 时，也可以填写服务提供的 `DCACCOUNT:` URI。

管理员密钥至少 10 个字符，只保存 PBKDF2 加盐哈希，不保存明文。

### 5. 获取邀请链接并启动

```powershell
.\manage.ps1 link
.\manage.ps1 start
.\manage.ps1 logs
```

Linux / NAS 将 `manage.ps1` 换成 `manage.sh`。

在 Delta Chat 中打开终端显示的邀请链接，把机器人加入门训群，然后在群中发送一条消息。日志会出现：

```text
chat_id=11
```

把这个数字填入对应网站的 `chat_ids`：

```json
"chat_ids": [11]
```

保存配置后重启：

```powershell
.\manage.ps1 restart
```

## 常用管理命令

| 操作 | Windows | Linux / NAS |
|---|---|---|
| 启动 | `.\manage.ps1 start` | `./manage.sh start` |
| 停止 | `.\manage.ps1 stop` | `./manage.sh stop` |
| 重启 | `.\manage.ps1 restart` | `./manage.sh restart` |
| 日志 | `.\manage.ps1 logs` | `./manage.sh logs` |
| 状态 | `.\manage.ps1 status` | `./manage.sh status` |
| 测试 | `.\manage.ps1 test` | `./manage.sh test` |
| 更新 | `.\manage.ps1 update` | `./manage.sh update` |
| 邀请链接 | `.\manage.ps1 link` | `./manage.sh link` |

运行 `link`、`init` 等账号管理命令前，若提示账户被锁定，请先停止正在运行的机器人，再执行命令。

## 用户指令

机器人使用中文指令，不需要 `/`：

```text
打卡 [项目]
补签 [项目] 日期
取消打卡 [项目] [日期]

我的状态
群状态
任务
绑定 姓名
网站
切换 网站代号
帮助
```

项目统一为：`灵修`、`周读物`、`视频`、`背经`。未填写项目时默认为灵修。

发送 `门训总结` 可统计当前绑定成员在所选网站的全部历史数据，包括活跃天数、灵修与周任务次数、最长连续灵修、补签次数和最近记录。

示例：

```text
打卡 视频
补签 视频 2026-08-20
取消打卡 视频
取消打卡 视频 昨天
```

## 管理员

设置密钥后，由管理员候选人私聊机器人：

```text
管理员验证 你的密钥
```

验证成功后，当前 Delta Chat 用户会持久绑定，重启后无需再次输入密钥。验证命令只能私聊使用，机器人日志会把密钥隐藏为 `***`。

```text
管理员状态
管理员 网站状态
管理员 成员列表
管理员 群列表
管理员 广播 公告内容
管理员 发布灵修
管理员 立即提醒 早间
管理员 立即提醒 晚间
管理员解除
```

## 多网站配置

在 `sites` 数组继续添加网站即可。每个网站拥有独立的：

- 网站地址
- Delta Chat 群 ID
- 时区
- 早晚提醒时间

群聊按群 ID 自动选择网站；私聊通过“切换 网站代号”选择当前网站。一个 Delta Chat 用户可在不同网站绑定不同姓名。

网站打卡监听默认每 5 秒检查一次。可在 `.env` 设置 `WEBSITE_POLL_INTERVAL` 调整，允许范围为 2–60 秒；间隔越短，通知越快，但会增加 NAS 和网站 API 请求量。

## 数据和备份

需要备份的运行数据：

```text
runtime/config/sites.json
runtime/data/
```

其中 `runtime/data` 包含 Delta Chat 账号、用户绑定、管理员绑定和密钥哈希。不要把这些文件上传到公开仓库。

迁移服务器时，停止容器后复制整个 `runtime` 目录，在新服务器执行：

```bash
docker compose up -d --build
```

## 健康检查

```bash
docker compose ps
```

正常状态应显示 `healthy`。机器人每 20 秒更新一次不含敏感数据的心跳文件。超过 120 秒未更新时，容器会显示为 `unhealthy`。

查看原因：

```bash
docker compose logs --tail 200 menxun-bot
```

## 本地开发

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m unittest -v test_menxun_bot.py
```

项目使用 `deltabot-cli==9.1.0` 和 `deltachat2`，参考 [Delta Chat Bot](https://github.com/deltachat-bot)。
