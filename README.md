<div align="center">

# hermes-napcat-plugin

**[Hermes Agent](https://github.com/NousResearch/hermes-agent) 的 NapCat（QQ / OneBot 11）平台插件**

</div>

通过 [NapCat](https://github.com/NapNeko/NapCatQQ) / [SnowLuma](https://github.com/SnowLuma/SnowLuma)
的 OneBot 11 反向 WebSocket 将 Hermes 接入 QQ：在群里 @机器人或私聊即可对话，一个通用
`napcat_call` 工具驱动几乎全部 OneBot 11 能力（消息、群管理、好友、文件、资料……）。
标准 Hermes 插件形式交付，零核心改动，升级 Hermes 免维护。

```
QQ客户端 ──── NapCat ──WS拨入──▶ hermes-napcat-plugin ──▶ Hermes（大模型）
                                    │
                                    └── 一条 Universal 全双工 WS：事件 + API
                                      （反向 WS 服务端，默认 ws://0.0.0.0:8646/napcat/ws）
```

---

## 功能特性

- **群聊 & 私聊** — 群里 @机器人触发（回复机器人的消息也算），私聊直达
- **通用 QQ 工具** — `napcat_call(action, params)` 直通全部 OneBot 11 / NapCat 动作
- **按需富媒体** — 入站图片 / 语音 / 视频 / 文件 / 表情内联标记化，agent 需要时再取
- **跨机文件发送** — `upload_file_stream` 流式上传（NapCat v4.8.115+），失败回退 `file://`
- **引用上下文 & 消息去重** — 回复消息自动携带被引用内容；`message_id` 5 分钟去重
- **出站纯文本** — Markdown 自动转 QQ 友好纯文本（QQ 不渲染 Markdown）
- **工具进度批处理** — 可选：攒批发送工具进度，替代默认的"一次工具调用一条消息"
- **独立 cron 投递** — 可选：无网关进程时经 NapCat HTTP API 投递

---

## 快速开始

环境：Python 3.11+；较新 `main` 构建的 [Hermes Agent](https://github.com/NousResearch/hermes-agent)
（含插件系统）；[NapCatQQ](https://github.com/NapNeko/NapCatQQ) v4.8.115+，或
[SnowLuma](https://github.com/SnowLuma/SnowLuma)（见第 3 步；更旧的 NapCat 仍可聊天收发，
只是本地文件发送回退 `file://`）；`aiohttp >= 3.9`。

### 1. 安装插件

```bash
git clone <本仓库>
mkdir -p ~/.hermes/plugins

# 复制（推荐）：
cp -r hermes-napcat-plugin/plugin ~/.hermes/plugins/napcat

# 或软链（便于 git pull 更新——目录名必须是 napcat）：
ln -s "$PWD/hermes-napcat-plugin/plugin" ~/.hermes/plugins/napcat

hermes plugins list            # 应能看到 napcat
hermes plugins enable napcat
```

### 2. 配置

只需一个共享 Token（其余全部有默认值）。密钥放 `~/.hermes/.env`：

```bash
NAPCAT_TOKEN=pick-a-long-random-string
```

行为类配置放 `~/.hermes/config.yaml`：

```yaml
platforms:
  napcat:
    enabled: true
    extra:
      token: "pick-a-long-random-string"   # 与 .env 二选一
      host: "0.0.0.0"                      # 同机部署可改 127.0.0.1
      port: 8646
      path: "/napcat/ws"
      allow_from: ["10001", "10002"]       # 私聊白名单（空 = 对所有人开放）
      group_allow_from: []                 # 群成员白名单（空 = 开放）
      allowed_groups: []                   # 群白名单，"*" = 任意群（空 = 开放）
      # http_url: "http://127.0.0.1:3000"  # 可选：独立进程 cron 投递用

platform_toolsets:
  napcat:
    - hermes-cli        # 终端 / 文件 / 搜索等核心工具
    - napcat            # napcat_call 工具（本插件）
```

访问控制（空 = 开放，层层收紧）：`allow_all: true`（env `NAPCAT_ALLOW_ALL_USERS`）全部
放行；私聊受 `allow_from` 限制；群聊受 `allowed_groups`（`*` = 任意群）与
`group_allow_from` 限制，群里仍需 @机器人 才触发。

### 3. 配置 NapCat 反向 WebSocket

NapCat WebUI → 网络配置 → 新建 → **WebSocket 客户端**（反向 WS）：

| 设置 | 值 |
|------|-----|
| URL | `ws://<hermes 主机>:8646/napcat/ws` |
| accessToken | 与 `NAPCAT_TOKEN` 完全一致 |
| messagePostFormat | `array`（推荐；CQ 码字符串也能容忍，会被剥掉） |
| 心跳 / 重连 | 默认 30 秒 / 开启（推荐） |

**也可换用 [SnowLuma](https://github.com/SnowLuma/SnowLuma)**（QQ 桌面端的 OneBot 11 运行时，
需 Node.js 22+）：在其 WebUI（默认 `http://localhost:5099`）同样新建 WebSocket 客户端，
URL 和 accessToken 填上面的值即可，其余配置不变。已实测与本插件完全兼容——包括
`upload_file_stream` 流式上传，迁移只需改这一处连接配置。注意其许可为源码可见的
非商业协议（非 OSI 开源），商用需作者书面授权。

### 4. 启动并验证

```bash
nohup hermes gateway run > /tmp/hermes-gateway.log 2>&1 &
tail -f /tmp/hermes-gateway.log
# 期望依次看到：
#   [NapCat] Reverse WebSocket listening on ws://0.0.0.0:8646/napcat/ws
#   [NapCat] NapCat connected (self_id=..., remote=...)
```

群里 @机器人（或私聊发消息）即可对话。发一张图片问"这是什么"，agent 会通过
`get_image` 拉取后调视觉工具识别。

---

## `napcat_call` 工具与 `napcat` skill

进入 napcat 平台会话后自动获得一个工具：

```text
napcat_call("get_group_msg_history", {"group_id": 12345678, "count": 30})
napcat_call("upload_group_file",     {"group_id": 12345678, "file": "/var/log/app.log", "name": "app.log"})
```

工具描述内嵌常用动作目录；完整参数表、出站 `MEDIA:` 标签用法、入站富媒体标记解析、
输出风格（QQ 不渲染 Markdown）都在随插件注册的 `napcat` skill 里
（`skill_view("napcat:napcat")`），agent 需要时自动加载。

---

## 工具进度批处理（可选）

QQ 不能编辑已发送消息（OneBot 11 无对应接口），Hermes 默认的逐工具进度在这条通道上是
"一次工具调用一条消息"。开启批处理后，插件把工具行攒起来发：攒满 `progress_batch_size`
条、距第一条超过 `progress_batch_window_s` 秒、或 agent 即将发言时发出——最后一种会把
工具行**并进那条消息**（工具行在上、横线分隔、正文在下，顺序不乱）。媒体消息的
工具行仍单独发。回合结束与断连时，残余批次会被发掉或清空。

**1)** 关掉网关自带的进度（否则每条工具调用会多收一条未缓冲的消息）：

```yaml
display:
  platforms:
    napcat:
      tool_progress: off
```

**2)** 打开插件的批处理（`config.yaml` 的 `platforms.napcat.extra`，或 `.env` 同名变量）：

```yaml
platforms:
  napcat:
    extra:
      progress_batch: true          # NAPCAT_PROGRESS_BATCH
      progress_batch_size: 5        # NAPCAT_PROGRESS_BATCH_SIZE
      progress_batch_window_s: 30   # NAPCAT_PROGRESS_BATCH_WINDOW_S
```

工具行形如 `🖥 terminal · git status`（一行一次调用，单行上限 140 字符）；与正文合并时
用 `──────────` 横线分隔。依赖插件的 `pre_tool_call` 钩子，宿主不提供时静默降级为
无批量进度。

---

## 环境变量一览

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `NAPCAT_TOKEN` | 共享密钥（`Authorization: Bearer` 或 `?access_token=`） | —（必填） |
| `NAPCAT_ENABLED` | 兼容保留的旧开关；能从 config.yaml 或 `.env` 读到 token 时无作用（无 token 强制启用只会死循环重连） | `false` |
| `NAPCAT_HOST` / `NAPCAT_PORT` / `NAPCAT_PATH` | 反向 WS 监听地址 / 端口 / 路径 | `0.0.0.0` / `8646` / `/napcat/ws` |
| `NAPCAT_ALLOWED_USERS` | 允许私聊的 QQ 号（逗号分隔；空 = 开放） | 开放 |
| `NAPCAT_GROUP_ALLOWED_USERS` | 允许群内互动的成员 QQ 号（逗号分隔） | 开放 |
| `NAPCAT_ALLOWED_GROUPS` | 允许互动的群号（`*` = 任意） | 开放 |
| `NAPCAT_ALLOW_ALL_USERS` | 跳过全部 allowlist | `false` |
| `NAPCAT_HTTP_URL` | NapCat OneBot HTTP API 地址（独立进程 cron 投递） | — |
| `NAPCAT_HOME_CHANNEL` | cron 投递目标（QQ 号或 `group:<群号>`） | — |

> **独立进程的 cron 投递（可选）：** 用 `hermes cron run`（与 `hermes gateway` 分离的
> 进程）执行 `deliver=napcat` 的任务时，需在 NapCat 开启 HTTP 服务并配置
> `NAPCAT_HTTP_URL` / `http_url`，否则报 "No live adapter for platform 'napcat'"。
> 网关进程内的投递无需此项。

---

## 常见问题排查

| 现象 | 原因 / 解决 |
|------|------------|
| 日志没有 `Reverse WebSocket listening` | `NAPCAT_TOKEN` 未设置（适配器短路）；或端口被占用（日志搜 `napcat_bind_error`），换 `NAPCAT_PORT` |
| NapCat 报 `401` | accessToken 与 `NAPCAT_TOKEN` 不一致（注意空格），URL 路径须与 `NAPCAT_PATH` 完全一致 |
| NapCat 报 `connection refused` | 网关未运行或端口不对；NapCat 与 Hermes 不同机时勿用 `127.0.0.1`（WSL 场景见下） |
| 群里不回消息 | 必须 @机器人（回复机器人的消息也会触发）；核对日志 `lifecycle=connect self_id=...` 与 @ 的对象一致 |
| 私聊不回 | 检查 `allow_from` 是否把发送者排除在外 |
| 消息被重复处理 | 两个 NapCat 实例往同一 URL 推送（适配器按 message_id 去重 5 分钟） |
| 图片/文件发送失败 | 跨机部署需 NapCat v4.8.115+（`upload_file_stream`）；或把文件暴露成 HTTP(S) URL——图片用 Markdown `![](url)` 写进回复，语音/视频用 `napcat_call` 传 `record`/`video` 段 |
| cron 报 "No live adapter" | 独立进程 cron 需配置 `NAPCAT_HTTP_URL`（见上） |

**WSL / Windows（NapCat 在宿主机，Hermes 在 WSL）：** `127.0.0.1` 是各自机器的回环地址，
到不了 WSL 内的监听。任选其一：

1. **直连 WSL IP**：WSL 里 `hostname -I`，NapCat URL 填 `ws://<WSL-IP>:8646/napcat/ws`
   （NAT 模式下 WSL IP 重启会变）。
2. **mirrored 网络模式**（Win11 22H2+）：`C:\Users\<你>\.wslconfig` 里设 `[wsl2]`
   `networkingMode=mirrored` 后 `wsl --shutdown` 重开，NapCat 继续用
   `ws://127.0.0.1:8646/napcat/ws`。

---

## 运行测试

```bash
# 需要 hermes-agent 检出 + 依赖（aiohttp、ruamel.yaml、pyyaml、pytest）
export HERMES_AGENT_ROOT=/path/to/hermes-agent
python -m pytest tests/ -q
```

覆盖：握手鉴权、入站管道（标记化 / @门控 / 引用 / 去重 / 白名单）、出站（分块 /
Markdown 剥离 / 媒体段 / `upload_file_stream` / 超时不重试）、`napcat_call`、注册面
冒烟、工具进度批处理（当前 178 项全部通过）。

---

## 上游与许可证

本仓库是 [Zhaosn/hermes-napcat](https://github.com/Zhaosn/hermes-napcat) 的重构版，
实现方式对齐 [NousResearch/hermes-agent#17917](https://github.com/NousResearch/hermes-agent/pull/17917)
（NapCat 官方集成 PR），以标准 Hermes 插件形式交付、适配最新 main 的插件 API。
MIT 许可（见 [LICENSE](LICENSE)），代码谱系与版权归属：

| 来源 | 移植内容 | 许可证 |
|------|----------|--------|
| [NousResearch/hermes-agent PR #17917](https://github.com/NousResearch/hermes-agent/pull/17917)（作者 MoeOver） | `adapter.py` 主体、`tools.py`（napcat_call）、`skills/napcat/SKILL.md`、大部分测试 | hermes-agent 仓库为 MIT（Copyright 2025 Nous Research），PR 按其贡献条款同为 MIT |
| [Zhaosn/hermes-napcat](https://github.com/Zhaosn/hermes-napcat)（fork 自 [shubyi/hermes-napcat](https://github.com/shubyi/hermes-napcat)） | `formatting.py`（Markdown→QQ 纯文本）、`standalone.py`（HTTP API 独立投递） | 仓库 README 声明 MIT（无独立 LICENSE 文件） |
| 本仓库 | 重构、main 规范适配、审阅修复（`__init__.py`、白名单、`send_image` 覆写等） | MIT |

### 与上游 Zhaosn 版（0.3.x）的差异

| 维度 | Zhaosn 版 | 本重构版（1.x，PR #17917 方案） |
|------|-----------|--------------------------------|
| QQ 工具 | 74 个 `qq_*` 专用工具（schemas+tools ≈ 2000 行） | 一个通用 `napcat_call(action, params)` + `napcat` skill 动作目录 |
| 入站富媒体 | 立即下载图片 / ffmpeg 转写语音 | 内联标记 `[图片:id]` 等，agent 按需 `get_image` / `get_record` / `get_file` 拉取 |
| 出站本地文件 | 直接传本地路径（仅同机可用） | `upload_file_stream` 流式上传（跨机可用），失败回退 `file://` |
| 消息去重 | 无 | 按 `message_id` 去重 5 分钟 |
| 连接模型 | 多连接集合 | 单活动连接，新连接顶掉旧连接（一号一端） |
| self_id 探测 | `get_login_info` 轮询 | `X-Self-ID` 握手头 + `meta_event` 生命周期事件 |
| 发送超时 | 标记可重试（可能重复发送） | 不重试（帧可能已送达） |
| 鉴权配置 | 端口 + token 必填；注册时改写 `os.environ` | 仅 `NAPCAT_TOKEN` 必填；全部走 scoped secrets，不改进程环境 |
| 群白名单 | `group_policy` + `admins` 权限体系 | `allowed_groups` / `group_allow_from`（审阅修复：真正生效） |
| WS 帧上限 | aiohttp 默认 | 显式 4 MiB |

保留自 Zhaosn 版的能力：出站 Markdown → QQ 纯文本转换、独立进程 cron 投递。

> **从 Zhaosn 版迁移：** 配置键已收敛——`ws_port` → `port`（默认 8646）、
> `ws_path` → `path`（默认 `/napcat/ws`）、`access_token` → `token`（env
> `NAPCAT_TOKEN`）；`admins` / `dm_policy` / `group_policy` 已移除，访问控制统一为
> allowlist。NapCat 侧把反向 WS 的 URL / Token 改成新值即可。
