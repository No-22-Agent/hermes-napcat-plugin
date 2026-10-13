<div align="center">

# hermes-napcat-plugin

**[Hermes Agent](https://github.com/NousResearch/hermes-agent) 的 NapCat（QQ / OneBot 11）平台插件**

</div>

通过 [NapCat](https://github.com/NapNeko/NapCatQQ) 的 OneBot 11 反向 WebSocket 将 Hermes 接入
QQ。在任意 QQ 群或私聊中与 AI 助手对话，通过一个通用 `napcat_call` 工具驱动几乎全部
OneBot 11 能力（消息、群管理、好友、文件、资料……）。

本仓库是 [Zhaosn/hermes-napcat](https://github.com/Zhaosn/hermes-napcat) 的重构版，
实现方式对齐 [NousResearch/hermes-agent#17917](https://github.com/NousResearch/hermes-agent/pull/17917)
（NapCat 官方集成 PR），并以标准 Hermes 插件形式交付、适配最新 main 的插件 API。

## 上游与许可证

本插件为 MIT 许可（见 [LICENSE](LICENSE)），代码谱系与版权归属：

| 来源 | 移植内容 | 许可证 |
|------|----------|--------|
| [NousResearch/hermes-agent PR #17917](https://github.com/NousResearch/hermes-agent/pull/17917)（作者 MoeOver） | `adapter.py` 主体、`tools.py`（napcat_call）、`skills/napcat/SKILL.md`、大部分测试 | hermes-agent 仓库为 MIT（Copyright 2025 Nous Research），PR 按其贡献条款同为 MIT |
| [Zhaosn/hermes-napcat](https://github.com/Zhaosn/hermes-napcat)（fork 自 [shubyi/hermes-napcat](https://github.com/shubyi/hermes-napcat)） | `formatting.py`（Markdown→QQ 纯文本）、`standalone.py`（HTTP API 独立投递） | 仓库 README 声明 MIT（无独立 LICENSE 文件） |
| 本仓库 | 重构、main 规范适配、审阅修复（`__init__.py`、白名单、`send_image` 覆写等） | MIT |

```
QQ客户端 ──── NapCat ──WS拨入──▶ hermes-napcat-plugin ──▶ Hermes（大模型）
                                    │
                                    └── 一条 Universal 全双工 WS：事件 + API
                                      （反向 WS 服务端，默认 ws://0.0.0.0:8646/napcat/ws）
```

---

## 与上游 Zhaosn/hermes-napcat 的差异

| 维度 | Zhaosn 版（0.3.x） | 本重构版（1.0，PR #17917 方案） |
|------|--------------------|--------------------------------|
| QQ 工具 | 74 个 `qq_*` 专用工具（schemas+tools ≈ 2000 行） | 一个通用 `napcat_call(action, params)` + `napcat` skill 动作目录 |
| 入站富媒体 | 立即下载图片 / ffmpeg 转写语音 | 内联标记 `[图片:id]` `[语音:id]` `[文件:名:id]`，agent 按需 `get_image` / `get_record` / `get_file` 拉取 |
| 出站本地文件 | 直接传 Hermes 本地路径（仅同机可用） | `upload_file_stream` 流式上传到 NapCat（v4.8.115+，跨机可用），失败回退 `file://` |
| 消息去重 | 无 | 按 `message_id` 去重 5 分钟 |
| 连接模型 | 多连接集合 | 单活动连接，新连接顶掉旧连接（一号一端） |
| self_id 探测 | `get_login_info` 轮询 | `X-Self-ID` 握手头 + `meta_event` 生命周期事件 |
| 发送超时 | 标记可重试（可能重复发送） | 不重试（帧可能已送达） |
| 鉴权配置 | `NAPCAT_WS_PORT` + `NAPCAT_ACCESS_TOKEN` 必填；注册时改写 `os.environ` | 仅 `NAPCAT_TOKEN` 必填；全部走 scoped secrets，绝不改写进程环境 |
| 群白名单 | `group_policy` + `admins` 权限提示体系 | `allowed_groups` / `group_allow_from`（PR 审阅意见 #2 的修复：真正生效） |
| WS 帧上限 | aiohttp 默认 | 显式 4 MiB 上限（PR 审阅意见 #1 的修复） |

保留自 Zhaosn 版的能力：出站 Markdown → QQ 纯文本转换（QQ 不渲染 Markdown），
以及独立进程 cron 投递（经 NapCat 的 OneBot HTTP API，可选）。

> **迁移提示：** 配置键已按 PR 命名收敛——`ws_port` → `port`（默认 8646）、
> `ws_path` → `path`（默认 `/napcat/ws`）、`access_token` → `token`（env
> `NAPCAT_TOKEN`）。`admins` / `dm_policy` / `group_policy` 权限提示体系已移除，
> 访问控制统一为 allowlist（见下）。NapCat 侧只需把反向 WS 的 URL / Token 改成新值。

---

## 功能特性

- **群聊 & 私聊** — 群聊 @机器人（回复机器人的消息自动触发），私聊直达
- **通用 QQ 工具** — `napcat_call(action, params)` 直通全部 OneBot 11 / NapCat 动作
- **按需富媒体** — 入站图片 / 语音 / 视频 / 文件 / 表情全部内联标记化，agent 需要时再取
- **跨机文件发送** — `upload_file_stream` 分块 + SHA256 校验流式上传，Hermes 与
  NapCat 不在同一台机器也能发图 / 发文件
- **消息去重** — `message_id` 5 分钟窗口去重，防两个 NapCat 实例重复推送
- **引用消息上下文** — 回复消息自动携带被引用内容（`get_msg` 渲染）
- **出站纯文本** — Markdown 自动转 QQ 友好纯文本（代码块边框、标题【】化等）
- **独立 cron 投递** — 无网关进程时经 NapCat HTTP API 投递（可选）
- **零核心改动** — 标准 Hermes 插件目录，升级 Hermes 免维护

---

## 环境要求

- Python 3.11+
- [Hermes Agent](https://github.com/NousResearch/hermes-agent)（较新的 `main` 构建，含插件系统）
- [NapCatQQ](https://github.com/NapNeko/NapCatQQ) v4.8.115+（`upload_file_stream` 需要此版本；
  更旧版本仍可聊天收发，只是本地文件发送会回退 `file://`）
- `aiohttp >= 3.9`（缺失时 `pip install aiohttp`）

---

## 快速开始

### 1. 安装插件

```bash
git clone <本仓库>
mkdir -p ~/.hermes/plugins

# 复制（推荐）：
cp -r hermes-napcat-plugin/plugin ~/.hermes/plugins/napcat

# 或软链（便于 git pull 更新——目录名必须是 napcat）：
ln -s "$PWD/hermes-napcat-plugin/plugin" ~/.hermes/plugins/napcat
```

验证并启用：

```bash
hermes plugins list            # 应能看到 napcat
hermes plugins enable napcat
```

### 2. 配置

只需一个共享 Token（其余全部有默认值）。`~/.hermes/.env`：

```bash
NAPCAT_TOKEN=pick-a-long-random-string
```

行为类配置放 `~/.hermes/config.yaml`（PR 审阅意见 #3：只有密钥进 `.env`）：

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

访问控制语义（空 = 开放，层层收紧）：

1. `allow_all: true`（env `NAPCAT_ALLOW_ALL_USERS`）→ 全部放行；
2. 私聊：`allow_from` 非空时仅名单内 QQ 可私聊；
3. 群聊：`allowed_groups` 非空时仅名单内群（`*` 任意群）可互动；
   `group_allow_from` 非空时群内仅名单成员可互动。群里仍需 @机器人 才触发。

### 3. 配置 NapCat 的反向 WebSocket

NapCat WebUI → 网络配置 → 新建 → **WebSocket 客户端**（反向 WS）：

| 设置 | 值 |
|------|-----|
| URL | `ws://<hermes 主机>:8646/napcat/ws` |
| accessToken | 与 `NAPCAT_TOKEN` 完全一致 |
| messagePostFormat | `array`（推荐；CQ 码字符串也能容忍，会被剥掉） |
| 心跳 | 默认 30 秒 |
| 重连 | 开启（推荐） |

### 4. 启动并验证

```bash
nohup hermes gateway run > /tmp/hermes-gateway.log 2>&1 &
tail -f /tmp/hermes-gateway.log
# 期望依次看到：
#   [NapCat] Reverse WebSocket listening on ws://0.0.0.0:8646/napcat/ws
#   [NapCat] NapCat connected (self_id=..., remote=...)
```

群里 @机器人（或私聊直接发消息）即可对话。发一张图片并问"这是什么"，agent 会
通过 `get_image` 拉取后调视觉工具识别。

---

## `napcat_call` 工具与 `napcat` skill

进入 napcat 平台会话后自动获得一个工具：

```text
napcat_call("get_group_msg_history", {"group_id": 12345678, "count": 30})
napcat_call("set_qq_profile",        {"nickname": "新昵称"})
napcat_call("delete_msg",            {"message_id": "abcdef"})
napcat_call("upload_group_file",     {"group_id": 12345678, "file": "/var/log/app.log", "name": "app.log"})
```

工具描述内嵌常用动作目录；完整参数表、出站 `MEDIA:` 标签用法、入站富媒体标记
解析、输出风格（QQ 不渲染 Markdown）都在随插件注册的 `napcat` skill 里
（`skill_view("napcat:napcat")`），agent 需要时会自动加载。

---

## 工具进度批处理（可选）

QQ 没有"编辑已发送消息"的能力（OneBot 11 无对应接口），所以 Hermes 自带的逐工具进度
在这条通道上会变成**一次工具调用一条消息**——一个长回合十几条，每条都是一次手机通知。
上游只在支持编辑的平台上合并进度，没有批处理开关；本插件把这件事做在插件侧，不需要
改动 Hermes 源码。

**1) 关掉网关自带的那条 rail：**

```yaml
display:
  platforms:
    napcat:
      tool_progress: off
```

**2) 打开插件的批处理**（`config.yaml` 的 `platforms.napcat.extra`，或 `.env` 里的同名变量）：

```yaml
platforms:
  napcat:
    extra:
      progress_batch: true          # NAPCAT_PROGRESS_BATCH
      progress_batch_size: 5        # NAPCAT_PROGRESS_BATCH_SIZE
      progress_batch_window_s: 30   # NAPCAT_PROGRESS_BATCH_WINDOW_S
```

**行为：**

- 攒满 `progress_batch_size` 条（默认 5）立即发出一条；
- 从**第一条**待发起的 `progress_batch_window_s` 秒（默认 30）到点也发一条，
  所以慢回合不会一直静默；
- 回合结束（网关清除 typing）与连接断开时，剩余批次会被发掉或清空，不会拖到下一回合；
- Agent 中途说话、或给出最终回复时，已攒的工具行会**并进那条消息本身**：工具行在上、
  一条横线分隔、正文在下，而不是再多发一条气泡。同一次发送内完成，所以**顺序不会乱**
  （QQ 不能改消息，晚发就会让工具行跑到后面去）；
- 媒体消息（图片 / 语音 / 视频 / 文件）例外：说明文字不是 Agent 的正文，它们的工具行仍单独发一条；
- `progress_batch` 打开但 `tool_progress` 没关时，首次工具调用会在日志里给出明确警告
  （否则每次调用会多收一条未缓冲的消息）。

工具行形如 `🖥 terminal · git status`，一行一次调用、单行上限 140 字符；与正文合并时用一条
`──────────` 横线分隔（刻意用制表符而不是 ASCII 的 `---`，后者会被 Markdown 清洗规则改写成
插件自己的横线）。
批处理依赖插件的 `pre_tool_call` 钩子；宿主不提供该钩子时会静默降级为"无批量进度"。

---

## 环境变量一览

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `NAPCAT_TOKEN` | 共享密钥（`Authorization: Bearer` 或 `?access_token=`） | —（必填） |
| `NAPCAT_ENABLED` | 兼容保留的旧开关；token 可从 config.yaml 或 `.env` 读到时已无作用（无 token 强制启用只会重连死循环） | `false` |
| `NAPCAT_HOST` | 反向 WS 监听地址 | `0.0.0.0` |
| `NAPCAT_PORT` | 反向 WS 监听端口 | `8646` |
| `NAPCAT_PATH` | WS 路径 | `/napcat/ws` |
| `NAPCAT_HTTP_URL` | NapCat OneBot HTTP API 地址（独立进程 cron 投递） | — |
| `NAPCAT_ALLOWED_USERS` | 允许私聊的 QQ 号（逗号分隔；空 = 开放） | 开放 |
| `NAPCAT_GROUP_ALLOWED_USERS` | 允许群内互动的成员 QQ 号（逗号分隔） | 开放 |
| `NAPCAT_ALLOWED_GROUPS` | 允许互动的群号（`*` = 任意） | 开放 |
| `NAPCAT_ALLOW_ALL_USERS` | 跳过全部 allowlist | `false` |
| `NAPCAT_HOME_CHANNEL` | cron 投递目标（QQ 号或 `group:<群号>`） | — |

> **独立进程的 cron 投递（可选）：** 若用 `hermes cron run`（与 `hermes gateway`
> 分离的进程）执行 `deliver=napcat` 的任务，需在 NapCat 开启 HTTP 服务并配置
> `NAPCAT_HTTP_URL` / `http_url`——否则该场景会报
> "No live adapter for platform 'napcat'"。网关进程内的投递无需此项。

---

## 运行测试

```bash
# 需要一个 hermes-agent 检出 + 依赖（aiohttp、ruamel.yaml、pyyaml、pytest）
export HERMES_AGENT_ROOT=/path/to/hermes-agent
python -m pytest tests/ -q
```

覆盖：握手鉴权、入站管道（标记化 / @门控 / 引用上下文 / 去重 / 白名单）、
出站（分块 / Markdown 剥离 / 媒体段 / `upload_file_stream` / 超时不重试 /
文本回退）、`napcat_call` 工具（校验 / 错误形态 / 响应整形）、注册面冒烟、
工具进度批处理（批次阈值 / 时间窗不从最后一条起算 / 发送前落地时序 / 回合结束与
断连清理 / 行渲染与参数摘要）。

---

## 常见问题排查

| 现象 | 原因 / 解决 |
|------|------------|
| 日志没有 `Reverse WebSocket listening` | `NAPCAT_TOKEN` 未设置（适配器短路）；或端口被占用（日志搜 `napcat_bind_error`），换 `NAPCAT_PORT` |
| NapCat 报 `401` | accessToken 与 `NAPCAT_TOKEN` 不一致（注意空格），URL 路径须与 `NAPCAT_PATH` 完全一致 |
| NapCat 报 `connection refused` | 网关未运行或端口不对；NapCat 与 Hermes 不同机时勿用 `127.0.0.1`（WSL 场景见下） |
| 群里不回消息 | 必须 @机器人（回复机器人的消息也会自动触发）；核对日志 `lifecycle=connect self_id=...` 与 @ 的对象一致 |
| 私聊不回 | 检查 `allow_from` 是否把发送者排除在外 |
| 消息被重复处理 | 检查是否有两个 NapCat 实例往同一 URL 推送（适配器按 message_id 去重 5 分钟） |
| 图片/文件发送失败 | 跨机部署需 NapCat v4.8.115+（`upload_file_stream`）；或把文件暴露成 HTTP(S) URL——图片用 Markdown `![](url)` 写进回复，语音/视频用 `napcat_call` 传 `record`/`video` 段 |
| cron 报 "No live adapter" | 独立进程 cron 需配置 `NAPCAT_HTTP_URL`（见上） |

### WSL / Windows 部署（NapCat 在宿主机，Hermes 在 WSL）

`127.0.0.1` 是各自机器的回环地址：Windows 上的 NapCat 连 `127.0.0.1:8646` 到不了
WSL 内的监听。任选其一：

1. **直连 WSL IP**：WSL 里 `hostname -I`，NapCat URL 填 `ws://<WSL-IP>:8646/napcat/ws`
   （NAT 模式下 WSL IP 重启会变）。
2. **mirrored 网络模式**（Win11 22H2+）：`C:\Users\<你>\.wslconfig` 里设
   `[wsl2]` `networkingMode=mirrored` 后 `wsl --shutdown` 重开，NapCat 继续用
   `ws://127.0.0.1:8646/napcat/ws`。

---

## 许可证

MIT（见 [LICENSE](LICENSE)）
