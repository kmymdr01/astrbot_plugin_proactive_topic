<p align="center">
  <img src="logo.png" width="120" alt="主动话题 Logo">
</p>

<h1 align="center">主动话题（astrbot_plugin_proactive_topic）</h1>

<p align="center">让 AstrBot 在聊天安静下来后主动开口，而不是永远等别人说话。</p>

- **频率可调**：每个会话独立计时，支持随机浮动、每日上限、活跃时段，避免机械式定时。
- **两种话题随机**：按概率在「结合最近聊天记录的相关话题」和「符合人设的全新话题」之间选择，概率可调。
- **私聊 / 群聊分别控制**：可单独关闭群聊，也支持群白名单 / 黑名单。
- **跟随人设**：自动读取当前会话 / 全局默认人格，也支持指定人格和额外提示词。
- **自动学习会话**：不需要手填会话 ID，机器人聊过天的会话会被自动记录。
- **省 token**：长期没人说话的会话会自动暂停，避免在废弃群聊里持续消耗。

## 兼容性

| 项目 | 要求 |
| --- | --- |
| AstrBot | `>= 4.5.7` |
| Python | 跟随 AstrBot（`>= 3.10`） |
| 第三方依赖 | 无 |

插件刻意避开了新版本才有的 `PermissionType.GROUP_ADMIN`、`Star.logger` 和插件 KV 存储，并针对 4.5.x 的 API 做了兼容处理。

## 安装

### 方式一：WebUI 上传（推荐）

1. 下载本仓库的 zip（或把整个目录打包）；
2. 打开 AstrBot WebUI →「插件」→「安装插件」→ 上传 zip；
3. 安装完成后在插件列表里确认「主动话题」已启用。

### 方式二：从 GitHub 安装

在 WebUI「插件」页选择从仓库地址安装，填入：

```text
https://github.com/kmymdr01/astrbot_plugin_proactive_topic
```

### 方式三：手动放置

把 `astrbot_plugin_proactive_topic` 整个文件夹放到 AstrBot 的 `data/plugins/` 目录下，然后重启 AstrBot 或在插件页点击重载。

安装后打开插件配置，按需调整参数。

## 快速上手

默认配置下就会生效：在机器人聊过天的私聊和群聊里，安静一段时间后，大约每 90 分钟（±30%）主动发一条话题。

想先看看效果，可以在任意会话里发送：

```text
/话题 立即
```

## 指令

| 指令 | 说明 | 权限 |
| --- | --- | --- |
| `/话题 状态` | 查看本会话的状态、下次触发时间、今日已发数量 | 所有人 |
| `/话题 开启` | 为本会话开启主动话题 | 私聊所有人；群聊需机器人管理员 |
| `/话题 关闭` | 为本会话关闭主动话题 | 同上 |
| `/话题 立即` | 立刻生成并发送一条话题 | 机器人管理员 |
| `/话题 间隔 <分钟>` | 修改全局触发间隔（1~1440） | 机器人管理员 |
| `/话题 概率 <百分比>` | 修改上下文相关话题的概率（0~100） | 机器人管理员 |

只发送 `/话题` 会列出所有子指令。

> 指令前缀以 AstrBot 的唤醒前缀为准（默认 `/`）。

## 配置说明

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| 总开关 `enabled` | 开 | 关闭后所有会话都不主动发言，但仍会记录会话 |
| 私聊启用 `enable_private` | 开 | 是否在私聊中主动发言 |
| 群聊启用 `enable_group` | 开 | 是否在群聊中主动发言 |
| 群聊白名单 `group_whitelist` | 空 | 留空＝所有群；填写后仅这些群生效 |
| 群聊黑名单 `group_blacklist` | 空 | 这些群不主动发言 |
| 触发间隔 `interval_minutes` | 90 | 每个会话的基准间隔（分钟） |
| 随机浮动 `interval_jitter_percent` | 30 | 实际间隔 = 基准 × (1 ± 浮动) |
| 上下文概率 `context_probability` | 50 | 生成「结合最近聊天」话题的概率，其余为全新话题 |
| 静默时间 `idle_minutes` | 10 | 会话最近有人说话时不会插话 |
| 长期无互动暂停 `session_active_days` | 7 | 超过 N 天没人说话就不再主动发言，0 为不限 |
| 活跃时段 `active_hours` | 08:00-23:30 | 支持多段与跨天，留空为全天 |
| 每日上限 `max_per_day` | 12 | 每个会话每天最多主动发言次数，0 为不限 |
| 话题字数 `topic_max_chars` | 120 | 超过会截断 |
| 参考消息条数 `context_message_count` | 20 | 上下文模式最多参考多少条最近消息 |
| 上下文最少消息 `context_min_messages` | 3 | 不足时改为生成全新话题 |
| 上下文有效期 `context_max_age_hours` | 24 | 超过该时长的旧消息不参与上下文 |
| 模型 `provider_id` | 跟随会话 | 可指定专用的模型提供商 |
| 人格 `persona_id` | 跟随会话 | 可指定专用人格 |
| 额外提示词 `extra_system_prompt` | 空 | 追加到系统提示词，例如语气、禁忌 |
| 补充上下文 `inject_proactive_context` | 开 | 用户回复主动消息时，临时告诉模型你刚说过什么 |
| 补充上下文有效期 `inject_context_minutes` | 180 | 超过该时长后不再补充这条背景 |
| 生成超时 `llm_timeout_seconds` | 60 | 单次生成话题的最长等待时间 |
| 会话保留 `session_ttl_days` | 30 | 长期没有消息的会话会被清理 |

白名单 / 黑名单支持三种写法：

- 完整会话 UMO：`aiocqhttp:GroupMessage:123456789`
- 群号：`123456789`
- 平台实例 ID + 群号：`<平台实例ID>:123456789`

## 工作原理

1. 插件监听所有消息，记录「会话 → 最近聊天内容」，因此无需手工配置会话 ID；
2. 后台协程每 20 秒检查一次：会话是否到期、是否在活跃时段、是否安静够久、是否超过每日上限；
3. 到期后按概率选择话题模式：
   - **上下文相关**：把最近聊天记录交给模型，让它接着某个没聊完的点说一句；
   - **人设新话题**：只给人设、时间和最近说过的话题，让它想一个全新话题；
4. 生成结果会做清洗（去引号、去「话题：」前缀、截断），并和最近 5 条话题做相似度查重，重复则重试；
5. 通过 AstrBot 的主动消息接口发送，并记录到本会话的「最近话题」，用于下次去重；
6. 状态保存到 `data/plugin_data/astrbot_plugin_proactive_topic/state.json`，重启后继续计时。

## 目录结构

```text
astrbot_plugin_proactive_topic/
├── .github/workflows/test.yml   # CI：跑冒烟测试
├── .gitignore
├── CHANGELOG.md
├── LICENSE
├── README.md
├── _conf_schema.json            # WebUI 配置面板定义
├── logo.png
├── main.py                      # 插件实现
├── metadata.yaml                # 插件元数据
├── requirements.txt             # 无第三方依赖
└── tests/
    └── test_smoke.py            # 不依赖真实 AstrBot 的冒烟测试
```

## 开发与测试

插件不依赖第三方库，测试通过轻量桩模块模拟 AstrBot API，直接运行即可：

```bash
python tests/test_smoke.py
```

测试覆盖：时间窗解析、会话记录、私聊/群聊开关、话题模式选择、重复检测、上下文注入、长期无互动暂停、状态持久化等。

## 数据与隐私

- 会话状态保存在 AstrBot 数据目录下的 `plugin_data/astrbot_plugin_proactive_topic/state.json`；
- 文件里包含每个会话最近 `context_message_count` 条聊天文本、最近话题和计时信息，**仅保存在本地**，不会上传到任何第三方；
- 超过 `session_ttl_days`（默认 30 天）没有消息的会话会被自动清理；
- 如果不想保留聊天内容，可以把 `context_message_count` 调小，或设置 `context_probability = 0`（此时不再需要最近消息）。

## 成本参考

每次生成话题都是一次独立的 LLM 请求：人格 + 任务规则（约 400~700 token）会重新发送，加上本次提示词（全新话题约 200~300 token，上下文模式约 800~1800 token），输出约 30~80 token。

默认 90 分钟间隔、每日上限 12 次时，单个活跃会话每天大约消耗 1 万~1.5 万 token。省钱的做法：

- 用 `provider_id` 指定便宜的小模型专门生成话题；
- 调小 `context_message_count`、`topic_max_chars`；
- 调大 `interval_minutes`、调小 `max_per_day`；
- 保留 `session_active_days`，避免在废弃会话里持续消耗。

## 注意事项

- **需要先聊过天**：插件只会对「见过消息」的会话主动发言，冷启动不会给陌生会话发消息。群聊同理，机器人至少要在群里收到过一条消息。
- **频率是近似值**：受随机浮动、静默时间、活跃时段、每日上限和模型响应速度影响，不会精确到分钟。
- **不是所有平台都支持主动消息**：QQ 官方机器人等平台不支持主动推送，发送会失败并在日志中提示。
- **模型调用会计费**：每次主动发言都是一次 LLM 请求。
- **修改配置后**：通常下一轮调度即生效；若未生效，可在插件页重载一次。
- **时间以服务器本地时间为准**：活跃时段、每日重置都使用宿主机时区，部署在境外服务器时请注意。
- **权限说明**：群里只有机器人管理员能改本会话开关；私聊里任何人都可以改自己的。

## 发布到 GitHub

仓库里已经是可直接上传的结构。首次发布：

1. 确认 `metadata.yaml` 的 `author`（当前 `kmymdr01`）和 `repo`（当前 `https://github.com/kmymdr01/astrbot_plugin_proactive_topic`）与你的仓库一致；如果仓库改名，记得同步修改；
2. 在仓库根目录执行：

```bash
git init
git add .
git commit -m "feat: initial release v1.1.1"
git branch -M main
git remote add origin https://github.com/kmymdr01/astrbot_plugin_proactive_topic.git
git push -u origin main
```

3. 到 AstrBot 插件市场提交仓库地址即可（可选）。

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)。

## 许可证

[MIT](LICENSE)
